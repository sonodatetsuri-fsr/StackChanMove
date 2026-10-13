#!/usr/bin/env python3
"""Open StackChan Move device simulator.

Connects to the Hub with the ``device`` role, integrates the received ``drive``
commands with a differential-drive kinematic model and reports ``odom`` (the true
pose of the model) at the rate the Hub asks for with ``odom_rate``.  It also
streams ``kind=0x02`` JPEG frames: either a synthetic image reflecting the
current pose (grid + pose text + a marker that moves with the pose), or a cycled
directory of images.  No hardware needed.

Audio: ``--mic-wav FILE`` streams a WAV (any rate / channels, converted
to PCM16 / 16 kHz / mono) as ``kind=0x01`` frames of 32 ms (1,024 B) in real time,
only while the Hub says ``listen{on:true}`` (paused on ``on:false`` and resumed from
the same position).  At the end of the file 1.5 s of silence is sent and the mic
stops; ``--mic-loop`` repeats it with 3 s of silence between passes.
``--speech-out DIR`` saves the ``kind=0x01`` PCM received between ``speak_start``
and ``speak_end`` as ``DIR/speech_NNN.wav``.  ``heard`` is logged as
``[Sim] heard: <text>``.

By default the camera frames go out on a *second* WebSocket connection
(``role=media``, URL derived from ``--hub`` or given via ``--media-url``) so a
slow/lost camera link cannot block the control connection (``drive`` / ``odom`` /
``joy`` / ``event``).  The two connections reconnect independently.  Pass
``--single-ws`` to send camera frames on the control connection instead, which is
useful for testing the Hub's backward-compatible ``device``-role binary handling.

``--joy`` reads ``x y btn`` lines from stdin as the ESP-NOW remote (M5StickC Plus +
MiniJoyC): the stick is applied only while ``drive_source == joystick``; a button
press (``btn`` 1) is an emergency stop that forces ``hub`` and sends
``event estop``; a long press (``btn`` 2) toggles the source only while the Hub is
disconnected (standalone mode), as on the firmware.

Derived from stackchan_move ``tools/sim_device.py`` (commit a6e8234) without the
wheel tracker / dead-zone model, raw PWM calibration and arena
 parts: the wheel readings here use the plain wrapping model and the
reported ``x / y / th`` are the true pose.

The kinematics live in :class:`DiffDriveSim`, which is pure (no I/O, no global
state) so that ``tests/test_sim_kinematics.py`` can import and drive it directly::

    import sim_device
    sim = sim_device.DiffDriveSim()

The frame sources (:class:`SyntheticFrameSource`, :class:`DirFrameSource`) are
likewise pure/synchronous so ``tests/test_sim_camera.py`` can exercise them
without a WebSocket.  Pillow is imported lazily inside those classes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import glob
import io
import json
import logging
import math
import os
import struct
import sys
import threading
import time
import wave
from typing import Any

import websockets

logger = logging.getLogger("sim_device")

# --- contract constants -------------------------------------------
DEFAULT_HUB = "ws://127.0.0.1:8341/ws/device"
DEFAULT_TRACK_MM = 77.5  # HUB_TRACK_WIDTH_MM
DEFAULT_WHEEL_MM = 37.0  # HUB_WHEEL_DIAMETER_MM
DRIVE_TIMEOUT_S = 0.5  # DRIVE_TIMEOUT_MS (firmware watchdog)
STEPS_PER_REV = 1024  # WHEEL_POS_STEPS: SCS0009 ReadPos range 0..1023
POS_VALID_DEG = 300.0  # WHEEL_POS_VALID_DEG: mechanical angle those 1024 steps span
V_MAX = 0.15  # HUB_MAX_V (m/s), used for the mock joystick mapping
W_MAX = 2.0  # HUB_MAX_W (rad/s), used for the mock joystick mapping
RECONNECT_DELAY_S = 2.0

# --- joystick contract --------------------
JOY_LONG_PRESS_S = 1.5  # JOY_LONG_PRESS_MS
JOY_SEND_MIN_DELTA = 0.02  # JOY_SEND_MIN_DELTA
JOY_SEND_HEARTBEAT_S = 1.0  # JOY_SEND_HEARTBEAT_MS
JOY_SEND_MIN_PERIOD_S = 0.1  # max 10 Hz

# --- camera contract --------------------------------------------------------
FRAME_KIND_JPEG = 0x02
CAM_FPS_MIN = 1
CAM_FPS_MAX = 10
CAM_QUALITY_MIN = 10
CAM_QUALITY_MAX = 90
CAM_PX_PER_M = 100  # marker placement scale around the image centre

# --- audio contract -----------------------------------------------
FRAME_KIND_PCM = 0x01
MIC_RATE = 16000  # PCM16 / 16 kHz / mono
MIC_CHUNK_BYTES = 1024  # 512 samples = 32 ms
MIC_CHUNK_S = MIC_CHUNK_BYTES / 2 / MIC_RATE
MIC_TAIL_SILENCE_S = 1.5  # after the last pass (VAD end-of-speech)
MIC_LOOP_GAP_S = 3.0  # between passes with --mic-loop

# --- odom rate contract ------------------------------------------
ODOM_HZ_MIN = 5
ODOM_HZ_MAX = 50

DRIVE_SOURCES = ("hub", "joystick")
KNOWN_TYPES = (
    "drive",
    "drive_source",
    "odom_reset",
    "listen",
    "anim",
    "face",
    "speak_start",
    "speak_end",
    "camera",
    "odom_rate",
    "heard",
)


def monotonic_ms() -> int:
    """Sender-local monotonic timestamp in ms, truncated to uint32 (SPEC §2)."""
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


def wrap_angle(th: float) -> float:
    """Wrap an angle to [-pi, pi)."""
    return (th + math.pi) % (2.0 * math.pi) - math.pi


CTRL_SUFFIX = "/ws/device"
MEDIA_SUFFIX = "/ws/media"


def derive_media_url(hub_url: str) -> str | None:
    """Derive the ``/ws/media`` URL from a ``/ws/device`` Hub URL.

    Replaces a trailing ``/ws/device`` with ``/ws/media``.  Returns ``None`` when
    ``hub_url`` does not end with ``/ws/device`` (caller should then require an
    explicit ``--media-url`` or run with ``--single-ws``).  Pure function (no I/O).
    """
    if hub_url.endswith(CTRL_SUFFIX):
        return hub_url[: -len(CTRL_SUFFIX)] + MEDIA_SUFFIX
    return None


class DiffDriveSim:
    """Differential-drive kinematics with a drive watchdog.

    Units are the protocol units: m, m/s, rad, rad/s.  ``track_mm`` / ``wheel_mm``
    are the real StackChan Move geometry.
    """

    def __init__(
        self,
        track_mm: float = DEFAULT_TRACK_MM,
        wheel_mm: float = DEFAULT_WHEEL_MM,
        clock=time.monotonic,
    ) -> None:
        self.track_mm = float(track_mm)
        self.wheel_mm = float(wheel_mm)
        self.track_m = track_mm / 1000.0
        self.wheel_m = wheel_mm / 1000.0
        self.circumference_m = math.pi * self.wheel_m
        self._clock = clock

        # pose
        self.x = 0.0
        self.y = 0.0
        self.th = 0.0
        # last step wheel travel (m)
        self.dl = 0.0
        self.dr = 0.0
        # accumulated wheel travel (m) -> SCS0009 step units
        self._l_travel = 0.0
        self._r_travel = 0.0

        # currently applied command
        self.cmd_v = 0.0
        self.cmd_w = 0.0
        self.cmd_src = "none"

        # drive source arbitration
        self.source = "hub"
        self._req: tuple[float, float, str] = (0.0, 0.0, "none")
        self._last_cmd_time: float | None = None
        self._timeout_flagged = True  # no command yet -> no spurious drive_timeout

        # joystick input: last (x, y, btn-level 0/1/2) sample
        self._joy: tuple[float, float, int] = (0.0, 0.0, 0)
        self._joy_btn_prev = 0  # for press-edge detection
        self.hub_connected = False  # set by SimDevice on session open/close

        # last `joy` message actually sent (x, y, pressed) and when, for
        # joy_message_if_due()'s delta/heartbeat throttling
        self._joy_sent: tuple[float, float, bool] | None = None
        self._joy_sent_time: float | None = None

        self._events: list[tuple[str, str]] = []

    # --- wheel positions -----------------------------------------------------
    def _reading(self, signed_travel_m: float) -> int:
        """Raw ``ReadPos`` reading: 1024 counts span 300 deg and simply wrap (no dead zone)."""
        counts = signed_travel_m / self.circumference_m * (360.0 / POS_VALID_DEG) * STEPS_PER_REV
        return int(round(counts)) % STEPS_PER_REV

    @property
    def l_pos(self) -> int:
        # Mirror-mounted wheels: the left reading counts *down* when driving forward
        # (config.h WHEEL_LEFT_SIGN * WHEEL_POS_SIGN = -1), as on the real robot.
        return self._reading(-self._l_travel)

    @property
    def r_pos(self) -> int:
        return self._reading(self._r_travel)

    # --- inputs ------------------------------------------------------------
    def apply_drive(self, v: float, w: float, src: str = "hub", now: float | None = None) -> bool:
        """Accept a drive command.  Returns False when it is ignored.

        ``drive`` from the Hub (``src`` in hub/auto) is ignored while the drive
        source is ``joystick``.
        """
        if src in ("hub", "auto") and self.source == "joystick":
            return False
        self._req = (float(v), float(w), src)
        self._last_cmd_time = self._clock() if now is None else now
        self._timeout_flagged = False
        return True

    def set_joystick(self, x: float, y: float, btn: int | bool = 0) -> None:
        """Feed a mock joystick sample.

        ``x``/``y`` are in -1..1 and are applied to the kinematics only while
        ``source == "joystick"`` (see :meth:`_effective_cmd`).

        ``btn``: ``0`` released, ``1`` short press, ``2`` long press.  A
        press *edge* (not-pressed -> pressed, ``btn`` 1 or 2) is an emergency
        stop regardless of ``source``: the applied command is dropped to
        standstill, ``source`` is forced to ``"hub"``, and an
        ``("estop", "joystick_button")`` event is queued.  Holding the button
        (repeated samples at the same level) does not re-fire.

        A long-press edge (``btn == 2``) additionally toggles ``source``
        between ``"joystick"`` and ``"hub"``, but only while
        ``self.hub_connected`` is false; while the Hub is connected the long
        press is a no-op beyond the estop (logged at debug).  The toggle is
        based on the source *before* the estop forced it to ``"hub"``, so a
        ``0 -> 2`` transition while running standalone on ``joystick`` ends
        on ``"hub"`` (the estop "wins"), and a following ``2 -> 0 -> 2``
        toggles back to ``"joystick"``.
        """
        x, y, btn = float(x), float(y), int(btn)
        pressed = btn >= 1
        was_pressed = self._joy_btn_prev >= 1
        if pressed and not was_pressed:
            source_before = self.source
            self._apply_estop()
            if btn == 2:
                if self.hub_connected:
                    logger.debug("[Joy] long press ignored (Hub connected)")
                else:
                    new_source = "hub" if source_before == "joystick" else "joystick"
                    self.source = new_source
                    self._req = (0.0, 0.0, "none")
                    self._last_cmd_time = None
                    self._timeout_flagged = True
                    if new_source == "joystick":
                        logger.info("[Joy] source -> joystick (standalone)")
                    else:
                        logger.info("[Joy] source -> hub")
        self._joy = (x, y, btn)
        self._joy_btn_prev = btn

    def _apply_estop(self) -> None:
        """Emergency stop: drop to standstill, force source=hub."""
        self.source = "hub"
        self._req = (0.0, 0.0, "none")
        self._last_cmd_time = None
        self._timeout_flagged = True
        self._events.append(("estop", "joystick_button"))

    def set_source(self, source: str) -> None:
        """Apply a ``drive_source`` snapshot from the Hub."""
        if source not in DRIVE_SOURCES or source == self.source:
            return
        self.source = source
        # start from a standstill on every transition (same rule as the Hub)
        self._req = (0.0, 0.0, "none")
        self._last_cmd_time = None
        self._timeout_flagged = True

    def reset(self) -> None:
        """``odom_reset``: zero the pose (the wheel travel, i.e. the raw readings, is kept)."""
        self.x = 0.0
        self.y = 0.0
        self.th = 0.0
        self.dl = 0.0
        self.dr = 0.0

    def pop_events(self) -> list[tuple[str, str]]:
        """Return and clear the pending ``(name, detail)`` device events.

        E.g. ``("drive_timeout", "no drive within DRIVE_TIMEOUT_MS")`` or
        ``("estop", "joystick_button")``.
        """
        events, self._events = self._events, []
        return events

    # --- integration -------------------------------------------------------
    def _effective_cmd(self, now: float) -> tuple[float, float, str]:
        """The applied ``(v, w, src)`` at ``now`` (joystick, Hub drive or the watchdog stop)."""
        if self.source == "joystick":
            jx, jy, _btn = self._joy
            return (jy * V_MAX, jx * W_MAX, "joystick")
        if self._last_cmd_time is None or now - self._last_cmd_time > DRIVE_TIMEOUT_S:
            if not self._timeout_flagged:
                self._timeout_flagged = True
                self._events.append(("drive_timeout", "no drive within DRIVE_TIMEOUT_MS"))
            return (0.0, 0.0, "none")
        return self._req

    # --- joy message throttling ------------------------------
    def joy_message_if_due(self, now: float) -> dict[str, Any] | None:
        """Return a ``joy`` message body if one is due to be sent, else ``None``.

        Only while ``source == "joystick"``.  Sent when the stick (x or y)
        moved by more than ``JOY_SEND_MIN_DELTA`` since the last sent sample,
        the button state changed, or ``JOY_SEND_HEARTBEAT_S`` elapsed since
        the last send; capped at ``1 / JOY_SEND_MIN_PERIOD_S`` (10 Hz).  Pure
        (no I/O) so it is unit-testable without a websocket; the caller
        (:meth:`SimDevice.send_loop`) is responsible for actually sending it.
        """
        if self.source != "joystick":
            return None
        x, y, btn = self._joy
        pressed = btn >= 1
        if self._joy_sent_time is not None and now - self._joy_sent_time < JOY_SEND_MIN_PERIOD_S:
            return None
        due = self._joy_sent is None
        if not due:
            lx, ly, lpressed = self._joy_sent
            if abs(x - lx) > JOY_SEND_MIN_DELTA or abs(y - ly) > JOY_SEND_MIN_DELTA or pressed != lpressed:
                due = True
        if not due and (self._joy_sent_time is None or now - self._joy_sent_time >= JOY_SEND_HEARTBEAT_S):
            due = True
        if not due:
            return None
        self._joy_sent = (x, y, pressed)
        self._joy_sent_time = now
        return {"type": "joy", "x": x, "y": y, "btn": pressed}

    def step(self, dt: float, now: float | None = None) -> dict[str, Any]:
        """Advance the model by ``dt`` seconds and return the odom body."""
        if now is None:
            now = self._clock()

        v, w, src = self._effective_cmd(now)
        self.cmd_v, self.cmd_w, self.cmd_src = v, w, src

        self.x += v * math.cos(self.th) * dt
        self.y += v * math.sin(self.th) * dt
        self.th = wrap_angle(self.th + w * dt)

        half_track = self.track_m / 2.0
        self.dl = (v - w * half_track) * dt
        self.dr = (v + w * half_track) * dt
        self._l_travel += self.dl
        self._r_travel += self.dr
        return self.odom_body()

    # --- reporting ---------------------------------------------------------
    def odom_body(self) -> dict[str, Any]:
        """The ``odom`` body (same fields as stackchan_move)."""
        return {
            "x": self.x,
            "y": self.y,
            "th": self.th,
            "v": self.cmd_v,
            "w": self.cmd_w,
            "dl": self.dl,
            "dr": self.dr,
            "cmd": {"v": self.cmd_v, "w": self.cmd_w, "src": self.cmd_src},
            "wheel": {"l_pos": self.l_pos, "r_pos": self.r_pos, "l_valid": True, "r_valid": True},
        }

    def odom_message(self, ts_ms: int, seq: int) -> dict[str, Any]:
        """Build an ``odom`` message matching the OdomMessage contract."""
        return {"type": "odom", "ts": int(ts_ms), "seq": int(seq), **self.odom_body()}


def build_camera_frame(jpeg: bytes, ts_ms: int) -> bytes:
    """Build a ``kind=0x02`` binary frame."""
    return struct.pack("<BI", FRAME_KIND_JPEG, ts_ms & 0xFFFFFFFF) + jpeg


class SyntheticFrameSource:
    """Renders a synthetic JPEG reflecting :class:`DiffDriveSim`'s pose.

    Pure/synchronous: no I/O beyond in-memory JPEG encoding, so it can be
    unit-tested and called directly from the camera send loop.
    """

    def __init__(self, sim: DiffDriveSim, size: tuple[int, int] = (320, 240)) -> None:
        self.sim = sim
        self.size = size
        self._frame_count = 0

    def next_frame(self, quality: int) -> bytes:
        from PIL import Image, ImageDraw, ImageFont

        w, h = self.size
        img = Image.new("RGB", (w, h), color=(20, 20, 24))
        draw = ImageDraw.Draw(img)

        grid_color = (60, 60, 68)
        for gx in range(0, w, 40):
            draw.line([(gx, 0), (gx, h)], fill=grid_color)
        for gy in range(0, h, 40):
            draw.line([(0, gy), (w, gy)], fill=grid_color)

        sim = self.sim
        font = ImageFont.load_default()
        draw.text(
            (4, 4),
            f"x={sim.x:.3f} y={sim.y:.3f} th={math.degrees(sim.th):.1f}deg",
            fill=(230, 230, 230),
            font=font,
        )
        draw.text(
            (4, 16),
            f"cmd v={sim.cmd_v:.2f} w={sim.cmd_w:.2f} {sim.cmd_src}",
            fill=(230, 230, 230),
            font=font,
        )
        self._frame_count += 1
        draw.text((4, h - 14), f"#{self._frame_count}", fill=(150, 150, 150), font=font)

        cx, cy = w / 2.0, h / 2.0
        px = (cx + sim.x * CAM_PX_PER_M) % w
        py = (cy - sim.y * CAM_PX_PER_M) % h
        radius = 8
        draw.ellipse(
            [(px - radius, py - radius), (px + radius, py + radius)],
            fill=(220, 80, 80),
        )
        heading_len = radius * 2.5
        hx = px + heading_len * math.cos(sim.th)
        hy = py - heading_len * math.sin(sim.th)
        draw.line([(px, py), (hx, hy)], fill=(220, 220, 80), width=2)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()


class DirFrameSource:
    """Cycles JPEG/PNG files from a directory."""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        patterns = ("*.jpg", "*.jpeg", "*.png")
        paths: list[str] = []
        for pattern in patterns:
            paths.extend(glob.glob(os.path.join(directory, pattern)))
            paths.extend(glob.glob(os.path.join(directory, pattern.upper())))
        self.paths = sorted(set(paths))
        if not self.paths:
            raise FileNotFoundError(f"no *.jpg/*.jpeg/*.png images found in {directory!r}")
        self._index = 0
        self._png_cache: dict[tuple[str, int], bytes] = {}

    @property
    def count(self) -> int:
        return len(self.paths)

    def next_frame(self, quality: int) -> bytes:
        path = self.paths[self._index]
        self._index = (self._index + 1) % len(self.paths)
        if path.lower().endswith((".jpg", ".jpeg")):
            with open(path, "rb") as f:
                return f.read()
        cache_key = (path, quality)
        cached = self._png_cache.get(cache_key)
        if cached is not None:
            return cached
        from PIL import Image

        with Image.open(path) as img:
            rgb = img.convert("RGB")
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=quality)
            jpeg = buf.getvalue()
        self._png_cache[cache_key] = jpeg
        return jpeg


def build_pcm_frame(pcm: bytes, ts_ms: int) -> bytes:
    """Build a ``kind=0x01`` binary frame."""
    return struct.pack("<BI", FRAME_KIND_PCM, ts_ms & 0xFFFFFFFF) + pcm


def convert_pcm(frames: bytes, sampwidth: int, channels: int, rate: int, target_rate: int = MIC_RATE) -> bytes:
    """Raw WAV frames -> PCM16 little-endian mono at ``target_rate`` (linear interpolation)."""
    import numpy as np

    if sampwidth == 1:
        samples = (np.frombuffer(frames, dtype=np.uint8).astype(np.float64) - 128.0) * 256.0
    elif sampwidth == 2:
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float64)
    elif sampwidth == 3:
        raw = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3)
        as_int = raw[:, 0].astype(np.int32) | (raw[:, 1].astype(np.int32) << 8) | (raw[:, 2].astype(np.int32) << 16)
        as_int = np.where(as_int & 0x800000, as_int - 0x1000000, as_int)
        samples = as_int.astype(np.float64) / 256.0
    elif sampwidth == 4:
        samples = np.frombuffer(frames, dtype=np.int32).astype(np.float64) / 65536.0
    else:
        raise ValueError(f"unsupported WAV sample width: {sampwidth}")
    if channels > 1:
        samples = samples[: len(samples) - len(samples) % channels].reshape(-1, channels).mean(axis=1)
    if rate != target_rate and len(samples) > 0:
        target_length = int(len(samples) * target_rate / rate)
        indices = np.linspace(0, len(samples) - 1, target_length)
        samples = np.interp(indices, np.arange(len(samples)), samples)
    return np.clip(np.round(samples), -32768, 32767).astype(np.int16).tobytes()


def load_wav_pcm16_mono(path: str, target_rate: int = MIC_RATE) -> bytes:
    """Read a WAV file (any rate / channels / 8-32 bit PCM) as PCM16 mono at ``target_rate``."""
    with wave.open(path, "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        return convert_pcm(frames, wf.getsampwidth(), wf.getnchannels(), wf.getframerate(), target_rate)


class MicStreamer:
    """Mock microphone: pure state, no I/O.

    :meth:`next_chunk` returns the next 1,024 B (32 ms) PCM chunk while
    ``listening`` is true, ``None`` while paused (the position is kept) or after
    the last pass.  One pass is the file followed by 1.5 s of silence, or by 3 s
    with ``loop`` (and then the file starts again).
    """

    def __init__(self, pcm: bytes, loop: bool = False) -> None:
        tail_s = MIC_LOOP_GAP_S if loop else MIC_TAIL_SILENCE_S
        tail = b"\x00" * (int(tail_s * MIC_RATE) * 2)
        self._buf = pcm[: len(pcm) - len(pcm) % 2] + tail
        self.loop = loop
        self.listening = False
        self.pos = 0
        self.passes = 0
        self.finished = False

    @property
    def duration_s(self) -> float:
        return len(self._buf) / 2 / MIC_RATE

    def next_chunk(self) -> bytes | None:
        if self.finished or not self.listening:
            return None
        chunk = self._buf[self.pos : self.pos + MIC_CHUNK_BYTES]
        self.pos += len(chunk)
        if self.pos >= len(self._buf):
            self.passes += 1
            if self.loop:
                self.pos = 0
            else:
                self.finished = True
        if len(chunk) < MIC_CHUNK_BYTES:
            chunk = chunk + b"\x00" * (MIC_CHUNK_BYTES - len(chunk))
        return chunk


class SpeechRecorder:
    """Saves the PCM received between ``speak_start`` and ``speak_end``."""

    def __init__(self, out_dir: str) -> None:
        self.out_dir = out_dir
        self.count = 0
        self._buf: bytearray | None = None

    @property
    def active(self) -> bool:
        return self._buf is not None

    def start(self) -> None:
        self._buf = bytearray()

    def feed(self, pcm: bytes) -> None:
        if self._buf is not None:
            self._buf.extend(pcm)

    def finish(self) -> tuple[str, float] | None:
        """Write ``speech_NNN.wav`` and return ``(path, seconds)`` (``None`` if not recording)."""
        buf, self._buf = self._buf, None
        if buf is None:
            return None
        os.makedirs(self.out_dir, exist_ok=True)
        self.count += 1
        path = os.path.join(self.out_dir, f"speech_{self.count:03d}.wav")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(MIC_RATE)
            wf.writeframes(bytes(buf))
        seconds = len(buf) / 2 / MIC_RATE
        logger.info("[Sim] speech saved: %s (%.1f s)", path, seconds)
        return path, seconds


class SimDevice:
    """WebSocket client wrapping :class:`DiffDriveSim`."""

    def __init__(
        self,
        sim: DiffDriveSim,
        hub: str,
        rate: float,
        frame_source: Any = None,
        fps: float = 5.0,
        media_url: str | None = None,
        single_ws: bool = False,
        mic: MicStreamer | None = None,
        speech: SpeechRecorder | None = None,
    ) -> None:
        self.sim = sim
        self.hub = hub
        self.period = 1.0 / rate if rate > 0 else 0.1
        self.seq = 0
        self.frame_source = frame_source
        # camera frames go on a second `role=media` connection by default
        # (`media_url`); `single_ws=True` sends them on the control connection instead.
        self.media_url = media_url
        self.single_ws = single_ws
        self.cam_on = True
        self.cam_fps = min(CAM_FPS_MAX, max(CAM_FPS_MIN, fps))
        self.cam_quality = 60
        self.cam_seq = 0
        # mock microphone / speaker
        self.mic = mic
        self.speech = speech

    # --- receive -----------------------------------------------------------
    def handle_text(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("non-JSON text frame ignored: %r", raw[:80])
            return
        if not isinstance(msg, dict):
            logger.debug("unexpected JSON payload ignored: %r", msg)
            return
        mtype = msg.get("type")
        if mtype == "drive":
            applied = self.sim.apply_drive(
                float(msg.get("v", 0.0)), float(msg.get("w", 0.0)), str(msg.get("src", "hub"))
            )
            if not applied:
                logger.debug("drive ignored (source=%s)", self.sim.source)
        elif mtype == "drive_source":
            source = str(msg.get("source", ""))
            if source in DRIVE_SOURCES:
                logger.info("drive_source: %s (was %s)", source, self.sim.source)
                self.sim.set_source(source)
            else:
                logger.warning("unknown drive_source: %r", source)
        elif mtype == "odom_reset":
            logger.info("odom_reset")
            self.sim.reset()
        elif mtype == "camera":
            self.cam_on = bool(msg.get("on", self.cam_on))
            if "fps" in msg:
                self.cam_fps = min(CAM_FPS_MAX, max(CAM_FPS_MIN, int(msg["fps"])))
            if "quality" in msg:
                self.cam_quality = min(CAM_QUALITY_MAX, max(CAM_QUALITY_MIN, int(msg["quality"])))
            logger.info("camera: on=%s fps=%s quality=%s", self.cam_on, self.cam_fps, self.cam_quality)
        elif mtype == "odom_rate":
            # the Hub sets the odom send rate in the greeting.
            try:
                hz = float(msg.get("hz"))
            except (TypeError, ValueError):
                logger.warning("odom_rate ignored (bad hz): %r", msg.get("hz"))
                return
            hz = min(ODOM_HZ_MAX, max(ODOM_HZ_MIN, hz))
            self.period = 1.0 / hz
            logger.info("odom_rate: %g Hz (period %.1f ms)", hz, self.period * 1000.0)
        elif mtype == "listen":
            on = bool(msg.get("on", False))
            if self.mic is not None and self.mic.listening != on:
                logger.info("mic %s (listen on=%s)", "resumed" if on else "paused", on)
                self.mic.listening = on
            else:
                logger.debug("listen: on=%s", on)
        elif mtype == "speak_start":
            logger.info("speak_start")
            if self.speech is not None:
                self.speech.start()
        elif mtype == "speak_end":
            logger.info("speak_end")
            if self.speech is not None:
                self.speech.finish()
        elif mtype == "heard":
            logger.info("[Sim] heard: %s", msg.get("text", ""))
        elif mtype in KNOWN_TYPES:
            logger.debug("accepted (no simulated effect): %s", raw[:120])
        else:
            logger.debug("unknown message ignored: %s", raw[:120])

    def handle_binary(self, raw: bytes) -> None:
        kind = raw[0] if raw else None
        if kind == FRAME_KIND_PCM and len(raw) >= 5 and self.speech is not None and self.speech.active:
            self.speech.feed(raw[5:])
            return
        logger.debug("binary frame ignored (kind=%s, %d bytes)", kind, len(raw))

    async def receive_loop(self, ws) -> None:
        async for raw in ws:
            if isinstance(raw, bytes | bytearray):
                self.handle_binary(bytes(raw))
                continue
            self.handle_text(raw)

    # --- send --------------------------------------------------------------
    async def send_loop(self, ws) -> None:
        last = time.monotonic()
        last_log = last
        while True:
            await asyncio.sleep(self.period)
            now = time.monotonic()
            dt = now - last
            last = now
            self.sim.step(dt, now)
            self.seq += 1
            await ws.send(json.dumps(self.sim.odom_message(monotonic_ms(), self.seq)))
            joy_msg = self.sim.joy_message_if_due(now)
            if joy_msg is not None:
                await ws.send(json.dumps(joy_msg))
            for name, detail in self.sim.pop_events():
                logger.info("event: %s %s", name, detail)
                await ws.send(json.dumps({"type": "event", "name": name, "detail": detail}))
            if now - last_log >= 1.0:
                last_log = now
                logger.info(
                    "pose=(%.3f,%.3f,%.1fdeg) cmd=(%.3f,%.3f,%s) source=%s",
                    self.sim.x,
                    self.sim.y,
                    math.degrees(self.sim.th),
                    self.sim.cmd_v,
                    self.sim.cmd_w,
                    self.sim.cmd_src,
                    self.sim.source,
                )

    # --- camera -------------------------------------------------
    async def camera_loop(self, ws) -> None:
        if self.frame_source is None:
            return
        last_size = 0
        sent_since_log = 0
        last_log = time.monotonic()
        while True:
            await asyncio.sleep(1.0 / self.cam_fps)
            if not self.cam_on:
                continue
            jpeg = self.frame_source.next_frame(self.cam_quality)
            await ws.send(build_camera_frame(jpeg, monotonic_ms()))
            self.cam_seq += 1
            sent_since_log += 1
            last_size = len(jpeg)
            now = time.monotonic()
            elapsed = now - last_log
            if elapsed >= 5.0:
                logger.info("camera: sent=%d last=%dB fps=%.1f", self.cam_seq, last_size, sent_since_log / elapsed)
                sent_since_log = 0
                last_log = now

    # --- microphone ----------------------------------------------
    async def mic_loop(self, ws) -> None:
        """Send 32 ms PCM chunks in real time while the Hub has listen on."""
        mic = self.mic
        if mic is None:
            return
        next_t = time.monotonic()
        while True:
            chunk = mic.next_chunk()
            if chunk is None:
                if mic.finished:
                    logger.info("mic finished (%d pass)", mic.passes)
                    await asyncio.Event().wait()  # keep the connection; nothing more to send
                await asyncio.sleep(MIC_CHUNK_S)
                next_t = time.monotonic()
                continue
            await ws.send(build_pcm_frame(chunk, monotonic_ms()))
            next_t += MIC_CHUNK_S
            await asyncio.sleep(max(0.0, next_t - time.monotonic()))

    async def media_session(self, ws) -> None:
        tasks = []
        if self.frame_source is not None:
            tasks.append(asyncio.create_task(self.camera_loop(ws)))
        if self.mic is not None:
            tasks.append(asyncio.create_task(self.mic_loop(ws)))
        if not tasks:
            return
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def session(self, ws) -> None:
        self.sim.hub_connected = True
        await ws.send(json.dumps({"type": "event", "name": "joy_link", "detail": "up SIM"}))
        boot = {
            "type": "event",
            "name": "boot",
            "detail": f"sim track_mm={self.sim.track_mm} wheel_mm={self.sim.wheel_mm}",
        }
        await ws.send(json.dumps(boot))
        tasks = [asyncio.create_task(self.receive_loop(ws)), asyncio.create_task(self.send_loop(ws))]
        if self.single_ws and self.frame_source is not None:
            # --single-ws は camera を制御コネクションに乗せる (旧動作、Hub 互換確認用)。
            tasks.append(asyncio.create_task(self.camera_loop(ws)))
        if self.single_ws and self.mic is not None:
            tasks.append(asyncio.create_task(self.mic_loop(ws)))
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.sim.hub_connected = False
            if self.mic is not None:
                # the next connection's greeting tells us the listen flag again
                self.mic.listening = False
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_forever(self) -> None:
        while True:
            try:
                async with websockets.connect(self.hub) as ws:
                    logger.info("connected: %s", self.hub)
                    await self.session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # any transport error -> retry
                logger.info("disconnected (%s: %s)", type(exc).__name__, exc)
            else:
                logger.info("disconnected")
            logger.info("reconnecting in %.0f s", RECONNECT_DELAY_S)
            await asyncio.sleep(RECONNECT_DELAY_S)

    async def media_run_forever(self) -> None:
        """`role=media` コネクションを独立に接続・再接続し、camera_loop / mic_loop を回す。

        制御コネクション (:meth:`run_forever`) とは完全に独立したループ。メディア
        コネクションが落ちている間は camera_loop 自体が動いていない (= フレームは
        生成も送信もされず、単に "スキップ" される) だけで、制御コネクションには
        一切影響しない。``self.media_url`` が ``None`` (``--hub`` から導出できず
        ``--media-url`` も未指定) なら何もせず戻る。
        """
        if self.media_url is None:
            logger.info("media disabled (no --media-url and it could not be derived from --hub)")
            return
        while True:
            try:
                async with websockets.connect(self.media_url) as ws:
                    logger.info("media connected: %s", self.media_url)
                    await self.media_session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # any transport error -> retry
                logger.info("media disconnected (%s: %s)", type(exc).__name__, exc)
            else:
                logger.info("media disconnected")
            logger.info("media reconnecting in %.0f s", RECONNECT_DELAY_S)
            await asyncio.sleep(RECONNECT_DELAY_S)


def start_joystick_reader(sim: DiffDriveSim, loop: asyncio.AbstractEventLoop) -> None:
    """Read ``x y btn`` lines from stdin in a daemon thread (mock joystick)."""

    def reader() -> None:
        for line in sys.stdin:
            parts = line.split()
            if not parts:
                continue
            try:
                x = float(parts[0])
                y = float(parts[1]) if len(parts) > 1 else 0.0
                btn = int(float(parts[2])) if len(parts) > 2 else 0
            except ValueError:
                logger.warning("bad joystick line (expected 'x y btn'): %r", line.strip())
                continue
            loop.call_soon_threadsafe(sim.set_joystick, x, y, btn)

    threading.Thread(target=reader, name="joy-stdin", daemon=True).start()


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open StackChan Move device simulator")
    parser.add_argument("--hub", default=DEFAULT_HUB, help="Hub device (control) WebSocket URL, role=device")
    parser.add_argument(
        "--media-url",
        default=None,
        help=(
            "Hub media WebSocket URL, role=media. Default: derived from --hub by replacing a trailing "
            "/ws/device with /ws/media; if --hub doesn't end with /ws/device, media is disabled unless this is given"
        ),
    )
    parser.add_argument(
        "--single-ws",
        action="store_true",
        help=(
            "send camera frames on the control connection instead of opening a second media connection "
            "(for Hub device-role compatibility testing)"
        ),
    )
    parser.add_argument(
        "--rate", type=float, default=10.0, help="initial odom send rate [Hz] (the Hub's odom_rate wins)"
    )
    parser.add_argument(
        "--track-mm",
        type=float,
        default=_env_float("HUB_TRACK_WIDTH_MM", DEFAULT_TRACK_MM),
        help="track width [mm] (env HUB_TRACK_WIDTH_MM)",
    )
    parser.add_argument(
        "--wheel-mm",
        type=float,
        default=_env_float("HUB_WHEEL_DIAMETER_MM", DEFAULT_WHEEL_MM),
        help="wheel diameter [mm] (env HUB_WHEEL_DIAMETER_MM)",
    )
    parser.add_argument(
        "--frames-dir",
        default=None,
        help="cycle *.jpg/*.jpeg/*.png from this directory instead of the synthetic image",
    )
    parser.add_argument("--fps", type=float, default=5.0, help="camera frame rate, 1..10 Hz (the Hub's camera wins)")
    parser.add_argument("--no-camera", action="store_true", help="disable camera frame streaming")
    parser.add_argument(
        "--joy",
        action="store_true",
        help=(
            "mock joystick: type 'x y btn' lines on stdin (btn: 0 release / 1 short press = estop / "
            "2 long press = standalone source toggle when Hub is down)"
        ),
    )
    parser.add_argument(
        "--mic-wav",
        default=None,
        help="mock microphone: stream this WAV (any rate/channels -> 16 kHz mono) while the Hub has listen on",
    )
    parser.add_argument(
        "--mic-loop", action="store_true", help="repeat --mic-wav (3 s of silence between passes); default: once"
    )
    parser.add_argument(
        "--speech-out",
        default=None,
        help="save the speech audio received between speak_start and speak_end as DIR/speech_NNN.wav",
    )
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    return parser


async def async_main(args: argparse.Namespace) -> int:
    sim = DiffDriveSim(track_mm=args.track_mm, wheel_mm=args.wheel_mm)
    frame_source = None
    if args.no_camera:
        logger.info("camera source: disabled (--no-camera)")
    elif args.frames_dir is not None:
        frame_source = DirFrameSource(args.frames_dir)
        logger.info("camera source: dir=%s (%d files)", args.frames_dir, frame_source.count)
    else:
        frame_source = SyntheticFrameSource(sim)
        logger.info("camera source: synthetic")
    if args.joy:
        logger.info(
            "mock joystick enabled: type 'x y btn' lines on stdin (btn: 0 release / 1 short press = estop / "
            "2 long press = standalone source toggle when Hub is down)"
        )
        start_joystick_reader(sim, asyncio.get_running_loop())
    mic = None
    if args.mic_wav is not None:
        mic = MicStreamer(load_wav_pcm16_mono(args.mic_wav), loop=args.mic_loop)
        logger.info("mic source: %s (%.1f s per pass, loop=%s)", args.mic_wav, mic.duration_s, args.mic_loop)
    speech = SpeechRecorder(args.speech_out) if args.speech_out is not None else None
    if speech is not None:
        logger.info("speech output: %s/speech_NNN.wav", args.speech_out)
    # derive the media URL from --hub unless --single-ws or --media-url override it.
    media_url = None
    if not args.single_ws:
        media_url = args.media_url if args.media_url is not None else derive_media_url(args.hub)
        if media_url is None:
            logger.info(
                "media disabled: --hub=%r does not end with /ws/device and no --media-url was given "
                "(camera frames will not be sent; pass --media-url or --single-ws)",
                args.hub,
            )
        else:
            logger.info("media URL: %s", media_url)
    device = SimDevice(
        sim,
        args.hub,
        args.rate,
        frame_source=frame_source,
        fps=args.fps,
        media_url=media_url,
        single_ws=args.single_ws,
        mic=mic,
        speech=speech,
    )
    tasks = [asyncio.create_task(device.run_forever())]
    if not args.single_ws and media_url is not None and (frame_source is not None or mic is not None):
        tasks.append(asyncio.create_task(device.media_run_forever()))
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    logger.info(
        "sim_device start hub=%s rate=%.1fHz track_mm=%s wheel_mm=%s",
        args.hub,
        args.rate,
        args.track_mm,
        args.wheel_mm,
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(async_main(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
