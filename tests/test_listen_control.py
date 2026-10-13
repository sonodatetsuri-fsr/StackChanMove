"""Tests for client-driven listen control (control listen, listening flag)."""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from openmovehub.config import Settings
from openmovehub.models.frames import KIND_PCM, encode_frame
from openmovehub.models.messages import ControlMessage, StateMessage, parse_message
from openmovehub.routers.ws import websocket_endpoint
from openmovehub.services.anim_state import AnimState
from openmovehub.services.connection_manager import ConnectionManager
from openmovehub.services.drive_controller import DriveController
from openmovehub.services.listen_state import ListenState
from openmovehub.services.stt_runner import SttRunner


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
def anim_state():
    return AnimState(mouth=True)


# 1. ControlMessage accepts command="listen".
def test_control_message_listen_parses():
    raw = json.dumps({"type": "control", "command": "listen", "params": {"on": False}})
    msg = parse_message(raw)
    assert isinstance(msg, ControlMessage)
    assert msg.command == "listen"
    assert msg.params == {"on": False}


# 2. Settings.listen_default env parsing (HUB_LISTEN_DEFAULT="off" -> False, unset -> True).
def _parse_listen_default() -> bool:
    return os.getenv("HUB_LISTEN_DEFAULT", "on").strip().lower() not in ("off", "false", "0")


def test_settings_listen_default_off_from_env(monkeypatch):
    monkeypatch.setenv("HUB_LISTEN_DEFAULT", "off")
    s = Settings(listen_default=_parse_listen_default())
    assert s.listen_default is False


def test_settings_listen_default_unset_defaults_on(monkeypatch):
    monkeypatch.delenv("HUB_LISTEN_DEFAULT", raising=False)
    s = Settings(listen_default=_parse_listen_default())
    assert s.listen_default is True


# 3. Device connection greeting reflects the current listening flag.
async def test_device_greeting_reflects_listen_off(manager, anim_state):
    listen_state = ListenState(on=False)
    ws = _make_mock_ws([])

    await websocket_endpoint(ws, "device", manager, listen_state, anim_state, _drive(manager))

    greeting = _sent_json(ws)[0]
    assert greeting == {"type": "listen", "on": False}


async def test_device_greeting_reflects_listen_on(manager, anim_state):
    listen_state = ListenState(on=True)
    ws = _make_mock_ws([])

    await websocket_endpoint(ws, "device", manager, listen_state, anim_state, _drive(manager))

    greeting = _sent_json(ws)[0]
    assert greeting == {"type": "listen", "on": True}


# 4 & 9. control listen (from an app-role connection) broadcasts listen off/on to devices
# and state listening:off/on to apps.
async def test_control_listen_off_from_app_broadcasts(manager, anim_state):
    listen_state = ListenState(on=True)

    device_ws = _make_mock_ws([])
    await manager.connect(device_ws, "device")
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")

    control_json = json.dumps({"type": "control", "command": "listen", "params": {"on": False}})
    control_ws = _make_mock_ws([{"type": "websocket.text", "text": control_json}])

    await websocket_endpoint(control_ws, "app", manager, listen_state, anim_state, _drive(manager))

    assert listen_state.on is False
    assert {"type": "listen", "on": False} in _sent_json(device_ws)
    assert {"type": "state", "value": "listening:off"} in _sent_json(app_ws)


async def test_control_listen_on_from_app_broadcasts(manager, anim_state):
    listen_state = ListenState(on=False)

    device_ws = _make_mock_ws([])
    await manager.connect(device_ws, "device")
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")

    control_json = json.dumps({"type": "control", "command": "listen", "params": {"on": True}})
    control_ws = _make_mock_ws([{"type": "websocket.text", "text": control_json}])

    await websocket_endpoint(control_ws, "app", manager, listen_state, anim_state, _drive(manager))

    assert listen_state.on is True
    assert {"type": "listen", "on": True} in _sent_json(device_ws)
    assert {"type": "state", "value": "listening:on"} in _sent_json(app_ws)


# listen OFF calls the registered purge callbacks (the audio pipeline registers here).
async def test_control_listen_off_calls_purge_callbacks(manager, anim_state):
    listen_state = ListenState(on=True)
    purge = MagicMock()
    listen_state.register_purge(purge)

    control_json = json.dumps({"type": "control", "command": "listen", "params": {"on": False}})
    control_ws = _make_mock_ws([{"type": "websocket.text", "text": control_json}])

    await websocket_endpoint(control_ws, "app", manager, listen_state, anim_state, _drive(manager))

    purge.assert_called_once_with()
    listen_state.unregister_purge(purge)
    listen_state.purge()
    purge.assert_called_once_with()


# --- listen OFF で音声の滞留分を破棄、state stt:* -----------------------------


def _ready_runner(text: str = "テスト") -> SttRunner:
    plugin = MagicMock()
    plugin.name = "mock"
    plugin.transcribe = MagicMock(return_value=text)
    plugin.status = MagicMock(return_value={})
    runner = SttRunner(plugin, label="mock")
    runner._state = "ready"
    return runner


async def test_listen_off_purges_the_vad_accumulator_of_audio_connections(manager, anim_state):
    """device 接続中に別の app が listen OFF → その device の VAD が作り直され heard は出ない。"""
    listen_state = ListenState(on=True)
    vad = MagicMock()
    vad.create_accumulator = MagicMock(side_effect=lambda: MagicMock(feed=MagicMock(return_value=None)))
    runner = _ready_runner()
    control_json = json.dumps({"type": "control", "command": "listen", "params": {"on": False}})
    steps = [
        {"type": "websocket.bytes", "bytes": encode_frame(KIND_PCM, b"\x00\x01" * 50)},
        "listen_off",
        {"type": "websocket.bytes", "bytes": encode_frame(KIND_PCM, b"\x00\x01" * 50)},
    ]

    async def receive():
        while steps:
            step = steps.pop(0)
            if step == "listen_off":
                app_ws = _make_mock_ws([{"type": "websocket.text", "text": control_json}])
                await websocket_endpoint(app_ws, "app", manager, listen_state, anim_state, _drive(manager))
                continue
            return step
        await asyncio.sleep(0.05)
        return {"type": "websocket.disconnect"}

    device_ws = MagicMock()
    device_ws.accept = AsyncMock()
    device_ws.send_text = AsyncMock()
    device_ws.close = AsyncMock()
    device_ws.receive = AsyncMock(side_effect=receive)
    await websocket_endpoint(
        device_ws, "device", manager, listen_state, anim_state, _drive(manager), stt_runner=runner, vad=vad
    )

    assert vad.create_accumulator.call_count == 2  # 接続時 + purge
    assert listen_state._purge_callbacks == []  # 切断で登録解除
    assert not any(m["type"] == "heard" for m in _sent_json(device_ws))
    runner.plugin.transcribe.assert_not_called()


@pytest.mark.parametrize("state", ["loading", "ready", "error", "off"])
async def test_app_greeting_includes_stt_state(manager, anim_state, state):
    runner = _ready_runner()
    runner._state = state
    ws = _make_mock_ws([])
    await websocket_endpoint(ws, "app", manager, ListenState(on=True), anim_state, _drive(manager), stt_runner=runner)
    assert {"type": "state", "value": f"stt:{state}"} in _sent_json(ws)


async def test_stt_state_change_is_broadcast_to_apps(manager):
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")

    class Plugin:
        name = "fake"

        def load(self):
            return None

        def status(self):
            return {}

    async def notify(state: str) -> None:
        await manager.send_json_to_apps(StateMessage(type="state", value=f"stt:{state}"))

    runner = SttRunner(Plugin(), notify=notify)
    runner.start()
    for _ in range(100):
        if {"type": "state", "value": "stt:ready"} in _sent_json(app_ws):
            break
        await asyncio.sleep(0.01)
    assert {"type": "state", "value": "stt:ready"} in _sent_json(app_ws)
