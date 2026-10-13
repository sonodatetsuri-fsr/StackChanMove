"""カメラ画像 (JPEG) の受信・保持・中継 (stackchan_move から記録連携を除いて流用)。

media (または device) から届いた ``kind=0x02`` のバイナリフレームを Hub 全体で 1 スロットだけ保持し
(latest-wins)、同じフレーム (ヘッダ込み) を全 app へ中継する。app ごとに送信中フラグを
持ち、前の送信が終わっていない app には新しいフレームを渡さず捨てる (app 単位の
latest-wins)。遅いブラウザが Hub の受信ループを詰まらせないための措置。

``on_frame()`` で登録したコールバックにも同じフレームを配る (VLM が最新フレームを使う)。

適応 fps 制御: リンクが安定して運べる最高 fps を Hub が探す。5 秒窓ごとに到着率 / 端末時刻の
間隔 / 到着遅延を測り、不安定な窓が 1 回で要求 fps を 1 下げ、安定な窓が 4 回連続で 1 上げる
(``fps_min``..``fps_max``)。``control camera {auto:false}`` で自動制御を止めて固定できる。
"""

import asyncio
import contextlib
import inspect
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from openmovehub.models.frames import decode_frame
from openmovehub.models.messages import CameraMessage
from openmovehub.services.connection_manager import ConnectionManager

logger = logging.getLogger(__name__)

CAMERA_FPS_MIN = 1
CAMERA_FPS_MAX = 10
CAMERA_QUALITY_MIN = 10
CAMERA_QUALITY_MAX = 90
CAMERA_FPS_DEFAULT = 4
CAMERA_QUALITY_DEFAULT = 60

#: ``fps_measured`` の測定窓 (秒)。
FPS_WINDOW_S = 2.0

# --- 適応 fps 制御 ------------------------------------------------
#: 適応制御の評価窓 (秒)。
WINDOW_S = 5.0
#: 端末時計と Hub 時計のオフセットを取る窓 (秒)。
OFFSET_WINDOW_S = 60.0
#: 何回連続で「安定」なら fps を 1 上げるか。
STABLE_UP_WINDOWS = 4
#: 「安定」の判定 (到着率 / 間隔比 p90 / 遅延 p90 [s])。
STABLE_DELIVERED = 0.9
STABLE_GAP_P90 = 1.5
STABLE_DELAY_P90_S = 0.3
#: 「不安定」の判定 (到着率 / 間隔比 p90 / 遅延 p90 [s])。
UNSTABLE_DELIVERED = 0.75
UNSTABLE_GAP_P90 = 2.0
UNSTABLE_DELAY_P90_S = 0.5
#: 自動制御の既定の下限 / 上限 fps (``HUB_CAMERA_FPS_MIN`` / ``HUB_CAMERA_FPS_MAX``)。
CAMERA_FPS_AUTO_MIN_DEFAULT = 2
CAMERA_FPS_AUTO_MAX_DEFAULT = 10

#: uint32 ``millis()`` の周期 [ms]。
_MS_WRAP = 1 << 32


def _percentile(values: list[float], q: float) -> float | None:
    """``values`` の分位点 (最近傍順位法)。空なら ``None``。"""
    if not values:
        return None
    data = sorted(values)
    idx = min(len(data) - 1, max(0, int(round(q * (len(data) - 1)))))
    return data[idx]


def clamp_fps(value: int) -> int:
    """配信 fps を 1..10 に丸める。"""
    return max(CAMERA_FPS_MIN, min(CAMERA_FPS_MAX, int(value)))


def clamp_quality(value: int) -> int:
    """JPEG quality を 10..90 に丸める。"""
    return max(CAMERA_QUALITY_MIN, min(CAMERA_QUALITY_MAX, int(value)))


FrameCallback = Callable[[bytes, int, float, int], Any]


class CameraRelay:
    """最新 JPEG フレームの保持と app / 購読者への配信。"""

    def __init__(
        self,
        on: bool = True,
        fps: int = CAMERA_FPS_DEFAULT,
        quality: int = CAMERA_QUALITY_DEFAULT,
        clock: Callable[[], float] = time.time,
        auto: bool = True,
        fps_min: int = CAMERA_FPS_AUTO_MIN_DEFAULT,
        fps_max: int = CAMERA_FPS_AUTO_MAX_DEFAULT,
    ) -> None:
        self.on = bool(on)
        self.fps = clamp_fps(fps)
        self.quality = clamp_quality(quality)
        self._clock = clock

        # 適応 fps 制御。
        self.auto = bool(auto)
        self.fps_min = clamp_fps(fps_min)
        self.fps_max = max(self.fps_min, clamp_fps(fps_max))
        self.stable_windows = 0
        self.last_change: float | None = None
        self.delivered: float | None = None
        self.gap_p90: float | None = None
        self.delay_p90_s: float | None = None
        # (hub_ts, 単調化した端末時刻 [ms])
        self._frames: deque[tuple[float, int]] = deque()
        self._last_device_ms: int | None = None
        self._ever_frame = False
        self._auto_task: asyncio.Task | None = None

        self.latest: dict | None = None
        self.app_drops = 0
        self._seq = 0
        self._arrivals: deque[float] = deque()
        self._callbacks: list[FrameCallback] = []
        self._inflight: set[int] = set()
        self._tasks: set[asyncio.Task] = set()

    # --- 受信 ---------------------------------------------------------------

    async def handle_frame(self, raw: bytes, manager: ConnectionManager) -> None:
        """``kind=0x02`` のバイナリフレームを保持・購読者配信・app 中継する。

        ``raw`` は 5 バイトヘッダを含む生フレーム。app へはそのまま転送する。
        """
        try:
            _kind, device_ts_ms, jpeg = decode_frame(raw)
        except ValueError as e:
            logger.warning("Invalid JPEG frame: %s", e)
            return

        hub_ts = self._clock()
        self._seq += 1
        seq = self._seq
        self.latest = {"jpeg": jpeg, "device_ts_ms": device_ts_ms, "hub_ts": hub_ts, "seq": seq}

        self._arrivals.append(hub_ts)
        self._prune(hub_ts)
        self._record_frame_time(hub_ts, device_ts_ms)

        await self._notify(jpeg, device_ts_ms, hub_ts, seq)
        self._relay_to_apps(raw, manager)

    async def _notify(self, jpeg: bytes, device_ts_ms: int, hub_ts: float, seq: int) -> None:
        for callback in list(self._callbacks):
            try:
                result = callback(jpeg, device_ts_ms, hub_ts, seq)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Camera frame callback failed")

    def _relay_to_apps(self, raw: bytes, manager: ConnectionManager) -> None:
        for ws in manager.apps:
            key = id(ws)
            if key in self._inflight:
                # 送信中の app は飛ばす (app 単位 latest-wins)。
                self.app_drops += 1
                continue
            self._inflight.add(key)
            task = asyncio.create_task(self._send_one(ws, key, raw, manager))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _send_one(self, ws: Any, key: int, raw: bytes, manager: ConnectionManager) -> None:
        try:
            await ws.send_bytes(raw)
        except Exception:
            logger.debug("Failed to relay JPEG frame to app; disconnecting", exc_info=True)
            try:
                await manager.disconnect(ws, "app")
            except Exception:
                logger.debug("Failed to disconnect app after relay failure", exc_info=True)
        finally:
            self._inflight.discard(key)

    async def wait_idle(self) -> None:
        """未完了の中継タスクをすべて待つ (テストと停止処理用)。"""
        while True:
            pending = [t for t in self._tasks if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    # --- 購読 ---------------------------------------------------------------

    def on_frame(self, callback: FrameCallback) -> None:
        """``callback(jpeg, device_ts_ms, hub_ts, seq)`` を登録する。"""
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def off_frame(self, callback: FrameCallback) -> None:
        """``on_frame`` で登録したコールバックを外す (未登録なら何もしない)。"""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    # --- 計測 ---------------------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - FPS_WINDOW_S
        while self._arrivals and self._arrivals[0] < cutoff:
            self._arrivals.popleft()

    @property
    def fps_measured(self) -> float:
        """直近 ``FPS_WINDOW_S`` 秒の到着フレーム数 / 窓幅。"""
        self._prune(self._clock())
        return len(self._arrivals) / FPS_WINDOW_S

    @property
    def last_frame_age_s(self) -> float | None:
        """最後のフレーム到着からの経過秒。未受信なら ``None``。"""
        if self.latest is None:
            return None
        return self._clock() - self.latest["hub_ts"]

    # --- 適応 fps 制御 --------------------------------------------

    def _unwrap_device_ms(self, device_ts_ms: int) -> int:
        """uint32 ``millis()`` の巻き戻りを単調化する。"""
        ts = int(device_ts_ms) & 0xFFFFFFFF
        last = self._last_device_ms
        if last is None:
            unwrapped = ts
        else:
            base = last - (last % _MS_WRAP)
            cand = base + ts
            if cand < last - (_MS_WRAP // 2):
                cand += _MS_WRAP
            elif cand > last + (_MS_WRAP // 2):
                cand -= _MS_WRAP
            unwrapped = cand
        self._last_device_ms = unwrapped
        return unwrapped

    def _record_frame_time(self, hub_ts: float, device_ts_ms: int) -> None:
        self._ever_frame = True
        self._frames.append((hub_ts, self._unwrap_device_ms(device_ts_ms)))
        self._prune_frames(hub_ts)

    def _prune_frames(self, now: float) -> None:
        cutoff = now - OFFSET_WINDOW_S
        while self._frames and self._frames[0][0] < cutoff:
            self._frames.popleft()

    @property
    def clock_offset_s(self) -> float | None:
        """直近 ``OFFSET_WINDOW_S`` 秒の ``hub_ts - device_ts/1000`` の最小値。"""
        if not self._frames:
            return None
        return min(h - d / 1000.0 for h, d in self._frames)

    def _window_metrics(self, now: float) -> tuple[float, float | None, float | None]:
        """直近 ``WINDOW_S`` 秒の (到着率, 間隔比 p90, 遅延 p90 [s])。"""
        cutoff = now - WINDOW_S
        window = [(h, d) for h, d in self._frames if h >= cutoff]
        expected = max(1.0, self.fps * WINDOW_S)
        delivered = len(window) / expected
        period_ms = 1000.0 / max(1, self.fps)
        gaps = [
            (window[i][1] - window[i - 1][1]) / period_ms
            for i in range(1, len(window))
            if window[i][1] >= window[i - 1][1]
        ]
        gap_p90 = _percentile(gaps, 0.9)
        offset = self.clock_offset_s or 0.0
        delays = [h - d / 1000.0 - offset for h, d in window]
        delay_p90 = _percentile(delays, 0.9)
        return delivered, gap_p90, delay_p90

    def evaluate_window(self, now: float | None = None) -> int | None:
        """1 つの評価窓を判定し、要求 fps を変えたなら新しい値を返す。

        同期・決定的なので (``clock`` を注入すれば) テストから直接駆動できる。
        自動制御が無効 / 配信 off、あるいはフレームを一度も受けていない場合は
        何もせず ``None`` を返す。
        """
        now = self._clock() if now is None else now
        self._prune_frames(now)
        if not self._ever_frame:
            # まだ端末が繋がっていない (窓が空でも「不安定」とは見なさない)。
            return None

        delivered, gap_p90, delay_p90 = self._window_metrics(now)
        self.delivered = delivered
        self.gap_p90 = gap_p90
        self.delay_p90_s = delay_p90

        if not self.auto or not self.on:
            return None
        if not any(now - 2 * WINDOW_S <= h < now - WINDOW_S for h, _ in self._frames):
            # 直前の窓にフレームが無い = 接続直後 (ウォームアップ) か途絶中。部分的な窓で
            # 判定して不必要に下げない (途絶は最初の空窓で既に 1 段下げている)。
            self.stable_windows = 0
            return None

        unstable = (
            delivered < UNSTABLE_DELIVERED
            or (gap_p90 is not None and gap_p90 > UNSTABLE_GAP_P90)
            or (delay_p90 is not None and delay_p90 > UNSTABLE_DELAY_P90_S)
        )
        stable = (
            delivered >= STABLE_DELIVERED
            and (gap_p90 is None or gap_p90 <= STABLE_GAP_P90)
            and (delay_p90 is None or delay_p90 <= STABLE_DELAY_P90_S)
        )

        if unstable:
            self.stable_windows = 0
            if self.fps > self.fps_min:
                self.fps -= 1
                self.last_change = now
                return self.fps
            return None
        if not stable:
            self.stable_windows = 0
            return None
        self.stable_windows += 1
        if self.stable_windows < STABLE_UP_WINDOWS:
            return None
        self.stable_windows = 0
        if self.fps < self.fps_max:
            self.fps += 1
            self.last_change = now
            return self.fps
        return None

    def start(self, manager: ConnectionManager) -> None:
        """``WINDOW_S`` 秒ごとに ``evaluate_window()`` を回すタスクを起動する。"""
        if self._auto_task is not None and not self._auto_task.done():
            return
        self._auto_task = asyncio.create_task(self._auto_loop(manager))

    async def stop(self) -> None:
        """適応制御タスクを停止する (未起動なら何もしない)。"""
        task = self._auto_task
        self._auto_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _auto_loop(self, manager: ConnectionManager) -> None:
        while True:
            await asyncio.sleep(WINDOW_S)
            if not manager.devices:
                # 端末未接続の空窓を「不安定」と数えて fps_min まで下げない。
                self.stable_windows = 0
                continue
            try:
                new_fps = self.evaluate_window(self._clock())
            except Exception:
                logger.exception("Camera fps adaptation failed")
                continue
            if new_fps is None:
                continue
            logger.info(
                "Camera fps auto -> %d (delivered=%.2f gap_p90=%s delay_p90=%s)",
                new_fps,
                self.delivered if self.delivered is not None else float("nan"),
                None if self.gap_p90 is None else round(self.gap_p90, 2),
                None if self.delay_p90_s is None else round(self.delay_p90_s, 3),
            )
            try:
                await manager.send_json_to_devices(self.snapshot_message())
            except Exception:
                logger.exception("Failed to send camera fps update to devices")

    def _clamp_fps_max(self, value: int) -> int:
        return max(self.fps_min, min(CAMERA_FPS_MAX, int(value)))

    # --- 設定 ---------------------------------------------------------------

    def apply_control(self, params: dict) -> CameraMessage:
        """``control camera`` を適用し、全量スナップショットの ``camera`` を返す。

        省略されたキーは現在値を保つ。``fps`` / ``quality`` はクランプする。

        ``auto`` (Hub 内の状態、``CameraMessage`` には載せない) で適応制御の
        入切を切り替える。``auto:false`` は現在値 (同じ呼びで ``fps`` があればその値) で
        固定し、``auto:true`` は現在の fps から探索を再開する。``auto`` を省いた ``fps``
        は、自動中なら上限 (``fps_max``) の更新、手動中なら固定値の変更として扱う。
        """
        if "on" in params:
            self.on = bool(params["on"])
        if "quality" in params:
            try:
                self.quality = clamp_quality(int(params["quality"]))
            except (TypeError, ValueError):
                logger.warning("control camera ignoring invalid quality: %r", params["quality"])

        fps_value: int | None = None
        if "fps" in params:
            try:
                fps_value = int(params["fps"])
            except (TypeError, ValueError):
                logger.warning("control camera ignoring invalid fps: %r", params["fps"])

        auto_value: bool | None = bool(params["auto"]) if "auto" in params else None

        if auto_value is False:
            self.auto = False
            self.stable_windows = 0
            if fps_value is not None:
                self.fps = clamp_fps(fps_value)
        else:
            if auto_value is True:
                self.auto = True
                self.stable_windows = 0
            if fps_value is not None:
                if self.auto:
                    self.fps_max = self._clamp_fps_max(fps_value)
                    if self.fps > self.fps_max:
                        self.fps = self.fps_max
                else:
                    self.fps = clamp_fps(fps_value)
        return self.snapshot_message()

    def snapshot_message(self) -> CameraMessage:
        """現在の配信設定を全量スナップショットとして返す。"""
        return CameraMessage(type="camera", on=self.on, fps=self.fps, quality=self.quality)

    def status(self) -> dict:
        """``GET /status`` の ``camera`` セクション。

        ``fps`` は端末へ要求中の値 (自動調整中は自動で変わる)、``auto`` は自動調整の入切。
        """
        latest = self.latest
        return {
            "on": self.on,
            "fps": self.fps,
            "quality": self.quality,
            "auto": self.auto,
            "fps_measured": self.fps_measured,
            "last_frame_age_s": self.last_frame_age_s,
            "seq": latest["seq"] if latest else 0,
            "size": len(latest["jpeg"]) if latest else 0,
            "app_drops": self.app_drops,
            # 適応 fps 制御の内部状態。
            "fps_min": self.fps_min,
            "fps_max": self.fps_max,
            "delivered": None if self.delivered is None else round(self.delivered, 3),
            "gap_p90": None if self.gap_p90 is None else round(self.gap_p90, 3),
            "delay_p90_s": None if self.delay_p90_s is None else round(self.delay_p90_s, 4),
            "last_change": self.last_change,
            "stable_windows": self.stable_windows,
        }
