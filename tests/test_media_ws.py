"""Tests for the `/ws/media` role。

`device` ロールのバイナリ処理も互換のため維持されることを確認する (``--single-ws`` シミュレータ)。
"""

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from openmovehub.config import Settings
from openmovehub.models.frames import KIND_JPEG, KIND_PCM, encode_frame
from openmovehub.routers.rest import create_rest_router
from openmovehub.routers.ws import websocket_endpoint
from openmovehub.services.anim_state import AnimState
from openmovehub.services.camera_relay import CameraRelay
from openmovehub.services.connection_manager import ConnectionManager
from openmovehub.services.drive_controller import DriveController
from openmovehub.services.listen_state import ListenState

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-body" * 8


def _make_mock_ws(messages: list[dict] | None = None) -> MagicMock:
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.send_bytes = AsyncMock()
    ws.close = AsyncMock()
    ws.receive = AsyncMock(side_effect=list(messages or []) + [{"type": "websocket.disconnect"}])
    return ws


def _text(raw: str) -> dict:
    return {"type": "websocket.text", "text": raw}


@pytest.fixture
def listen_state():
    return ListenState(on=True)


@pytest.fixture
def anim_state():
    return AnimState(mouth=True)


@pytest.fixture
def drive(manager):
    return DriveController(manager, Settings())


async def run_ws(ws, role, manager, listen_state, anim_state, drive, camera_relay=None):
    await websocket_endpoint(ws, role, manager, listen_state, anim_state, drive, camera_relay=camera_relay)


# --- greeting ----------------------------------------------------------------


async def test_media_role_gets_no_greeting(manager, listen_state, anim_state, drive):
    ws = _make_mock_ws([])
    await run_ws(ws, "media", manager, listen_state, anim_state, drive)

    ws.send_text.assert_not_awaited()


# --- binary relay --------------------------------------------------------------


async def test_media_jpeg_relayed_to_app_and_stored(manager, listen_state, anim_state, drive):
    relay = CameraRelay()
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    frame = encode_frame(KIND_JPEG, JPEG, ts_ms=42)
    media_ws = _make_mock_ws([{"type": "websocket.bytes", "bytes": frame}])

    await run_ws(media_ws, "media", manager, listen_state, anim_state, drive, relay)
    await relay.wait_idle()

    app_ws.send_bytes.assert_awaited_once_with(frame)
    assert relay.latest is not None
    assert relay.latest["jpeg"] == JPEG
    assert relay.latest["device_ts_ms"] == 42


async def test_device_jpeg_still_works_compat(manager, listen_state, anim_state, drive):
    """device ロールのバイナリ処理は --single-ws 互換のため残る。"""
    relay = CameraRelay()
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    frame = encode_frame(KIND_JPEG, JPEG, ts_ms=7)
    device_ws = _make_mock_ws([{"type": "websocket.bytes", "bytes": frame}])

    await run_ws(device_ws, "device", manager, listen_state, anim_state, drive, relay)
    await relay.wait_idle()

    app_ws.send_bytes.assert_awaited_once_with(frame)
    assert relay.latest["device_ts_ms"] == 7


async def test_media_pcm_is_ignored_without_error(manager, listen_state, anim_state, drive, caplog):
    """マイク PCM の処理は別モジュール。この Hub では捨てる (app へも中継しない、後続フレームは処理する)。"""
    relay = CameraRelay()
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    pcm = encode_frame(KIND_PCM, b"\x00\x01" * 50)
    jpeg = encode_frame(KIND_JPEG, JPEG, ts_ms=9)
    media_ws = _make_mock_ws([{"type": "websocket.bytes", "bytes": pcm}, {"type": "websocket.bytes", "bytes": jpeg}])

    with caplog.at_level(logging.WARNING):
        await run_ws(media_ws, "media", manager, listen_state, anim_state, drive, relay)
    await relay.wait_idle()

    app_ws.send_bytes.assert_awaited_once_with(jpeg)
    assert relay.latest["device_ts_ms"] == 9
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_unknown_binary_kind_is_dropped_and_logged(manager, listen_state, anim_state, drive, caplog):
    relay = CameraRelay()
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    media_ws = _make_mock_ws([{"type": "websocket.bytes", "bytes": encode_frame(0x7F, b"x")}])

    with caplog.at_level(logging.WARNING):
        await run_ws(media_ws, "media", manager, listen_state, anim_state, drive, relay)

    app_ws.send_bytes.assert_not_awaited()
    assert relay.latest is None
    assert "Unknown binary frame kind: 0x7F" in caplog.text


async def test_binary_from_app_is_dropped(manager, listen_state, anim_state, drive):
    relay = CameraRelay()
    other_app = _make_mock_ws([])
    await manager.connect(other_app, "app")
    app_ws = _make_mock_ws([{"type": "websocket.bytes", "bytes": encode_frame(KIND_JPEG, JPEG)}])

    await run_ws(app_ws, "app", manager, listen_state, anim_state, drive, relay)

    other_app.send_bytes.assert_not_awaited()
    assert relay.latest is None


# --- text is ignored on media --------------------------------------------------


async def test_media_text_not_relayed_and_warns(manager, listen_state, anim_state, drive, caplog):
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    raw = json.dumps(
        {
            "type": "odom",
            "ts": 1000,
            "seq": 1,
            "x": 0.0,
            "y": 0.0,
            "th": 0.0,
            "v": 0.0,
            "w": 0.0,
            "dl": 0.0,
            "dr": 0.0,
            "cmd": {"v": 0.0, "w": 0.0, "src": "hub"},
            "wheel": {"l_pos": 0, "r_pos": 0, "l_valid": True, "r_valid": True},
        }
    )
    media_ws = _make_mock_ws([_text(raw)])

    with caplog.at_level(logging.WARNING):
        await run_ws(media_ws, "media", manager, listen_state, anim_state, drive)

    assert raw not in [c.args[0] for c in app_ws.send_text.await_args_list]
    assert drive.last_odom is None
    assert any("Ignoring text on media role" in r.getMessage() for r in caplog.records)


# --- invalid role --------------------------------------------------------------


async def test_invalid_role_closes_with_4000(manager, listen_state, anim_state, drive):
    ws = _make_mock_ws([])
    await run_ws(ws, "bogus", manager, listen_state, anim_state, drive)

    ws.close.assert_awaited_once_with(code=4000, reason="Invalid role")
    ws.accept.assert_not_awaited()


# --- ConnectionManager.media_count ---------------------------------------------


async def test_media_count_increments_and_decrements_on_connect_disconnect():
    manager = ConnectionManager()
    ws = _make_mock_ws()

    assert manager.media_count == 0
    await manager.connect(ws, "media")
    assert manager.media_count == 1
    assert ws in manager.media

    await manager.disconnect(ws, "media")
    assert manager.media_count == 0
    assert ws not in manager.media


async def test_media_connect_disconnect_logs(caplog):
    manager = ConnectionManager()
    ws = _make_mock_ws()

    with caplog.at_level(logging.INFO):
        await manager.connect(ws, "media")
        await manager.disconnect(ws, "media")

    assert "media connected (1)" in caplog.text
    assert "media disconnected (0)" in caplog.text


# --- GET /status ----------------------------------------------------------------


def _build_status_app(manager):
    app = FastAPI()
    app.include_router(create_rest_router())
    settings = Settings()
    app.state.manager = manager
    app.state.listen_state = ListenState(on=settings.listen_default)
    app.state.anim_state = AnimState(mouth=settings.anim_mouth_default)
    app.state.drive = DriveController(manager, settings)
    app.state.camera_relay = CameraRelay()
    return app


async def test_status_includes_media_connections():
    manager = ConnectionManager()
    media_ws = _make_mock_ws()
    await manager.connect(media_ws, "media")
    app = _build_status_app(manager)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        body = (await c.get("/status")).json()

    assert body["media_connections"] == 1
