"""Reminder scheduler. Persists one reminder per occurrence and drives its notification
states. It never touches hardware: snooze and dismissal only change notifications.

States: SCHEDULED, DUE, SNOOZED, ACKNOWLEDGED, NEEDS_REVIEW, CLOSED."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from .config import Settings
from .errors import WorkflowError
from .plans import is_as_needed, slot_id
from .store import Store
from .timeutil import iso, local_date, parse, parse_hhmm, resolve_local

log = logging.getLogger("doseguard.reminders")

OPEN_STATES = ("SCHEDULED", "DUE", "SNOOZED")
UNRESOLVED_STATES = ("DUE", "SNOOZED", "ACKNOWLEDGED", "NEEDS_REVIEW")
# Dose workflow states that mean a physical workflow is under way (alerts pause).
ACTIVE_WORKFLOW = ("PERSON_CHECK", "EMPTY_TRAY_CHECK", "DISPENSING", "DOSE_CHECK",
                   "RECIPIENT_RECHECK", "RELEASE_PENDING", "FAULT", "UNCERTAIN")


class ReminderService:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self.clock = store.clock
        self._task: asyncio.Task | None = None
        self.on_alert = None  # called after each reminder sound event (the laptop chime); never affects state

    # -- lifecycle ------------------------------------------------------------
    async def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # keep the scheduler alive; the error is logged
                log.exception("reminder tick failed")
            await asyncio.sleep(self.settings.scheduler_tick_s)

    def start(self) -> None:
        self.restore()
        self._task = asyncio.create_task(self.run(), name="reminder-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def restore(self) -> None:
        """After a restart: keep unresolved reminders, but never burst-replay missed sounds."""
        now = self.clock.now()
        nxt = iso(now + timedelta(seconds=self.settings.sound_interval_s))
        with self.store.tx() as conn:
            rows = conn.execute(
                "SELECT reminder_id FROM reminders WHERE state='DUE' AND next_alert_at IS NOT NULL AND next_alert_at<?",
                (iso(now),),
            ).fetchall()
            for r in rows:
                conn.execute("UPDATE reminders SET next_alert_at=?, updated_at=? WHERE reminder_id=?",
                             (nxt, iso(now), r["reminder_id"]))
            unresolved = conn.execute(
                f"SELECT COUNT(*) AS n FROM reminders WHERE state IN ({','.join('?' * len(UNRESOLVED_STATES))})",
                UNRESOLVED_STATES,
            ).fetchone()["n"]
            self.store.event(conn, "REMINDERS_RESTORED", details={"unresolved": unresolved, "rescheduled_sounds": len(rows)})

    # -- occurrences ----------------------------------------------------------------
    def materialize(self) -> int:
        """Create today's and tomorrow's occurrences for each active plan. Uniqueness on
        occurrence_id (slot + local date) makes this safe to call every tick."""
        created = 0
        now = self.clock.now()
        for demo in (0, 1):
            plan = self.store.active_plan(demo)
            if not plan:
                continue
            tz = ZoneInfo(plan["timezone"])
            confirmed_at = parse(plan["confirmed_at"])
            today = local_date(now, tz)
            with self.store.tx() as conn:
                for day in (today, today + timedelta(days=1)):
                    for e in plan["entries"]:
                        if is_as_needed(e):
                            continue  # taken on request only: no occurrences, no reminders
                        for hhmm in e["times"]:
                            sid = slot_id(e["entry_key"], hhmm)
                            occ = f"{sid}|{day.isoformat()}"
                            due, issue = resolve_local(day, parse_hhmm(hhmm), tz)
                            if due < confirmed_at:
                                continue  # slot did not exist yet when this version was confirmed
                            created += self._insert_occurrence(conn, plan, e, sid, occ, day.isoformat(), hhmm, due, issue, demo)
        return created

    def _insert_occurrence(self, conn, plan, e, sid, occ, day, hhmm, due, issue, demo) -> int:
        dose_id = uuid.uuid4().hex[:12]
        now = self.store.now()
        cur = conn.execute(
            "INSERT OR IGNORE INTO doses (dose_id, occurrence_id, slot_id, local_date, local_time, entry_key, plan_version_id,"
            " label, strength_text, quantity, class_id, cartridge, delivery_mode, due_at_utc, review_reason, demo, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (dose_id, occ, sid, day, hhmm, e["entry_key"], plan["plan_version_id"], e["label"], e.get("strength_text"),
             e["quantity"], e.get("class_id"), e.get("cartridge"), e["delivery_mode"], iso(due),
             f"DST_{issue}" if issue else None, demo, now, now),
        )
        if cur.rowcount != 1:
            return 0
        reminder_id = uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO reminders (reminder_id, occurrence_id, dose_id, state, due_at_utc, demo, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (reminder_id, occ, dose_id, "SCHEDULED", iso(due), demo, now),
        )
        self.store.event(conn, "OCCURRENCE_CREATED", dose_id=dose_id, reminder_id=reminder_id, demo=demo,
                         details={"occurrence_id": occ, "due_at_utc": iso(due), "dst_issue": issue})
        return 1

    # -- scheduler tick ----------------------------------------------------------------
    def tick(self) -> None:
        self.materialize()
        now = self.clock.now()
        s = self.settings
        rows = self.store.q(
            "SELECT r.*, d.workflow_state, d.review_reason FROM reminders r JOIN doses d ON d.dose_id=r.dose_id"
            " WHERE r.state IN ('SCHEDULED','DUE','SNOOZED')"
        )
        for r in rows:
            due = parse(r["due_at_utc"])
            busy = r["workflow_state"] in ACTIVE_WORKFLOW
            if r["state"] == "SCHEDULED" and now >= due:
                late = now - due > timedelta(minutes=s.needs_review_minutes)
                if r["review_reason"]:
                    self._to_review(r, r["review_reason"])
                elif late:
                    self._to_review(r, "LATE")
                else:
                    self._become_due(r, now)
            elif r["state"] == "SNOOZED" and now >= parse(r["snoozed_until"]):
                self._become_due(r, now, after_snooze=True)
            elif r["state"] == "DUE" and not busy:
                active_since = parse(r["active_since"]) or due
                if now - active_since >= timedelta(minutes=s.needs_review_minutes):
                    self._to_review(r, "NO_RESPONSE")
                elif r["next_alert_at"] and now >= parse(r["next_alert_at"]) and r["sound_repeat_count"] < s.sound_max:
                    self._emit(r, now)

    def _become_due(self, r: dict, now, after_snooze: bool = False) -> None:
        with self.store.tx() as conn:
            changed = conn.execute(
                "UPDATE reminders SET state='DUE', active_since=?, sound_repeat_count=0, snoozed_until=NULL, next_alert_at=?, updated_at=?"
                " WHERE reminder_id=? AND state=?",
                (iso(now), iso(now), iso(now), r["reminder_id"], r["state"]),
            ).rowcount
            if changed:
                self.store.event(conn, "REMINDER_DUE", dose_id=r["dose_id"], reminder_id=r["reminder_id"], demo=r["demo"],
                                 details={"after_snooze": after_snooze})
        if changed and r["workflow_state"] not in ACTIVE_WORKFLOW:
            fresh = self.store.q1("SELECT * FROM reminders WHERE reminder_id=?", (r["reminder_id"],))
            self._emit(fresh, now)

    def _emit(self, r: dict, now) -> None:
        s = self.settings
        count = r["sound_repeat_count"] + 1
        nxt = iso(now + timedelta(seconds=s.sound_interval_s)) if count < s.sound_max else None
        with self.store.tx() as conn:
            changed = conn.execute(
                "UPDATE reminders SET sound_repeat_count=?, last_emitted_at=?, next_alert_at=?, updated_at=?"
                " WHERE reminder_id=? AND state='DUE' AND sound_repeat_count=?",
                (count, iso(now), nxt, iso(now), r["reminder_id"], r["sound_repeat_count"]),
            ).rowcount
            if changed:
                self.store.event(conn, "REMINDER_ALERT", dose_id=r["dose_id"], reminder_id=r["reminder_id"], demo=r["demo"],
                                 details={"sound": True, "repeat": count, "scheduled_at": r["next_alert_at"],
                                          "delay_ms": _delay_ms(r["next_alert_at"], now)})
        if changed and self.on_alert:
            try:
                self.on_alert(r)
            except Exception:
                pass  # a sound problem must never change reminder state

    def _to_review(self, r: dict, reason: str) -> None:
        now = self.store.now()
        with self.store.tx() as conn:
            changed = conn.execute(
                "UPDATE reminders SET state='NEEDS_REVIEW', next_alert_at=NULL, updated_at=? WHERE reminder_id=? AND state=?",
                (now, r["reminder_id"], r["state"]),
            ).rowcount
            if changed:
                self.store.event(conn, "REMINDER_NEEDS_REVIEW", dose_id=r["dose_id"], reminder_id=r["reminder_id"],
                                 reason=reason, demo=r["demo"])

    # -- user actions (notification only) ----------------------------------------------------
    def _idempotent(self, request_id: str, route: str, fn):
        if not request_id or len(request_id) > 64:
            raise WorkflowError(422, "VALIDATION", "request_id is required.")
        with self.store.tx() as conn:
            prev = conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if prev:
                if prev["route"] != route:
                    raise WorkflowError(409, "REQUEST_ID_REUSED")
                return json.loads(prev["response_json"])
            result = fn(conn)
            self.store.save_request(conn, request_id, route, 200, result)
            return result

    def _load(self, conn, reminder_id: str) -> dict:
        row = conn.execute(
            "SELECT r.*, d.workflow_state, d.delivery_mode FROM reminders r JOIN doses d ON d.dose_id=r.dose_id WHERE reminder_id=?",
            (reminder_id,),
        ).fetchone()
        if not row:
            raise WorkflowError(404, "NOT_FOUND", "Reminder not found.")
        row = dict(row)
        if row["demo"] != self.store.demo_flag(conn):
            raise WorkflowError(409, "DEMO_MISMATCH")
        return row

    def snooze(self, request_id: str, reminder_id: str) -> dict:
        def run(conn):
            r = self._load(conn, reminder_id)
            if r["state"] != "DUE":
                raise WorkflowError(409, "INVALID_TRANSITION", "Only a due alert can be snoozed.")
            until = self.clock.now() + timedelta(minutes=self.settings.snooze_minutes)
            conn.execute("UPDATE reminders SET state='SNOOZED', snoozed_until=?, next_alert_at=NULL, updated_at=? WHERE reminder_id=?",
                         (iso(until), self.store.now(), reminder_id))
            self.store.event(conn, "REMINDER_SNOOZED", dose_id=r["dose_id"], reminder_id=reminder_id,
                             details={"snoozed_until": iso(until)})
            return {"reminder_id": reminder_id, "state": "SNOOZED", "snoozed_until": iso(until)}
        return self._idempotent(request_id, "reminders/snooze", run)

    def acknowledge(self, request_id: str, reminder_id: str) -> dict:
        def run(conn):
            r = self._load(conn, reminder_id)
            if r["state"] not in ("DUE", "SNOOZED", "NEEDS_REVIEW"):
                raise WorkflowError(409, "INVALID_TRANSITION", "This reminder has no active alert to dismiss.")
            now = self.store.now()
            conn.execute("UPDATE reminders SET state='ACKNOWLEDGED', acknowledged_at=?, next_alert_at=NULL, snoozed_until=NULL,"
                         " updated_at=? WHERE reminder_id=?", (now, now, reminder_id))
            self.store.event(conn, "REMINDER_ACKNOWLEDGED", dose_id=r["dose_id"], reminder_id=reminder_id,
                             details={"previous": r["state"], "note": "notification dismissed only; not taken or collected"})
            return {"reminder_id": reminder_id, "state": "ACKNOWLEDGED"}
        return self._idempotent(request_id, "reminders/ack", run)

    def mark_taken_self_report(self, request_id: str, dose_id: str) -> dict:
        """REMINDER_ONLY entries: an explicitly unverified self report. Never sets camera checks."""
        def run(conn):
            d = conn.execute("SELECT * FROM doses WHERE dose_id=?", (dose_id,)).fetchone()
            if not d:
                raise WorkflowError(404, "NOT_FOUND", "Dose not found.")
            if d["delivery_mode"] != "REMINDER_ONLY":
                raise WorkflowError(409, "INVALID_TRANSITION", "Device doses are confirmed through verification.")
            if d["demo"] != self.store.demo_flag(conn):
                raise WorkflowError(409, "DEMO_MISMATCH")
            if d["self_report"]:
                raise WorkflowError(409, "INVALID_TRANSITION", "Already reported.")
            r = conn.execute("SELECT * FROM reminders WHERE dose_id=?", (dose_id,)).fetchone()
            if r is None:  # as-needed doses are reported when requested, never later
                raise WorkflowError(409, "INVALID_TRANSITION")
            if r["state"] == "SCHEDULED":
                raise WorkflowError(409, "NOT_DUE")
            self.record_self_report(conn, dose_id, r["reminder_id"])
            return {"dose_id": dose_id, "self_report": "TAKEN_UNVERIFIED"}
        return self._idempotent(request_id, "doses/self-report", run)

    def record_self_report(self, conn, dose_id: str, reminder_id: str | None, **details) -> str:
        """The one write for an unverified "I took it" report. Never touches camera checks or hardware."""
        now = self.store.now()
        conn.execute("UPDATE doses SET self_report='TAKEN_UNVERIFIED', self_report_at=?, outcome='USER_REPORTED_TAKEN', updated_at=?"
                     " WHERE dose_id=?", (now, now, dose_id))
        if reminder_id:
            conn.execute("UPDATE reminders SET state='CLOSED', closed_at=?, close_reason='USER_REPORTED_TAKEN', next_alert_at=NULL,"
                         " updated_at=? WHERE dose_id=?", (now, now, dose_id))
        self.store.event(conn, "SELF_REPORT", dose_id=dose_id, reminder_id=reminder_id,
                         details={"report": "TAKEN_UNVERIFIED", "verified": False, **details})
        return now

    def close_for_dose(self, conn, dose_id: str, reason: str) -> None:
        """Called by the workflow engine inside its transaction (for example after release)."""
        now = self.store.now()
        r = conn.execute("SELECT reminder_id, state FROM reminders WHERE dose_id=?", (dose_id,)).fetchone()
        if r and r["state"] != "CLOSED":
            conn.execute("UPDATE reminders SET state='CLOSED', closed_at=?, close_reason=?, next_alert_at=NULL, updated_at=?"
                         " WHERE dose_id=?", (now, reason, now, dose_id))
            self.store.event(conn, "REMINDER_CLOSED", dose_id=dose_id, reminder_id=r["reminder_id"], reason=reason)

    # -- demo -----------------------------------------------------------------------------
    def trigger_demo(self, request_id: str, entry_keys: list[str] | None, delay_s: int = 30) -> dict:
        def run(conn):
            if not self.store.demo_flag(conn):
                raise WorkflowError(409, "DEMO_MISMATCH", "Turn on demo mode first.")
            plan = self.store.active_plan(1)
            if not plan:
                raise WorkflowError(409, "INVALID_TRANSITION", "Confirm a demo plan first.")
            entries = [e for e in plan["entries"] if (not entry_keys or e["entry_key"] in entry_keys) and not is_as_needed(e)]
            if not entries:
                raise WorkflowError(422, "VALIDATION", "No matching demo entries.")
            tz = ZoneInfo(plan["timezone"])
            due = self.clock.now().replace(microsecond=0) + timedelta(seconds=delay_s)
            local = due.astimezone(tz)
            tag = uuid.uuid4().hex[:6]
            made = []
            for e in entries:
                sid = f"demo-{tag}-{e['entry_key']}"
                occ = f"{sid}|{local.date().isoformat()}"
                if self._insert_occurrence(conn, plan, e, sid, occ, local.date().isoformat(), local.strftime("%H:%M"), due, None, 1):
                    made.append(e["entry_key"])
            self.store.event(conn, "DEMO_REMINDER_TRIGGERED", details={"due_at_utc": iso(due), "entries": made})
            return {"due_at_utc": iso(due), "entries": made}
        return self._idempotent(request_id, "demo/trigger", run)


def _delay_ms(scheduled_iso: str | None, now) -> int | None:
    sched = parse(scheduled_iso)
    if not sched:
        return None
    return int((now - sched).total_seconds() * 1000)
