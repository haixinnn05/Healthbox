"""Builds the versioned snapshot the browser polls. Reading only."""
from __future__ import annotations

import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from . import as_needed
from .plans import is_as_needed
from .reasons import message
from .timeutil import fmt_local, iso, local_date, parse
from .vision import look_alike_pairs


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
        "collected_by": row.get("collected_by"), "action": row.get("action"),
        "earlier_person": row.get("handed_over_name") if row.get("handed_over_at") else None,
        # As-needed doses have no reminder.
        "reminder": None if row["reminder_id"] is None else {
            "reminder_id": row["reminder_id"], "state": row["r_state"], "snoozed_until": row["snoozed_until"],
            "snoozed_until_local": fmt_local(parse(row["snoozed_until"]), tz),
            "sound_repeat_count": row["sound_repeat_count"], "acknowledged_at": row["acknowledged_at"],
            "close_reason": row["close_reason"],
        },
    }


DOSE_SQL = (
    "SELECT d.*, r.reminder_id, r.state AS r_state, r.snoozed_until, r.sound_repeat_count, r.acknowledged_at, r.close_reason,"
    " pv.handed_over_at, pv.handed_over_name"
    " FROM doses d LEFT JOIN reminders r ON r.dose_id=d.dose_id"
    " LEFT JOIN plan_versions pv ON pv.plan_version_id=d.plan_version_id"
)
CURRENT = " AND pv.handed_over_at IS NULL"  # leave out doses of someone Healthbox was handed over from


def today_cards(services) -> list[dict]:
    store = services.store
    tz = tz_for(services)
    demo = store.demo
    today = local_date(store.clock.now(), tz).isoformat()
    rows = store.q(
        DOSE_SQL + " WHERE d.demo=? AND d.schedule='SCHEDULED' AND (d.local_date=? OR r.state IN ('DUE','SNOOZED','NEEDS_REVIEW')"
        " OR d.workflow_state IN ('PERSON_CHECK','EMPTY_TRAY_CHECK','DISPENSING','DOSE_CHECK','RECIPIENT_RECHECK','RELEASE_PENDING',"
        "'RELEASED','FAULT','UNCERTAIN'))" + CURRENT + " ORDER BY d.due_at_utc, d.label",
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
        DOSE_SQL + " WHERE d.demo=? AND r.state='SCHEDULED' AND d.due_at_utc>=?" + CURRENT + " ORDER BY d.due_at_utc LIMIT 1",
        (demo, iso(now)))
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
    snap = {
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
        "laptop_sound": services.sound.status(),
        "laptop_voice": services.voice.status(),
        "watch": watch_status(services),
        "followups": followups(services, tz, now),
        "cartridges": cartridge_status(store, plan),
        "look_alike": look_alike_pairs(store.token_classes()),
        "look_alike_blocks": not services.vision.tray_simulated,
        "ai": services.ai.status(),
        "account": services.account.brief(),  # exists, name, unlocked only
        "signed_out": False,
        "public_demo": s.demo_reset_minutes if s.public_demo else None,
        "config": {"hardware_mode": s.hardware_mode, "camera_mode": s.camera_mode,
                   "snooze_minutes": s.snooze_minutes, "followup_minutes": s.followup_minutes,
                   "hand_check": services.engine.hand_check_on, "sound_max": s.sound_max, "needs_review_minutes": s.needs_review_minutes},
    }
    return signed_out_view(snap) if services.account.signed_out() else snap


DOSE_ONLY_STATES = ("PERSON_CHECK", "EMPTY_TRAY_CHECK", "DISPENSING", "DOSE_CHECK", "RECIPIENT_RECHECK", "RELEASE_PENDING",
                    "RELEASED", "FAULT", "UNCERTAIN")


def signed_out_view(snap: dict) -> dict:
    """Signed out, the page gets only what taking a due dose needs: due and in-progress doses, the dose
    steps, follow-ups, the taking watch, the device and camera. No plan, no other medicines, no records."""
    snap = dict(snap)
    snap["today"] = [c for c in snap["today"] if c["workflow_state"] in DOSE_ONLY_STATES or
                     (c["workflow_state"] == "NOT_STARTED" and (c["reminder"] or {}).get("state") in ("DUE", "SNOOZED", "NEEDS_REVIEW"))]
    snap["as_needed"] = []
    snap["next_dose"] = None
    snap["plan"] = None
    snap["cartridges"] = []
    snap["signed_out"] = True
    return snap


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
                   " AND kind NOT LIKE 'ACCOUNT\\_%' ESCAPE '\\'"
                   " ORDER BY event_id DESC LIMIT ?", (store.demo, limit))
    for r in rows:
        r["ts_local"] = fmt_local(parse(r["ts"]), tz)
    return rows


def followups(services, tz: ZoneInfo, now) -> list[dict]:
    """Released doses to ask about: no "I took it" yet and no hand-to-mouth movement seen,
    FOLLOWUP_MINUTES after release (or after "Not yet"). Shown for 12 hours at most."""
    store = services.store
    rows = store.q(
        DOSE_SQL + " WHERE d.demo=? AND d.delivery_mode='DEVICE' AND d.workflow_state IN ('RELEASED','COMPLETE')"
        " AND d.self_report IS NULL AND COALESCE(d.action,'')!='OBSERVED' AND d.followup_after IS NOT NULL"
        " AND d.followup_after<=? AND d.followup_after>=?" + CURRENT + " ORDER BY d.due_at_utc",
        (store.demo, iso(now), iso(now - timedelta(hours=12))))
    classes = store.class_by_id()
    out = []
    for r in rows:
        c = _card(r, tz, classes)
        # Still in the tray (the dose was never collected) asks a different question.
        c["followup"] = "collect" if r["workflow_state"] == "RELEASED" or r["collection"] == "PENDING" else "taken"
        out.append(c)
    return out


def cartridge_status(store, plan: dict | None) -> list[dict]:
    """Estimated pills left per cartridge. The count is set when the cartridge is filled and lowered
    by each finished dispense motion, so it is an estimate, never a measurement."""
    stock = {r["cartridge"]: r for r in store.q("SELECT * FROM cartridge_stock")}
    per_day: dict[str, int] = {}
    used: set[str] = set()
    for e in (plan or {}).get("entries", []):
        cart = e.get("cartridge")
        if e.get("delivery_mode") != "DEVICE" or not cart:
            continue
        used.add(cart)
        if not is_as_needed(e):
            per_day[cart] = per_day.get(cart, 0) + int(e.get("quantity") or 0) * len(e.get("times") or [])
    out = []
    for c in store.token_classes():
        cart = c["cartridge"]
        row = stock.get(cart)
        count = row["count"] if row else None
        need = per_day.get(cart, 0)
        low = count is not None and cart in used and (count == 0 or count <= max(3, 2 * need))
        out.append({"cartridge": cart, "class_id": c["class_id"], "label": c["label"], "color": c["color"],
                    "count": count, "filled_at": row["filled_at"] if row else None, "per_day": need,
                    "days_left": None if count is None or not need else round(count / need, 1),
                    "used": cart in used, "low": low, "empty": count == 0})
    return out


def watch_status(services) -> dict | None:
    """The live collection watch, with the doses it is for (so the page can offer "I took it")."""
    w = services.vision.watch_view()
    if not w:
        return None
    classes = services.store.class_by_id()
    rows = services.store.q("SELECT dose_id, label, quantity, class_id, self_report, action, demo FROM doses WHERE txn_id=?",
                            (w["txn_id"],))
    w["doses"] = [{"dose_id": r["dose_id"], "label": r["label"], "quantity": r["quantity"], "self_report": r["self_report"],
                   "action": r["action"], "class_color": (classes.get(r["class_id"]) or {}).get("color")}
                  for r in rows if r["demo"] == services.store.demo]
    return w
