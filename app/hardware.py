"""Hardware adapters. The fake and the real serial adapter expose the same interface and
speak the same newline-delimited JSON protocol (version 3, see docs/serial-protocol.md).

Only the workflow engine may call `send` with movement commands. Everything else reads
`status()`. STOP goes through `stop_now`, which skips every queue."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
from typing import Awaitable, Callable

from .config import Settings
from .timeutil import Clock

log = logging.getLogger("doseguard.hardware")

PROTOCOL_VERSION = 3
MAX_LINE_BYTES = 256
CHANNELS = ("A", "B")

Listener = Callable[[dict], Awaitable[None]]


def encode(msg: dict) -> str:
    line = json.dumps(msg, separators=(",", ":"))
    if len(line.encode()) + 1 > MAX_LINE_BYTES:
        raise ValueError(f"message exceeds {MAX_LINE_BYTES} bytes")
    return line


class HardwareAdapter:
    mode = "base"
    simulated = False

    def __init__(self, settings: Settings, clock: Clock):
        self.settings = settings
        self.clock = clock
        self.listener: Listener | None = None
        self.connected = False
        self.session: str | None = None
        self.fw: str | None = None
        self.protocol_error: str | None = None
        self.last_rx: float | None = None
        self.fields: dict = {"mA": None, "mB": None, "hA": None, "hB": None, "door": None,
                             "servo": None, "rev": None, "txn": None, "fault": None}
        self._queue: asyncio.Queue[dict] | None = None
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- lifecycle --------------------------------------------------------------
    def set_listener(self, listener: Listener) -> None:
        self.listener = listener

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=1000)
        self._tasks.append(asyncio.create_task(self._dispatch(), name="hw-dispatch"))
        await self._open()
        self._tasks.append(asyncio.create_task(self._heartbeat(), name="hw-heartbeat"))

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        await self._close_transport()

    # -- transport hooks (subclasses) -------------------------------------------------
    async def _open(self) -> None:
        raise NotImplementedError

    async def _close_transport(self) -> None:
        pass

    async def _write(self, line: str) -> bool:
        raise NotImplementedError

    async def _maybe_reconnect(self) -> None:
        pass

    # -- outgoing -------------------------------------------------------------------
    async def send(self, msg: dict) -> bool:
        """Write one command. Returns False if the link is down (nothing was written)."""
        full = {"v": PROTOCOL_VERSION}
        if self.session:
            full["s"] = self.session
        full.update(msg)
        line = encode(full)
        if not self.connected and msg.get("cmd") not in ("PING",):
            return False
        return await self._write(line)

    async def stop_now(self, command_id: int | None = None) -> bool:
        msg = {"cmd": "STOP"}
        if command_id is not None:
            msg["id"] = command_id
        try:
            return await self.send(msg)
        except Exception:
            log.exception("STOP write failed")
            return False

    # -- incoming -------------------------------------------------------------------
    def _on_line(self, line: str) -> None:
        """Runs on the event loop thread."""
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                raise ValueError("not an object")
        except ValueError:
            self._push({"event": "PROTOCOL_ERROR", "raw": line[:80]})
            return
        if msg.get("v") != PROTOCOL_VERSION:
            self.protocol_error = f"controller speaks protocol {msg.get('v')!r}, expected {PROTOCOL_VERSION}"
            if self.connected:
                self.connected = False
                self._push({"event": "DISCONNECTED", "reason": "PROTOCOL_MISMATCH"})
            return
        self.protocol_error = None
        self.last_rx = self.clock.monotonic()
        if not self.connected:
            self.connected = True
            self._push({"event": "CONNECTED"})
        s = msg.get("s")
        if s and s != self.session:
            old, self.session = self.session, s
            self.fw = msg.get("fw")
            if self.fw is None and self._loop:
                # HELLO was missed (sent before the port opened); STATUS also names the firmware.
                self._loop.create_task(self.send({"cmd": "STATUS"}))
            self._push({"event": "SESSION", "old": old, "s": s})
        ev = msg.get("event")
        if msg.get("fw"):
            self.fw = msg["fw"]
        before = dict(self.fields)
        for k in self.fields:
            if k in msg:
                self.fields[k] = msg[k]
        if "rev" in msg and msg["rev"] is not None:
            self.fields["rev"] = msg["rev"]
        changes = {k: (before[k], self.fields[k]) for k in self.fields if before[k] != self.fields[k]}
        if ev in ("PONG", "STATUS"):
            if changes:
                self._push({"event": "STATE_CHANGED", "changes": changes})
            return
        if changes:
            msg = dict(msg, changes=changes)
        self._push(msg)

    def _push(self, evt: dict) -> None:
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(evt)
        except asyncio.QueueFull:
            log.error("hardware event queue full; dropping %s", evt.get("event"))

    async def _dispatch(self) -> None:
        assert self._queue is not None
        while True:
            evt = await self._queue.get()
            if self.listener:
                try:
                    await self.listener(evt)
                except Exception:
                    log.exception("listener failed for %s", evt.get("event"))

    async def _heartbeat(self) -> None:
        s = self.settings
        while True:
            try:
                await self._maybe_reconnect()
                await self.send({"cmd": "PING"})
            except Exception:
                log.debug("ping failed", exc_info=True)
            if self.connected and self.last_rx is not None and self.clock.monotonic() - self.last_rx > s.watchdog_s:
                self.connected = False
                self._push({"event": "DISCONNECTED", "reason": "HEARTBEAT_LOST"})
            await asyncio.sleep(s.ping_interval_s)

    # -- snapshot -------------------------------------------------------------------
    def status(self) -> dict:
        f = self.fields
        age = None if self.last_rx is None else round((self.clock.monotonic() - self.last_rx) * 1000)
        return {
            "mode": self.mode,
            "simulated": self.simulated,
            "connected": self.connected,
            "session": self.session,
            "firmware": self.fw,
            "protocol_version": PROTOCOL_VERSION,
            "protocol_error": self.protocol_error,
            "heartbeat_age_ms": age,
            "motor": {"A": f["mA"], "B": f["mB"]},
            "home": {"A": _bool(f["hA"]), "B": _bool(f["hB"])},
            "door_closed": _bool(f["door"]),
            "servo_commanded": {"L": "locked", "U": "unlocked"}.get(f["servo"], f["servo"]),
            "servo_measured": None,  # no latch feedback switch is fitted
            "lid_closed_measured": None,  # the fixed tray top has no sensor
            "transaction": f["txn"],
            "tray_revision": f["rev"],
            "fault": f["fault"],
        }

    def motors_idle(self) -> bool:
        return self.fields["mA"] == "idle" and self.fields["mB"] == "idle"

    def sim_tray(self) -> dict | None:
        return None


def _bool(v):
    return None if v is None else bool(v)


# ---------------------------------------------------------------------------------
# Real USB serial adapter (ESP-WROOM-32)
# ---------------------------------------------------------------------------------
class SerialAdapter(HardwareAdapter):
    mode = "serial"

    def __init__(self, settings: Settings, clock: Clock):
        super().__init__(settings, clock)
        self._ser = None
        self._reader: threading.Thread | None = None
        self._write_lock = asyncio.Lock()
        self._next_attempt = 0.0
        self.port_error: str | None = None

    async def _open(self) -> None:
        await self._maybe_reconnect(force=True)

    async def _maybe_reconnect(self, force: bool = False) -> None:
        if self._ser is not None:
            return
        now = self.clock.monotonic()
        if not force and now < self._next_attempt:
            return
        self._next_attempt = now + 2.0
        try:
            import serial  # pyserial
            # exclusive: a second program (bench check, another app instance) gets "port busy" instead of
            # silently splitting the byte stream. Serial monitors that ignore the lock are not stopped.
            ser = serial.Serial(self.settings.serial_port, self.settings.serial_baud, timeout=0.1, write_timeout=0.5,
                                exclusive=True)
        except Exception as exc:  # port missing, busy, or permission denied
            self.port_error = str(exc)
            return
        self.port_error = None
        self._ser = ser
        self._reader = threading.Thread(target=self._read_loop, args=(ser,), name="serial-reader", daemon=True)
        self._reader.start()
        log.info("serial port %s opened", self.settings.serial_port)

    def _read_loop(self, ser) -> None:
        buf = b""
        while self._ser is ser:
            try:
                chunk = ser.read(256)
            except Exception as exc:
                log.warning("serial read failed: %s", exc)
                break
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                if len(raw) > MAX_LINE_BYTES:
                    continue
                text = raw.decode("utf-8", errors="replace")
                if self._loop:
                    self._loop.call_soon_threadsafe(self._on_line, text)
            if len(buf) > 4 * MAX_LINE_BYTES:
                buf = b""
        if self._loop:
            self._loop.call_soon_threadsafe(self._port_lost)

    def _port_lost(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        if self.connected:
            self.connected = False
            self._push({"event": "DISCONNECTED", "reason": "PORT_LOST"})

    async def _write(self, line: str) -> bool:
        ser = self._ser
        if ser is None:
            return False
        async with self._write_lock:
            try:
                await asyncio.to_thread(ser.write, (line + "\n").encode())
                return True
            except Exception as exc:
                log.warning("serial write failed: %s", exc)
                self._port_lost()
                return False

    async def _close_transport(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            ser.close()

    def status(self) -> dict:
        st = super().status()
        st["port"] = self.settings.serial_port
        st["port_error"] = self.port_error
        return st


# ---------------------------------------------------------------------------------
# Fake adapter with an in-process simulated controller
# ---------------------------------------------------------------------------------
class FakeController:
    """Behaves like the agreed firmware, including its failure modes. Every reading it
    produces is simulated and labeled as such in the UI."""

    FW = "doseguard-sim-0.4"

    def __init__(self, settings: Settings, clock: Clock, emit: Callable[[str], None]):
        self.settings = settings
        self.clock = clock
        self.emit_line = emit
        self.link_up = True
        self.feed_mode = {"A": "ok", "B": "ok"}  # ok | empty | double | wrong_class | stall | lost_done | no_ack
        self.ack_delay_s = 0.0
        self.tray = {"A": 0, "B": 0}  # tokens in the tray by source cartridge
        self.door = 1
        self._motion: dict[str, asyncio.Task] = {}
        self._boot(first=True)

    def _boot(self, first: bool = False) -> None:
        self.session = secrets.token_hex(2)
        self.rev = 0 if first else self.rev + 1
        self.motors = {"A": "idle", "B": "idle"}
        self.home = {"A": 1, "B": 1}
        self.servo = "L"
        self.fault = None
        self.txn = None
        self.seen: dict[int, str] = {}
        self.released: set[str] = set()
        self.last_ping = self.clock.monotonic()
        self._send({"event": "HELLO", "fw": self.FW, "ch": list(CHANNELS)})

    # -- link -------------------------------------------------------------------
    def _send(self, msg: dict) -> None:
        if not self.link_up:
            return
        self.emit_line(encode({"v": PROTOCOL_VERSION, "s": self.session, **msg}))

    def _compact(self) -> dict:
        return {"mA": self.motors["A"], "mB": self.motors["B"], "hA": self.home["A"], "hB": self.home["B"],
                "door": self.door, "servo": self.servo, "rev": self.rev, "txn": self.txn, "fault": self.fault}

    def receive(self, line: str) -> None:
        if not self.link_up:
            return
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if msg.get("v") != PROTOCOL_VERSION:
            self._send({"event": "NACK", "id": msg.get("id"), "reason": "BAD_VERSION"})
            return
        cmd = msg.get("cmd")
        if cmd == "PING":
            self.last_ping = self.clock.monotonic()
            self._send({"event": "PONG", **self._compact()})
            return
        if msg.get("s") != self.session:
            self._send({"event": "NACK", "id": msg.get("id"), "reason": "BAD_SESSION"})
            return
        if cmd == "STOP":
            self._stop(msg.get("id"))
            return
        if cmd == "STATUS":
            self._send({"event": "STATUS", **self._compact(), "fw": self.FW})
            return
        cid = msg.get("id")
        if not isinstance(cid, int):
            self._send({"event": "NACK", "reason": "MISSING_ID"})
            return
        payload = json.dumps({k: v for k, v in msg.items() if k not in ("v", "s")}, sort_keys=True)
        if cid in self.seen:
            if self.seen[cid] == payload:
                self._send({"event": "ACK", "id": cid, "dup": 1})
            else:
                self._send({"event": "NACK", "id": cid, "reason": "ID_REUSED"})
            return
        self.seen[cid] = payload
        handler = {"DISPENSE": self._dispense, "RELEASE": self._release, "LOCK": self._lock, "RESET": self._reset}.get(cmd)
        if not handler:
            self._send({"event": "NACK", "id": cid, "reason": "UNKNOWN_CMD"})
            return
        handler(cid, msg)

    def _ack(self, cid: int) -> None:
        self._send({"event": "ACK", "id": cid})

    # -- commands ---------------------------------------------------------------
    def _dispense(self, cid: int, msg: dict) -> None:
        c, n = msg.get("c"), msg.get("n")
        if c not in CHANNELS:
            self._send({"event": "NACK", "id": cid, "reason": "UNKNOWN_CHANNEL"})
            return
        if not isinstance(n, int) or not 1 <= n <= self.settings.max_tokens_per_channel:
            self._send({"event": "NACK", "id": cid, "reason": "BAD_QUANTITY"})
            return
        if self.fault:
            self._send({"event": "NACK", "id": cid, "reason": "FAULT"})
            return
        if self.motors[c] != "idle" or not self.home[c]:
            self._send({"event": "NACK", "id": cid, "reason": "NOT_READY"})
            return
        mode = self.feed_mode[c]
        if mode == "no_ack":
            return  # command lost: no ACK, no motion
        self.txn = msg.get("t")
        self._motion[c] = asyncio.get_running_loop().create_task(self._run_motion(cid, c, n, mode, msg.get("t")))

    async def _run_motion(self, cid: int, c: str, n: int, mode: str, t: str | None) -> None:
        if self.ack_delay_s:
            await asyncio.sleep(self.ack_delay_s)
        self._ack(cid)
        self.motors[c] = "moving"
        self.home[c] = 0
        try:
            for i in range(n):
                await asyncio.sleep(self.settings.fake_motion_s_per_unit)
                if self.clock.monotonic() - self.last_ping > self.settings.watchdog_s:
                    self._fault("HEARTBEAT_LOST", c)
                    return
                self.rev += 1  # any rotor motion increments the tray revision
                if mode == "stall":
                    self._fault("MOTOR_STALL", c, cid)
                    return
                if mode == "ok":
                    self.tray[c] += 1
                elif mode == "double":
                    self.tray[c] += 2
                elif mode == "wrong_class":
                    other = "B" if c == "A" else "A"
                    self.tray[other] += 1
                # "empty" and "lost_done" add nothing / something below
                if mode == "lost_done":
                    self.tray[c] += 1
            self.motors[c] = "idle"
            self.home[c] = 1
            if mode == "lost_done":
                return  # motion finished but the completion message is lost
            # Status fields ride along so the host sees the motor idle the moment the cycle ends.
            self._send({"event": "CYCLE_DONE", "id": cid, "c": c, **self._compact(), "t": t})
        except asyncio.CancelledError:
            self.motors[c] = "idle"
            raise

    def _fault(self, reason: str, c: str | None = None, cid: int | None = None) -> None:
        self.fault = reason
        if c:
            self.motors[c] = "fault"
        msg = {"event": "FAULT", "reason": reason}
        if c:
            msg["c"] = c
        if cid is not None:
            msg["id"] = cid
        self._send(msg)

    def _release(self, cid: int, msg: dict) -> None:
        t = msg.get("t")
        reason = None
        if self.fault:
            reason = "FAULT"
        elif self.motors["A"] != "idle" or self.motors["B"] != "idle":
            reason = "MOTOR_BUSY"
        elif not self.door:
            reason = "DOOR_OPEN"
        elif msg.get("rev") != self.rev:
            reason = "BAD_REV"
        elif t in self.released:
            reason = "ALREADY_RELEASED"
        elif self.clock.monotonic() - self.last_ping > self.settings.watchdog_s:
            reason = "HEARTBEAT_LOST"
        if reason:
            self._send({"event": "NACK", "id": cid, "reason": reason})
            return
        self._ack(cid)
        self.released.add(t)
        self.servo = "U"
        self._send({"event": "RELEASED", "id": cid, "t": t, "rev": self.rev, "servo": "U"})

    def _lock(self, cid: int, msg: dict) -> None:
        if not self.door:
            self._send({"event": "NACK", "id": cid, "reason": "DOOR_OPEN"})
            return
        self._ack(cid)
        self.servo = "L"
        self.txn = None
        self._send({"event": "LOCKED", "id": cid, "servo": "L"})

    def _stop(self, cid) -> None:
        for task in self._motion.values():
            task.cancel()
        self._motion.clear()
        self.motors = {c: "idle" for c in CHANNELS}
        self.fault = "STOPPED"
        if cid is not None:
            self._ack(cid)
        self._send({"event": "STOPPED", "id": cid, **self._compact()})

    def _reset(self, cid: int, msg: dict) -> None:
        self._ack(cid)

        async def reboot():
            await asyncio.sleep(0.3)
            self._boot()  # new session; rotors rehome; servo commanded locked
        asyncio.get_running_loop().create_task(reboot())

    # -- simulator controls (Device controls page) ---------------------------------------
    def open_door(self, force: bool = False) -> str | None:
        if self.door == 0:
            return "Door is already open."
        if self.servo == "L" and not force:
            return "Door is latched. Release unlocks it; use force to simulate tampering or maintenance."
        self.door = 0
        self.rev += 1
        self._send({"event": "SWITCH", "door": 0, "hA": self.home["A"], "hB": self.home["B"], "rev": self.rev})
        return None

    def close_door(self) -> str | None:
        if self.door == 1:
            return "Door is already closed."
        self.door = 1
        self.rev += 1
        self._send({"event": "SWITCH", "door": 1, "hA": self.home["A"], "hB": self.home["B"], "rev": self.rev})
        return None

    def collect(self) -> str | None:
        if self.door != 1 and any(self.tray.values()):
            self.tray = {c: 0 for c in CHANNELS}
            return None
        if self.door == 1:
            return "Open the door before collecting."
        return "Tray is already empty."

    def place_tokens(self, channel: str, n: int) -> None:
        self.tray[channel] = max(0, self.tray[channel] + n)


class FakeAdapter(HardwareAdapter):
    mode = "fake"
    simulated = True

    def __init__(self, settings: Settings, clock: Clock):
        super().__init__(settings, clock)
        self.controller: FakeController | None = None

    async def _open(self) -> None:
        self.controller = FakeController(self.settings, self.clock, self._from_controller)

    def _from_controller(self, line: str) -> None:
        loop = self._loop or asyncio.get_running_loop()
        loop.call_soon(self._on_line, line)

    async def _write(self, line: str) -> bool:
        ctl = self.controller
        if ctl is None or not ctl.link_up:
            return False
        loop = self._loop or asyncio.get_running_loop()
        loop.call_soon(ctl.receive, line)
        return True

    # Simulator knobs -------------------------------------------------------------------
    def sim(self, action: str, **kw) -> str | None:
        ctl = self.controller
        assert ctl is not None
        if action == "feed_mode":
            ch, mode = kw.get("channel"), kw.get("mode")
            if ch not in CHANNELS or mode not in ("ok", "empty", "double", "wrong_class", "stall", "lost_done", "no_ack"):
                return "Unknown channel or feed mode."
            ctl.feed_mode[ch] = mode
        elif action == "ack_delay":
            ctl.ack_delay_s = max(0.0, min(5.0, float(kw.get("seconds", 0))))
        elif action == "disconnect":
            ctl.link_up = False
        elif action == "reconnect":
            ctl.link_up = True
            ctl.last_ping = self.clock.monotonic()
        elif action == "power_cycle":
            ctl.link_up = True
            ctl._stop(None)
            ctl._boot()
        elif action == "open_door":
            return ctl.open_door(force=bool(kw.get("force")))
        elif action == "close_door":
            return ctl.close_door()
        elif action == "collect":
            return ctl.collect()
        elif action == "place_token":
            ch = kw.get("channel")
            if ch not in CHANNELS:
                return "Unknown channel."
            ctl.place_tokens(ch, int(kw.get("n", 1)))
        else:
            return "Unknown simulator action."
        return None

    def sim_state(self) -> dict:
        ctl = self.controller
        return {
            "feed_mode": dict(ctl.feed_mode), "ack_delay_s": ctl.ack_delay_s, "link_up": ctl.link_up,
            "tray": dict(ctl.tray), "door_closed": bool(ctl.door),
        } if ctl else {}

    def sim_tray(self) -> dict | None:
        return dict(self.controller.tray) if self.controller else None


def make_adapter(settings: Settings, clock: Clock) -> HardwareAdapter:
    if settings.hardware_mode == "serial":
        return SerialAdapter(settings, clock)
    return FakeAdapter(settings, clock)
