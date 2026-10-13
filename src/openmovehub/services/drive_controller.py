"""駆動指令リピータ ``DriveController`` (stackchan_move から縮小)。

Hub 全体で単一の駆動状態 (``source`` / ``target`` / ``seq``) を持ち、
``source == hub`` の間は ``HUB_DRIVE_RATE_HZ`` で全 device に ``drive`` を
送り続ける (command hold)。``target`` が ``HUB_DRIVE_HOLD_S`` 以上更新されなければ
``(0, 0)`` に戻し、UI が落ちた際に走り続けないようにする。``joystick`` の間は送らない
(リモコンの指令は端末がローカルに適用する、stackchan_move と同じ)。
"""

import asyncio
import logging
import time
from collections.abc import Callable

from openmovehub.config import Settings
from openmovehub.models.messages import (
    DriveMessage,
    DriveSource,
    DriveSourceMessage,
    StateMessage,
)
from openmovehub.services.connection_manager import ConnectionManager

logger = logging.getLogger(__name__)

DRIVE_SOURCES: tuple[DriveSource, ...] = ("hub", "joystick")

_ZERO: tuple[float, float] = (0.0, 0.0)


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


class DriveController:
    def __init__(
        self,
        manager: ConnectionManager,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._manager = manager
        self._settings = settings
        self._clock = clock
        self.source: DriveSource = settings.drive_source_default
        self.target: tuple[float, float] = _ZERO
        self.target_ts: float = clock()
        self.seq: int = 0
        # ws が odom を受信するたびに生 dict をセットする (GET /status 用)。
        self.last_odom: dict | None = None
        self._task: asyncio.Task | None = None

    # --- 目標速度 ---------------------------------------------------------

    def set_target(self, v: float, w: float) -> tuple[float, float]:
        """目標速度を ±max_v / ±max_w にクランプして保持する。戻り値はクランプ後の (v, w)。"""
        clamped = (_clamp(float(v), self._settings.max_v), _clamp(float(w), self._settings.max_w))
        self.target = clamped
        self.target_ts = self._clock()
        logger.debug("Drive target set to v=%.3f w=%.3f", clamped[0], clamped[1])
        return clamped

    # --- 駆動ソース -------------------------------------------------------

    async def set_source(self, source: str) -> DriveSource:
        """駆動ソースを切り替える。戻り値は切り替え後 (拒否時は現在) のソース。

        ``hub`` / ``joystick`` 以外 (``auto`` を含む) は拒否し、
        app に ``state drive_source:<現在値>`` を返す。
        """
        if source not in DRIVE_SOURCES:
            logger.warning("drive_source=%r refused (expected hub|joystick)", source)
            await self._manager.send_json_to_apps(StateMessage(type="state", value=f"drive_source:{self.source}"))
            return self.source

        if source == self.source:
            return self.source

        if self.source == "joystick":
            # ジョイスティックから戻すときは必ず停止状態から始める。
            self.target = _ZERO
            self.target_ts = self._clock()

        self.source = source  # type: ignore[assignment]
        logger.info("Drive source changed to %s", source)
        await self._manager.send_json_to_devices(DriveSourceMessage(type="drive_source", source=self.source))
        await self._manager.send_json_to_apps(StateMessage(type="state", value=f"drive_source:{self.source}"))
        return self.source

    # --- app / REST からの指令 -------------------------------------------

    async def apply_app_command(self, v: float, w: float) -> bool:
        """app / REST の ``control drive``。``joystick`` 中は無視して ``False`` を返す。"""
        if self.source != "hub":
            logger.debug("control drive ignored (drive_source=%s)", self.source)
            return False
        self.set_target(v, w)
        return True

    async def handle_estop(self, detail: str = "") -> None:
        """端末の ``event estop``: 目標速度を 0 にし、駆動ソースを hub に戻す。

        ``detail`` は device が送った ``event.detail`` (例: ``joystick_button``)。
        """
        logger.warning("[Drive] estop from device (%s)", detail)
        self.set_target(0.0, 0.0)
        await self.set_source("hub")

    # --- 定周期リピータ ---------------------------------------------------

    async def tick(self) -> None:
        """1 周期分の送信。``source == joystick`` の間は何も送らない。"""
        if self.source != "hub":
            return

        if self._clock() - self.target_ts >= self._settings.drive_hold_s:
            if self.target != _ZERO:
                logger.info("Drive command hold expired (%.1fs); stopping", self._settings.drive_hold_s)
                self.target = _ZERO

        self.seq += 1
        v, w = self.target
        await self._manager.send_json_to_devices(DriveMessage(type="drive", v=v, w=w, seq=self.seq, src="hub"))

    async def _run(self) -> None:
        period = 1.0 / max(self._settings.drive_rate_hz, 0.001)
        while True:
            await asyncio.sleep(period)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Drive tick failed")

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        logger.info(
            "Drive repeater started (rate=%.1f Hz, hold=%.1f s, source=%s)",
            self._settings.drive_rate_hz,
            self._settings.drive_hold_s,
            self.source,
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # --- スナップショット -------------------------------------------------

    def snapshot(self) -> dict:
        v, w = self.target
        return {
            "drive_source": self.source,
            "target": {"v": v, "w": w},
            "seq": self.seq,
        }

    def limits(self) -> dict:
        """``/status.limits`` (Web クライアントの速度スライダの上限)。"""
        return {"max_v": self._settings.max_v, "max_w": self._settings.max_w}
