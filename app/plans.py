"""Plan validation and confirmation. A plan becomes active only after the user confirms
every entry, its delivery mode, quantity, recurring times and the timezone. Edits create
a new immutable version and only replace occurrences that have not been issued yet.

An entry is SCHEDULED (recurring local times, the default) or AS_NEEDED (no times; taken on
request within a minimum interval and a rolling 24 hour maximum, enforced by the server)."""
from __future__ import annotations

import re
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import WorkflowError
from .store import Store, dumps
from .timeutil import local_date, upcoming_dst_issues

DELIVERY_MODES = ("DEVICE", "REMINDER_ONLY")
SCHEDULES = ("SCHEDULED", "AS_NEEDED")
MIN_INTERVAL_HOURS = (1, 24)
MAX_PER_DAY = (1, 8)
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def slot_id(entry_key: str, hhmm: str) -> str:
    return f"{entry_key}@{hhmm.replace(':', '')}"


def is_as_needed(entry: dict) -> bool:
    """Entries stored before as-needed support have no schedule field: they are SCHEDULED."""
    return entry.get("schedule") == "AS_NEEDED"


def _whole_number(value) -> int | None:
    """A whole number from JSON (5 or "5"). Rejects booleans, fractions and text, so a safety
    limit is never silently rounded."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*\d{1,3}\s*", value):
        return int(value)
    return None


def validate_entries(store: Store, entries: list[dict], max_per_channel: int,
                     require_label: bool = False) -> tuple[list[dict], list[dict]]:
    """Returns (normalized entries, field errors)."""
    errors: list[dict] = []
    classes = store.class_by_id()
    out: list[dict] = []
    if not entries:
        errors.append({"field": "entries", "message": "Add at least one entry."})
    seen_keys: set[str] = set()
    for i, raw in enumerate(entries):
        def err(field: str, msg: str) -> None:
            errors.append({"entry": i, "field": field, "message": msg})

        label = str(raw.get("label") or "").strip()
        if not 1 <= len(label) <= 60:
            err("label", "Label is required (up to 60 characters).")
        strength = str(raw.get("strength_text") or "").strip()[:40] or None
        try:
            qty = int(raw.get("quantity"))
        except (TypeError, ValueError):
            qty = 0
        if not 1 <= qty <= max_per_channel:
            err("quantity", f"Quantity must be between 1 and {max_per_channel}.")
        mode = str(raw.get("delivery_mode") or "").upper()
        if mode not in DELIVERY_MODES:
            err("delivery_mode", "Choose Device or Reminder only.")
        class_id = raw.get("class_id") or None
        cartridge = None
        if mode == "DEVICE":
            if class_id not in classes or not classes[class_id]["cartridge"]:
                err("class_id", "Device entries need a token class mapped to a cartridge.")
            else:
                cartridge = classes[class_id]["cartridge"]
        else:
            class_id = None
        schedule = str(raw.get("schedule") or "SCHEDULED").upper()
        if schedule not in SCHEDULES:
            err("schedule", "Choose Scheduled or As needed.")
        times = [str(t).strip() for t in (raw.get("times") or [])]
        min_interval = max_day = None
        if schedule == "AS_NEEDED":
            if times:
                err("times", "As-needed entries have no times. Remove the times or choose Scheduled.")
            min_interval = _whole_number(raw.get("min_interval_hours"))
            lo, hi = MIN_INTERVAL_HOURS
            if min_interval is None or not lo <= min_interval <= hi:
                err("min_interval_hours", f"Set the minimum time between doses: a whole number of hours from {lo} to {hi}.")
            max_day = _whole_number(raw.get("max_per_day"))
            lo, hi = MAX_PER_DAY
            if max_day is None or not lo <= max_day <= hi:
                err("max_per_day", f"Set the most doses in 24 hours: a whole number from {lo} to {hi}.")
        else:
            if not times:
                err("times", "Add at least one time.")
            bad = [t for t in times if not HHMM.match(t)]
            if bad:
                err("times", "Times must look like 08:00 (24 hour).")
            if len(set(times)) != len(times):
                err("times", "Times must not repeat.")
            if len(times) > 6:
                err("times", "At most six times per entry.")
        label_id = str(raw.get("label_id") or "").strip() or None
        if label_id and not store.q1("SELECT 1 FROM label_scans WHERE label_id=?", (label_id,)):
            label_id = None
        if require_label and not label_id:
            err("label_id", "Show this medication's label to the camera (or upload a photo) before confirming.")
        if not raw.get("confirmed"):
            err("confirmed", "Confirm this entry after reviewing it.")
        key = str(raw.get("entry_key") or "").strip() or uuid.uuid4().hex[:8]
        if not re.match(r"^[A-Za-z0-9_-]{1,32}$", key) or key in seen_keys:
            key = uuid.uuid4().hex[:8]
        seen_keys.add(key)
        out.append({
            "entry_key": key, "label": label, "strength_text": strength, "quantity": qty,
            "delivery_mode": mode, "class_id": class_id, "cartridge": cartridge,
            "times": sorted(times) if schedule != "AS_NEEDED" else [], "label_id": label_id,
            "schedule": schedule, "min_interval_hours": min_interval, "max_per_day": max_day,
        })
    # One shared tray: same-time device entries are dispensed together; bound per channel.
    per_slot: dict[tuple[str, str], int] = {}
    for e in out:
        if e["delivery_mode"] != "DEVICE" or not e["cartridge"]:
            continue
        for t in e["times"]:
            per_slot[(t, e["cartridge"])] = per_slot.get((t, e["cartridge"]), 0) + e["quantity"]
    for (t, cart), n in per_slot.items():
        if n > max_per_channel:
            errors.append({"field": "quantity", "message": f"At {t}, cartridge {cart} would need {n} tokens; the limit is {max_per_channel}."})
    return out, errors


def confirm_plan(store: Store, reminders, *, request: dict, max_per_channel: int, require_label: bool = False) -> dict:
    tz_name = str(request.get("timezone") or "")
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise WorkflowError(422, "VALIDATION", "Unknown timezone.", errors=[{"field": "timezone", "message": "Unknown timezone."}])
    if not request.get("timezone_confirmed"):
        raise WorkflowError(422, "VALIDATION", "Confirm the timezone.", errors=[{"field": "timezone", "message": "Confirm the timezone."}])
    entries, errors = validate_entries(store, request.get("entries") or [], max_per_channel, require_label)
    if errors:
        raise WorkflowError(422, "VALIDATION", "Some fields need attention.", errors=errors)

    demo = store.demo
    current = store.active_plan(demo)
    base = request.get("base_plan_version_id")
    if current and base != current["plan_version_id"]:
        raise WorkflowError(409, "PLAN_VERSION_MISMATCH", "The plan changed since you opened it. Reload and review again.")
    if not current and base not in (None, 0):
        raise WorkflowError(409, "PLAN_VERSION_MISMATCH", "The plan changed since you opened it.")

    person_id = request.get("person_id") or None
    if not person_id or not store.person(person_id):  # every medicine, including reminder-only ones
        raise WorkflowError(422, "NOT_ENROLLED", "Add your face before adding medicines.",
                            errors=[{"field": "person_id", "message": "Add your face first."}])

    all_times = sorted({t for e in entries for t in e["times"]})
    issues = upcoming_dst_issues(all_times, tz, local_date(store.clock.now(), tz))
    if issues and not request.get("dst_reviewed"):
        raise WorkflowError(422, "DST_REVIEW", "Some times fall in a daylight saving change. Review them before confirming.",
                            errors=[{"field": "times", "message": f"{i['date']} {i['time']}: {i['issue'].lower()} local time"} for i in issues[:6]],
                            dst_issues=issues)

    now = store.now()
    with store.tx() as conn:
        if current:
            plan_id, version = current["plan_id"], current["version"] + 1
            conn.execute("UPDATE plan_versions SET active=0 WHERE plan_id=?", (plan_id,))
        else:
            plan_id, version = uuid.uuid4().hex[:10], 1
        cur = conn.execute(
            "INSERT INTO plan_versions (plan_id, version, person_id, timezone, entries_json, confirmed_at, active, demo)"
            " VALUES (?,?,?,?,?,?,1,?)",
            (plan_id, version, person_id, tz_name, dumps(entries), now, demo),
        )
        pv_id = int(cur.lastrowid)
        # Replace only future occurrences that were never issued.
        stale = conn.execute(
            "SELECT d.dose_id, d.occurrence_id FROM doses d JOIN reminders r ON r.dose_id=d.dose_id"
            " WHERE d.demo=? AND d.workflow_state='NOT_STARTED' AND r.state='SCHEDULED' AND d.due_at_utc>?",
            (demo, now),
        ).fetchall()
        for row in stale:
            conn.execute("DELETE FROM reminders WHERE dose_id=?", (row["dose_id"],))
            conn.execute("DELETE FROM doses WHERE dose_id=?", (row["dose_id"],))
        store.event(conn, "PLAN_CONFIRMED", details={
            "plan_id": plan_id, "version": version, "plan_version_id": pv_id,
            "entries": len(entries), "superseded_future_occurrences": len(stale),
        })
    reminders.materialize()
    return {"plan_id": plan_id, "version": version, "plan_version_id": pv_id, "dst_issues": issues}
