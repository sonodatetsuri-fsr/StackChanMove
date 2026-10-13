"""Mock joystick tests for the device simulator."""

import asyncio
import contextlib
import json

import pytest
import sim_device
from sim_device import DiffDriveSim, SimDevice

# --- (v, w) mapping (mirrored on the firmware side) -------------


def test_joystick_forward_full_maps_to_v_max():
    sim = DiffDriveSim()
    sim.set_source("joystick")
    sim.set_joystick(0.0, 1.0, 0)
    sim.step(0.1, 0.1)
    assert sim.cmd_v == pytest.approx(sim_device.V_MAX)
    assert sim.cmd_w == pytest.approx(0.0)
    assert sim.cmd_src == "joystick"


def test_joystick_left_full_spins_in_place():
    sim = DiffDriveSim()
    sim.set_source("joystick")
    sim.set_joystick(-1.0, 0.0, 0)
    sim.step(0.1, 0.1)
    assert sim.cmd_v == pytest.approx(0.0)
    assert sim.cmd_w == pytest.approx(-sim_device.W_MAX)


def test_joystick_forward_right_arc():
    sim = DiffDriveSim()
    sim.set_source("joystick")
    sim.set_joystick(1.0, 0.5, 0)
    sim.step(0.1, 0.1)
    assert sim.cmd_v == pytest.approx(0.5 * sim_device.V_MAX)
    assert sim.cmd_w == pytest.approx(sim_device.W_MAX)


def test_joystick_backward_left_w_sign_independent_of_v():
    sim = DiffDriveSim()
    sim.set_source("joystick")
    sim.set_joystick(1.0, -1.0, 0)
    sim.step(0.1, 0.1)
    assert sim.cmd_v == pytest.approx(-sim_device.V_MAX)
    assert sim.cmd_w == pytest.approx(sim_device.W_MAX)


# --- button: short press = emergency stop -----------------------


def test_short_press_estops_from_joystick_and_forces_hub():
    sim = DiffDriveSim()
    sim.set_source("joystick")
    sim.set_joystick(0.0, 1.0, 0)
    sim.step(0.1, 0.1)
    assert sim.cmd_v > 0

    sim.set_joystick(0.0, 1.0, 1)  # press edge -> estop
    sim.step(0.1, 0.2)
    assert (sim.cmd_v, sim.cmd_w) == (0.0, 0.0)
    assert sim.source == "hub"
    assert sim.pop_events() == [("estop", "joystick_button")]

    # holding the button does not re-fire
    sim.set_joystick(0.0, 1.0, 1)
    sim.step(0.1, 0.3)
    assert sim.pop_events() == []

    # release then press fires again
    sim.set_joystick(0.0, 1.0, 0)
    sim.step(0.1, 0.4)
    assert sim.pop_events() == []
    sim.set_joystick(0.0, 1.0, 1)
    assert sim.pop_events() == [("estop", "joystick_button")]


def test_short_press_estops_from_hub_and_drops_active_drive():
    sim = DiffDriveSim()
    sim.apply_drive(0.15, 0.0, "hub", now=0.0)
    sim.step(0.1, 0.1)
    assert sim.cmd_v > 0

    sim.set_joystick(0.0, 0.0, 1)  # press edge -> estop, source already hub
    sim.step(0.1, 0.2)
    assert (sim.cmd_v, sim.cmd_w) == (0.0, 0.0)
    assert sim.source == "hub"
    assert sim.pop_events() == [("estop", "joystick_button")]

    # the dropped hub drive does not resume on its own
    sim.step(0.1, 0.3)
    assert (sim.cmd_v, sim.cmd_w) == (0.0, 0.0)


# --- button: long press = standalone source toggle --------------


def test_long_press_ignored_when_hub_connected():
    sim = DiffDriveSim()
    sim.hub_connected = True
    sim.set_source("joystick")

    sim.set_joystick(0.0, 0.0, 2)  # press edge -> estop still fires
    assert sim.source == "hub"
    assert sim.pop_events() == [("estop", "joystick_button")]

    # a further long-press edge (after release) still does not toggle
    sim.set_joystick(0.0, 0.0, 0)
    sim.set_joystick(0.0, 0.0, 2)
    assert sim.source == "hub"
    assert sim.pop_events() == [("estop", "joystick_button")]


def test_long_press_toggles_when_hub_disconnected():
    """'0->2 while standalone-joystick results in hub; a
    subsequent 2->0->2 toggles back to joystick.'"""
    sim = DiffDriveSim()
    assert sim.hub_connected is False
    sim.set_source("joystick")  # start "standalone-joystick"

    # 0 -> 2: estop forces hub, then the toggle (based on the pre-estop
    # source, "joystick") also lands on "hub" -- the estop "wins"
    sim.set_joystick(0.3, 0.4, 2)
    assert sim.source == "hub"
    assert sim.pop_events() == [("estop", "joystick_button")]

    # 2 -> 0: release, not a press edge, no estop, no toggle
    sim.set_joystick(0.3, 0.4, 0)
    assert sim.source == "hub"
    assert sim.pop_events() == []

    # 0 -> 2 again: estop is a no-op (already hub), then the toggle (based on
    # the pre-estop source, "hub") lands on "joystick"
    sim.set_joystick(0.3, 0.4, 2)
    assert sim.source == "joystick"
    assert sim.pop_events() == [("estop", "joystick_button")]
    sim.step(0.1, 0.1)
    assert sim.cmd_src == "joystick"


def test_long_press_toggle_round_trip_hub_joystick_hub():
    sim = DiffDriveSim()
    sim.hub_connected = False
    sim.set_source("joystick")

    sim.set_joystick(0.0, 0.0, 2)  # joystick -> (estop: hub) -> toggle -> hub
    assert sim.source == "hub"
    sim.pop_events()

    sim.set_joystick(0.0, 0.0, 0)
    sim.set_joystick(0.0, 0.0, 2)  # hub -> (estop: hub, no-op) -> toggle -> joystick
    assert sim.source == "joystick"
    sim.pop_events()

    sim.set_joystick(0.0, 0.0, 0)
    sim.set_joystick(0.0, 0.0, 2)  # joystick -> (estop: hub) -> toggle -> hub
    assert sim.source == "hub"


# --- joy_message_if_due throttling --------------------------


def test_joy_message_first_sample_small_delta_btn_change_and_heartbeat():
    sim = DiffDriveSim()
    assert sim.hub_connected is False

    # single long press from the default "hub" source lands on "joystick"
    # (see test_long_press_toggles_when_hub_disconnected) and leaves btn "pressed"
    sim.set_joystick(0.2, 0.3, 2)
    assert sim.source == "joystick"
    sim.pop_events()

    # emitted on first sample
    msg = sim.joy_message_if_due(0.0)
    assert msg == {"type": "joy", "x": 0.2, "y": 0.3, "btn": True}

    # suppressed for a small (<0.02) delta within the 1 s heartbeat window
    sim.set_joystick(0.21, 0.3, 2)
    assert sim.joy_message_if_due(0.05) is None

    # emitted on a button-state change (release: pressed True -> False; this
    # is not a press edge, so no estop and source stays "joystick")
    sim.set_joystick(0.21, 0.3, 0)
    msg = sim.joy_message_if_due(0.2)
    assert msg == {"type": "joy", "x": 0.21, "y": 0.3, "btn": False}

    # emitted again after the 1 s heartbeat even with no change
    assert sim.joy_message_if_due(0.3) is None  # < 1 s since last send
    msg = sim.joy_message_if_due(1.3)
    assert msg == {"type": "joy", "x": 0.21, "y": 0.3, "btn": False}


def test_joy_message_capped_at_10hz():
    sim = DiffDriveSim()
    sim.set_joystick(0.0, 0.0, 2)  # -> joystick, btn pressed
    sim.pop_events()
    assert sim.joy_message_if_due(0.0) is not None

    sim.set_joystick(1.0, 1.0, 2)  # large delta, but within 100 ms of last send
    assert sim.joy_message_if_due(0.05) is None
    assert sim.joy_message_if_due(0.11) is not None


def test_joy_message_not_emitted_when_source_is_hub():
    sim = DiffDriveSim()
    assert sim.source == "hub"
    sim.set_joystick(1.0, 1.0, 0)
    assert sim.joy_message_if_due(0.0) is None


# --- SimDevice integration ---------------------------------------------------


class _FakeWS:
    """Minimal async websocket stand-in: records sends, never yields on receive."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_session_sends_joy_link_and_toggles_hub_connected():
    sim = DiffDriveSim()
    device = SimDevice(sim, "ws://example.invalid/ws/device", rate=10.0)
    ws = _FakeWS()

    assert sim.hub_connected is False
    task = asyncio.create_task(device.session(ws))
    await asyncio.sleep(0.05)
    assert sim.hub_connected is True

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert sim.hub_connected is False

    assert len(ws.sent) >= 2
    joy_link = json.loads(ws.sent[0])
    boot = json.loads(ws.sent[1])
    assert joy_link == {"type": "event", "name": "joy_link", "detail": "up SIM"}
    assert boot["type"] == "event" and boot["name"] == "boot"


@pytest.mark.asyncio
async def test_send_loop_emits_joy_message_and_estop_event():
    sim = DiffDriveSim()
    sim.set_source("joystick")
    sim.set_joystick(0.0, 1.0, 0)
    device = SimDevice(sim, "ws://example.invalid/ws/device", rate=20.0)
    ws = _FakeWS()

    task = asyncio.create_task(device.send_loop(ws))
    await asyncio.sleep(0.1)
    sim.set_joystick(0.0, 1.0, 1)  # short press -> estop, source -> hub
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    types = [json.loads(raw)["type"] for raw in ws.sent]
    assert "odom" in types
    assert "joy" in types
    events = [json.loads(raw) for raw in ws.sent if json.loads(raw)["type"] == "event"]
    assert any(e["name"] == "estop" and e["detail"] == "joystick_button" for e in events)


# --- tools/drive_cli.py ----------------------------------------


def test_drive_cli_parser_defaults():
    import drive_cli

    args = drive_cli.build_parser().parse_args([])
    assert args.hub == "ws://127.0.0.1:8341/ws/app"
    assert args.source is None
    assert args.duration == pytest.approx(3.0)
    assert (args.v, args.w) == (0.0, 0.0)


def test_drive_cli_source_choices_exclude_auto():
    import drive_cli

    parser = drive_cli.build_parser()
    assert parser.parse_args(["--source", "joystick"]).source == "joystick"
    assert parser.parse_args(["--source", "hub"]).source == "hub"
    with pytest.raises(SystemExit):
        parser.parse_args(["--source", "auto"])


def test_drive_cli_drive_message_is_a_control_drive():
    import drive_cli

    assert json.loads(drive_cli.drive_message(0.1, 0.5)) == {
        "type": "control",
        "command": "drive",
        "params": {"v": 0.1, "w": 0.5},
    }
