"""Read-only metrics from the local event store (design doc section 12).

Two separate measures, never mixed:
  - reminder delivery delay: when a reminder actually became due in the app, minus its
    scheduled time. Large values mean the laptop slept or the scheduler fell behind.
  - dispense cycle duration: from a DISPENSE command being written to the controller to its CYCLE_DONE, per
    cartridge, with the outcome of every command (done, unknown, rejected...). CYCLE_DONE means
    the motion finished; it does not prove a token fell.

Like the CSV export, this only reads copied rows. It has no access to the serial port or the
workflow engine. Demo and ordinary records stay separate."""
from __future__ import annotations

import statistics

from .store import Store
from .timeutil import parse


def _ms(start: str | None, end: str | None) -> int | None:
    a, b = parse(start), parse(end)
    if a is None or b is None:
        return None
    return round((b - a).total_seconds() * 1000)


def _summary(values: list[int]) -> dict:
    if not values:
        return {"count": 0, "median_ms": None, "max_ms": None, "min_ms": None}
    return {"count": len(values), "median_ms": round(statistics.median(values)), "max_ms": max(values), "min_ms": min(values)}


def reminder_delays(store: Store, demo: int, limit: int = 200) -> list[dict]:
    rows = store.q(
        "SELECT e.ts, r.reminder_id, r.dose_id, r.due_at_utc FROM events e JOIN reminders r ON r.reminder_id = e.reminder_id"
        " WHERE e.kind = 'REMINDER_DUE' AND e.demo = ? ORDER BY e.event_id DESC LIMIT ?", (demo, limit))
    out = []
    for r in rows:
        ms = _ms(r["due_at_utc"], r["ts"])
        if ms is not None:
            out.append({"reminder_id": r["reminder_id"], "dose_id": r["dose_id"], "due_at_utc": r["due_at_utc"],
                        "delivered_at_utc": r["ts"], "delay_ms": max(0, ms)})
    return out


def dispense_cycles(store: Store, demo: int, limit: int = 200) -> list[dict]:
    rows = store.q(
        "SELECT c.command_id, c.channel, c.state, c.detail, c.created_at, c.sent_at, c.completed_at, c.txn_id FROM commands c"
        " JOIN transactions t ON t.txn_id = c.txn_id"
        " WHERE c.kind = 'DISPENSE' AND t.demo = ? ORDER BY c.command_id DESC LIMIT ?", (demo, limit))
    # Timed from the moment the command was written to the controller. Rows from before sent_at was
    # recorded have no trustworthy start (a second cartridge waits for the first), so they get no duration.
    return [{"command_id": r["command_id"], "channel": r["channel"], "outcome": r["state"], "detail": r["detail"],
             "started_at_utc": r["sent_at"], "finished_at_utc": r["completed_at"], "txn_id": r["txn_id"],
             "duration_ms": _ms(r["sent_at"], r["completed_at"]) if r["state"] == "DONE" and r["sent_at"] else None}
            for r in rows]


def metrics(store: Store, simulated: bool, limit: int = 200) -> dict:
    demo = store.demo
    delays = reminder_delays(store, demo, limit)
    cycles = dispense_cycles(store, demo, limit)
    by_channel = {}
    for ch in ("A", "B"):
        mine = [c for c in cycles if c["channel"] == ch]
        outcomes: dict[str, int] = {}
        for c in mine:
            outcomes[c["outcome"]] = outcomes.get(c["outcome"], 0) + 1
        by_channel[ch] = {**_summary([c["duration_ms"] for c in mine if c["duration_ms"] is not None]),
                          "commands": len(mine), "outcomes": outcomes}
    return {
        "demo": bool(demo),
        "simulated_hardware": simulated,
        "reminder_delay": {**_summary([d["delay_ms"] for d in delays]), "late_over_60s": sum(d["delay_ms"] > 60000 for d in delays)},
        "dispense_cycles": by_channel,
        "recent_reminders": delays[:10],
        "recent_cycles": cycles[:10],
    }
