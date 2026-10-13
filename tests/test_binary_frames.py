"""Tests for 5-byte binary frame header (kind + ts_ms) and its WebSocket routing."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from openmovehub.config import Settings
from openmovehub.models.frames import (
    HEADER_SIZE,
    KIND_JPEG,
    KIND_PCM,
    decode_frame,
    encode_frame,
    monotonic_ms,
)
from openmovehub.routers.ws import websocket_endpoint
from openmovehub.services.anim_state import AnimState
from openmovehub.services.camera_relay import CameraRelay
from openmovehub.services.connection_manager import ConnectionManager
from openmovehub.services.drive_controller import DriveController
from openmovehub.services.listen_state import ListenState


def _make_mock_ws(messages: list[dict]) -> MagicMock:
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.send_bytes = AsyncMock()
    ws.close = AsyncMock()
    ws.receive = AsyncMock(side_effect=list(messages) + [{"type": "websocket.disconnect"}])
    return ws


def _drive(manager: ConnectionManager) -> DriveController:
    return DriveController(manager, Settings())


@pytest.fixture
def listen_state():
    return ListenState(on=True)


@pytest.fixture
def anim_state():
    return AnimState(mouth=True)


# --- encode / decode --------------------------------------------------------


class TestFrameCodec:
    def test_header_size_and_kinds(self):
        assert HEADER_SIZE == 5
        assert KIND_PCM == 0x01
        assert KIND_JPEG == 0x02

    def test_round_trip(self):
        payload = b"\x01\x02\x03\x04"
        frame = encode_frame(KIND_PCM, payload, ts_ms=123456)
        assert len(frame) == HEADER_SIZE + len(payload)
        assert decode_frame(frame) == (KIND_PCM, 123456, payload)

    def test_round_trip_jpeg(self):
        payload = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        assert decode_frame(encode_frame(KIND_JPEG, payload, ts_ms=7)) == (KIND_JPEG, 7, payload)

    def test_header_layout_is_little_endian(self):
        frame = encode_frame(KIND_JPEG, b"", ts_ms=0x01020304)
        assert frame == bytes([0x02, 0x04, 0x03, 0x02, 0x01])

    def test_empty_payload(self):
        assert decode_frame(encode_frame(KIND_PCM, b"", ts_ms=0)) == (KIND_PCM, 0, b"")

    def test_ts_ms_defaults_to_monotonic(self):
        before = monotonic_ms()
        _, ts_ms, _ = decode_frame(encode_frame(KIND_PCM, b"x"))
        after = monotonic_ms()
        assert before <= ts_ms <= after

    def test_ts_ms_wraps_to_uint32(self):
        _, ts_ms, _ = decode_frame(encode_frame(KIND_PCM, b"x", ts_ms=0x1_0000_0001))
        assert ts_ms == 1

    def test_short_frame_raises(self):
        for length in range(HEADER_SIZE):
            with pytest.raises(ValueError):
                decode_frame(b"\x00" * length)

    def test_exactly_header_is_valid(self):
        assert decode_frame(b"\x00" * HEADER_SIZE) == (0x00, 0, b"")


# --- WebSocket routing ------------------------------------------------------


async def test_pcm_frame_is_dropped_without_relay(manager, listen_state, anim_state):
    """マイク PCM の処理は別モジュール。この Hub では受け取って捨てる (app へも中継しない)。"""
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    relay = CameraRelay()
    ws = _make_mock_ws([{"type": "websocket.bytes", "bytes": encode_frame(KIND_PCM, b"\x00\x01" * 50)}])

    await websocket_endpoint(ws, "device", manager, listen_state, anim_state, _drive(manager), camera_relay=relay)
    await relay.wait_idle()

    app_ws.send_bytes.assert_not_awaited()
    assert relay.latest is None


async def test_jpeg_frame_relayed_to_apps_unchanged(manager, listen_state, anim_state):
    frame = encode_frame(KIND_JPEG, b"\xff\xd8jpegbytes", ts_ms=42)
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    device_ws = _make_mock_ws([{"type": "websocket.bytes", "bytes": frame}])
    # app への中継は CameraRelay のタスクで行うので完了を待つ。
    relay = CameraRelay()

    await websocket_endpoint(
        device_ws, "device", manager, listen_state, anim_state, _drive(manager), camera_relay=relay
    )
    await relay.wait_idle()

    app_ws.send_bytes.assert_awaited_once_with(frame)


async def test_unknown_kind_dropped(manager, listen_state, anim_state):
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    device_ws = _make_mock_ws([{"type": "websocket.bytes", "bytes": encode_frame(0x7F, b"payload")}])

    await websocket_endpoint(device_ws, "device", manager, listen_state, anim_state, _drive(manager))

    app_ws.send_bytes.assert_not_awaited()


async def test_short_frame_dropped(manager, listen_state, anim_state):
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    device_ws = _make_mock_ws([{"type": "websocket.bytes", "bytes": b"\x01\x02"}])

    await websocket_endpoint(device_ws, "device", manager, listen_state, anim_state, _drive(manager))

    app_ws.send_bytes.assert_not_awaited()


async def test_app_binary_is_ignored(manager, listen_state, anim_state):
    other_app = _make_mock_ws([])
    await manager.connect(other_app, "app")
    device_ws = _make_mock_ws([])
    await manager.connect(device_ws, "device")
    app_ws = _make_mock_ws([{"type": "websocket.bytes", "bytes": encode_frame(KIND_JPEG, b"jpeg")}])
    relay = CameraRelay()

    await websocket_endpoint(app_ws, "app", manager, listen_state, anim_state, _drive(manager), camera_relay=relay)
    await relay.wait_idle()

    other_app.send_bytes.assert_not_awaited()
    device_ws.send_bytes.assert_not_awaited()
    assert relay.latest is None


async def test_device_text_frames_are_plain_json(manager, listen_state, anim_state):
    """テキストフレームはヘッダ無しの JSON のまま (greeting の先頭は listen)。"""
    device_ws = _make_mock_ws([])

    await websocket_endpoint(device_ws, "device", manager, listen_state, anim_state, _drive(manager))

    assert json.loads(device_ws.send_text.await_args_list[0].args[0])["type"] == "listen"
    device_ws.send_bytes.assert_not_awaited()
