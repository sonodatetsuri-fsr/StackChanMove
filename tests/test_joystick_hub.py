"""Tests for Hub 側のジョイスティック処理 (estop detail / joy 中継 / joy_link)。"""

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from openmovehub.config import Settings
from openmovehub.routers.ws import websocket_endpoint
from openmovehub.services.anim_state import AnimState
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


def _text(raw: str) -> dict:
    return {"type": "websocket.text", "text": raw}


def _sent_raw(ws: MagicMock) -> list[str]:
    return [c.args[0] for c in ws.send_text.await_args_list]


def _sent_json(ws: MagicMock) -> list[dict]:
    return [json.loads(t) for t in _sent_raw(ws)]


@pytest.fixture
def manager():
    return ConnectionManager()


@pytest.fixture
def listen_state():
    return ListenState(on=True)


@pytest.fixture
def anim_state():
    return AnimState(mouth=True)


@pytest.fixture
def drive(manager):
    return DriveController(manager, Settings())


async def run_ws(ws, role, manager, listen_state, anim_state, drive):
    await websocket_endpoint(ws, role, manager, listen_state, anim_state, drive)


# --- event estop (detail) -------------------------------


async def test_estop_from_joystick_with_detail(manager, listen_state, anim_state, drive):
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    drive.source = "joystick"
    drive.set_target(0.15, 2.0)
    raw = json.dumps({"type": "event", "name": "estop", "detail": "joystick_button"})
    device_ws = _make_mock_ws([_text(raw)])

    await run_ws(device_ws, "device", manager, listen_state, anim_state, drive)

    assert drive.target == (0.0, 0.0)
    assert drive.source == "hub"
    assert {"type": "drive_source", "source": "hub"} in _sent_json(device_ws)
    assert {"type": "state", "value": "drive_source:hub"} in _sent_json(app_ws)


async def test_estop_logs_detail(manager, listen_state, anim_state, drive, caplog):
    drive.source = "joystick"
    raw = json.dumps({"type": "event", "name": "estop", "detail": "joystick_button"})
    device_ws = _make_mock_ws([_text(raw)])

    with caplog.at_level(logging.WARNING):
        await run_ws(device_ws, "device", manager, listen_state, anim_state, drive)

    assert any("estop from device" in r.getMessage() and "joystick_button" in r.getMessage() for r in caplog.records)


async def test_estop_while_already_hub_does_not_crash_or_duplicate(manager, listen_state, anim_state, drive):
    drive.set_target(0.15, 2.0)
    assert drive.source == "hub"
    raw = json.dumps({"type": "event", "name": "estop", "detail": "joystick_button"})
    device_ws = _make_mock_ws([_text(raw)])

    await run_ws(device_ws, "device", manager, listen_state, anim_state, drive)

    assert drive.target == (0.0, 0.0)
    assert drive.source == "hub"
    # ソース不変のため set_source は broadcast しない。device が受け取るのは greeting の 5 通のみで、
    # drive_source は greeting 由来の 1 通だけ (estop による重複送信はない)。
    sent = _sent_json(device_ws)
    assert len(sent) == 5
    assert sum(1 for m in sent if m.get("type") == "drive_source") == 1


# --- joy relay ----------------------------------------------------------------


async def test_joy_relayed_verbatim_to_apps_not_device(manager, listen_state, anim_state, drive):
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    other_device = _make_mock_ws([])
    await manager.connect(other_device, "device")

    raw = json.dumps({"type": "joy", "x": 0.42, "y": -0.13, "btn": True})
    device_ws = _make_mock_ws([_text(raw)])

    await run_ws(device_ws, "device", manager, listen_state, anim_state, drive)

    assert raw in _sent_raw(app_ws)
    assert raw not in _sent_raw(other_device)


async def test_joy_from_app_role_is_ignored(manager, listen_state, anim_state, drive):
    other_app = _make_mock_ws([])
    await manager.connect(other_app, "app")
    raw = json.dumps({"type": "joy", "x": 0.1, "y": 0.2, "btn": False})
    app_ws = _make_mock_ws([_text(raw)])

    await run_ws(app_ws, "app", manager, listen_state, anim_state, drive)

    assert raw not in _sent_raw(other_app)


# --- event joy_link -------------------------------------------------------------


async def test_event_joy_link_relayed_and_logged(manager, listen_state, anim_state, drive, caplog):
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    raw = json.dumps({"type": "event", "name": "joy_link", "detail": "up AA:BB:CC:DD:EE:FF"})
    device_ws = _make_mock_ws([_text(raw)])

    with caplog.at_level(logging.INFO):
        await run_ws(device_ws, "device", manager, listen_state, anim_state, drive)

    assert raw in _sent_raw(app_ws)
    assert "joy_link" in caplog.text


# --- tick: source joystick suppresses drive, resumes on hub --------------------


async def test_tick_resumes_after_leaving_joystick():
    """joystick ソース中は tick が何も送らず、hub に戻すと再開する。"""
    manager = MagicMock()
    manager.send_json_to_devices = AsyncMock()
    manager.send_json_to_apps = AsyncMock()
    drive = DriveController(manager, Settings(drive_source_default="joystick"))
    drive.set_target(0.1, 0.0)

    await drive.tick()
    manager.send_json_to_devices.assert_not_awaited()

    await drive.set_source("hub")
    manager.send_json_to_devices.reset_mock()
    await drive.tick()

    manager.send_json_to_devices.assert_awaited_once()


async def test_control_drive_ignored_while_joystick(manager, listen_state, anim_state, drive):
    """joystick 中の control drive は目標を変えない (REST では 409)。"""
    drive.source = "joystick"
    raw = json.dumps({"type": "control", "command": "drive", "params": {"v": 0.1, "w": 0.5}})
    ws = _make_mock_ws([_text(raw)])

    await run_ws(ws, "app", manager, listen_state, anim_state, drive)

    assert drive.target == (0.0, 0.0)
    assert drive.source == "joystick"
