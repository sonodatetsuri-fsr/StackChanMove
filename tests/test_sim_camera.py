"""Camera frame source and streaming tests for the device simulator."""

import asyncio
import contextlib
import io
import struct
from unittest.mock import AsyncMock

import pytest
import sim_device
from sim_device import (
    DiffDriveSim,
    DirFrameSource,
    SimDevice,
    SyntheticFrameSource,
    build_camera_frame,
    derive_media_url,
)

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402, I001


# --- SyntheticFrameSource ---------------------------------------------------


def test_synthetic_frame_is_valid_jpeg_of_expected_size():
    sim = DiffDriveSim()
    source = SyntheticFrameSource(sim)
    jpeg = source.next_frame(60)

    assert isinstance(jpeg, bytes)
    assert jpeg.startswith(b"\xff\xd8")
    assert jpeg.endswith(b"\xff\xd9")

    img = Image.open(io.BytesIO(jpeg))
    assert img.size == (320, 240)


def test_synthetic_consecutive_frames_differ():
    sim = DiffDriveSim()
    source = SyntheticFrameSource(sim)
    frame1 = source.next_frame(60)
    frame2 = source.next_frame(60)
    assert frame1 != frame2


def test_synthetic_frame_reflects_pose_change():
    sim = DiffDriveSim()
    source = SyntheticFrameSource(sim)
    origin_frame = source.next_frame(60)

    sim.x = 0.5
    moved_frame = source.next_frame(60)

    assert origin_frame != moved_frame


# --- DirFrameSource ----------------------------------------------------------


def _write_jpeg(path, color=(255, 0, 0)) -> None:
    img = Image.new("RGB", (16, 16), color=color)
    img.save(path, format="JPEG")


def _write_png(path, color=(0, 255, 0)) -> None:
    img = Image.new("RGB", (16, 16), color=color)
    img.save(path, format="PNG")


def test_dir_frame_source_cycles_sorted_and_encodes_jpeg(tmp_path):
    jpeg_path = tmp_path / "a_frame.jpg"
    png_path = tmp_path / "b_frame.png"
    _write_jpeg(jpeg_path)
    _write_png(png_path)

    source = DirFrameSource(str(tmp_path))
    assert source.count == 2

    frame1 = source.next_frame(60)
    frame2 = source.next_frame(60)
    frame3 = source.next_frame(60)

    for frame in (frame1, frame2, frame3):
        assert frame.startswith(b"\xff\xd8")
        assert frame.endswith(b"\xff\xd9")

    # cycles back to the first file on the third call
    assert frame1 == frame3

    decoded_png_frame = Image.open(io.BytesIO(frame2))
    assert decoded_png_frame.format == "JPEG"


def test_dir_frame_source_caches_png_reencode(tmp_path):
    png_path = tmp_path / "only.png"
    _write_png(png_path)

    source = DirFrameSource(str(tmp_path))
    frame1 = source.next_frame(60)
    frame2 = source.next_frame(60)
    assert frame1 == frame2
    assert (str(png_path), 60) in source._png_cache


def test_dir_frame_source_empty_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        DirFrameSource(str(tmp_path))


# --- build_camera_frame -------------------------------------------------------


def test_build_camera_frame_header_and_payload():
    jpeg = b"\xff\xd8fake\xff\xd9"
    frame = build_camera_frame(jpeg, ts_ms=1234)

    assert frame[0] == sim_device.FRAME_KIND_JPEG
    assert frame[0] == 0x02
    kind, ts_ms = struct.unpack_from("<BI", frame, 0)
    assert kind == 0x02
    assert ts_ms == 1234
    assert frame[5:] == jpeg


def test_build_camera_frame_ts_wraps_at_32_bits():
    frame = build_camera_frame(b"x", ts_ms=(2**32) + 42)
    _, ts_ms = struct.unpack_from("<BI", frame, 0)
    assert ts_ms == 42


# --- SimDevice.handle_text: camera -------------------------------------------


def _device() -> SimDevice:
    sim = DiffDriveSim()
    return SimDevice(sim, "ws://example.invalid/ws/device", rate=10.0)


def test_handle_text_camera_applies_values():
    device = _device()
    device.handle_text('{"type":"camera","on":false,"fps":3,"quality":40}')
    assert device.cam_on is False
    assert device.cam_fps == 3
    assert device.cam_quality == 40


def test_handle_text_camera_clamps_fps_and_quality():
    device = _device()
    device.handle_text('{"type":"camera","fps":0,"quality":5}')
    assert device.cam_fps == 1
    assert device.cam_quality == 10

    device.handle_text('{"type":"camera","fps":50,"quality":200}')
    assert device.cam_fps == 10
    assert device.cam_quality == 90


def test_handle_text_camera_missing_keys_preserved():
    device = _device()
    device.cam_on = False
    device.cam_fps = 7
    device.cam_quality = 33
    device.handle_text('{"type":"camera"}')
    assert device.cam_on is False
    assert device.cam_fps == 7
    assert device.cam_quality == 33


# --- camera_loop ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_camera_loop_sends_frames():
    sim = DiffDriveSim()
    device = SimDevice(sim, "ws://example.invalid/ws/device", rate=10.0, frame_source=SyntheticFrameSource(sim))
    device.cam_fps = 10
    ws = AsyncMock()

    task = asyncio.create_task(device.camera_loop(ws))
    await asyncio.sleep(0.35)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert ws.send.await_count >= 2
    for call in ws.send.await_args_list:
        payload = call.args[0]
        assert payload[0:1] == b"\x02"
        assert payload[5:7] == b"\xff\xd8"


@pytest.mark.asyncio
async def test_camera_loop_does_not_send_when_off():
    sim = DiffDriveSim()
    device = SimDevice(sim, "ws://example.invalid/ws/device", rate=10.0, frame_source=SyntheticFrameSource(sim))
    device.cam_fps = 10
    device.cam_on = False
    ws = AsyncMock()

    task = asyncio.create_task(device.camera_loop(ws))
    await asyncio.sleep(0.35)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    ws.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_camera_loop_returns_immediately_without_frame_source():
    sim = DiffDriveSim()
    device = SimDevice(sim, "ws://example.invalid/ws/device", rate=10.0, frame_source=None)
    ws = AsyncMock()

    await asyncio.wait_for(device.camera_loop(ws), timeout=1.0)
    ws.send.assert_not_awaited()


# --- DirFrameSource cycling --------------------------------------------------


def test_dir_source_cycles_every_call(tmp_path):
    """Each call advances to the next file (no hold)."""
    for i, color in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)]):
        _write_jpeg(tmp_path / f"{i}.jpg", color=color)
    source = DirFrameSource(str(tmp_path))
    frames = [source.next_frame(60) for _ in range(3)]
    assert len(set(frames)) == 3  # each call advanced, none held


# --- 最大 fps -------------------------------------------------


def test_fps_10_is_accepted_end_to_end():
    """``--fps 10`` (CAM_FPS_MAX) がそのまま camera_loop の周期になる。"""
    args = sim_device.build_parser().parse_args(["--fps", "10"])
    assert args.fps == 10.0

    device = SimDevice(DiffDriveSim(), "ws://example.invalid/ws/device", rate=10.0, fps=args.fps)
    assert device.cam_fps == 10
    assert 1.0 / device.cam_fps == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_camera_loop_at_10_fps_sends_about_ten_frames_per_second():
    sim = DiffDriveSim()
    device = SimDevice(sim, "ws://example.invalid/ws/device", rate=10.0, frame_source=SyntheticFrameSource(sim), fps=10)
    ws = AsyncMock()
    task = asyncio.create_task(device.camera_loop(ws))
    await asyncio.sleep(0.55)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert 3 <= ws.send.await_count <= 8


# --- media URL derivation and CLI -------------------


def test_derive_media_url_replaces_trailing_ws_device():
    assert derive_media_url("ws://127.0.0.1:8341/ws/device") == "ws://127.0.0.1:8341/ws/media"


def test_derive_media_url_preserves_scheme_and_host():
    assert derive_media_url("wss://hub.example.com:9000/ws/device") == "wss://hub.example.com:9000/ws/media"


def test_derive_media_url_none_when_no_trailing_ws_device():
    assert derive_media_url("ws://127.0.0.1:8341/ws/other") is None
    assert derive_media_url("ws://127.0.0.1:8341/ws/device/") is None
    assert derive_media_url("ws://127.0.0.1:8341") is None


def test_derive_media_url_matches_default_hub():
    assert sim_device.DEFAULT_HUB == "ws://127.0.0.1:8341/ws/device"
    assert derive_media_url(sim_device.DEFAULT_HUB) == "ws://127.0.0.1:8341/ws/media"


def test_build_parser_media_defaults():
    args = sim_device.build_parser().parse_args([])
    assert args.single_ws is False
    assert args.media_url is None
    assert args.hub == sim_device.DEFAULT_HUB


def test_build_parser_single_ws_and_media_url():
    args = sim_device.build_parser().parse_args(["--single-ws", "--media-url", "ws://example.invalid/ws/media"])
    assert args.single_ws is True
    assert args.media_url == "ws://example.invalid/ws/media"
