#!/usr/bin/env python3
"""Open StackChan Move drive CLI.

Connects to the Hub with the ``app`` role and sends ``control drive``
at a fixed rate for a fixed duration (the Hub holds a target for 1 s only, so a
single message is hard to observe), optionally switching ``drive_source`` first.

Example::

    python tools/drive_cli.py --hub ws://127.0.0.1:8341/ws/app --v 0.1 --w 0.5 --duration 3
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import sys
import time

import websockets

DEFAULT_HUB = "ws://127.0.0.1:8341/ws/app"
STATE_WAIT_S = 2.0
ODOM_PRINT_PERIOD_S = 1.0
#: ``control drive`` の送信レート [Hz] (Hub は目標を 1 s しか保持しないので送り続ける)。
SEND_RATE_HZ = 10.0


class Printer:
    """Prints incoming Hub messages; captures the greeting and the next ``drive_source`` state."""

    def __init__(self) -> None:
        self.state_future: asyncio.Future[str] | None = None
        #: resolved by the greeting's ``state drive_source:*`` (the last greeting state)
        self.greeting_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._last_odom_print = 0.0

    def arm_state_wait(self) -> asyncio.Future[str]:
        self.state_future = asyncio.get_running_loop().create_future()
        return self.state_future

    def handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict):
            return
        mtype = msg.get("type")
        if mtype == "state":
            value = str(msg.get("value", ""))
            print(f"state: {value}", flush=True)
            if not value.startswith("drive_source:"):
                return
            if not self.greeting_future.done():
                self.greeting_future.set_result(value)
                return
            fut = self.state_future
            if fut is not None and not fut.done():
                fut.set_result(value)
        elif mtype == "event":
            print(f"event: {msg.get('name')} {msg.get('detail', '')}".rstrip(), flush=True)
        elif mtype == "odom":
            now = time.monotonic()
            if now - self._last_odom_print < ODOM_PRINT_PERIOD_S:
                return
            self._last_odom_print = now
            cmd = msg.get("cmd") or {}
            print(
                "odom: x={:.3f} y={:.3f} th={:.1f}deg v={:.3f} w={:.3f} src={}".format(
                    float(msg.get("x", 0.0)),
                    float(msg.get("y", 0.0)),
                    math.degrees(float(msg.get("th", 0.0))),
                    float(msg.get("v", 0.0)),
                    float(msg.get("w", 0.0)),
                    cmd.get("src", "?"),
                ),
                flush=True,
            )


async def receive_loop(ws, printer: Printer) -> None:
    async for raw in ws:
        if isinstance(raw, bytes | bytearray):
            continue
        printer.handle(raw)


def drive_message(v: float, w: float) -> str:
    return json.dumps({"type": "control", "command": "drive", "params": {"v": v, "w": w}})


async def run(args: argparse.Namespace) -> int:
    printer = Printer()
    async with websockets.connect(args.hub) as ws:
        print(f"connected: {args.hub}", flush=True)
        rx = asyncio.create_task(receive_loop(ws, printer))
        try:
            if args.source:
                # wait for the greeting so that its drive_source state is not taken as the reply
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(printer.greeting_future), STATE_WAIT_S)
                fut = printer.arm_state_wait()
                await ws.send(
                    json.dumps({"type": "control", "command": "drive_source", "params": {"source": args.source}})
                )
                try:
                    await asyncio.wait_for(fut, STATE_WAIT_S)
                except TimeoutError:
                    print(f"(no drive_source state within {STATE_WAIT_S:.0f} s)", flush=True)

            if args.duration > 0:
                period = 1.0 / SEND_RATE_HZ
                print(
                    f"drive v={args.v} w={args.w} for {args.duration}s at {SEND_RATE_HZ:g}Hz",
                    flush=True,
                )
                deadline = time.monotonic() + args.duration
                while time.monotonic() < deadline:
                    await ws.send(drive_message(args.v, args.w))
                    await asyncio.sleep(period)
                await ws.send(drive_message(0.0, 0.0))
                print("stop sent (v=0 w=0)", flush=True)
                # let the last state/odom messages arrive
                await asyncio.sleep(0.3)
        finally:
            rx.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await rx
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open StackChan Move drive CLI")
    parser.add_argument("--hub", default=DEFAULT_HUB, help="Hub app WebSocket URL")
    parser.add_argument("--v", type=float, default=0.0, help="linear velocity [m/s] (forward positive)")
    parser.add_argument("--w", type=float, default=0.0, help="angular velocity [rad/s] (left positive)")
    parser.add_argument("--duration", type=float, default=3.0, help="send duration [s] (0 = do not drive)")
    parser.add_argument("--source", choices=["hub", "joystick"], default=None, help="switch drive source")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rc = 0
    with contextlib.suppress(KeyboardInterrupt):
        rc = asyncio.run(run(args))
    return rc


if __name__ == "__main__":
    sys.exit(main())
