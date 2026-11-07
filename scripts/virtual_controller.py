"""Run a simulated ESP32 controller on a virtual serial port.

    .venv/bin/python scripts/virtual_controller.py
    # then, in another terminal, the line it prints:
    HARDWARE_MODE=serial SERIAL_PORT=/dev/ttysNNN ./run.sh

This exercises the app's real serial code path (pyserial, reader thread, reconnects)
before the hardware exists. It is also what scripts/bench_check.py is tested against.
Everything it reports is simulated; the firmware name is doseguard-sim-*.

Type commands while it runs:
    door open | door close | door force     access door switch (force = open while latched)
    feed A ok|empty|double|wrong_class|stall|lost_done|no_ack
    tray                                    show simulated tray contents
    collect                                 take the tokens out (door must be open)
    status                                  controller state
    power                                   power cycle (new session)
    quit
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.virtual_controller import VirtualSerialController  # noqa: E402

FEED_MODES = ("ok", "empty", "double", "wrong_class", "stall", "lost_done", "no_ack")


def handle(vc: VirtualSerialController, line: str) -> str | None:
    ctl = vc.controller
    parts = line.split()
    if not parts:
        return None
    cmd, args = parts[0].lower(), parts[1:]
    if cmd == "door" and args:
        if args[0] == "open":
            return ctl.open_door() or "door open"
        if args[0] == "force":
            return ctl.open_door(force=True) or "door forced open"
        if args[0] == "close":
            return ctl.close_door() or "door closed"
    if cmd == "feed" and len(args) == 2 and args[0].upper() in ("A", "B") and args[1] in FEED_MODES:
        ctl.feed_mode[args[0].upper()] = args[1]
        return f"feed {args[0].upper()} = {args[1]}"
    if cmd == "tray":
        return f"tray A={ctl.tray['A']} B={ctl.tray['B']}"
    if cmd == "collect":
        return ctl.collect() or "tokens collected"
    if cmd == "status":
        return f"session {ctl.session}  {ctl._compact()}  feed {ctl.feed_mode}"
    if cmd == "power":
        ctl._stop(None)
        ctl._boot()
        return f"power cycled, new session {ctl.session}"
    return "unknown command (see the top of this script)"


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--motion-s", type=float, default=0.8, help="seconds per pocket cycle (default 0.8)")
    ap.add_argument("--feed", action="append", default=[], metavar="CH=MODE",
                    help="start with a feed mode, e.g. --feed A=double")
    ap.add_argument("--no-input", action="store_true", help="don't read commands from stdin")
    a = ap.parse_args()

    settings = Settings.from_env(hardware_mode="fake", genai_backend="off", fake_motion_s_per_unit=a.motion_s)
    vc = VirtualSerialController(settings)
    path = vc.open()
    task = asyncio.create_task(vc.run())
    await asyncio.sleep(0.05)
    for spec in a.feed:
        ch, _, mode = spec.partition("=")
        if ch.upper() not in ("A", "B") or mode not in FEED_MODES:
            sys.exit(f"bad --feed {spec!r}; use A=MODE or B=MODE with MODE in {', '.join(FEED_MODES)}")
        vc.controller.feed_mode[ch.upper()] = mode

    print(f"Virtual DoseGuard controller (SIMULATED) on {path}")
    print(f"  HARDWARE_MODE=serial SERIAL_PORT={path} ./run.sh")
    print(f"  .venv/bin/python scripts/bench_check.py --port {path}", flush=True)
    if a.no_input:
        await task
        return
    print("Type 'status', 'door open', 'feed A double', ... or 'quit'.")
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line or line.strip() in ("quit", "exit"):
            break
        out = handle(vc, line.strip())
        if out:
            print(out, flush=True)
    task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
