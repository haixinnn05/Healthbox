"""Builds the versioned snapshot the browser polls. Reading only."""
from __future__ import annotations

import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from . import as_needed
from .plans import is_as_needed
from .reasons import message
from .timeutil import fmt_local, iso, local_date, parse


def tz_for(services) -> ZoneInfo:
    plan = services.store.active_plan()
    return ZoneInfo(plan["timezone"] if plan else services.settings.timezone)


def _day_label(local_iso: str, tz: ZoneInfo) -> str:
    from datetime import date, datetime
    today = datetime.now(tz).date()
    d = date.fromisoformat(local_iso)
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%a %-d %b")


def _card(row: dict, tz: ZoneInfo, classes: dict) -> dict:
    due = parse(row["due_at_utc"])
    cls = classes.get(row["class_id"]) if row["class_id"] else None
    return {
        "dose_id": row["dose_id"], "occurrence_id": row["occurrence_id"], "label": row["label"],
        "strength_text": row["strength_text"], "quantity": row["quantity"], "class_id": row["class_id"],
        "class_label": cls["label"] if cls else None, "class_color": cls["color"] if cls else None,
        "cartridge": row["cartridge"], "delivery_mode": row["delivery_mode"], "plan_version_id": row["plan_version_id"],
        "due_at_utc": row["due_at_utc"], "due_local": fmt_local(due, tz), "local_date": row["local_date"],
        "due_day": _day_label(row["local_date"], tz),
        "workflow_state": row["workflow_state"], "txn_id": row["txn_id"], "outcome": row["outcome"],
        "collection": row["collection"], "self_report": row["self_report"], "review_reason": row["review_reason"],
        "schedule": row["schedule"], "as_needed": row["schedule"] == "AS_NEEDED",
        # As-needed doses have no reminder.
        "reminder": None if row["reminder_id"] is None else {
            "reminder_id": row["reminder_id"], "state": row["r_state"], "snoozed_until": row["snoozed_until"],
            "snoozed_until_local": fmt_local(parse(row["snoozed_until"]), tz),
            "sound_repeat_count": row["sound_repeat_count"], "acknowledged_at": row["acknowledged_at"],
            "close_reason": row["close_reason"],
        },
    }


DOSE_SQL = (
    "SELECT d.*, r.reminder_id, r.state AS r_state, r.snoozed_until, r.sound_repeat_count, r.acknowledged_at, r.close_reason"
    " FROM doses d LEFT JOIN reminders r ON r.dose_id=d.dose_id"
)


def today_cards(services) -> list[dict]:
    store = services.store
    tz = tz_for(services)
    demo = store.demo
    today = local_date(store.clock.now(), tz).isoformat()
    rows = store.q(
        DOSE_SQL + " WHERE d.demo=? AND d.schedule='SCHEDULED' AND (d.local_date=? OR r.state IN ('DUE','SNOOZED','NEEDS_REVIEW')"
        " OR d.workflow_state IN ('PERSON_CHECK','EMPTY_TRAY_CHECK','DISPENSING','DOSE_CHECK','RECIPIENT_RECHECK','RELEASE_PENDING',"
        "'RELEASED','FAULT','UNCERTAIN')) ORDER BY d.due_at_utc, d.label",
        (demo, today),
    )
    classes = store.class_by_id()
    return [_card(r, tz, classes) for r in rows]


def build_status(services) -> dict:
    store, s = services.store, services.settings
    now = store.clock.now()
    tz = tz_for(services)
    demo = store.demo
    cards = today_cards(services)
    upcoming = store.q1(
        DOSE_SQL + " WHERE d.demo=? AND r.state='SCHEDULED' AND d.due_at_utc>=? ORDER BY d.due_at_utc LIMIT 1", (demo, iso(now)))
    sounds = store.q(
        "SELECT event_id, reminder_id, ts FROM events WHERE kind='REMINDER_ALERT' AND demo=? AND ts>=? ORDER BY event_id",
        (demo, iso(now - timedelta(seconds=30))),
    )
    plan = store.active_plan()
    person = store.person(plan["person_id"]) if plan else None
    wf = services.engine.snapshot()
    device = services.hardware.status()
    camera = services.camera.status()
    pose = (wf.get("txn") or {}).get("pose")
    return {
        "state_version": store.version,
        "server_time": iso(now),
        "server_time_local": now.astimezone(tz).strftime("%A %-d %B, %-I:%M:%S %p"),
        "timezone": str(tz.key),
        "demo_mode": bool(demo),
        "today": cards,
        "as_needed": as_needed_items(store, plan, tz, now),
        "next_dose": _card(upcoming, tz, store.class_by_id()) if upcoming else None,
        "due_count": sum(1 for c in cards if c["reminder"]["state"] in ("DUE", "NEEDS_REVIEW")),
        "sounds": sounds,
        "last_event_id": (store.q1("SELECT MAX(event_id) AS m FROM events") or {}).get("m") or 0,
        "plan": {"plan_id": plan["plan_id"], "version": plan["version"], "plan_version_id": plan["plan_version_id"],
                 "confirmed_at": plan["confirmed_at"], "entries": plan["entries"],
                 "recipient": person["label"] if person else None} if plan else None,
        "workflow": wf,
        "device": device,
        "simulator": services.hardware.sim_state() if hasattr(services.hardware, "sim_state") else None,
        "camera": dict(camera, pose_required=pose),
        "token_classes": [{"class_id": c["class_id"], "label": c["label"], "cartridge": c["cartridge"], "color": c["color"],
                           "shape": c["hsv"].get("shape")} for c in store.token_classes()],
        "vision": services.vision.status(),
        "ai": services.ai.status(),
        "config": {"hardware_mode": s.hardware_mode, "camera_mode": s.camera_mode,
                   "snooze_minutes": s.snooze_minutes, "sound_max": s.sound_max, "needs_review_minutes": s.needs_review_minutes},
    }


def as_needed_items(store, plan: dict | None, tz: ZoneInfo, now) -> list[dict]:
    """One item per AS_NEEDED entry of the active plan, with its server-side availability."""
    if not plan:
        return []
    classes = store.class_by_id()
    out = []
    for e in plan["entries"]:
        if not is_as_needed(e):
            continue
        lim = as_needed.evaluate(store, e, plan["demo"], now)
        cls = classes.get(e.get("class_id")) if e.get("class_id") else None
        last = lim["last_dose"]
        code = lim["blocked_reason"]
        when = as_needed.when_local(parse(lim["next_available_at"]), tz, now)
        out.append({
            "entry_key": e["entry_key"], "plan_version_id": plan["plan_version_id"], "label": e["label"],
            "strength_text": e.get("strength_text"), "quantity": e["quantity"], "delivery_mode": e["delivery_mode"],
            "class_id": e.get("class_id"), "class_label": cls["label"] if cls else None,
            "class_color": cls["color"] if cls else None, "cartridge": e.get("cartridge"),
            "min_interval_hours": e["min_interval_hours"], "max_per_day": e["max_per_day"],
            "taken_last_24h": lim["taken_last_24h"],
            "last_taken_at": lim["last_taken_at"], "last_taken_local": as_needed.when_local(parse(lim["last_taken_at"]), tz, now),
            "available": lim["available"], "next_available_at": lim["next_available_at"], "next_available_local": when,
            "blocked_reason": code, "blocked_message": message(code, when=when) if code else None,
            "in_progress_dose_id": lim["in_progress_dose_id"],
            "last_dose": None if not last else {
                "dose_id": last["dose_id"], "requested_at": last["created_at"], "workflow_state": last["workflow_state"],
                "outcome": last["outcome"], "collection": last["collection"], "self_report": last["self_report"],
                "txn_id": last["txn_id"]},
        })
    return out


def history(services, limit: int = 50) -> list[dict]:
    store = services.store
    tz = tz_for(services)
    rows = store.q(
        DOSE_SQL + " WHERE d.demo=? AND (r.state!='SCHEDULED' OR d.workflow_state!='NOT_STARTED' OR d.schedule='AS_NEEDED')"
        " ORDER BY d.due_at_utc DESC LIMIT ?", (store.demo, min(max(limit, 1), 200)))
    classes = store.class_by_id()
    return [_card(r, tz, classes) for r in rows]


def dose_detail(services, dose_id: str) -> dict | None:
    store = services.store
    tz = tz_for(services)
    row = store.q1(DOSE_SQL + " WHERE d.dose_id=?", (dose_id,))
    if not row or row["demo"] != store.demo:
        return None
    card = _card(row, tz, store.class_by_id())
    txns = [t["txn_id"] for t in store.q("SELECT txn_id FROM transactions WHERE dose_ids_json LIKE ?", (f'%"{dose_id}"%',))]
    marks = ",".join("?" * len(txns)) or "''"
    events = store.q(f"SELECT * FROM events WHERE dose_id=? OR reminder_id=? OR txn_id IN ({marks}) ORDER BY event_id",
                     (dose_id, row["reminder_id"], *txns))
    for e in events:
        e["details"] = json.loads(e.pop("details_json") or "null")
        e["ts_local"] = fmt_local(parse(e["ts"]), tz)
    obs = store.q("SELECT * FROM observations WHERE txn_id=? ORDER BY obs_id", (row["txn_id"] or "-",))
    for o in obs:
        o["details"] = json.loads(o.pop("details_json") or "{}")
        o.pop("frame_ids_json", None)
        o["details"].pop("similarities", None)
    cmds = store.q("SELECT command_id, kind, channel, state, detail, created_at, completed_at FROM commands WHERE txn_id=? ORDER BY command_id",
                   (row["txn_id"] or "-",))
    return {"dose": card, "events": events, "observations": obs, "commands": cmds}


def recent_events(services, limit: int = 8) -> list[dict]:
    store = services.store
    tz = tz_for(services)
    rows = store.q("SELECT event_id, ts, kind, reason_code FROM events WHERE demo=? AND kind NOT IN ('OBSERVATION','OCCURRENCE_CREATED')"
                   " ORDER BY event_id DESC LIMIT ?", (store.demo, limit))
    for r in rows:
        r["ts_local"] = fmt_local(parse(r["ts"]), tz)
    return rows
