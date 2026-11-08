"""Safety limits for AS_NEEDED ("as needed") entries. Reading only.

A dose counts toward the limits whenever medicine may have been delivered: it is in a
workflow (even before dispensing, so two requests cannot race), it was released or
completed, it ended faulted or uncertain, or the user reported taking it. Only a dose
whose verification was cancelled before anything was dispensed (workflow back to
NOT_STARTED, no self report) does not count.

Each counted dose has a reference time, the moment the medicine may have become available:
  released  -> when the tray was released
  reported  -> when the user reported taking it (reminder-only)
  otherwise -> the latest reservation (release, else dispense, else the request itself)
min_interval_hours runs from the latest reference time; max_per_day counts reference
times in the rolling 24 hours before now."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .store import Store
from .timeutil import fmt_local, iso, parse

WINDOW = timedelta(hours=24)
IN_PROGRESS = ("PERSON_CHECK", "EMPTY_TRAY_CHECK", "DISPENSING", "DOSE_CHECK", "RECIPIENT_RECHECK", "RELEASE_PENDING")

COUNTED_SQL = (
    "SELECT d.dose_id, d.workflow_state, d.outcome, d.self_report, d.self_report_at, d.created_at, d.collection,"
    " d.delivery_mode, d.txn_id, t.dispense_reserved_at, t.release_reserved_at,"
    " (SELECT MIN(e.ts) FROM events e WHERE e.kind='RELEASED' AND e.txn_id=d.txn_id) AS released_at"
    " FROM doses d LEFT JOIN transactions t ON t.txn_id=d.txn_id"
    " WHERE d.schedule='AS_NEEDED' AND d.entry_key=? AND d.demo=?"
    " AND (d.workflow_state!='NOT_STARTED' OR d.self_report IS NOT NULL)"
    " AND d.plan_version_id NOT IN (SELECT plan_version_id FROM plan_versions WHERE handed_over_at IS NOT NULL)"
)


def reference_time(row: dict) -> datetime:
    if row.get("released_at"):
        return parse(row["released_at"])
    if row.get("self_report_at") and row.get("delivery_mode") == "REMINDER_ONLY":
        return parse(row["self_report_at"])
    for key in ("release_reserved_at", "dispense_reserved_at", "created_at"):
        if row.get(key):
            return parse(row[key])
    return parse(row["created_at"])


def counted_doses(store: Store, entry_key: str, demo: int) -> list[dict]:
    rows = store.q(COUNTED_SQL, (entry_key, demo))
    for r in rows:
        r["ref_time"] = reference_time(r)
    rows.sort(key=lambda r: r["ref_time"])
    return rows


def evaluate(store: Store, entry: dict, demo: int, now: datetime) -> dict:
    """Availability for one AS_NEEDED entry at `now`."""
    rows = counted_doses(store, entry["entry_key"], demo)
    interval = timedelta(hours=int(entry["min_interval_hours"]))
    max_day = int(entry["max_per_day"])
    in_window = [r["ref_time"] for r in rows if r["ref_time"] > now - WINDOW]
    last = rows[-1]["ref_time"] if rows else None
    in_progress = next((r for r in rows if r["workflow_state"] in IN_PROGRESS), None)

    reason, waits = None, []
    if len(in_window) >= max_day:
        # The oldest doses roll out of the window first; enough must roll out to leave room for one.
        waits.append(in_window[len(in_window) - max_day] + WINDOW)
        reason = "DAILY_LIMIT"
    if last and now < last + interval:
        waits.append(last + interval)
        reason = reason or "TOO_SOON"
    if in_progress:
        reason = "AS_NEEDED_IN_PROGRESS"
    next_at = max(waits) if waits else None
    latest = store.q1("SELECT dose_id, workflow_state, outcome, collection, self_report, txn_id, created_at"
                      " FROM doses WHERE schedule='AS_NEEDED' AND entry_key=? AND demo=?"
                      " AND plan_version_id NOT IN (SELECT plan_version_id FROM plan_versions WHERE handed_over_at IS NOT NULL)"
                      " ORDER BY created_at DESC, rowid DESC LIMIT 1",
                      (entry["entry_key"], demo))
    return {
        "taken_last_24h": len(in_window),
        "last_taken_at": iso(last),
        "available": reason is None,
        "next_available_at": iso(next_at) if reason else None,
        "blocked_reason": reason,
        "in_progress_dose_id": in_progress["dose_id"] if in_progress else None,
        "last_dose": latest,
    }


def when_local(dt: datetime | None, tz: ZoneInfo, now: datetime) -> str | None:
    """'6:15 PM', 'tomorrow at 6:15 PM', 'yesterday at 6:15 PM' or 'Mon 9 Nov at 6:15 PM' in the plan's timezone."""
    if dt is None:
        return None
    day, today = dt.astimezone(tz).date(), now.astimezone(tz).date()
    clock = fmt_local(dt, tz)
    if day == today:
        return clock
    if day == today + timedelta(days=1):
        return f"tomorrow at {clock}"
    if day == today - timedelta(days=1):
        return f"yesterday at {clock}"
    return f"{dt.astimezone(tz).strftime('%a %-d %b')} at {clock}"
