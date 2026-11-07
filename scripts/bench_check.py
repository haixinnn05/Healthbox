"""Bench check for the DoseGuard controller firmware (protocol v3, docs/serial-protocol.md).

Close the DoseGuard app first: only one program may own the serial port.

    .venv/bin/python scripts/bench_check.py --port /dev/cu.usbserial-0001
        Protocol conformance: framing, rejections, duplicate IDs, A and B cycles, release
        once, STOP during motion, watchdog, RESET. Prints PASS / FAIL / SKIP per check and
        exits 1 if anything failed. It moves both rotors and the latch servo, so run it with
        the enclosure assembled, the access door closed and hands clear.

    .venv/bin/python scripts/bench_check.py --port ... --trials 20 --channel A [--ask]
        Feed trials: one calibrated pocket cycle per trial on one channel, timing each.
        With --ask, type how many tokens actually fell after each cycle (CYCLE_DONE only
        means the motion finished). Results go to a CSV (default data/bench/).

Try it without hardware against the simulator:
    .venv/bin/python scripts/virtual_controller.py      # prints a port path
"""
from __future__ import annotations

import argparse
import csv
import json
import queue
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V = 3
MAX_LINE = 256
STATUS_FIELDS = ("mA", "mB", "hA", "hB", "door", "servo", "rev", "txn", "fault")
MOTOR_STATES = {"idle", "moving", "homing", "fault"}


class CheckFailed(Exception):
    pass


class Link:
    """Owns the port: a reader thread queues parsed lines; a pinger keeps the watchdog fed."""

    def __init__(self, port: str, baud: int, verbose: bool = False):
        import serial  # pyserial
        self.ser = serial.Serial(port, baud, timeout=0.05, write_timeout=1, exclusive=True)
        self.verbose = verbose
        self.inbox: queue.Queue[tuple[float, dict, int]] = queue.Queue()
        self.session: str | None = None
        self.last_status: dict = {}
        self.problems: list[str] = []  # framing problems seen at any time
        self.pinging = threading.Event()
        self.pinging.set()
        self.closed = False
        self._lock = threading.Lock()
        self._next_id = 1000
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._ping, daemon=True).start()

    def new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _read(self) -> None:
        buf = b""
        while not self.closed:
            try:
                chunk = self.ser.read(256)
            except Exception as exc:
                self.problems.append(f"serial read failed: {exc}")
                return
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                raw = raw.rstrip(b"\r")
                if not raw:
                    continue
                size = len(raw) + 1
                text = raw.decode("utf-8", errors="replace")
                if self.verbose:
                    print(f"    <- {text}")
                try:
                    msg = json.loads(text)
                    if not isinstance(msg, dict):
                        raise ValueError
                except ValueError:
                    # Boot ROM chatter before the sketch starts is normal; anything later is not.
                    if self.session:
                        self.problems.append(f"non-JSON line: {text[:60]!r}")
                    continue
                if size > MAX_LINE:
                    self.problems.append(f"{size}-byte line (limit {MAX_LINE}): {text[:40]}...")
                if msg.get("v") != V:
                    self.problems.append(f"line with v={msg.get('v')!r}: {text[:60]}")
                if any("lid" in str(k).lower() for k in msg):
                    self.problems.append("a message has a lid field; the tray top has no sensor")
                if msg.get("s"):
                    self.session = msg["s"]
                if msg.get("event") in ("PONG", "STATUS"):
                    self.last_status = msg
                self.inbox.put((time.monotonic(), msg, size))

    def _ping(self) -> None:
        while not self.closed:
            if self.pinging.is_set():
                self.write({"cmd": "PING"}, quiet=True)
            time.sleep(0.25)

    def write(self, msg: dict, quiet: bool = False, raw: dict | None = None) -> None:
        body = raw if raw is not None else {"v": V, **({"s": self.session} if self.session else {}), **msg}
        line = json.dumps(body, separators=(",", ":"))
        if self.verbose and not quiet:
            print(f"    -> {line}")
        with self._lock:
            try:
                self.ser.write((line + "\n").encode())
            except Exception as exc:
                self.problems.append(f"serial write failed: {exc}")

    def drain(self) -> None:
        while not self.inbox.empty():
            self.inbox.get_nowait()

    def expect(self, pred, timeout: float, what: str) -> dict:
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise CheckFailed(f"no {what} within {timeout:g} s")
            try:
                _, msg, _ = self.inbox.get(timeout=min(left, 0.1))
            except queue.Empty:
                continue
            if pred(msg):
                return msg

    def quiet_for(self, pred, seconds: float) -> dict | None:
        """Return the first matching message within `seconds`, or None if none arrived."""
        try:
            return self.expect(pred, seconds, "")
        except CheckFailed:
            return None

    def command(self, cmd: str, timeout: float = 2.0, **fields) -> dict:
        """Send a command with a fresh id and wait for its ACK or NACK."""
        cid = fields.pop("id", None) or self.new_id()
        self.write({"id": cid, "cmd": cmd, **fields})
        return self.expect(lambda m: m.get("id") == cid and m.get("event") in ("ACK", "NACK"), timeout,
                           f"ACK/NACK for {cmd} id {cid}")

    def status(self) -> dict:
        cid = self.new_id()
        self.write({"id": cid, "cmd": "STATUS"})
        return self.expect(lambda m: m.get("event") == "STATUS", 2, "STATUS reply")

    def close(self) -> None:
        self.closed = True
        time.sleep(0.1)
        self.ser.close()


# ------------------------------------------------------------------------------- checks
def nack(reply: dict, reason: str) -> None:
    if reply.get("event") != "NACK" or reply.get("reason") != reason:
        raise CheckFailed(f"expected NACK {reason}, got {reply.get('event')} {reply.get('reason') or ''}".strip())


def wait_idle(link: Link, timeout: float) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = link.status()
        if st.get("mA") == "idle" and st.get("mB") == "idle":
            return st
        time.sleep(0.2)
    raise CheckFailed(f"motors not idle after {timeout:g} s: {link.last_status}")


def recover(link: Link, timeout: float) -> str:
    """RESET and wait for the new session's HELLO. Returns the new session."""
    old = link.session
    link.drain()
    r = link.command("RESET")
    if r.get("event") != "ACK":
        raise CheckFailed(f"RESET was not accepted: {r}")
    hello = link.expect(lambda m: m.get("event") == "HELLO", timeout, "HELLO after RESET")
    if hello.get("s") == old:
        raise CheckFailed("RESET kept the old session")
    wait_idle(link, timeout)
    return hello["s"]


def check_link(link: Link, a) -> str:
    link.drain()
    pong = link.expect(lambda m: m.get("event") == "PONG", 3, "PONG (is the board running the sketch?)")
    if not re.fullmatch(r"[0-9a-f]{4}", str(pong.get("s", ""))):
        raise CheckFailed(f"session should be 4 hex characters, got {pong.get('s')!r}")
    missing = [f for f in STATUS_FIELDS if f not in pong]
    if missing:
        raise CheckFailed(f"PONG is missing status fields {missing}")
    for m in ("mA", "mB"):
        if pong[m] not in MOTOR_STATES:
            raise CheckFailed(f"{m}={pong[m]!r} is not one of {sorted(MOTOR_STATES)}")
    if pong["servo"] not in ("L", "U"):
        raise CheckFailed(f"servo={pong['servo']!r}, expected L or U")
    if not isinstance(pong["rev"], int):
        raise CheckFailed("rev must be an integer")
    st = link.status()
    if st.get("s") != link.session:
        raise CheckFailed("STATUS reply carries a different session")
    door = "closed" if pong["door"] else "OPEN"
    return f"session {pong['s']}, door {door}, home A={pong['hA']} B={pong['hB']}, rev {pong['rev']}"


def check_ready(link: Link, a) -> str:
    st = link.status()
    if st.get("fault"):
        recover(link, a.reset_timeout)
        st = link.status()
    if st.get("fault"):
        raise CheckFailed(f"fault {st['fault']} persists after RESET")
    if not st.get("door"):
        raise CheckFailed("access door is open; close it (the door switch must read 1) and rerun")
    if st.get("mA") != "idle" or st.get("mB") != "idle" or not st.get("hA") or not st.get("hB"):
        raise CheckFailed(f"rotors not idle at home: {st}")
    return "idle, homed, no fault, door closed"


def check_rejections(link: Link, a) -> str:
    cid = link.new_id()
    link.write({}, raw={"v": 2, "s": link.session, "id": cid, "cmd": "STATUS"})
    nack(link.expect(lambda m: m.get("event") == "NACK" and m.get("id") == cid, 2, "NACK for v=2"), "BAD_VERSION")
    cid = link.new_id()
    link.write({}, raw={"v": V, "s": "zzzz", "id": cid, "cmd": "STATUS"})
    nack(link.expect(lambda m: m.get("event") == "NACK" and m.get("id") == cid, 2, "NACK for wrong session"),
         "BAD_SESSION")
    link.write({"cmd": "DISPENSE", "t": "bench0", "c": "A", "n": 1})
    nack(link.expect(lambda m: m.get("event") == "NACK", 2, "NACK for a missing id"), "MISSING_ID")
    nack(link.command("SPIN"), "UNKNOWN_CMD")
    nack(link.command("DISPENSE", t="bench0", c="C", n=1), "UNKNOWN_CHANNEL")
    nack(link.command("DISPENSE", t="bench0", c="A", n=0), "BAD_QUANTITY")
    nack(link.command("DISPENSE", t="bench0", c="A", n=99), "BAD_QUANTITY")
    rev = link.status()["rev"]
    nack(link.command("RELEASE", t="bench0", rev=rev + 1000), "BAD_REV")
    if link.status().get("servo") != "L":
        raise CheckFailed("servo is not commanded locked after a rejected RELEASE")
    return "BAD_VERSION, BAD_SESSION, MISSING_ID, UNKNOWN_CMD, UNKNOWN_CHANNEL, BAD_QUANTITY x2, BAD_REV"


def dispense_once(link: Link, channel: str, txn: str, a, cid: int | None = None) -> dict:
    """One pocket cycle. Returns timings and revisions."""
    before = link.status()["rev"]
    cid = cid or link.new_id()
    t0 = time.monotonic()
    link.write({"id": cid, "cmd": "DISPENSE", "t": txn, "c": channel, "n": 1})
    reply = link.expect(lambda m: m.get("id") == cid and m.get("event") in ("ACK", "NACK", "FAULT"), a.ack_timeout,
                        f"ACK for DISPENSE {channel}")
    if reply.get("event") != "ACK":
        raise CheckFailed(f"DISPENSE {channel} got {reply.get('event')} {reply.get('reason', '')}")
    t_ack = time.monotonic()
    done = link.expect(lambda m: m.get("event") in ("CYCLE_DONE", "FAULT") and m.get("id") in (cid, None),
                       a.cycle_timeout, f"CYCLE_DONE for {channel}")
    t_done = time.monotonic()
    if done["event"] == "FAULT":
        raise CheckFailed(f"FAULT {done.get('reason')} during {channel} cycle")
    for k, want in (("t", txn), ("c", channel)):
        if done.get(k) != want:
            raise CheckFailed(f"CYCLE_DONE has {k}={done.get(k)!r}, expected {want!r}")
    m_key, h_key = ("mA", "hA") if channel == "A" else ("mB", "hB")
    if done.get(m_key) != "idle" or done.get(h_key) != 1:
        raise CheckFailed(f"CYCLE_DONE should carry status fields with {m_key}=idle and {h_key}=1 "
                          f"(got {m_key}={done.get(m_key)!r}, {h_key}={done.get(h_key)!r})")
    if not isinstance(done.get("rev"), int) or done["rev"] <= before:
        raise CheckFailed(f"tray revision did not increase across the cycle ({before} -> {done.get('rev')})")
    wait_idle(link, 3)
    return {"id": cid, "ack_ms": round((t_ack - t0) * 1000), "cycle_ms": round((t_done - t0) * 1000),
            "rev_before": before, "rev_after": done["rev"]}


def check_dispense(channel: str):
    def run(link: Link, a) -> str:
        r = dispense_once(link, channel, f"bnch{channel.lower()}1", a)
        return f"ACK {r['ack_ms']} ms, CYCLE_DONE {r['cycle_ms']} ms, rev {r['rev_before']} -> {r['rev_after']}"
    run.__name__ = f"check_dispense_{channel}"
    return run


def check_duplicates(link: Link, a) -> str:
    r = dispense_once(link, "A", "bnchd1", a)
    rev = link.status()["rev"]
    link.drain()
    link.write({"id": r["id"], "cmd": "DISPENSE", "t": "bnchd1", "c": "A", "n": 1})
    dup = link.expect(lambda m: m.get("id") == r["id"] and m.get("event") in ("ACK", "NACK"), 2, "reply to the repeat")
    if dup.get("event") != "ACK" or dup.get("dup") != 1:
        raise CheckFailed(f"identical repeat should get ACK dup:1, got {dup}")
    moved = link.quiet_for(lambda m: m.get("event") == "CYCLE_DONE" or m.get("mA") == "moving", 1.5)
    if moved or link.status()["rev"] != rev:
        raise CheckFailed("the identical repeat moved the rotor again")
    link.write({"id": r["id"], "cmd": "DISPENSE", "t": "bnchd1", "c": "A", "n": 2})
    nack(link.expect(lambda m: m.get("id") == r["id"] and m.get("event") in ("ACK", "NACK"), 2,
                     "reply to a changed payload"), "ID_REUSED")
    return "identical repeat -> ACK dup:1 without motion; changed payload -> ID_REUSED"


def check_release_once(link: Link, a) -> str:
    st = link.status()
    if not st.get("door"):
        raise CheckFailed("door open; cannot test release")
    rev = st["rev"]
    link.drain()
    r = link.command("RELEASE", t="bnchr1", rev=rev)
    if r.get("event") != "ACK":
        raise CheckFailed(f"RELEASE with the current rev got {r.get('event')} {r.get('reason', '')}")
    rel = link.expect(lambda m: m.get("event") == "RELEASED", 2, "RELEASED")
    if rel.get("servo") != "U" or rel.get("t") != "bnchr1":
        raise CheckFailed(f"RELEASED should carry t and servo:U, got {rel}")
    nack(link.command("RELEASE", t="bnchr1", rev=rev), "ALREADY_RELEASED")
    r = link.command("LOCK")
    if r.get("event") != "ACK":
        raise CheckFailed(f"LOCK got {r.get('event')} {r.get('reason', '')} (is the door closed?)")
    link.expect(lambda m: m.get("event") == "LOCKED" and m.get("servo") == "L", 2, "LOCKED")
    return "unlocked once; second release -> ALREADY_RELEASED; relocked"


def check_stop(link: Link, a) -> str:
    link.drain()
    r = link.command("DISPENSE", t="bnchs1", c="A", n=a.max_n, timeout=a.ack_timeout)
    if r.get("event") != "ACK":
        raise CheckFailed(f"DISPENSE for the STOP test got {r.get('event')} {r.get('reason', '')}")
    t0 = time.monotonic()
    cid = link.new_id()
    link.write({"id": cid, "cmd": "STOP"})
    ev = link.expect(lambda m: m.get("event") in ("STOPPED", "CYCLE_DONE"), 1, "STOPPED")
    ms = round((time.monotonic() - t0) * 1000)
    if ev["event"] == "CYCLE_DONE":
        recover(link, a.reset_timeout)
        raise CheckFailed(f"the {a.max_n}-pocket motion finished before STOP took effect")
    st = link.status()
    if st.get("mA") == "moving" or st.get("mB") == "moving":
        raise CheckFailed("a motor still reports moving after STOP")
    if st.get("fault") != "STOPPED":
        raise CheckFailed(f"fault should be STOPPED, got {st.get('fault')!r}")
    nack(link.command("DISPENSE", t="bnchs2", c="A", n=1), "FAULT")
    if link.quiet_for(lambda m: m.get("event") == "CYCLE_DONE", 0.5):
        raise CheckFailed("a CYCLE_DONE arrived after STOP")
    s = recover(link, a.reset_timeout)
    return f"STOPPED in {ms} ms mid-motion; new commands refused; RESET -> new session {s}"


def check_watchdog(link: Link, a) -> str:
    link.drain()
    r = link.command("DISPENSE", t="bnchw1", c="B", n=a.max_n, timeout=a.ack_timeout)
    if r.get("event") != "ACK":
        raise CheckFailed(f"DISPENSE for the watchdog test got {r.get('event')} {r.get('reason', '')}")
    link.pinging.clear()
    try:
        ev = link.quiet_for(lambda m: m.get("event") in ("FAULT", "CYCLE_DONE"), 2.5)
    finally:
        link.pinging.set()
    if ev is None:
        raise CheckFailed("no FAULT within 2.5 s of silence")
    if ev["event"] == "CYCLE_DONE":
        recover(link, a.reset_timeout)
        raise CheckFailed("the motion finished before the watchdog fired; it should stop within 1 s without PING "
                          "(if one pocket takes under 1 s, rerun with a slower STEP_INTERVAL_US)")
    if ev.get("reason") != "HEARTBEAT_LOST":
        raise CheckFailed(f"expected FAULT HEARTBEAT_LOST, got {ev.get('reason')}")
    s = recover(link, a.reset_timeout)
    return f"motion stopped with HEARTBEAT_LOST; RESET -> new session {s}"


CHECKS = [
    ("link", check_link, False),
    ("ready", check_ready, False),
    ("rejections", check_rejections, False),
    ("dispense_A", check_dispense("A"), False),
    ("dispense_B", check_dispense("B"), False),
    ("duplicate_ids", check_duplicates, False),
    ("release_once", check_release_once, False),
    ("stop_mid_motion", check_stop, False),
    ("watchdog", check_watchdog, True),  # skipped by --quick
]


def run_checks(link: Link, a) -> int:
    failed = 0
    for name, fn, slow in CHECKS:
        if a.only and name not in a.only:
            continue
        if slow and a.quick:
            print(f"  SKIP  {name:<16} (--quick)")
            continue
        try:
            detail = fn(link, a)
            print(f"  PASS  {name:<16} {detail}", flush=True)
        except CheckFailed as exc:
            failed += 1
            print(f"  FAIL  {name:<16} {exc}", flush=True)
            if name in ("link", "ready"):
                print("        stopping: later checks need a working, idle controller")
                break
            try:  # leave the controller usable for the next check
                st = link.status()
                if st.get("fault") or st.get("mA") != "idle" or st.get("mB") != "idle":
                    recover(link, a.reset_timeout)
            except CheckFailed:
                pass
    if link.problems:
        failed += 1
        print("  FAIL  framing          " + "; ".join(dict.fromkeys(link.problems)))
    else:
        print("  PASS  framing          every line was v3 JSON, under 256 bytes, no lid field")
    return failed


def run_trials(link: Link, a) -> int:
    path = Path(a.csv) if a.csv else ROOT / "data" / "bench" / (
        f"trials-{a.channel}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    check_ready(link, a)
    fw = None
    rows, ok_cycles, delivered_ok = [], 0, 0
    print(f"  {a.trials} feed trials on channel {a.channel}. Session {link.session}.")
    for i in range(1, a.trials + 1):
        row = {"trial": i, "channel": a.channel, "session": link.session, "started_utc":
               datetime.now(timezone.utc).isoformat(timespec="seconds"), "ack_ms": "", "cycle_ms": "",
               "rev_before": "", "rev_after": "", "outcome": "", "tokens_observed": "", "note": ""}
        try:
            r = dispense_once(link, a.channel, f"trl{i:03d}", a)
            row.update({k: r[k] for k in ("ack_ms", "cycle_ms", "rev_before", "rev_after")}, outcome="CYCLE_DONE")
            ok_cycles += 1
        except CheckFailed as exc:
            row.update(outcome="FAIL", note=str(exc))
            try:
                recover(link, a.reset_timeout)
            except CheckFailed as exc2:
                row["note"] += f"; recovery failed: {exc2}"
        if a.ask:
            ans = input(f"  trial {i}: tokens that fell (0/1/2/..., blank = not counted): ").strip()
            if ans.isdigit():
                row["tokens_observed"] = int(ans)
                delivered_ok += int(ans) == 1 and row["outcome"] == "CYCLE_DONE"
        rows.append(row)
        print(f"  trial {i:>3}: {row['outcome']:<10} {row['cycle_ms'] or '-':>6} ms  {row['note']}", flush=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    times = [r["cycle_ms"] for r in rows if r["outcome"] == "CYCLE_DONE"]
    print(f"\n  Channel {a.channel}: {ok_cycles}/{a.trials} cycles completed", end="")
    if times:
        print(f", cycle time mean {sum(times) / len(times):.0f} ms, max {max(times)} ms", end="")
    print(".")
    counted = [r for r in rows if r["tokens_observed"] != ""]
    if counted:
        print(f"  Exactly one token delivered: {delivered_ok}/{len(counted)} counted trials.")
    else:
        print("  Token delivery not counted (CYCLE_DONE alone does not prove a token fell; use --ask).")
    print(f"  Saved {path}")
    return 0 if ok_cycles == a.trials else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True, help="e.g. /dev/cu.usbserial-0001 (ls /dev/cu.*)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--quick", action="store_true", help="skip the slow watchdog check")
    ap.add_argument("--only", nargs="*", help=f"run only these checks: {' '.join(n for n, _, _ in CHECKS)}")
    ap.add_argument("--trials", type=int, help="run N feed trials instead of the conformance checks")
    ap.add_argument("--channel", choices=("A", "B"), default="A")
    ap.add_argument("--ask", action="store_true", help="ask how many tokens fell after each trial")
    ap.add_argument("--csv", help="where to write trial results")
    ap.add_argument("--max-n", type=int, default=3, help="largest quantity the firmware accepts (default 3)")
    ap.add_argument("--ack-timeout", type=float, default=2.0)
    ap.add_argument("--cycle-timeout", type=float, default=8.0, help="seconds for one pocket cycle")
    ap.add_argument("--reset-timeout", type=float, default=20.0, help="seconds for RESET and rehoming")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every line sent and received")
    a = ap.parse_args()

    try:
        link = Link(a.port, a.baud, a.verbose)
    except Exception as exc:
        print(f"Could not open {a.port}: {exc}\nIs the DoseGuard app (or a serial monitor) still holding the port?")
        return 2
    try:
        # Opening the port resets most ESP32 boards; give the sketch time to boot and say HELLO.
        try:
            link.expect(lambda m: m.get("event") in ("HELLO", "PONG"), 6, "")
        except CheckFailed:
            pass
        print(f"DoseGuard bench check on {a.port} at {a.baud} baud")
        if a.trials:
            return run_trials(link, a)
        failed = run_checks(link, a)
        print(f"\n{'All checks passed.' if not failed else f'{failed} check(s) failed.'}")
        return 1 if failed else 0
    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main())
