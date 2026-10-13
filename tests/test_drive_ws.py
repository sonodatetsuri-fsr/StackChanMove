"""Tests for WebSocket greetings, device->app relay (odom/joy/event) and drive control."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from openmovehub.config import Settings
from openmovehub.routers.ws import websocket_endpoint
from openmovehub.services.anim_state import AnimState
from openmovehub.services.camera_relay import CameraRelay
from openmovehub.services.drive_controller import DriveController
from openmovehub.services.listen_state import ListenState

ODOM_PAYLOAD = {
    "type": "odom",
    "ts": 1000,
    "seq": 3,
    "x": 0.5,
    "y": 0.0,
    "th": 0.1,
    "v": 0.15,
    "w": 0.0,
    "dl": 0.01,
    "dr": 0.01,
    "cmd": {"v": 0.15, "w": 0.0, "src": "hub"},
    "wheel": {"l_pos": 100, "r_pos": 100, "l_valid": True, "r_valid": True},
}


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
def listen_state():
    return ListenState(on=True)


@pytest.fixture
def anim_state():
    return AnimState(mouth=True)


@pytest.fixture
def drive(manager):
    return DriveController(manager, Settings())


@pytest.fixture
def camera_relay():
    return CameraRelay()


async def run_ws(ws, role, manager, listen_state, anim_state, drive, camera_relay=None, odom_hz=10):
    await websocket_endpoint(
        ws, role, manager, listen_state, anim_state, drive, camera_relay=camera_relay, odom_hz=odom_hz
    )


# --- greetings --------------------------------------------------------------


async def test_device_greeting_order(manager, listen_state, anim_state, drive):
    ws = _make_mock_ws([])
    await run_ws(ws, "device", manager, listen_state, anim_state, drive)

    assert _sent_json(ws) == [
        {"type": "listen", "on": True},
        {"type": "anim", "mouth": True},
        {"type": "drive_source", "source": "hub"},
        # 接続直後にカメラ配信設定 (全量) を送る。
        {"type": "camera", "on": True, "fps": 4, "quality": 60},
        # odom_rate は既定 10 Hz (stackchan_move の 50 は記録用)。
        {"type": "odom_rate", "hz": 10},
    ]


async def test_device_greeting_reflects_current_drive_source(manager, listen_state, anim_state, drive):
    drive.source = "joystick"
    ws = _make_mock_ws([])
    await run_ws(ws, "device", manager, listen_state, anim_state, drive)

    assert _sent_json(ws)[2] == {"type": "drive_source", "source": "joystick"}


async def test_app_greeting_state_messages(manager, listen_state, anim_state, drive):
    ws = _make_mock_ws([])
    await run_ws(ws, "app", manager, listen_state, anim_state, drive)

    assert _sent_json(ws) == [
        {"type": "state", "value": "listening:on"},
        {"type": "state", "value": "anim:mouth:on"},
        {"type": "state", "value": "drive_source:hub"},
        # STT の状態 (音声サービス無しは off)。
        {"type": "state", "value": "stt:off"},
    ]
    assert not any(m["value"].startswith("anim:gesture") for m in _sent_json(ws))


async def test_app_greeting_listen_off(manager, anim_state, drive):
    ws = _make_mock_ws([])
    await run_ws(ws, "app", manager, ListenState(on=False), anim_state, drive)

    assert _sent_json(ws)[0] == {"type": "state", "value": "listening:off"}


# --- device -> app relay ----------------------------------------------------


async def test_odom_relayed_raw_and_stored(manager, listen_state, anim_state, drive):
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    raw = json.dumps(ODOM_PAYLOAD)
    device_ws = _make_mock_ws([_text(raw)])

    await run_ws(device_ws, "device", manager, listen_state, anim_state, drive)

    # The exact JSON string is relayed (not re-serialized).
    assert raw in _sent_raw(app_ws)
    assert drive.last_odom == ODOM_PAYLOAD


async def test_invalid_odom_not_relayed(manager, listen_state, anim_state, drive):
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    bad = json.dumps({"type": "odom", "ts": 1, "seq": 1})
    device_ws = _make_mock_ws([_text(bad)])

    await run_ws(device_ws, "device", manager, listen_state, anim_state, drive)

    assert bad not in _sent_raw(app_ws)
    assert drive.last_odom is None


async def test_joy_relayed(manager, listen_state, anim_state, drive):
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    raw = json.dumps({"type": "joy", "x": 0.5, "y": -0.5, "btn": False})
    device_ws = _make_mock_ws([_text(raw)])

    await run_ws(device_ws, "device", manager, listen_state, anim_state, drive)

    assert raw in _sent_raw(app_ws)


async def test_event_relayed(manager, listen_state, anim_state, drive):
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    raw = json.dumps({"type": "event", "name": "boot", "detail": "track=77.5 wheel=37.0"})
    device_ws = _make_mock_ws([_text(raw)])

    await run_ws(device_ws, "device", manager, listen_state, anim_state, drive)

    assert raw in _sent_raw(app_ws)


async def test_odom_from_app_role_is_ignored(manager, listen_state, anim_state, drive):
    other_app = _make_mock_ws([])
    await manager.connect(other_app, "app")
    raw = json.dumps(ODOM_PAYLOAD)
    app_ws = _make_mock_ws([_text(raw)])

    await run_ws(app_ws, "app", manager, listen_state, anim_state, drive)

    assert raw not in _sent_raw(other_app)
    assert drive.last_odom is None


async def test_event_estop_stops_and_returns_to_hub(manager, listen_state, anim_state, drive):
    app_ws = _make_mock_ws([])
    await manager.connect(app_ws, "app")
    drive.source = "joystick"
    drive.set_target(0.15, 2.0)
    raw = json.dumps({"type": "event", "name": "estop", "detail": ""})
    device_ws = _make_mock_ws([_text(raw)])

    await run_ws(device_ws, "device", manager, listen_state, anim_state, drive)

    assert drive.target == (0.0, 0.0)
    assert drive.source == "hub"
    assert {"type": "state", "value": "drive_source:hub"} in _sent_json(app_ws)
    assert {"type": "drive_source", "source": "hub"} in _sent_json(device_ws)


# --- control over the WebSocket --------------------------------------------


async def test_control_drive_sets_clamped_target(manager, listen_state, anim_state, drive):
    raw = json.dumps({"type": "control", "command": "drive", "params": {"v": 9.0, "w": -9.0}})
    ws = _make_mock_ws([_text(raw)])

    await run_ws(ws, "app", manager, listen_state, anim_state, drive)

    assert drive.target == (0.15, -2.0)


async def test_control_drive_missing_params_defaults_to_zero(manager, listen_state, anim_state, drive):
    drive.set_target(0.1, 0.1)
    raw = json.dumps({"type": "control", "command": "drive", "params": {}})
    ws = _make_mock_ws([_text(raw)])

    await run_ws(ws, "app", manager, listen_state, anim_state, drive)

    assert drive.target == (0.0, 0.0)


async def test_control_drive_source_from_app(manager, listen_state, anim_state, drive):
    device_ws = _make_mock_ws([])
    await manager.connect(device_ws, "device")
    raw = json.dumps({"type": "control", "command": "drive_source", "params": {"source": "joystick"}})
    ws = _make_mock_ws([_text(raw)])

    await run_ws(ws, "app", manager, listen_state, anim_state, drive)

    assert drive.source == "joystick"
    assert {"type": "drive_source", "source": "joystick"} in _sent_json(device_ws)
    assert {"type": "state", "value": "drive_source:joystick"} in _sent_json(ws)


async def test_control_drive_source_invalid_is_ignored(manager, listen_state, anim_state, drive):
    raw = json.dumps({"type": "control", "command": "drive_source", "params": {"source": "manual"}})
    ws = _make_mock_ws([_text(raw)])

    await run_ws(ws, "app", manager, listen_state, anim_state, drive)

    assert drive.source == "hub"


async def test_control_odom_reset_sent_to_devices(manager, listen_state, anim_state, drive):
    device_ws = _make_mock_ws([])
    await manager.connect(device_ws, "device")
    raw = json.dumps({"type": "control", "command": "odom_reset", "params": {}})
    ws = _make_mock_ws([_text(raw)])

    await run_ws(ws, "app", manager, listen_state, anim_state, drive)

    assert {"type": "odom_reset"} in _sent_json(device_ws)


async def test_control_camera_is_forwarded_to_devices(manager, listen_state, anim_state, drive):
    """control camera は全量スナップショットとして device に転送される。"""
    device_ws = _make_mock_ws([])
    await manager.connect(device_ws, "device")
    raw = json.dumps({"type": "control", "command": "camera", "params": {"on": True}})
    ws = _make_mock_ws([_text(raw)])

    await run_ws(ws, "app", manager, listen_state, anim_state, drive)

    assert _sent_json(device_ws) == [{"type": "camera", "on": True, "fps": 4, "quality": 60}]


# --- Hub additions ------------------------------------------------------------


async def test_device_greeting_odom_rate_follows_hub_odom_hz(manager, listen_state, anim_state, drive):
    """greeting の odom_rate は HUB_ODOM_HZ (5..50 にクランプ)。"""
    for hz, expected in [(25, 25), (1, 5), (999, 50)]:
        ws = _make_mock_ws([])
        await run_ws(ws, "device", manager, listen_state, anim_state, drive, odom_hz=hz)
        assert _sent_json(ws)[-1] == {"type": "odom_rate", "hz": expected}


async def test_device_greeting_with_default_odom_hz_argument(manager, listen_state, anim_state, drive):
    """odom_hz を省略した websocket_endpoint の既定も 10 Hz。"""
    ws = _make_mock_ws([])
    await websocket_endpoint(ws, "device", manager, listen_state, anim_state, drive)
    assert _sent_json(ws)[-1] == {"type": "odom_rate", "hz": 10}


async def test_control_drive_source_auto_is_rejected_with_current_state(manager, listen_state, anim_state, drive):
    """drive_source auto は拒否し、app に state drive_source:<現在値> を返す。"""
    device_ws = _make_mock_ws([])
    await manager.connect(device_ws, "device")
    other_app = _make_mock_ws([])
    await manager.connect(other_app, "app")
    drive.source = "joystick"
    raw = json.dumps({"type": "control", "command": "drive_source", "params": {"source": "auto"}})
    ws = _make_mock_ws([_text(raw)])

    await run_ws(ws, "app", manager, listen_state, anim_state, drive)

    assert drive.source == "joystick"
    assert {"type": "state", "value": "drive_source:joystick"} in _sent_json(other_app)
    assert _sent_json(device_ws) == []


@pytest.mark.parametrize("command", ["record", "explore", "odom_rate", "wheel_pwm", "autonomy", "train", "pose"])
async def test_control_removed_commands_are_rejected(manager, listen_state, anim_state, drive, command, caplog):
    """control.command の Literal から削ったコマンドは検証で拒否される (効果なし、落ちない)。"""
    device_ws = _make_mock_ws([])
    await manager.connect(device_ws, "device")
    other_app = _make_mock_ws([])
    await manager.connect(other_app, "app")
    raw = json.dumps({"type": "control", "command": command, "params": {"on": True, "hz": 25}})
    # 拒否の後もコネクションは生きていて次のメッセージを処理する。
    follow = json.dumps({"type": "control", "command": "odom_reset", "params": {}})
    ws = _make_mock_ws([_text(raw), _text(follow)])

    with caplog.at_level("WARNING"):
        await run_ws(ws, "app", manager, listen_state, anim_state, drive)

    assert "Invalid message" in caplog.text
    assert _sent_json(device_ws) == [{"type": "odom_reset"}]
    assert _sent_json(other_app) == []


async def test_speak_is_ignored_without_error(manager, listen_state, anim_state, drive, caplog):
    """speak の処理 (TTS) は別モジュール。この Hub では受け取って捨てる。"""
    device_ws = _make_mock_ws([])
    await manager.connect(device_ws, "device")
    raw = json.dumps({"type": "speak", "text": "こんにちは"})
    follow = json.dumps({"type": "control", "command": "odom_reset", "params": {}})
    ws = _make_mock_ws([_text(raw), _text(follow)])

    with caplog.at_level("ERROR"):
        await run_ws(ws, "app", manager, listen_state, anim_state, drive)

    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert _sent_json(device_ws) == [{"type": "odom_reset"}]
