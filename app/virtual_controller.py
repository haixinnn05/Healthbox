"""A simulated ESP32 controller behind a real pseudo-terminal (macOS/Linux).

The app's FakeAdapter talks to the simulated controller in-process, which never exercises
the SerialAdapter code (pyserial, the reader thread, port loss). This puts the same
FakeController behind a pty so `HARDWARE_MODE=serial SERIAL_PORT=<pty>` runs the real
serial path end to end, and so scripts/bench_check.py can be checked without hardware.

Everything it sends is simulated. The firmware string says so (doseguard-sim-*)."""
from __future__ import annotations

import asyncio
import os
import pty
import threading
import tty

from .config import Settings
from .hardware import FakeController
from .timeutil import Clock


class VirtualSerialController:
    def __init__(self, settings: Settings | None = None, clock: Clock | None = None):
        self.settings = settings or Settings.from_env(hardware_mode="fake", genai_backend="off")
        self.clock = clock or Clock()
        self.master: int | None = None
        self._slave: int | None = None
        self.path: str | None = None
        self.controller: FakeController | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._buf = b""
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.dropped_writes = 0

    # -- lifecycle ----------------------------------------------------------------
    def open(self) -> str:
        master, slave = pty.openpty()
        tty.setraw(slave)  # no echo, no line editing: bytes pass through as-is
        os.set_blocking(master, False)
        # Keep our own handle on the slave so the pty survives the app closing and reopening it.
        self.master, self._slave = master, slave
        self.path = os.ttyname(slave)
        return self.path

    async def run(self) -> None:
        """Serve until cancelled. Call from inside an event loop."""
        if self.master is None:
            self.open()
        self.loop = asyncio.get_running_loop()
        self.controller = FakeController(self.settings, self.clock, self._emit)
        self.loop.add_reader(self.master, self._readable)
        self._ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.loop.remove_reader(self.master)

    def start_in_thread(self) -> str:
        """Run on a private event loop in a daemon thread (used by tests). Returns the port path."""
        path = self.open()

        def main():
            try:
                asyncio.run(self.run())
            except Exception:
                pass
        self._thread = threading.Thread(target=main, name="virtual-controller", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return path

    def call(self, fn, *args):
        """Run fn on the controller's loop and wait for the result (thread-safe)."""
        assert self.loop is not None
        done = threading.Event()
        box: dict = {}

        def wrapped():
            try:
                box["v"] = fn(*args)
            except Exception as exc:  # surface to the caller
                box["e"] = exc
            done.set()
        self.loop.call_soon_threadsafe(wrapped)
        done.wait(5)
        if "e" in box:
            raise box["e"]
        return box.get("v")

    def unplug(self) -> None:
        """Simulate pulling the USB cable: the pty disappears and the app's reads fail."""
        def close():
            if self.master is not None:
                self.loop.remove_reader(self.master)
                for fd in (self.master, self._slave):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                self.master = self._slave = None
        if self.loop and self.loop.is_running():
            self.call(close)
        else:
            close()

    # -- byte pump ----------------------------------------------------------------
    def _readable(self) -> None:
        try:
            chunk = os.read(self.master, 1024)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            return
        self._buf += chunk
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            if self.controller and len(raw) < 256:
                self.controller.receive(raw.decode("utf-8", errors="replace"))
        if len(self._buf) > 1024:
            self._buf = b""

    def _emit(self, line: str) -> None:
        if self.master is None:
            return
        try:
            os.write(self.master, (line + "\n").encode())
        except (BlockingIOError, OSError):
            # Nobody is reading and the pty buffer is full; a real UART would drop bytes too.
            self.dropped_writes += 1
