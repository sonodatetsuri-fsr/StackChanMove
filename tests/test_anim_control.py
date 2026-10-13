"""Tests for speak animation (mouth) switch (control anim, anim state).

首サーボ廃止 (StackChanMove) に伴い gesture フラグは削除され、口パク (mouth) のみを扱う。
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from openmovehub.config import Settings
from openmovehub.models.messages import AnimMessage, ControlMessage, parse_message
from openmovehub.routers.ws import websocket_endpoint
from openmovehub.services.anim_state import AnimState
from openmovehub.services.connection_manager import ConnectionManager
from openmovehub.services.drive_controller import DriveController
from openmovehub.services.listen_state import ListenState


def _make_mock_ws(messages: list[dict]) -> MagicMock:
    """Create a mock WebSocket that yields the given messages then disconnects."""
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.send_bytes = AsyncMock()
    ws.close = AsyncMock()
    all_messages = list(messages) + [{"type": "websocket.disconnect"}]
    ws.receive = AsyncMock(side_effect=all_messages)
    return ws


def _sent_json(ws: MagicMock) -> list[dict]:
    return [json.loads(c.args[0]) for c in ws.send_text.await_args_list]


def _drive(manager: ConnectionManager) -> DriveController:
    """A fresh DriveController for the endpoint under test; its repeater is not started."""
    return DriveController(manager, Settings())


@pytest.fixture
def manager():
    return ConnectionManager()


@pytest.fixture
def listen_state():
    return ListenState(on=True)


# 1. ControlMessage accepts command="anim".
def test_control_message_anim_parses():
    raw = json.dumps({"type": "control", "command": "anim", "params": {"mouth": False}})
    msg = parse_message(raw)
    assert isinstance(msg, ControlMessage)
    assert msg.command == "anim"
    assert msg.params == {"mouth": False}


# 1b. AnimMessage parses/serializes as a full snapshot.
def test_anim_message_parses_and_serializes():
    raw = json.dumps({"type": "anim", "mouth": True})
    msg = parse_message(raw)
    assert isinstance(msg, AnimMessage)
    assert msg.mouth is True
    assert json.loads(msg.model_dump_json()) == {"type": "anim", "mouth": True}


# 2. Settings anim_mouth_default env parsing (off -> False, unset -> True).
def _parse_anim_default(env_var: str) -> bool:
    return os.getenv(env_var, "on").strip().lower() not in ("off", "false", "0")


def test_settings_anim_mouth_default_off_from_env(monkeypatch):
    monkeypatch.setenv("HUB_ANIM_MOUTH_DEFAULT", "off")
    s = Settings(anim_mouth_default=_parse_anim_default("HUB_ANIM_MOUTH_DEFAULT"))
    assert s.anim_mouth_default is False


def test_settings_anim_mouth_default_unset_defaults_on(monkeypatch):
    monkeypatch.delenv("HUB_ANIM_MOUTH_DEFAULT", raising=False)
    s = Settings(anim_mouth_default=_parse_anim_default("HUB_ANIM_MOUTH_DEFAULT"))
    assert s.anim_mouth_default is True


# 3. Device connection greeting sends anim snapshot right after listen greeting.
async def test_device_greeting_sends_anim_after_listen_default(manager):
    listen_state = ListenState(on=True)
    anim_state = AnimState(mouth=True)
    ws = _make_mock_ws([])

    await websocket_endpoint(ws, "device", manager, listen_state, anim_state, _drive(manager))

    sent = _sent_json(ws)
    assert sent[0] == {"type": "listen", "on": True}
    assert sent[1] == {"type": "anim", "mouth": True}


async def test_device_greeting_sends_anim_after_listen_non_default(manager):
    listen_state = ListenState(on=True)
    anim_state = AnimState(mouth=False)
    ws = _make_mock_ws([])

    await websocket_endpoint(ws, "device", manager, listen_state, anim_state, _drive(manager))

    sent = _sent_json(ws)
    assert sent[0] == {"type": "listen", "on": True}
    assert sent[1] == {"type": "anim", "mouth": False}


# 4. control anim {"mouth": false} from app role: updates state, broadcasts full snapshot to
# devices and a state message to apps.
async def test_control_anim_mouth_off_from_app_broadcasts(manager, listen_state):
    anim_state = AnimState(mouth=True)

    device_ws = _make_mock_ws([])
    await manager.connect(device_ws, "device")
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")

    control_json = json.dumps({"type": "control", "command": "anim", "params": {"mouth": False}})
    control_ws = _make_mock_ws([{"type": "websocket.text", "text": control_json}])

    await websocket_endpoint(control_ws, "app", manager, listen_state, anim_state, _drive(manager))

    assert anim_state.mouth is False
    assert {"type": "anim", "mouth": False} in _sent_json(device_ws)
    assert {"type": "state", "value": "anim:mouth:off"} in _sent_json(app_ws)


# 5. Unknown params only (legacy "gesture"): ignored, no state change, no broadcast, no error.
async def test_control_anim_unknown_key_only_is_ignored(manager, listen_state):
    anim_state = AnimState(mouth=True)

    device_ws = _make_mock_ws([])
    await manager.connect(device_ws, "device")
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")

    control_json = json.dumps({"type": "control", "command": "anim", "params": {"gesture": False}})
    control_ws = _make_mock_ws([{"type": "websocket.text", "text": control_json}])

    await websocket_endpoint(control_ws, "app", manager, listen_state, anim_state, _drive(manager))

    assert anim_state.mouth is True
    assert not any(m["type"] == "anim" for m in _sent_json(device_ws))
    assert not any(m["type"] == "state" and m["value"].startswith("anim:") for m in _sent_json(app_ws))


# 6. mouth plus a legacy "gesture" key: mouth applies, the unknown key is ignored silently.
async def test_control_anim_mouth_with_unknown_key(manager, listen_state):
    anim_state = AnimState(mouth=True)

    device_ws = _make_mock_ws([])
    await manager.connect(device_ws, "device")
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")

    control_json = json.dumps({"type": "control", "command": "anim", "params": {"mouth": False, "gesture": False}})
    control_ws = _make_mock_ws([{"type": "websocket.text", "text": control_json}])

    await websocket_endpoint(control_ws, "app", manager, listen_state, anim_state, _drive(manager))

    assert anim_state.mouth is False
    assert {"type": "anim", "mouth": False} in _sent_json(device_ws)
    app_sent = _sent_json(app_ws)
    assert {"type": "state", "value": "anim:mouth:off"} in app_sent
    assert not any(m["type"] == "state" and m["value"].startswith("anim:gesture") for m in app_sent)


# 7. control anim also accepted from a device-role connection.
async def test_control_anim_from_device_role_accepted(manager, listen_state):
    anim_state = AnimState(mouth=True)

    other_device_ws = _make_mock_ws([])
    await manager.connect(other_device_ws, "device")
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")

    control_json = json.dumps({"type": "control", "command": "anim", "params": {"mouth": False}})
    control_ws = _make_mock_ws([{"type": "websocket.text", "text": control_json}])

    await websocket_endpoint(control_ws, "device", manager, listen_state, anim_state, _drive(manager))

    assert anim_state.mouth is False
    # control_ws is itself a device connection and receives its own greeting + the broadcast.
    assert {"type": "anim", "mouth": False} in _sent_json(control_ws)
    assert {"type": "anim", "mouth": False} in _sent_json(other_device_ws)
    assert {"type": "state", "value": "anim:mouth:off"} in _sent_json(app_ws)


# 8. Empty params: state unchanged, no broadcast to devices/apps.
async def test_control_anim_empty_params_no_change_no_broadcast(manager, listen_state):
    anim_state = AnimState(mouth=True)

    device_ws = _make_mock_ws([])
    await manager.connect(device_ws, "device")
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")

    control_json = json.dumps({"type": "control", "command": "anim", "params": {}})
    control_ws = _make_mock_ws([{"type": "websocket.text", "text": control_json}])

    await websocket_endpoint(control_ws, "app", manager, listen_state, anim_state, _drive(manager))

    assert anim_state.mouth is True
    assert not any(m["type"] == "anim" for m in _sent_json(device_ws))
    assert not any(m["type"] == "state" and m["value"].startswith("anim:") for m in _sent_json(app_ws))
