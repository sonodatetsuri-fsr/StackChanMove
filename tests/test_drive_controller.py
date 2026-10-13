"""Tests for DriveController (command hold repeater / drive source arbitration)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from openmovehub.config import Settings
from openmovehub.services.drive_controller import DriveController


class FakeClock:
    """Monotonic clock stub so hold expiry can be tested without sleeping."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def make_manager():
    manager = MagicMock()
    manager.send_json_to_devices = AsyncMock()
    manager.send_json_to_apps = AsyncMock()
    manager.send_to_devices = AsyncMock()
    manager.send_to_apps = AsyncMock()
    return manager


def make_drive(manager=None, clock=None, **setting_overrides):
    manager = manager or make_manager()
    clock = clock or FakeClock()
    return DriveController(manager, Settings(**setting_overrides), clock=clock), manager, clock


def device_msgs(manager):
    return [c.args[0] for c in manager.send_json_to_devices.await_args_list]


def app_msgs(manager):
    return [c.args[0] for c in manager.send_json_to_apps.await_args_list]


# --- set_target -------------------------------------------------------------


def test_initial_state_from_settings():
    drive, _, _ = make_drive(drive_source_default="joystick")
    assert drive.source == "joystick"
    assert drive.target == (0.0, 0.0)
    assert drive.seq == 0
    assert drive.last_odom is None


def test_set_target_clamps_to_max():
    drive, _, _ = make_drive(max_v=0.15, max_w=2.0)
    assert drive.set_target(10.0, 10.0) == (0.15, 2.0)
    assert drive.target == (0.15, 2.0)


def test_set_target_clamps_negative():
    drive, _, _ = make_drive(max_v=0.15, max_w=2.0)
    assert drive.set_target(-10.0, -10.0) == (-0.15, -2.0)


def test_set_target_within_limits_is_unchanged():
    drive, _, _ = make_drive(max_v=0.15, max_w=2.0)
    assert drive.set_target(0.05, -1.0) == (0.05, -1.0)


def test_set_target_updates_timestamp():
    drive, _, clock = make_drive()
    clock.advance(5.0)
    drive.set_target(0.1, 0.0)
    assert drive.target_ts == 5.0


# --- tick -------------------------------------------------------------------


async def test_tick_sends_drive_to_devices():
    drive, manager, _ = make_drive()
    drive.set_target(0.1, 0.2)
    await drive.tick()

    sent = device_msgs(manager)
    assert len(sent) == 1
    assert sent[0].type == "drive"
    assert (sent[0].v, sent[0].w, sent[0].src) == (0.1, 0.2, "hub")


async def test_tick_increments_seq():
    drive, manager, _ = make_drive()
    drive.set_target(0.1, 0.0)
    await drive.tick()
    await drive.tick()
    await drive.tick()

    assert drive.seq == 3
    assert [m.seq for m in device_msgs(manager)] == [1, 2, 3]


async def test_tick_holds_target_before_expiry():
    drive, manager, clock = make_drive(drive_hold_s=1.0)
    drive.set_target(0.1, 0.2)
    clock.advance(0.9)
    await drive.tick()

    assert drive.target == (0.1, 0.2)
    assert (device_msgs(manager)[0].v, device_msgs(manager)[0].w) == (0.1, 0.2)


async def test_tick_hold_expiry_resets_target_to_zero():
    drive, manager, clock = make_drive(drive_hold_s=1.0)
    drive.set_target(0.1, 0.2)
    clock.advance(1.0)
    await drive.tick()

    assert drive.target == (0.0, 0.0)
    last = device_msgs(manager)[-1]
    assert (last.v, last.w) == (0.0, 0.0)


async def test_tick_keeps_sending_zero_after_expiry():
    drive, manager, clock = make_drive(drive_hold_s=1.0)
    drive.set_target(0.1, 0.2)
    clock.advance(2.0)
    await drive.tick()
    await drive.tick()

    assert [(m.v, m.w) for m in device_msgs(manager)] == [(0.0, 0.0), (0.0, 0.0)]


async def test_tick_sends_nothing_while_joystick():
    drive, manager, _ = make_drive(drive_source_default="joystick")
    drive.set_target(0.1, 0.2)
    await drive.tick()

    manager.send_json_to_devices.assert_not_awaited()
    assert drive.seq == 0


# --- set_source -------------------------------------------------------------


async def test_set_source_broadcasts_to_devices_and_apps():
    drive, manager, _ = make_drive()
    assert await drive.set_source("joystick") == "joystick"

    device = device_msgs(manager)[0]
    assert device.type == "drive_source"
    assert device.source == "joystick"
    app = app_msgs(manager)[0]
    assert app.type == "state"
    assert app.value == "drive_source:joystick"


async def test_set_source_same_value_does_not_broadcast():
    drive, manager, _ = make_drive()
    assert await drive.set_source("hub") == "hub"

    manager.send_json_to_devices.assert_not_awaited()
    manager.send_json_to_apps.assert_not_awaited()


async def test_set_source_auto_is_refused():
    """``auto`` は拒否し、app に現在値の state を返す (device には送らない)。"""
    drive, manager, _ = make_drive()
    assert await drive.set_source("auto") == "hub"

    assert drive.source == "hub"
    manager.send_json_to_devices.assert_not_awaited()
    assert [m.value for m in app_msgs(manager)] == ["drive_source:hub"]


async def test_set_source_auto_refused_while_joystick_reports_joystick():
    drive, manager, _ = make_drive(drive_source_default="joystick")
    assert await drive.set_source("auto") == "joystick"

    assert drive.source == "joystick"
    manager.send_json_to_devices.assert_not_awaited()
    assert [m.value for m in app_msgs(manager)] == ["drive_source:joystick"]


@pytest.mark.parametrize("source", ["manual", "", "None"])
async def test_set_source_unknown_value_is_refused(source):
    drive, manager, _ = make_drive()
    assert await drive.set_source(source) == "hub"
    manager.send_json_to_devices.assert_not_awaited()
    assert [m.value for m in app_msgs(manager)] == ["drive_source:hub"]


async def test_leaving_joystick_resets_target():
    drive, _, clock = make_drive(drive_source_default="joystick")
    drive.set_target(0.1, 0.2)
    clock.advance(3.0)
    await drive.set_source("hub")

    assert drive.target == (0.0, 0.0)
    assert drive.target_ts == 3.0


# --- app commands -----------------------------------------------------------


async def test_apply_app_command_sets_target():
    drive, _, _ = make_drive()
    assert await drive.apply_app_command(0.1, -0.5) is True
    assert drive.target == (0.1, -0.5)


async def test_apply_app_command_clamps():
    drive, _, _ = make_drive(max_v=0.15, max_w=2.0)
    await drive.apply_app_command(1.0, -9.0)
    assert drive.target == (0.15, -2.0)


async def test_apply_app_command_while_joystick_is_ignored():
    """joystick 中の app 指令は無視する (REST ``POST /drive`` は 409)。"""
    drive, manager, clock = make_drive(drive_source_default="joystick")
    clock.advance(2.0)
    assert await drive.apply_app_command(0.1, 0.0) is False

    assert drive.source == "joystick"
    assert drive.target == (0.0, 0.0)
    assert drive.target_ts == 0.0
    manager.send_json_to_devices.assert_not_awaited()
    manager.send_json_to_apps.assert_not_awaited()


# --- estop ------------------------------------------------------------------


async def test_handle_estop_stops_and_returns_to_hub():
    drive, manager, _ = make_drive(drive_source_default="joystick")
    drive.set_target(0.1, 0.2)

    await drive.handle_estop()

    assert drive.target == (0.0, 0.0)
    assert drive.source == "hub"
    assert device_msgs(manager)[0].source == "hub"


async def test_handle_estop_while_already_hub():
    drive, _, _ = make_drive()
    drive.set_target(0.1, 0.2)

    await drive.handle_estop()

    assert drive.target == (0.0, 0.0)
    assert drive.source == "hub"


# --- snapshot / lifecycle ---------------------------------------------------


def test_snapshot():
    drive, _, _ = make_drive()
    drive.set_target(0.1, -0.2)
    drive.seq = 42
    assert drive.snapshot() == {
        "drive_source": "hub",
        "target": {"v": 0.1, "w": -0.2},
        "seq": 42,
    }


@pytest.mark.asyncio
async def test_start_repeats_and_stop_cancels():
    import asyncio

    manager = make_manager()
    drive = DriveController(manager, Settings(drive_rate_hz=200.0, drive_hold_s=10.0))
    drive.set_target(0.1, 0.0)
    drive.start()
    await asyncio.sleep(0.05)
    await drive.stop()

    assert drive.seq >= 2
    seq_after_stop = drive.seq
    await asyncio.sleep(0.03)
    assert drive.seq == seq_after_stop
