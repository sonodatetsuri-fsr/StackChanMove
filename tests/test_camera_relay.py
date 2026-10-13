"""Tests for CameraRelay (JPEG フレームの保持・購読・app 中継)。"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from openmovehub.config import Settings
from openmovehub.models.frames import KIND_JPEG, encode_frame
from openmovehub.routers.ws import websocket_endpoint
from openmovehub.services.anim_state import AnimState
from openmovehub.services.camera_relay import (
    CAMERA_FPS_DEFAULT,
    CAMERA_QUALITY_DEFAULT,
    FPS_WINDOW_S,
    STABLE_UP_WINDOWS,
    WINDOW_S,
    CameraRelay,
)
from openmovehub.services.connection_manager import ConnectionManager
from openmovehub.services.drive_controller import DriveController
from openmovehub.services.listen_state import ListenState

JPEG_A = b"\xff\xd8" + b"A" * 32
JPEG_B = b"\xff\xd8" + b"B" * 64


def _make_mock_ws(messages: list[dict] | None = None) -> MagicMock:
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.send_bytes = AsyncMock()
    ws.close = AsyncMock()
    ws.receive = AsyncMock(side_effect=list(messages or []) + [{"type": "websocket.disconnect"}])
    return ws


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


async def _connect_app(manager: ConnectionManager) -> MagicMock:
    ws = _make_mock_ws()
    await manager.connect(ws, "app")
    return ws


# --- 受信と保持 -------------------------------------------------------------


class TestLatestFrame:
    async def test_header_is_parsed_into_latest(self, manager):
        relay = CameraRelay()
        await relay.handle_frame(encode_frame(KIND_JPEG, JPEG_A, ts_ms=4242), manager)

        assert relay.latest is not None
        assert relay.latest["jpeg"] == JPEG_A
        assert relay.latest["device_ts_ms"] == 4242
        assert relay.latest["seq"] == 1
        assert isinstance(relay.latest["hub_ts"], float)

    async def test_latest_wins_keeps_only_the_newest(self, manager):
        relay = CameraRelay()
        await relay.handle_frame(encode_frame(KIND_JPEG, JPEG_A, ts_ms=1), manager)
        await relay.handle_frame(encode_frame(KIND_JPEG, JPEG_B, ts_ms=2), manager)

        assert relay.latest["jpeg"] == JPEG_B
        assert relay.latest["device_ts_ms"] == 2
        assert relay.latest["seq"] == 2

    async def test_no_frame_yet(self):
        relay = CameraRelay()
        assert relay.latest is None
        assert relay.last_frame_age_s is None
        assert relay.status()["seq"] == 0
        assert relay.status()["size"] == 0

    async def test_short_frame_is_ignored(self, manager):
        relay = CameraRelay()
        await relay.handle_frame(b"\x02\x00", manager)
        assert relay.latest is None


# --- app への中継 -----------------------------------------------------------


class TestRelayToApps:
    async def test_frame_is_relayed_unchanged(self, manager):
        relay = CameraRelay()
        app1 = await _connect_app(manager)
        app2 = await _connect_app(manager)
        frame = encode_frame(KIND_JPEG, JPEG_A, ts_ms=7)

        await relay.handle_frame(frame, manager)
        await relay.wait_idle()

        app1.send_bytes.assert_awaited_once_with(frame)
        app2.send_bytes.assert_awaited_once_with(frame)
        assert relay.app_drops == 0

    async def test_slow_app_is_skipped_while_in_flight(self, manager):
        relay = CameraRelay()
        gate = asyncio.Event()

        async def blocked(_data):
            await gate.wait()

        slow = _make_mock_ws()
        slow.send_bytes = AsyncMock(side_effect=blocked)
        fast = _make_mock_ws()
        await manager.connect(slow, "app")
        await manager.connect(fast, "app")

        frame1 = encode_frame(KIND_JPEG, JPEG_A, ts_ms=1)
        frame2 = encode_frame(KIND_JPEG, JPEG_B, ts_ms=2)
        await relay.handle_frame(frame1, manager)
        await asyncio.sleep(0)  # 送信タスクを開始させる
        await relay.handle_frame(frame2, manager)

        assert relay.app_drops == 1
        assert [c.args[0] for c in slow.send_bytes.await_args_list] == [frame1]

        gate.set()
        await relay.wait_idle()

        assert [c.args[0] for c in fast.send_bytes.await_args_list] == [frame1, frame2]
        assert [c.args[0] for c in slow.send_bytes.await_args_list] == [frame1]

    async def test_send_failure_disconnects_the_app(self):
        relay = CameraRelay()
        manager = MagicMock()
        broken = _make_mock_ws()
        broken.send_bytes = AsyncMock(side_effect=RuntimeError("closed"))
        manager.apps = [broken]
        manager.disconnect = AsyncMock()

        await relay.handle_frame(encode_frame(KIND_JPEG, JPEG_A, ts_ms=1), manager)
        await relay.wait_idle()

        manager.disconnect.assert_awaited_once_with(broken, "app")

    async def test_in_flight_mark_is_cleared_after_send(self, manager):
        relay = CameraRelay()
        app = await _connect_app(manager)

        for ts in (1, 2, 3):
            await relay.handle_frame(encode_frame(KIND_JPEG, JPEG_A, ts_ms=ts), manager)
            await relay.wait_idle()

        assert app.send_bytes.await_count == 3
        assert relay.app_drops == 0

    async def test_wait_idle_without_tasks(self):
        await CameraRelay().wait_idle()


# --- 購読 -------------------------------------------------------------------


class TestSubscribers:
    async def test_sync_callback_receives_frame(self, manager):
        relay = CameraRelay()
        seen = []
        relay.on_frame(lambda jpeg, ts, hub_ts, seq: seen.append((jpeg, ts, hub_ts, seq)))

        await relay.handle_frame(encode_frame(KIND_JPEG, JPEG_A, ts_ms=11), manager)

        assert len(seen) == 1
        jpeg, device_ts_ms, hub_ts, seq = seen[0]
        assert (jpeg, device_ts_ms, seq) == (JPEG_A, 11, 1)
        assert hub_ts == relay.latest["hub_ts"]

    async def test_async_callback_is_awaited(self, manager):
        relay = CameraRelay()
        seen = []

        async def cb(jpeg, device_ts_ms, hub_ts, seq):
            seen.append((jpeg, device_ts_ms, seq))

        relay.on_frame(cb)
        await relay.handle_frame(encode_frame(KIND_JPEG, JPEG_B, ts_ms=12), manager)

        assert seen == [(JPEG_B, 12, 1)]

    async def test_callback_exception_does_not_break_relay(self, manager):
        relay = CameraRelay()
        app = await _connect_app(manager)
        seen = []

        def boom(*_args):
            raise RuntimeError("callback failed")

        relay.on_frame(boom)
        relay.on_frame(lambda *args: seen.append(args))

        frame = encode_frame(KIND_JPEG, JPEG_A, ts_ms=13)
        await relay.handle_frame(frame, manager)
        await relay.wait_idle()

        assert len(seen) == 1
        app.send_bytes.assert_awaited_once_with(frame)
        assert relay.latest["seq"] == 1

    async def test_off_frame_stops_delivery(self, manager):
        relay = CameraRelay()
        seen = []

        def cb(*args):
            seen.append(args)

        relay.on_frame(cb)
        await relay.handle_frame(encode_frame(KIND_JPEG, JPEG_A, ts_ms=1), manager)
        relay.off_frame(cb)
        await relay.handle_frame(encode_frame(KIND_JPEG, JPEG_A, ts_ms=2), manager)

        assert len(seen) == 1

    async def test_off_frame_unknown_callback_is_noop(self):
        CameraRelay().off_frame(lambda *args: None)


# --- 計測 -------------------------------------------------------------------


class TestMeasurements:
    async def test_fps_measured_over_window(self, manager):
        clock = FakeClock()
        relay = CameraRelay(clock=clock)
        assert relay.fps_measured == 0.0

        for _ in range(5):
            await relay.handle_frame(encode_frame(KIND_JPEG, JPEG_A, ts_ms=1), manager)
            clock.advance(0.2)
        # 5 フレームすべてが直近 2 秒の窓に入る。
        assert relay.fps_measured == 5 / FPS_WINDOW_S

        clock.advance(FPS_WINDOW_S + 1.0)
        assert relay.fps_measured == 0.0

    async def test_last_frame_age_s(self, manager):
        clock = FakeClock()
        relay = CameraRelay(clock=clock)
        assert relay.last_frame_age_s is None

        await relay.handle_frame(encode_frame(KIND_JPEG, JPEG_A, ts_ms=1), manager)
        assert relay.last_frame_age_s == 0.0

        clock.advance(1.5)
        assert relay.last_frame_age_s == pytest.approx(1.5)


# --- 設定 -------------------------------------------------------------------


class TestApplyControl:
    def test_defaults(self):
        relay = CameraRelay()
        assert (relay.on, relay.fps, relay.quality) == (True, CAMERA_FPS_DEFAULT, CAMERA_QUALITY_DEFAULT)

    @pytest.mark.parametrize(
        ("given", "expected"),
        [(0, 1), (-5, 1), (1, 1), (5, 5), (10, 10), (99, 10)],
    )
    def test_fps_is_clamped(self, given, expected):
        """手動 (auto=False) では ``fps`` が固定値としてそのまま 1..10 に丸められる。"""
        assert CameraRelay(auto=False).apply_control({"fps": given}).fps == expected

    @pytest.mark.parametrize(
        ("given", "expected"),
        [(5, 10), (10, 10), (60, 60), (90, 90), (100, 90)],
    )
    def test_quality_is_clamped(self, given, expected):
        assert CameraRelay().apply_control({"quality": given}).quality == expected

    def test_missing_keys_are_preserved(self):
        relay = CameraRelay(auto=False)
        relay.apply_control({"fps": 3, "quality": 80, "on": False})
        msg = relay.apply_control({"fps": 7})
        assert (msg.type, msg.on, msg.fps, msg.quality) == ("camera", False, 7, 80)
        assert (relay.on, relay.fps, relay.quality) == (False, 7, 80)

    def test_empty_params_returns_current_snapshot(self):
        relay = CameraRelay(on=False, fps=2, quality=30)
        msg = relay.apply_control({})
        assert (msg.on, msg.fps, msg.quality) == (False, 2, 30)

    def test_on_is_coerced_to_bool(self):
        relay = CameraRelay()
        assert relay.apply_control({"on": 0}).on is False
        assert relay.apply_control({"on": 1}).on is True

    def test_numeric_strings_and_floats_are_accepted(self):
        relay = CameraRelay(auto=False)
        assert relay.apply_control({"fps": "7"}).fps == 7
        assert relay.apply_control({"quality": 45.9}).quality == 45

    def test_invalid_values_are_ignored(self):
        relay = CameraRelay(fps=4, quality=50, auto=False)
        msg = relay.apply_control({"fps": "fast", "quality": None})
        assert (msg.fps, msg.quality) == (4, 50)

    def test_snapshot_message(self):
        relay = CameraRelay(on=False, fps=8, quality=20)
        msg = relay.snapshot_message()
        assert json.loads(msg.model_dump_json()) == {"type": "camera", "on": False, "fps": 8, "quality": 20}


class TestStatus:
    async def test_status_keys(self, manager):
        clock = FakeClock()
        relay = CameraRelay(clock=clock)
        assert set(relay.status()) == {
            "on",
            "fps",
            "quality",
            "auto",
            "fps_measured",
            "last_frame_age_s",
            "seq",
            "size",
            "app_drops",
            "fps_min",
            "fps_max",
            "delivered",
            "gap_p90",
            "delay_p90_s",
            "last_change",
            "stable_windows",
        }

        await relay.handle_frame(encode_frame(KIND_JPEG, JPEG_A, ts_ms=1), manager)
        clock.advance(0.5)
        status = relay.status()
        assert status["seq"] == 1
        assert status["size"] == len(JPEG_A)
        assert status["fps_measured"] == 1 / FPS_WINDOW_S
        assert status["last_frame_age_s"] == pytest.approx(0.5)
        assert status["app_drops"] == 0


# --- WebSocket エンドポイント統合 --------------------------------------------


class TestWebSocketIntegration:
    async def test_jpeg_from_device_reaches_app(self, manager):
        relay = CameraRelay()
        app_ws = await _connect_app(manager)
        frame = encode_frame(KIND_JPEG, JPEG_A, ts_ms=99)
        device_ws = _make_mock_ws([{"type": "websocket.bytes", "bytes": frame}])

        await websocket_endpoint(
            device_ws,
            "device",
            manager,
            ListenState(on=True),
            AnimState(mouth=True),
            DriveController(manager, Settings()),
            camera_relay=relay,
        )
        await relay.wait_idle()

        app_ws.send_bytes.assert_awaited_once_with(frame)
        assert relay.latest["device_ts_ms"] == 99

    async def test_device_receives_camera_snapshot_on_connect(self, manager):
        relay = CameraRelay(on=False, fps=3, quality=70)
        device_ws = _make_mock_ws([])

        await websocket_endpoint(
            device_ws,
            "device",
            manager,
            ListenState(on=True),
            AnimState(mouth=True),
            DriveController(manager, Settings()),
            camera_relay=relay,
        )

        sent = [json.loads(c.args[0]) for c in device_ws.send_text.await_args_list]
        assert {"type": "camera", "on": False, "fps": 3, "quality": 70} in sent

    async def test_control_camera_from_app_is_forwarded_as_snapshot(self, manager):
        relay = CameraRelay()
        device_ws = _make_mock_ws([])
        await manager.connect(device_ws, "device")
        control = json.dumps({"type": "control", "command": "camera", "params": {"fps": 99, "auto": False}})
        app_ws = _make_mock_ws([{"type": "websocket.receive", "text": control}])

        await websocket_endpoint(
            app_ws,
            "app",
            manager,
            ListenState(on=True),
            AnimState(mouth=True),
            DriveController(manager, Settings()),
            camera_relay=relay,
        )

        sent = [json.loads(c.args[0]) for c in device_ws.send_text.await_args_list]
        assert sent[-1] == {"type": "camera", "on": True, "fps": 10, "quality": CAMERA_QUALITY_DEFAULT}
        assert relay.fps == 10


# --- 適応 fps 制御 -------------------------------------------------


def _frame(ts_ms: int) -> bytes:
    return encode_frame(KIND_JPEG, JPEG_A, ts_ms=ts_ms & 0xFFFFFFFF)


class Link:
    """端末時刻と Hub 到着時刻を制御してフレームを流す補助。"""

    def __init__(self, relay: CameraRelay, clock: FakeClock, manager) -> None:
        self.relay = relay
        self.clock = clock
        self.manager = manager

    async def feed(self, hub_ts: float, device_ts_ms: int) -> None:
        self.clock.now = hub_ts
        await self.relay.handle_frame(_frame(device_ts_ms), self.manager)

    async def _stream(self, rate: float, delay_s: float, window_s: float) -> None:
        start = self.clock.now
        n = int(round(rate * window_s))
        step = window_s / n if n else window_s
        for i in range(n):
            t = start + i * step
            await self.feed(t + delay_s, int(round(t * 1000)))
        self.clock.now = start + window_s

    async def window(self, rate: float, delay_s: float = 0.0, window_s: float = WINDOW_S):
        """``window_s`` 秒の窓に ``rate`` fps でフレームを流し、窓末で判定する。

        最初の呼び出しでは直前の窓 (ウォームアップ) を同じレートで先に流す
        (接続直後の部分的な窓は判定しない仕様のため)。
        """
        if self.relay.latest is None:
            await self._stream(rate, delay_s, window_s)
        await self._stream(rate, delay_s, window_s)
        return self.relay.evaluate_window(self.clock.now)


class TestAdaptiveFps:
    def test_defaults(self):
        relay = CameraRelay()
        assert (relay.fps, relay.auto, relay.fps_min, relay.fps_max) == (CAMERA_FPS_DEFAULT, True, 2, 10)
        assert CAMERA_FPS_DEFAULT == 4

    async def test_no_frame_ever_does_nothing(self, manager):
        clock = FakeClock()
        relay = CameraRelay(clock=clock)
        assert relay.evaluate_window(clock.now) is None
        assert (relay.fps, relay.delivered) == (4, None)

    async def test_one_unstable_window_lowers_fps_by_one(self, manager):
        clock = FakeClock()
        relay = CameraRelay(clock=clock)
        link = Link(relay, clock, manager)
        # 要求 4 fps に対し 2 fps しか届かない -> delivered 0.5 < 0.75。
        assert await link.window(rate=2.0) == 3
        assert relay.fps == 3
        assert relay.delivered == pytest.approx(0.5)
        assert relay.last_change == clock.now

    async def test_unstable_window_is_bounded_by_fps_min(self, manager):
        clock = FakeClock()
        relay = CameraRelay(clock=clock, fps=2, fps_min=2)
        link = Link(relay, clock, manager)
        assert await link.window(rate=0.4) is None
        assert relay.fps == 2

    async def test_partial_first_window_after_connect_is_not_judged(self, manager):
        """接続直後 (直前の窓にフレームが無い) の部分的な窓では fps を下げない。"""
        clock = FakeClock()
        relay = CameraRelay(clock=clock)
        link = Link(relay, clock, manager)
        start = clock.now
        for i in range(8):  # 窓の後半 2 s だけ 4 fps で届く (delivered 0.4)
            t = start + 3.0 + i * 0.25
            await link.feed(t, int(round(t * 1000)))
        clock.now = start + WINDOW_S
        assert relay.evaluate_window(clock.now) is None
        assert relay.fps == 4
        assert relay.delivered == pytest.approx(0.4)

    async def test_missing_frames_while_on_count_as_unstable(self, manager):
        """一度でも受信していれば、窓に 1 枚も来なければ delivered=0 で 1 下げる。"""
        clock = FakeClock()
        relay = CameraRelay(clock=clock)
        link = Link(relay, clock, manager)
        await link.feed(clock.now, 1000)
        clock.advance(WINDOW_S + 1.0)
        assert relay.evaluate_window(clock.now) == 3
        assert relay.delivered == 0.0

    async def test_four_stable_windows_raise_fps_by_one(self, manager):
        clock = FakeClock()
        relay = CameraRelay(clock=clock)
        link = Link(relay, clock, manager)
        for i in range(STABLE_UP_WINDOWS - 1):
            assert await link.window(rate=4.0) is None
            assert relay.stable_windows == i + 1
        assert await link.window(rate=4.0) == 5
        assert (relay.fps, relay.stable_windows) == (5, 0)

    async def test_stable_windows_are_bounded_by_fps_max(self, manager):
        clock = FakeClock()
        relay = CameraRelay(clock=clock, fps=4, fps_max=4)
        link = Link(relay, clock, manager)
        for _ in range(STABLE_UP_WINDOWS):
            result = await link.window(rate=4.0)
        assert result is None
        assert relay.fps == 4

    async def test_unstable_window_resets_the_stable_counter(self, manager):
        clock = FakeClock()
        relay = CameraRelay(clock=clock)
        link = Link(relay, clock, manager)
        await link.window(rate=4.0)
        await link.window(rate=4.0)
        assert relay.stable_windows == 2
        assert await link.window(rate=1.0) == 3
        assert relay.stable_windows == 0

    async def test_delay_p90_over_limit_is_unstable(self, manager):
        """到着率が十分でも、遅延 p90 > 0.5 s なら 1 下げる (offset は最小値で吸収)。"""
        clock = FakeClock()
        relay = CameraRelay(clock=clock)
        link = Link(relay, clock, manager)
        await link._stream(4.0, 0.0, WINDOW_S)  # ウォームアップ窓
        start = clock.now
        for i in range(20):
            t = start + i * 0.25
            delay = 0.0 if i < 10 else 1.0
            await link.feed(t + delay, int(round(t * 1000)))
        clock.now = start + WINDOW_S
        assert relay.evaluate_window(clock.now) == 3
        assert relay.delay_p90_s == pytest.approx(1.0)

    async def test_gap_p90_over_limit_is_unstable(self, manager):
        """到着率が 0.75 以上でも、端末時刻の間隔比 p90 > 2.0 なら 1 下げる。"""
        clock = FakeClock()
        relay = CameraRelay(clock=clock)
        link = Link(relay, clock, manager)
        await link._stream(4.0, 0.0, WINDOW_S)  # ウォームアップ窓
        start = clock.now
        # 16/20 枚 (delivered 0.8) を 4 枚ずつ束ねて送る -> 束の間で 1 s (比 4.0) 空く。
        n = 0
        for burst in range(4):
            for k in range(4):
                t = start + burst * 1.25 + k * 0.05
                await link.feed(t, int(round(t * 1000)))
                n += 1
        clock.now = start + WINDOW_S
        assert n == 16
        assert relay.evaluate_window(clock.now) == 3
        assert relay.gap_p90 is not None and relay.gap_p90 > 2.0

    async def test_frozen_when_auto_is_off(self, manager):
        clock = FakeClock()
        relay = CameraRelay(clock=clock, auto=False)
        link = Link(relay, clock, manager)
        assert await link.window(rate=1.0) is None
        assert relay.fps == 4

    async def test_frozen_when_camera_is_off(self, manager):
        clock = FakeClock()
        relay = CameraRelay(clock=clock, on=False)
        link = Link(relay, clock, manager)
        assert await link.window(rate=1.0) is None
        assert relay.fps == 4

    async def test_device_ts_wrap_is_unwrapped(self, manager):
        """uint32 ``millis()`` が巻き戻っても間隔は単調に扱われる。"""
        clock = FakeClock()
        relay = CameraRelay(clock=clock)
        link = Link(relay, clock, manager)
        base = (1 << 32) - 500
        start = clock.now
        for i in range(20):
            t = start + i * 0.25
            await link.feed(t, base + i * 250)
        clock.now = start + WINDOW_S
        assert relay.evaluate_window(clock.now) is None  # 安定 (1 窓目)
        assert relay.gap_p90 == pytest.approx(1.0)


class TestAutoControl:
    def test_auto_false_fixes_the_current_value(self):
        relay = CameraRelay(fps=4)
        msg = relay.apply_control({"auto": False})
        assert (relay.auto, relay.fps, msg.fps) == (False, 4, 4)

    def test_auto_false_with_fps_sets_it_directly(self):
        relay = CameraRelay(fps=4)
        msg = relay.apply_control({"auto": False, "fps": 6})
        assert (relay.auto, relay.fps, msg.fps) == (False, 6, 6)

    def test_auto_true_resumes_from_the_current_fps(self):
        relay = CameraRelay(fps=4, auto=False)
        relay.stable_windows = 3
        relay.apply_control({"auto": True})
        assert (relay.auto, relay.fps, relay.stable_windows) == (True, 4, 0)

    def test_fps_in_auto_mode_updates_fps_max(self):
        relay = CameraRelay(fps=4)
        relay.apply_control({"fps": 8})
        assert (relay.fps_max, relay.fps) == (8, 4)

    def test_fps_in_auto_mode_lowers_current_fps_to_the_new_max(self):
        relay = CameraRelay(fps=6)
        relay.apply_control({"fps": 3})
        assert (relay.fps_max, relay.fps) == (3, 3)

    def test_fps_max_is_clamped_to_fps_min_and_device_max(self):
        relay = CameraRelay(fps=4, fps_min=2)
        relay.apply_control({"fps": 1})
        assert relay.fps_max == 2
        relay.apply_control({"fps": 99})
        assert relay.fps_max == 10

    def test_fps_with_auto_true_is_also_a_max(self):
        relay = CameraRelay(fps=4, auto=False)
        relay.apply_control({"auto": True, "fps": 7})
        assert (relay.auto, relay.fps_max, relay.fps) == (True, 7, 4)

    def test_camera_message_has_no_auto_field(self):
        relay = CameraRelay()
        assert "auto" not in json.loads(relay.apply_control({"auto": False}).model_dump_json())

    def test_status_reports_the_adaptive_state(self):
        relay = CameraRelay(fps=4)
        status = relay.status()
        assert status["fps"] == 4
        assert status["auto"] is True
        assert (status["fps_min"], status["fps_max"]) == (2, 10)
        assert status["delivered"] is None and status["gap_p90"] is None
        assert status["delay_p90_s"] is None and status["last_change"] is None
        assert status["stable_windows"] == 0


class TestAutoLoop:
    async def test_start_sends_the_new_fps_to_devices(self, monkeypatch, manager):
        """適応タスクが窓ごとに評価し、変わったときだけ device へ ``camera`` を送る。"""
        from openmovehub.services import camera_relay as camera_relay_module

        monkeypatch.setattr(camera_relay_module, "WINDOW_S", 0.01)
        clock = FakeClock()
        relay = CameraRelay(clock=clock)
        sender = MagicMock()
        sender.send_json_to_devices = AsyncMock()
        base = clock.now
        clock.now = base - 0.015  # 直前の窓 (ウォームアップ) にもフレームがあること
        await relay.handle_frame(_frame(1000), manager)
        clock.now = base
        await relay.handle_frame(_frame(1015), manager)

        relay.start(sender)
        relay.start(sender)  # 二重起動しない
        for _ in range(200):
            await asyncio.sleep(0.01)
            if relay.fps == 5:
                break
        await relay.stop()

        assert relay.fps == 5
        sent = sender.send_json_to_devices.await_args[0][0]
        assert (sent.type, sent.fps) == ("camera", 5)

    async def test_stop_without_start_is_a_noop(self):
        await CameraRelay().stop()


def test_status_auto_follows_control_camera_auto():
    """``/status.camera.auto`` は自動調整の入切を表す。"""
    relay = CameraRelay()
    assert relay.status()["auto"] is True
    relay.apply_control({"auto": False})
    assert relay.status()["auto"] is False
    relay.apply_control({"auto": True})
    assert relay.status()["auto"] is True
