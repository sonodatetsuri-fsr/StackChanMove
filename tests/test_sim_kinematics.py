"""Kinematics tests for the device simulator."""

import json
import math

import pytest
import sim_device
from sim_device import DiffDriveSim


def run_steps(
    sim: DiffDriveSim,
    dt: float,
    seconds: float,
    t0: float = 0.0,
    cmd: tuple[float, float, str] | None = None,
) -> float:
    """Advance the simulator by ``seconds`` in ``dt`` slices, returning the end time.

    When ``cmd`` is given it is re-applied before every step, the way the Hub
    repeater keeps sending ``drive`` at ``HUB_DRIVE_RATE_HZ``.
    """
    now = t0
    for _ in range(int(round(seconds / dt))):
        if cmd is not None:
            sim.apply_drive(cmd[0], cmd[1], cmd[2], now=now)
        now += dt
        sim.step(dt, now)
    return now


def test_defaults_match_hardware_geometry():
    sim = DiffDriveSim()
    assert sim.track_m == 0.0775
    assert sim.wheel_m == 0.037
    assert sim.circumference_m == math.pi * 0.037


def test_straight_line():
    """0.30 m straight; odom reports the true pose."""
    sim = DiffDriveSim()
    start_l, start_r = sim.l_pos, sim.r_pos
    run_steps(sim, 0.02, 2.0, cmd=(0.15, 0.0, "hub"))

    assert sim.x == pytest.approx(0.30)
    assert sim.y == pytest.approx(0.0)
    assert sim.th == pytest.approx(0.0)
    assert sim.cmd_src == "hub"
    assert sim.dl == pytest.approx(sim.dr)

    body = sim.odom_body()
    assert (body["x"], body["y"], body["th"]) == (sim.x, sim.y, sim.th)

    # mirror-mounted wheels -> the left raw reading counts *down*
    # while the right one counts up (WHEEL_LEFT_SIGN * WHEEL_POS_SIGN = -1).
    assert 0 <= sim.l_pos <= 1023
    assert 0 <= sim.r_pos <= 1023
    short = DiffDriveSim()
    run_steps(short, 0.02, 0.2, cmd=(0.15, 0.0, "hub"))  # less than one revolution
    # readings start at 0 and wrap mod 1024, so compare the modular step
    assert 0 < (start_l - short.l_pos) % 1024 < 512
    assert 0 < (short.r_pos - start_r) % 1024 < 512


def test_spin_in_place():
    sim = DiffDriveSim()
    run_steps(sim, 0.1, 1.0, cmd=(0.0, 2.0, "hub"))

    assert sim.th == pytest.approx(2.0)
    assert sim.x == pytest.approx(0.0)
    assert sim.y == pytest.approx(0.0)
    assert sim.dl == pytest.approx(-sim.dr)
    # left turn (w > 0): the left wheel goes backwards and the right one forwards,
    # and because the left reading counts down both raw readings run together
    assert sim.r_pos > 0
    assert sim.l_pos == sim.r_pos


def test_wheel_reading_wraps_without_a_dead_zone():
    """1024 counts span 300 deg and simply wrap (no dead-zone model)."""
    sim = DiffDriveSim()
    # one full wheel revolution forward = 1024 * 360/300 counts -> wraps back into 0..1023
    rev_s = sim.circumference_m / 0.15
    run_steps(sim, 0.01, round(rev_s, 2), cmd=(0.15, 0.0, "hub"))
    assert 0 <= sim.r_pos <= 1023
    assert 0 <= sim.l_pos <= 1023
    body = sim.odom_body()
    assert body["wheel"]["l_valid"] is True
    assert body["wheel"]["r_valid"] is True


def test_theta_wraps_to_pi_range():
    sim = DiffDriveSim()
    run_steps(sim, 0.1, 2.0, cmd=(0.0, 2.0, "hub"))  # 4.0 rad > pi
    assert -math.pi <= sim.th < math.pi
    assert sim.th == pytest.approx(4.0 - 2.0 * math.pi)


def test_watchdog_stops_and_emits_drive_timeout_once():
    sim = DiffDriveSim()
    sim.apply_drive(0.15, 0.0, "hub", now=0.0)

    now = run_steps(sim, 0.1, 0.5)  # still within the 0.5 s watchdog
    assert sim.cmd_src == "hub"
    assert sim.pop_events() == []

    now += 0.1  # 0.6 s since the last command
    sim.step(0.1, now)
    assert (sim.cmd_v, sim.cmd_w, sim.cmd_src) == (0.0, 0.0, "none")
    assert sim.pop_events() == [("drive_timeout", "no drive within DRIVE_TIMEOUT_MS")]

    # flagged once per timeout, not once per step
    run_steps(sim, 0.1, 1.0, t0=now)
    assert sim.pop_events() == []
    assert sim.cmd_src == "none"

    # a new command re-arms the watchdog
    x_before = sim.x
    sim.apply_drive(0.15, 0.0, "hub", now=now + 1.0)
    sim.step(0.1, now + 1.1)
    assert sim.cmd_src == "hub"
    assert sim.x > x_before


def test_reset_zeroes_pose():
    """``odom_reset`` zeroes the pose; the wheel travel (raw readings) is kept."""
    sim = DiffDriveSim()
    run_steps(sim, 0.02, 1.0, cmd=(0.15, 1.0, "hub"))
    assert sim.x != 0.0
    l_before, r_before = sim.l_pos, sim.r_pos

    sim.reset()
    assert (sim.x, sim.y, sim.th) == (0.0, 0.0, 0.0)
    assert (sim.dl, sim.dr) == (0.0, 0.0)
    body = sim.odom_body()
    assert (body["x"], body["y"], body["th"]) == (0.0, 0.0, 0.0)
    # the wheel travel (and so the raw readings) survive the reset
    assert (sim.l_pos, sim.r_pos) == (l_before, r_before)


def test_odom_message_matches_contract():
    sim = DiffDriveSim()
    sim.apply_drive(0.1, 0.5, "hub", now=0.0)
    sim.step(0.1, 0.1)
    msg = sim.odom_message(123456, 7)

    assert set(msg) == {"type", "ts", "seq", "x", "y", "th", "v", "w", "dl", "dr", "cmd", "wheel"}
    assert msg["type"] == "odom"
    assert msg["ts"] == 123456
    assert msg["seq"] == 7
    assert set(msg["cmd"]) == {"v", "w", "src"}
    assert set(msg["wheel"]) == {"l_pos", "r_pos", "l_valid", "r_valid"}
    assert msg["cmd"] == {"v": pytest.approx(0.1), "w": pytest.approx(0.5), "src": "hub"}
    assert (msg["v"], msg["w"]) == (pytest.approx(0.1), pytest.approx(0.5))
    assert isinstance(msg["wheel"]["l_valid"], bool)
    assert isinstance(msg["wheel"]["r_valid"], bool)
    assert isinstance(msg["wheel"]["l_pos"], int)
    assert isinstance(msg["wheel"]["r_pos"], int)


def test_joystick_source_ignores_hub_drive_and_maps_joy():
    sim = DiffDriveSim()
    sim.set_source("joystick")

    # Hub drive is ignored while the joystick owns the robot
    assert sim.apply_drive(0.15, 0.0, "hub", now=0.0) is False
    sim.step(0.1, 0.1)
    assert (sim.cmd_v, sim.cmd_w, sim.cmd_src) == (0.0, 0.0, "joystick")
    assert sim.x == 0.0

    sim.set_joystick(0.5, 1.0, False)
    sim.step(0.1, 0.2)
    assert sim.cmd_v == pytest.approx(sim_device.V_MAX)
    assert sim.cmd_w == pytest.approx(0.5 * sim_device.W_MAX)
    assert sim.cmd_src == "joystick"

    # the joystick has no Hub watchdog: the value is held
    sim.step(0.1, 5.0)
    assert sim.cmd_src == "joystick"
    assert sim.pop_events() == []


def test_source_switch_back_to_hub_starts_stopped():
    sim = DiffDriveSim()
    sim.set_source("joystick")
    sim.set_joystick(1.0, 1.0)
    sim.step(0.1, 0.1)
    assert sim.cmd_v > 0

    sim.set_source("hub")
    sim.step(0.1, 0.2)
    assert (sim.cmd_v, sim.cmd_w, sim.cmd_src) == (0.0, 0.0, "none")
    assert sim.pop_events() == []

    sim.apply_drive(0.15, 0.0, "hub", now=0.2)
    sim.step(0.1, 0.3)
    assert sim.cmd_src == "hub"


# --- odom_rate ---------------------------------------------------


def _device(rate: float = 10.0) -> sim_device.SimDevice:
    return sim_device.SimDevice(DiffDriveSim(), "ws://example.invalid/ws/device", rate=rate)


def test_handle_text_odom_rate_changes_the_send_period():
    device = _device(rate=10.0)
    assert device.period == pytest.approx(0.1)

    device.handle_text('{"type":"odom_rate","hz":50}')
    assert device.period == pytest.approx(0.02)

    device.handle_text('{"type":"odom_rate","hz":25}')
    assert device.period == pytest.approx(0.04)


@pytest.mark.parametrize(("hz", "expected_period"), [(0, 0.2), (1, 0.2), (5, 0.2), (50, 0.02), (999, 0.02)])
def test_handle_text_odom_rate_is_clamped(hz, expected_period):
    device = _device()
    device.handle_text(json.dumps({"type": "odom_rate", "hz": hz}))
    assert device.period == pytest.approx(expected_period)


def test_handle_text_odom_rate_bad_value_is_ignored():
    device = _device(rate=10.0)
    device.handle_text('{"type":"odom_rate","hz":"fast"}')
    assert device.period == pytest.approx(0.1)
    device.handle_text('{"type":"odom_rate"}')
    assert device.period == pytest.approx(0.1)


def test_odom_rate_is_a_known_type():
    assert "odom_rate" in sim_device.KNOWN_TYPES


@pytest.mark.asyncio
async def test_send_loop_uses_the_current_period():
    """``send_loop`` は毎周期 ``self.period`` を読む (途中で ``odom_rate`` が来ても効く)。"""
    import asyncio
    import contextlib
    import json
    from unittest.mock import AsyncMock

    def odom_count(ws) -> int:
        return sum(1 for c in ws.send.await_args_list if json.loads(c.args[0]).get("type") == "odom")

    device = _device(rate=50.0)
    ws = AsyncMock()
    task = asyncio.create_task(device.send_loop(ws))
    try:
        await asyncio.sleep(0.1)
        assert odom_count(ws) >= 3  # 50 Hz なら 100 ms で数通

        device.handle_text('{"type":"odom_rate","hz":5}')
        assert device.period == pytest.approx(0.2)
        await asyncio.sleep(0.05)  # 直前の 20 ms スリープを消化させる
        settled = odom_count(ws)
        await asyncio.sleep(0.1)
        # 5 Hz (200 ms 周期) なので 100 ms では 1 通も増えない。
        assert odom_count(ws) == settled
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_odom_message_is_accepted_by_the_hub_model():
    """The simulator's odom passes the Hub's OdomMessage validation."""
    from openmovehub.models.messages import OdomMessage

    sim = DiffDriveSim()
    sim.apply_drive(0.1, 0.2, "hub", now=0.0)
    sim.step(0.1, 0.1)
    OdomMessage.model_validate(sim.odom_message(1, 1))


def test_handle_text_drive_source_and_drive():
    device = _device()
    device.handle_text('{"type":"drive_source","source":"joystick"}')
    assert device.sim.source == "joystick"
    device.handle_text('{"type":"drive","v":0.1,"w":0,"seq":1,"src":"hub"}')
    device.sim.step(0.1)
    assert device.sim.cmd_src == "joystick"

    device.handle_text('{"type":"drive_source","source":"hub"}')
    assert device.sim.source == "hub"
    # "auto" was removed from this project: the simulator ignores it
    device.handle_text('{"type":"drive_source","source":"auto"}')
    assert device.sim.source == "hub"


def test_removed_message_types_are_not_known():
    assert "wheel_pwm" not in sim_device.KNOWN_TYPES
    assert not hasattr(DiffDriveSim, "apply_wheel_pwm")


# --- CLI ----------------------------------------------------------------------


def test_cli_defaults():
    args = sim_device.build_parser().parse_args([])
    assert args.hub == "ws://127.0.0.1:8341/ws/device"
    assert args.rate == pytest.approx(10.0)
    assert args.track_mm == pytest.approx(77.5)
    assert args.wheel_mm == pytest.approx(37.0)
    assert args.frames_dir is None
    assert args.fps == pytest.approx(5.0)
    assert args.no_camera is False
    assert args.joy is False
    assert args.verbose is False


@pytest.mark.parametrize(
    "argv",
    [
        ["--arena", "1", "1"],
        ["--deadzone-model", "mid"],
        ["--tracker", "old"],
        ["--odom-truth"],
        ["--deadband-l", "60"],
    ],
)
def test_cli_rejects_removed_options(argv):
    with pytest.raises(SystemExit):
        sim_device.build_parser().parse_args(argv)
