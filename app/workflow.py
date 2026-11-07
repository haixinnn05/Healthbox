"""The rules engine. The only component that can enqueue movement commands.

Physical workflow states (per transaction):
  PERSON_CHECK -> EMPTY_TRAY_CHECK -> DISPENSING -> DOSE_CHECK -> RECIPIENT_RECHECK
  -> RELEASE_PENDING -> COLLECTION_CHECK -> COMPLETE
  with FAULT and UNCERTAIN blocking new movement, and terminal CANCELLED,
  ENDED_FAULT, ENDED_UNCERTAIN.

A transaction loads one shared tray: every due DEVICE dose with the same due time.
Reminder state is separate and never advances this workflow."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from zoneinfo import ZoneInfo

from . import as_needed
from .config import Settings
from .errors import WorkflowError
from .hardware import HardwareAdapter
from .plans import is_as_needed
from .reasons import block, message
from .reminders import ReminderService
from .store import Store, dumps
from .timeutil import iso, parse
from .vision import Observation, VisionService

log = logging.getLogger("doseguard.workflow")

ACTIVE = ("PERSON_CHECK", "EMPTY_TRAY_CHECK", "DISPENSING", "DOSE_CHECK", "RECIPIENT_RECHECK",
          "RELEASE_PENDING", "COLLECTION_CHECK", "FAULT", "UNCERTAIN")
FINAL = ("COMPLETE", "CANCELLED", "ENDED_FAULT", "ENDED_UNCERTAIN")
CHECK_KIND = {"PERSON_CHECK": "FACE_MATCH", "EMPTY_TRAY_CHECK": "TRAY_EMPTY",
              "DOSE_CHECK": "CLASS_COUNTS", "COLLECTION_CHECK": "TRAY_EMPTY"}
STAGES = {
    "PERSON_CHECK": ("Recipient check", "FACE", "Check recipient"),
    "EMPTY_TRAY_CHECK": ("Empty tray check", "TRAY", "Check empty tray"),
    "DISPENSING": ("Dispensing", "TRAY", None),
    "DOSE_CHECK": ("Dose check", "TRAY", "Check dose"),
    "RECIPIENT_RECHECK": ("Recipient recheck and release", "FACE", "Recheck and release"),
    "RELEASE_PENDING": ("Releasing", "FACE", None),
    "COLLECTION_CHECK": ("Collection", "TRAY", "Check collection"),
    "FAULT": ("Supervised recovery needed", None, None),
    "UNCERTAIN": ("Outcome uncertain", None, None),
}


class WorkflowEngine:
    def __init__(self, store: Store, reminders: ReminderService, hardware: HardwareAdapter,
                 vision: VisionService, settings: Settings):
        self.store = store
        self.reminders = reminders
        self.hw = hardware
        self.vision = vision
        self.settings = settings
        self.clock = store.clock
        self.lock = asyncio.Lock()
        self.boot_id = uuid.uuid4().hex[:8]
        self.armed_session: str | None = None
        self.pending: dict | None = None
        self.lock_when_closed = False
        self.busy: str | None = None
        self.last_error: dict | None = None
        self._task: asyncio.Task | None = None
        hardware.set_listener(self.on_device_event)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.reconcile_startup()
        self._task = asyncio.create_task(self._watch(), name="workflow-watch")

    async def stop_task(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            try:
                await self.check_deadlines()
            except Exception:
                log.exception("deadline check failed")

    def reconcile_startup(self) -> None:
        """After a restart nothing replays. Possible motion requires supervised inspection."""
        with self.store.tx() as conn:
            for t in conn.execute(f"SELECT * FROM transactions WHERE state IN ({_qs(ACTIVE)})", ACTIVE).fetchall():
                t = dict(t)
                open_cmds = conn.execute(
                    "SELECT command_id FROM commands WHERE txn_id=? AND state IN ('RESERVED','SENT','ACKED')", (t["txn_id"],)
                ).fetchall()
                if t["state"] in ("DISPENSING", "RELEASE_PENDING") or open_cmds:
                    conn.execute("UPDATE commands SET state='UNKNOWN', detail='app restarted' WHERE txn_id=? AND state IN ('RESERVED','SENT','ACKED')",
                                 (t["txn_id"],))
                    self._set_state(conn, t, "UNCERTAIN", [block("RESTART_DURING_MOTION")])
                elif not t["dispense_reserved_at"]:
                    self._cancel(conn, t, "RESTART_BEFORE_DISPENSE")
                else:
                    self._set_blocks(conn, t["txn_id"], [block("DEVICE_RESTARTED")])
            self.store.event(conn, "ENGINE_STARTED", details={"boot_id": self.boot_id})

    # ------------------------------------------------------------------ helpers
    def active_txn(self, conn=None) -> dict | None:
        sql = f"SELECT * FROM transactions WHERE state IN ({_qs(ACTIVE)}) ORDER BY created_at DESC LIMIT 1"
        if conn is not None:
            row = conn.execute(sql, ACTIVE).fetchone()
            return dict(row) if row else None
        return self.store.q1(sql, ACTIVE)

    def _txn(self, txn_id: str) -> dict:
        t = self.store.q1("SELECT * FROM transactions WHERE txn_id=?", (txn_id,))
        if not t:
            raise WorkflowError(404, "NOT_FOUND", "Transaction not found.")
        if t["demo"] != self.store.demo:
            raise WorkflowError(409, "DEMO_MISMATCH")
        return t

    def _set_state(self, conn, txn: dict, state: str, blocks: list[dict] | None = None, outcome: str | None = None) -> None:
        conn.execute("UPDATE transactions SET state=?, blocks_json=?, outcome=COALESCE(?, outcome), updated_at=? WHERE txn_id=?",
                     (state, dumps(blocks or []), outcome, self.store.now(), txn["txn_id"]))
        dose_state = {"COLLECTION_CHECK": "RELEASED"}.get(state, state)
        if state not in ("CANCELLED",):
            conn.execute(f"UPDATE doses SET workflow_state=?, updated_at=? WHERE dose_id IN ({_qs(_ids(txn))})",
                         (dose_state, self.store.now(), *_ids(txn)))
        self.store.event(conn, "WORKFLOW_STATE", txn_id=txn["txn_id"], reason=(blocks or [{}])[0].get("code"),
                         details={"from": txn["state"], "to": state, "blocks": [b["code"] for b in blocks or []]})
        txn["state"] = state

    def _set_blocks(self, conn, txn_id: str, blocks: list[dict]) -> None:
        conn.execute("UPDATE transactions SET blocks_json=?, updated_at=? WHERE txn_id=?", (dumps(blocks), self.store.now(), txn_id))
        if blocks:
            self.store.event(conn, "WORKFLOW_BLOCKED", txn_id=txn_id, reason=blocks[0]["code"],
                             details={"blocks": [b["code"] for b in blocks]})

    def _cancel(self, conn, txn: dict, reason: str) -> None:
        conn.execute("UPDATE transactions SET state='CANCELLED', outcome=?, updated_at=? WHERE txn_id=?",
                     (reason, self.store.now(), txn["txn_id"]))
        conn.execute(f"UPDATE doses SET workflow_state='NOT_STARTED', txn_id=NULL, updated_at=? WHERE dose_id IN ({_qs(_ids(txn))})",
                     (self.store.now(), *_ids(txn)))
        # An as-needed dose is single use: cancelled before dispensing, it stays in history and never counts.
        conn.execute(f"UPDATE doses SET outcome='CANCELLED_BEFORE_DISPENSE', updated_at=? WHERE schedule='AS_NEEDED'"
                     f" AND dose_id IN ({_qs(_ids(txn))})", (self.store.now(), *_ids(txn)))
        self.store.event(conn, "WORKFLOW_CANCELLED", txn_id=txn["txn_id"], reason=reason)

    def _is_armed(self) -> bool:
        return bool(self.armed_session) and self.armed_session == self.hw.session and self.hw.connected

    def _device_blocks(self, need_armed: bool = True, need_door_closed: bool = True) -> list[dict]:
        hw, f = self.hw, self.hw.fields
        out = []
        if not hw.connected:
            return [block("DEVICE_DISCONNECTED")]
        if f["fault"]:
            out.append(block("DEVICE_FAULT", fault=f["fault"]))
        if need_armed and not self._is_armed():
            out.append(block("NOT_ARMED"))
        if not hw.motors_idle():
            out.append(block("MOTOR_BUSY"))
        if need_door_closed and f["door"] == 0:
            out.append(block("DOOR_OPEN"))
        return out

    def _save_obs(self, conn, txn: dict, obs: Observation) -> int:
        details = dict(obs.details)
        details["boot_id"] = self.boot_id
        if obs.message:
            details["message"] = obs.message
        cur = conn.execute(
            "INSERT INTO observations (txn_id, kind, result, reason_code, method, capture_time, capture_mono, frame_ids_json,"
            " tray_revision, controller_session, plan_version_id, details_json, simulated, demo, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (txn["txn_id"], obs.kind, obs.result, obs.reason_code, obs.method, iso(obs.capture_time), obs.capture_mono,
             dumps(obs.frame_ids[:20]), obs.tray_revision, obs.controller_session, txn.get("plan_version_id"),
             dumps(details), int(obs.simulated), txn["demo"], self.store.now()),
        )
        self.store.event(conn, "OBSERVATION", txn_id=txn["txn_id"], reason=obs.reason_code,
                         details={"kind": obs.kind, "result": obs.result, "simulated": obs.simulated,
                                  "observed": obs.details.get("observed"), "expected": obs.details.get("expected")})
        return int(cur.lastrowid)

    def _latest_obs(self, txn_id: str, kind: str) -> dict | None:
        row = self.store.q1("SELECT * FROM observations WHERE txn_id=? AND kind=? ORDER BY obs_id DESC LIMIT 1", (txn_id, kind))
        if row:
            row["details"] = json.loads(row.pop("details_json") or "{}")
        return row

    def _obs_age_s(self, obs: dict) -> float | None:
        if obs.get("details", {}).get("boot_id") != self.boot_id or obs.get("capture_mono") is None:
            return None  # evidence from another app run is never fresh
        return self.clock.monotonic() - obs["capture_mono"]

    async def _idempotent(self, request_id: str, route: str, fn):
        if not request_id or len(request_id) > 64:
            raise WorkflowError(422, "VALIDATION", "request_id is required.")
        async with self.lock:
            prev = self.store.get_request(request_id)
            if prev:
                if prev["route"] != route:
                    raise WorkflowError(409, "REQUEST_ID_REUSED")
                return json.loads(prev["response_json"])
            result = await fn()
            with self.store.tx() as conn:
                self.store.save_request(conn, request_id, route, 200, result)
            return result

    # ------------------------------------------------------------------ start
    def _start_preconditions(self, plan: dict | None) -> None:
        """Checks every new transaction passes, whether it starts from a due reminder or an as-needed request."""
        if self.active_txn():
            raise WorkflowError(409, "ANOTHER_DOSE_ACTIVE")
        blocks = self._device_blocks()
        if blocks:
            status = 503 if blocks[0]["code"] == "DEVICE_DISCONNECTED" else 409
            raise WorkflowError(status, blocks[0]["code"], blocks[0]["message"], reason_codes=[b["code"] for b in blocks])
        if not plan or not self.store.person(plan["person_id"]):
            raise WorkflowError(409, "NOT_ENROLLED")

    def _tray_totals(self, group: list[dict]) -> tuple[dict, dict]:
        by_class: dict[str, int] = {}
        by_channel: dict[str, int] = {}
        for g in group:
            by_class[g["class_id"]] = by_class.get(g["class_id"], 0) + g["quantity"]
            by_channel[g["cartridge"]] = by_channel.get(g["cartridge"], 0) + g["quantity"]
        if not by_channel or any(n > self.settings.max_tokens_per_channel or n < 1 for n in by_channel.values()):
            raise WorkflowError(409, "QUANTITY_LIMIT")
        return by_class, by_channel

    def _open_txn(self, conn, request_id: str, group: list[dict], plan: dict, demo: int, by_class: dict,
                  by_channel: dict, lead_dose_id: str) -> dict:
        """Create the transaction at PERSON_CHECK and attach its doses. Caller holds the store transaction."""
        txn_id = uuid.uuid4().hex[:12]
        short = secrets.token_hex(3)
        ids = [g["dose_id"] for g in group]
        now = self.store.now()
        conn.execute(
            "INSERT INTO transactions (txn_id, short_id, request_id, dose_ids_json, expected_json, state, controller_session,"
            " demo, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (txn_id, short, request_id, dumps(ids), dumps({"by_class": by_class, "by_channel": by_channel,
                                                           "plan_version_id": plan["plan_version_id"],
                                                           "person_id": plan["person_id"]}),
             "PERSON_CHECK", self.hw.session, demo, now, now),
        )
        n = conn.execute(f"UPDATE doses SET workflow_state='PERSON_CHECK', txn_id=?, updated_at=? WHERE dose_id IN ({_qs(ids)})"
                         " AND workflow_state='NOT_STARTED'", (txn_id, now, *ids)).rowcount
        if n != len(ids):
            raise WorkflowError(409, "ALREADY_STARTED")
        self.store.event(conn, "DOSE_STARTED", dose_id=lead_dose_id, txn_id=txn_id,
                         details={"doses": ids, "by_class": by_class, "by_channel": by_channel})
        return {"txn_id": txn_id, "state": "PERSON_CHECK", "dose_ids": ids}

    async def start_dose(self, request_id: str, dose_id: str, plan_version_id: int) -> dict:
        async def fn():
            d = self.store.q1("SELECT d.*, r.state AS reminder_state FROM doses d LEFT JOIN reminders r ON r.dose_id=d.dose_id"
                              " WHERE d.dose_id=?", (dose_id,))
            if not d:
                raise WorkflowError(404, "NOT_FOUND", "Dose not found.")
            if d["demo"] != self.store.demo:
                raise WorkflowError(409, "DEMO_MISMATCH")
            if d["schedule"] == "AS_NEEDED" or d["reminder_state"] is None:
                raise WorkflowError(409, "INVALID_TRANSITION", "As-needed doses start with a new request.")
            if d["delivery_mode"] != "DEVICE":
                raise WorkflowError(409, "REMINDER_ONLY")
            if d["plan_version_id"] != plan_version_id:
                raise WorkflowError(409, "PLAN_VERSION_MISMATCH")
            if d["workflow_state"] != "NOT_STARTED" or d["outcome"] or d["reminder_state"] == "CLOSED":
                raise WorkflowError(409, "ALREADY_STARTED")
            if d["reminder_state"] == "SCHEDULED":
                raise WorkflowError(409, "NOT_DUE")
            plan = self.store.plan_version(d["plan_version_id"])
            self._start_preconditions(plan)
            group = self.store.q(
                "SELECT d.* FROM doses d JOIN reminders r ON r.dose_id=d.dose_id WHERE d.due_at_utc=? AND d.demo=?"
                " AND d.delivery_mode='DEVICE' AND d.schedule='SCHEDULED' AND d.workflow_state='NOT_STARTED' AND d.outcome IS NULL"
                " AND r.state NOT IN ('SCHEDULED','CLOSED') AND d.plan_version_id=?",
                (d["due_at_utc"], d["demo"], d["plan_version_id"]),
            )
            by_class, by_channel = self._tray_totals(group)
            with self.store.tx() as conn:
                return self._open_txn(conn, request_id, group, plan, d["demo"], by_class, by_channel, dose_id)
        return await self._idempotent(request_id, "doses/start", fn)

    async def request_as_needed(self, request_id: str, entry_key: str, plan_version_id: int) -> dict:
        """One on-demand dose of an AS_NEEDED entry. DEVICE entries start the same guarded workflow
        as a due dose (nothing is dispensed by this call); REMINDER_ONLY entries record an
        unverified self report. Limits are checked here, under the engine lock, every time."""
        async def fn():
            occurrence_id = f"prn|{request_id}"
            prev = self.store.q1("SELECT * FROM doses WHERE occurrence_id=?", (occurrence_id,))
            if prev:  # the same request committed before its reply was saved: never a second dose
                return self._as_needed_reply(prev)
            demo = self.store.demo
            plan = self.store.active_plan(demo)
            if not plan or plan["plan_version_id"] != plan_version_id:
                raise WorkflowError(409, "PLAN_VERSION_MISMATCH")
            entry = next((e for e in plan["entries"] if e["entry_key"] == entry_key), None)
            if not entry:
                raise WorkflowError(409, "UNKNOWN_ENTRY")
            if not is_as_needed(entry):
                raise WorkflowError(409, "NOT_AS_NEEDED")
            now_dt = self.clock.now()
            tz = ZoneInfo(plan["timezone"])
            limits = as_needed.evaluate(self.store, entry, demo, now_dt)
            if not limits["available"]:
                code = limits["blocked_reason"]
                nxt = parse(limits["next_available_at"])
                when = as_needed.when_local(nxt, tz, now_dt)
                raise WorkflowError(409, code, message(code, when=when), next_available_at=limits["next_available_at"],
                                    next_available_local=when, taken_last_24h=limits["taken_last_24h"],
                                    last_taken_at=limits["last_taken_at"], min_interval_hours=entry["min_interval_hours"],
                                    max_per_day=entry["max_per_day"])
            dose = {
                "dose_id": uuid.uuid4().hex[:12], "occurrence_id": occurrence_id, "slot_id": f"{entry_key}@PRN",
                "local_date": now_dt.astimezone(tz).date().isoformat(), "local_time": now_dt.astimezone(tz).strftime("%H:%M"),
                "entry_key": entry_key, "plan_version_id": plan["plan_version_id"], "label": entry["label"],
                "strength_text": entry.get("strength_text"), "quantity": entry["quantity"], "class_id": entry.get("class_id"),
                "cartridge": entry.get("cartridge"), "delivery_mode": entry["delivery_mode"], "due_at_utc": iso(now_dt),
                "demo": demo, "schedule": "AS_NEEDED",
            }
            if entry["delivery_mode"] == "DEVICE":
                self._start_preconditions(plan)
                by_class, by_channel = self._tray_totals([dose])  # this entry alone; never combined
            now = self.store.now()
            with self.store.tx() as conn:
                cols = list(dose) + ["created_at", "updated_at"]
                conn.execute(f"INSERT INTO doses ({','.join(cols)}) VALUES ({_qs(cols)})", (*dose.values(), now, now))
                self.store.event(conn, "AS_NEEDED_REQUESTED", dose_id=dose["dose_id"], details={
                    "entry_key": entry_key, "request_id": request_id, "delivery_mode": entry["delivery_mode"],
                    "min_interval_hours": entry["min_interval_hours"], "max_per_day": entry["max_per_day"],
                    "counted_last_24h_before": limits["taken_last_24h"]})
                if entry["delivery_mode"] == "DEVICE":
                    self._open_txn(conn, request_id, [dose], plan, demo, by_class, by_channel, dose["dose_id"])
                else:
                    self.reminders.record_self_report(conn, dose["dose_id"], None, as_needed=True)
            return self._as_needed_reply(self.store.q1("SELECT * FROM doses WHERE dose_id=?", (dose["dose_id"],)))
        return await self._idempotent(request_id, "doses/request", fn)

    def _as_needed_reply(self, d: dict) -> dict:
        device = d["delivery_mode"] == "DEVICE"
        state = "SELF_REPORTED"
        if device:
            t = self.store.q1("SELECT state FROM transactions WHERE txn_id=?", (d["txn_id"],)) if d["txn_id"] else None
            state = t["state"] if t else d["workflow_state"]
        return {"dose_id": d["dose_id"], "occurrence_id": d["occurrence_id"], "entry_key": d["entry_key"],
                "schedule": "AS_NEEDED", "delivery_mode": d["delivery_mode"], "plan_version_id": d["plan_version_id"],
                "txn_id": d["txn_id"], "dose_ids": [d["dose_id"]], "state": state, "self_report": d["self_report"],
                "requested_at": d["created_at"]}

    async def cancel(self, request_id: str, txn_id: str) -> dict:
        async def fn():
            t = self._txn(txn_id)
            if t["state"] not in ACTIVE or t["dispense_reserved_at"]:
                raise WorkflowError(409, "INVALID_TRANSITION", "Verification can be cancelled only before dispensing.")
            with self.store.tx() as conn:
                self._cancel(conn, t, "USER_CANCELLED")
            return {"txn_id": txn_id, "state": "CANCELLED"}
        return await self._idempotent(request_id, "doses/cancel", fn)

    # ------------------------------------------------------------------ checks
    async def run_check(self, request_id: str, txn_id: str) -> dict:
        async def fn():
            t = self._txn(txn_id)
            kind = CHECK_KIND.get(t["state"])
            if not kind:
                raise WorkflowError(409, "INVALID_TRANSITION")
            if self.pending:
                raise WorkflowError(409, "PENDING_OPERATION")
            if t["state"] in ("EMPTY_TRAY_CHECK", "DOSE_CHECK"):
                dev = self._device_blocks(need_armed=False)
                if dev:
                    with self.store.tx() as conn:
                        self._set_blocks(conn, txn_id, dev)
                    raise WorkflowError(503 if dev[0]["code"] == "DEVICE_DISCONNECTED" else 409, dev[0]["code"], dev[0]["message"],
                                        reason_codes=[b["code"] for b in dev])
            expected = json.loads(t["expected_json"])
            self.busy = kind
            try:
                if kind == "FACE_MATCH":
                    obs = await self.vision.check_face(self.store.person(expected["person_id"]))
                elif kind == "CLASS_COUNTS":
                    obs = await self.vision.check_tray(expected["by_class"], kind)
                else:
                    obs = await self.vision.check_tray({}, kind)
            finally:
                self.busy = None
            t = self._txn(txn_id)  # state may have changed (fault, door) while capturing
            t["plan_version_id"] = expected["plan_version_id"]
            if CHECK_KIND.get(t["state"]) != kind:
                with self.store.tx() as conn:
                    self._save_obs(conn, t, obs)
                raise WorkflowError(409, "INVALID_TRANSITION", "The stage changed during the check.")
            stage = t["state"]  # _set_state mutates t, so remember where this check started
            with self.store.tx() as conn:
                obs_id = self._save_obs(conn, t, obs)
                if obs.result != "PASS":
                    code = obs.reason_code or "INVALID_TRANSITION"
                    self._set_blocks(conn, txn_id, [{"code": code, "message": obs.message or message(code), "details": obs.details}])
                elif t["state"] == "PERSON_CHECK":
                    self._set_state(conn, t, "EMPTY_TRAY_CHECK")
                elif t["state"] == "DOSE_CHECK":
                    self._set_state(conn, t, "RECIPIENT_RECHECK")
                elif t["state"] == "COLLECTION_CHECK":
                    self._complete(conn, t, "OBSERVED")
                else:
                    self._set_blocks(conn, txn_id, [])
            if obs.result == "PASS" and stage == "EMPTY_TRAY_CHECK":
                await self._begin_dispense(t)
            if obs.result == "PASS" and stage == "COLLECTION_CHECK":
                await self._lock_latch(t)
            t = self._txn(txn_id)
            return {"txn_id": txn_id, "observation_id": obs_id, "kind": kind, "result": obs.result,
                    "reason_codes": [obs.reason_code] if obs.reason_code else [], "message": obs.message, "state": t["state"]}
        return await self._idempotent(request_id, "doses/check", fn)

    # ------------------------------------------------------------------ dispensing
    async def _begin_dispense(self, t: dict) -> None:
        blocks = self._device_blocks()
        home = self.hw.fields
        if not (home["hA"] and home["hB"]):
            blocks.append(block("NOT_HOMED"))
        empty = self._latest_obs(t["txn_id"], "TRAY_EMPTY")
        if not empty or empty["result"] != "PASS" or empty["tray_revision"] != self.hw.fields["rev"] \
                or empty["controller_session"] != self.hw.session:
            blocks.append(block("TRAY_REVISION_CHANGED"))
        if blocks:
            with self.store.tx() as conn:
                self._set_blocks(conn, t["txn_id"], blocks)
            return
        expected = json.loads(t["expected_json"])
        now = self.store.now()
        with self.store.tx() as conn:
            n = conn.execute("UPDATE transactions SET dispense_reserved_at=?, controller_session=?, updated_at=?"
                             " WHERE txn_id=? AND dispense_reserved_at IS NULL AND state='EMPTY_TRAY_CHECK'",
                             (now, self.hw.session, now, t["txn_id"])).rowcount
            if n != 1:
                raise WorkflowError(409, "ALREADY_STARTED")
            for ch in sorted(expected["by_channel"]):
                qty = expected["by_channel"][ch]
                if qty <= 0:
                    continue
                conn.execute("INSERT INTO commands (request_id, session, txn_id, kind, channel, payload_json, state, created_at)"
                             " VALUES (?,?,?,?,?,?,?,?)",
                             (t["request_id"], self.hw.session, t["txn_id"], "DISPENSE", ch,
                              dumps({"cmd": "DISPENSE", "t": t["short_id"], "c": ch, "n": qty}), "RESERVED", now))
            self._set_state(conn, t, "DISPENSING")
            self.store.event(conn, "DISPENSE_RESERVED", txn_id=t["txn_id"], details=expected["by_channel"])
        await self._send_next_dispense(t["txn_id"])

    async def _send_next_dispense(self, txn_id: str) -> None:
        t = self.store.q1("SELECT * FROM transactions WHERE txn_id=?", (txn_id,))
        if t["state"] != "DISPENSING":
            return
        cmd = self.store.q1("SELECT * FROM commands WHERE txn_id=? AND kind='DISPENSE' AND state='RESERVED' ORDER BY channel LIMIT 1",
                            (txn_id,))
        if not cmd:
            with self.store.tx() as conn:
                self._set_state(conn, t, "DOSE_CHECK")
            return
        payload = json.loads(cmd["payload_json"])
        await self._transmit(t, cmd, payload, ack_s=self.settings.ack_timeout_s,
                             done_s=self.settings.ack_timeout_s + payload["n"] * self.settings.cycle_timeout_per_unit_s)

    async def _transmit(self, t: dict, cmd: dict, payload: dict, ack_s: float, done_s: float) -> None:
        """Mark SENT before writing, so a crash after the write is treated as possible motion."""
        with self.store.tx() as conn:
            conn.execute("UPDATE commands SET state='SENT', session=?, sent_at=? WHERE command_id=?",
                         (self.hw.session, self.store.now(), cmd["command_id"]))
        now = self.clock.monotonic()
        self.pending = {"command_id": cmd["command_id"], "kind": cmd["kind"], "channel": cmd["channel"],
                        "txn_id": t["txn_id"], "acked": False, "ack_deadline": now + ack_s, "done_deadline": now + done_s}
        ok = await self.hw.send({"id": cmd["command_id"], **payload})
        if not ok:
            # Nothing was written, but the reservation is spent: no automatic retry.
            self.pending = None
            with self.store.tx() as conn:
                conn.execute("UPDATE commands SET state='NOT_SENT', detail='link down', completed_at=? WHERE command_id=?",
                             (self.store.now(), cmd["command_id"]))
                tt = dict(conn.execute("SELECT * FROM transactions WHERE txn_id=?", (t["txn_id"],)).fetchone())
                self._set_state(conn, tt, "FAULT", [block("DEVICE_DISCONNECTED")])

    async def check_deadlines(self) -> None:
        p = self.pending
        if not p:
            return
        now = self.clock.monotonic()
        late = (not p["acked"] and now > p["ack_deadline"]) or now > p["done_deadline"]
        if not late:
            return
        async with self.lock:
            p = self.pending
            if not p:
                return
            now = self.clock.monotonic()
            reason = "ACK_TIMEOUT" if not p["acked"] and now > p["ack_deadline"] else "COMPLETION_TIMEOUT" if now > p["done_deadline"] else None
            if not reason:
                return
            await self.hw.stop_now()  # make sure nothing keeps moving
            self._uncertain(p, reason)

    def _uncertain(self, p: dict, reason: str) -> None:
        self.pending = None
        with self.store.tx() as conn:
            conn.execute("UPDATE commands SET state='UNKNOWN', detail=?, completed_at=? WHERE command_id=?",
                         (reason, self.store.now(), p["command_id"]))
            t = conn.execute("SELECT * FROM transactions WHERE txn_id=?", (p["txn_id"],)).fetchone()
            if t:
                t = dict(t)
                self._set_state(conn, t, "UNCERTAIN", [block("OUTCOME_UNCERTAIN", cause=reason, command=p["kind"])])
        self.armed_session = None

    # ------------------------------------------------------------------ release
    def release_blocks(self, t: dict) -> list[dict]:
        out = self._device_blocks()
        if self.pending:
            out.append(block("PENDING_OPERATION"))
        obs = self._latest_obs(t["txn_id"], "CLASS_COUNTS")
        if not obs or obs["result"] != "PASS":
            out.append(block("INSPECTION_MISSING"))
        else:
            if obs["tray_revision"] != self.hw.fields["rev"] or obs["controller_session"] != self.hw.session:
                out.append(block("TRAY_REVISION_CHANGED"))
            age = self._obs_age_s(obs)
            if age is None or age > self.settings.tray_max_age_s:
                out.append(block("INSPECTION_EXPIRED", max_age_s=self.settings.tray_max_age_s))
        return out

    async def request_release(self, request_id: str, txn_id: str) -> dict:
        async def fn():
            t = self._txn(txn_id)
            if t["state"] != "RECIPIENT_RECHECK":
                raise WorkflowError(409, "INVALID_TRANSITION")
            if t["release_reserved_at"]:
                raise WorkflowError(409, "ALREADY_STARTED", "Release was already requested for this dose.")
            expected = json.loads(t["expected_json"])
            t["plan_version_id"] = expected["plan_version_id"]
            blocks = self.release_blocks(t)
            if blocks:
                with self.store.tx() as conn:
                    self._set_blocks(conn, txn_id, blocks)
                    if any(b["code"] in ("TRAY_REVISION_CHANGED", "INSPECTION_EXPIRED", "INSPECTION_MISSING") for b in blocks):
                        self._set_state(conn, t, "DOSE_CHECK", blocks)
                raise WorkflowError(409, blocks[0]["code"], blocks[0]["message"], reason_codes=[b["code"] for b in blocks])
            # Recipient recheck happens now, so face evidence is seconds old at release.
            self.busy = "FACE_MATCH"
            try:
                obs = await self.vision.check_face(self.store.person(expected["person_id"]))
            finally:
                self.busy = None
            face_age = None if obs.capture_mono is None else self.clock.monotonic() - obs.capture_mono
            with self.store.tx() as conn:
                self._save_obs(conn, t, obs)
            if obs.result != "PASS":
                code = obs.reason_code or "UNKNOWN_PERSON"
                with self.store.tx() as conn:
                    self._set_blocks(conn, txn_id, [{"code": code, "message": obs.message or message(code), "details": None}])
                raise WorkflowError(409, code, obs.message or message(code), reason_codes=[code])
            if face_age is None or face_age > self.settings.face_max_age_s + self.settings.check_min_span_s:
                with self.store.tx() as conn:
                    self._set_blocks(conn, txn_id, [block("INSPECTION_EXPIRED")])
                raise WorkflowError(409, "INSPECTION_EXPIRED")
            t = self._txn(txn_id)
            blocks = self.release_blocks(t) if t["state"] == "RECIPIENT_RECHECK" else [block("INVALID_TRANSITION")]
            if blocks:
                with self.store.tx() as conn:
                    self._set_blocks(conn, txn_id, blocks)
                raise WorkflowError(409, blocks[0]["code"], blocks[0]["message"], reason_codes=[b["code"] for b in blocks])
            rev = self.hw.fields["rev"]
            now = self.store.now()
            with self.store.tx() as conn:
                n = conn.execute("UPDATE transactions SET release_reserved_at=?, updated_at=? WHERE txn_id=?"
                                 " AND release_reserved_at IS NULL AND state='RECIPIENT_RECHECK'", (now, now, txn_id)).rowcount
                if n != 1:
                    raise WorkflowError(409, "ALREADY_STARTED")
                cur = conn.execute("INSERT INTO commands (request_id, session, txn_id, kind, channel, payload_json, state, created_at)"
                                   " VALUES (?,?,?,?,?,?,?,?)",
                                   (request_id, self.hw.session, txn_id, "RELEASE", "",
                                    dumps({"cmd": "RELEASE", "t": t["short_id"], "rev": rev}), "RESERVED", now))
                cmd_id = int(cur.lastrowid)
                self._set_state(conn, t, "RELEASE_PENDING")
                self.store.event(conn, "RELEASE_RESERVED", txn_id=txn_id, details={"rev": rev, "command_id": cmd_id})
            cmd = self.store.q1("SELECT * FROM commands WHERE command_id=?", (cmd_id,))
            await self._transmit(t, cmd, json.loads(cmd["payload_json"]), ack_s=self.settings.ack_timeout_s,
                                 done_s=self.settings.release_timeout_s)
            return {"txn_id": txn_id, "result": "pending", "operation_id": cmd_id, "state": "RELEASE_PENDING"}
        return await self._idempotent(request_id, "doses/release", fn)

    # ------------------------------------------------------------------ completion
    def _complete(self, conn, t: dict, collection: str) -> None:
        self._set_state(conn, t, "COMPLETE", outcome="RELEASED")
        conn.execute(f"UPDATE doses SET collection=?, outcome='RELEASED', updated_at=? WHERE dose_id IN ({_qs(_ids(t))})",
                     (collection, self.store.now(), *_ids(t)))
        self.store.event(conn, "DOSE_COMPLETE", txn_id=t["txn_id"], details={"collection": collection})

    async def finish_without_collection(self, request_id: str, txn_id: str) -> dict:
        async def fn():
            t = self._txn(txn_id)
            if t["state"] != "COLLECTION_CHECK":
                raise WorkflowError(409, "INVALID_TRANSITION")
            with self.store.tx() as conn:
                self._complete(conn, t, "PENDING")
            await self._lock_latch(t)
            return {"txn_id": txn_id, "state": "COMPLETE", "collection": "PENDING"}
        return await self._idempotent(request_id, "doses/finish", fn)

    async def _lock_latch(self, t: dict) -> None:
        if self.hw.fields["door"] == 0:
            self.lock_when_closed = True
            return
        self.lock_when_closed = False
        now = self.store.now()
        with self.store.tx() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO commands (request_id, session, txn_id, kind, channel, payload_json, state, created_at)"
                               " VALUES (?,?,?,?,?,?,?,?)",
                               (None, self.hw.session, t["txn_id"], "LOCK", "", dumps({"cmd": "LOCK"}), "SENT", now))
            cmd_id = cur.lastrowid if cur.rowcount else None
        if cmd_id:
            await self.hw.send({"id": int(cmd_id), "cmd": "LOCK"})

    async def confirm_taken(self, request_id: str, dose_id: str) -> dict:
        """User confirmation, stored separately from anything the camera observed."""
        async def fn():
            d = self.store.q1("SELECT * FROM doses WHERE dose_id=?", (dose_id,))
            if not d or d["demo"] != self.store.demo:
                raise WorkflowError(404, "NOT_FOUND", "Dose not found.")
            if d["delivery_mode"] != "DEVICE" or d["workflow_state"] not in ("RELEASED", "COMPLETE"):
                raise WorkflowError(409, "INVALID_TRANSITION", "Confirmation is available after release.")
            if d["self_report"]:
                raise WorkflowError(409, "INVALID_TRANSITION", "Already confirmed.")
            now = self.store.now()
            with self.store.tx() as conn:
                conn.execute("UPDATE doses SET self_report='TAKEN_UNVERIFIED', self_report_at=?, updated_at=? WHERE dose_id=?",
                             (now, now, dose_id))
                conn.execute("INSERT INTO observations (txn_id, kind, result, method, capture_time, details_json, simulated, demo, created_at)"
                             " VALUES (?,?,?,?,?,?,?,?,?)",
                             (d["txn_id"], "USER_CONFIRMED", "PASS", "user", now,
                              dumps({"dose_id": dose_id, "ingestion": "UNVERIFIED"}), 0, d["demo"], now))
                self.store.event(conn, "USER_CONFIRMED", dose_id=dose_id, txn_id=d["txn_id"], details={"ingestion": "UNVERIFIED"})
            return {"dose_id": dose_id, "self_report": "TAKEN_UNVERIFIED"}
        return await self._idempotent(request_id, "doses/confirm", fn)

    # ------------------------------------------------------------------ device control
    async def stop(self, request_id: str) -> dict:
        sent = await self.hw.stop_now()  # first, ahead of any queued work or lock
        async with self.lock:
            p = self.pending
            if p:
                self._uncertain(p, "STOPPED")
            else:
                t = self.active_txn()
                with self.store.tx() as conn:
                    if t and t["state"] not in ("FAULT", "UNCERTAIN", "COLLECTION_CHECK"):
                        self._set_state(conn, t, "FAULT", [block("STOPPED")])
            self.armed_session = None
            with self.store.tx() as conn:
                self.store.event(conn, "STOP_PRESSED", details={"written": sent, "request_id": request_id})
            return {"stopped": True, "written": sent}

    async def arm(self, request_id: str, inspected: bool) -> dict:
        async def fn():
            if not inspected:
                raise WorkflowError(422, "INSPECTION_REQUIRED")
            blocks = self._device_blocks(need_armed=False)
            f = self.hw.fields
            if self.hw.connected and not (f["hA"] and f["hB"]):
                blocks.append(block("NOT_HOMED"))
            if self.pending:
                blocks.append(block("PENDING_OPERATION"))
            if blocks:
                raise WorkflowError(503 if blocks[0]["code"] == "DEVICE_DISCONNECTED" else 409, blocks[0]["code"],
                                    blocks[0]["message"], reason_codes=[b["code"] for b in blocks])
            self.armed_session = self.hw.session
            with self.store.tx() as conn:
                self.store.event(conn, "DEVICE_ARMED", details={"session": self.hw.session, "enclosure_inspected": True})
            return {"armed": True, "session": self.hw.session}
        return await self._idempotent(request_id, "device/arm", fn)

    async def reset(self, request_id: str, inspected: bool) -> dict:
        """Supervised recovery after physical inspection. Ends any unresolved transaction
        without replaying it; doses whose outcome is unknown stay flagged for review."""
        async def fn():
            if not inspected:
                raise WorkflowError(422, "INSPECTION_REQUIRED")
            if not self.hw.connected:
                raise WorkflowError(503, "DEVICE_DISCONNECTED")
            self.pending = None
            self.armed_session = None
            t = self.active_txn()
            now = self.store.now()
            with self.store.tx() as conn:
                if t:
                    if not t["dispense_reserved_at"]:
                        self._cancel(conn, t, "RESET_BEFORE_DISPENSE")
                    elif t["state"] == "COLLECTION_CHECK":
                        self._complete(conn, t, "NOT_OBSERVED")
                    else:
                        final = "ENDED_UNCERTAIN" if t["state"] == "UNCERTAIN" else "ENDED_FAULT"
                        self._set_state(conn, t, final, [block("OUTCOME_UNCERTAIN" if final == "ENDED_UNCERTAIN" else "DEVICE_FAULT")],
                                        outcome="UNRESOLVED")
                        conn.execute(f"UPDATE doses SET outcome='UNRESOLVED_NEEDS_REVIEW', updated_at=? WHERE dose_id IN ({_qs(_ids(t))})",
                                     (now, *_ids(t)))
                        for did in _ids(t):
                            self.reminders.close_for_dose(conn, did, "WORKFLOW_UNRESOLVED")
                cur = conn.execute("INSERT INTO commands (request_id, session, txn_id, kind, channel, payload_json, state, created_at)"
                                   " VALUES (?,?,?,?,?,?,?,?)",
                                   (request_id, self.hw.session, None, "RESET", "", dumps({"cmd": "RESET"}), "SENT", now))
                cmd_id = int(cur.lastrowid)
                self.store.event(conn, "SUPERVISED_RESET", txn_id=t["txn_id"] if t else None,
                                 details={"inspected": True, "previous_state": t["state"] if t else None})
            await self.hw.send({"id": cmd_id, "cmd": "RESET"})
            return {"reset": True, "note": "Controller restarts with a new unarmed session."}
        return await self._idempotent(request_id, "device/reset", fn)

    # ------------------------------------------------------------------ device events
    async def on_device_event(self, evt: dict) -> None:
        async with self.lock:
            await self._handle(evt)

    async def _handle(self, evt: dict) -> None:
        ev = evt.get("event")
        p = self.pending
        cid = evt.get("id")
        if ev == "ACK":
            if p and cid == p["command_id"]:
                p["acked"] = True
                with self.store.tx() as conn:
                    conn.execute("UPDATE commands SET state='ACKED' WHERE command_id=? AND state='SENT'", (cid,))
            return
        if ev == "CYCLE_DONE":
            if p and cid == p["command_id"] and p["kind"] == "DISPENSE":
                self.pending = None
                with self.store.tx() as conn:
                    conn.execute("UPDATE commands SET state='DONE', detail=?, completed_at=? WHERE command_id=?",
                                 (f"rev={evt.get('rev')}", self.store.now(), cid))
                    self.store.event(conn, "CYCLE_DONE", txn_id=p["txn_id"], details={"channel": evt.get("c"), "rev": evt.get("rev"),
                                                                                       "note": "motion finished; token delivery not proven"})
                await self._send_next_dispense(p["txn_id"])
            else:
                self.store.log("UNEXPECTED_DEVICE_EVENT", details=evt)
            return
        if ev == "RELEASED":
            if p and cid == p["command_id"] and p["kind"] == "RELEASE":
                self.pending = None
                with self.store.tx() as conn:
                    conn.execute("UPDATE commands SET state='DONE', completed_at=? WHERE command_id=?", (self.store.now(), cid))
                    t = dict(conn.execute("SELECT * FROM transactions WHERE txn_id=?", (p["txn_id"],)).fetchone())
                    self._set_state(conn, t, "COLLECTION_CHECK")
                    for did in _ids(t):
                        self.reminders.close_for_dose(conn, did, "RELEASED")
                    self.store.event(conn, "RELEASED", txn_id=t["txn_id"], details={"rev": evt.get("rev")})
            else:
                self.store.log("UNEXPECTED_DEVICE_EVENT", details=evt)
            return
        if ev == "LOCKED":
            with self.store.tx() as conn:
                conn.execute("UPDATE commands SET state='DONE', completed_at=? WHERE command_id=?", (self.store.now(), cid))
                self.store.event(conn, "LATCH_LOCKED")
            return
        if ev == "NACK":
            if p and cid == p["command_id"]:
                self.pending = None
                with self.store.tx() as conn:
                    conn.execute("UPDATE commands SET state='NACKED', detail=?, completed_at=? WHERE command_id=?",
                                 (evt.get("reason"), self.store.now(), cid))
                    t = dict(conn.execute("SELECT * FROM transactions WHERE txn_id=?", (p["txn_id"],)).fetchone())
                    self._set_state(conn, t, "FAULT", [block("COMMAND_REJECTED", reason=evt.get("reason"))])
            else:
                self.store.log("DEVICE_NACK", reason=evt.get("reason"), details=evt)
            return
        if ev == "FAULT" or (ev == "STATE_CHANGED" and _changed_to_fault(evt)):
            reason = evt.get("reason") or (evt.get("changes", {}).get("fault") or [None, None])[1]
            self.armed_session = None
            if p:
                self._uncertain(p, f"FAULT:{reason}")
            else:
                t = self.active_txn()
                with self.store.tx() as conn:
                    if t and t["state"] not in ("FAULT", "UNCERTAIN", "COLLECTION_CHECK"):
                        self._set_state(conn, t, "FAULT", [block("DEVICE_FAULT", fault=reason)])
                    self.store.event(conn, "DEVICE_FAULT", reason=reason)
            if ev == "FAULT":
                return
        if ev in ("SESSION", "DISCONNECTED"):
            first = ev == "SESSION" and evt.get("old") is None
            self.armed_session = None
            if p:
                self._uncertain(p, "CONTROLLER_RESET" if ev == "SESSION" else "DEVICE_DISCONNECTED")
            elif not first:
                t = self.active_txn()
                if t:
                    with self.store.tx() as conn:
                        self._set_blocks(conn, t["txn_id"], [block("DEVICE_RESTARTED" if ev == "SESSION" else "DEVICE_DISCONNECTED")])
            self.store.log("DEVICE_SESSION" if ev == "SESSION" else "DEVICE_DISCONNECTED",
                           reason=evt.get("reason"), details={"session": evt.get("s"), "old": evt.get("old")})
            return
        if ev == "CONNECTED":
            self.store.log("DEVICE_CONNECTED")
            return
        changes = evt.get("changes") or {}
        if "door" in changes or "rev" in changes:
            await self._on_tray_change(changes)
        if ev == "PROTOCOL_ERROR":
            self.store.log("PROTOCOL_ERROR", details=evt)

    async def _on_tray_change(self, changes: dict) -> None:
        door = changes.get("door")
        with self.store.tx() as conn:
            if door:
                self.store.event(conn, "DOOR_OPENED" if door[1] == 0 else "DOOR_CLOSED",
                                 details={"rev": self.hw.fields["rev"]})
            t = self.active_txn(conn)
            if t and t["state"] in ("DOSE_CHECK", "RECIPIENT_RECHECK") and not self.pending:
                code = "TRAY_DOOR_OPENED" if door and door[1] == 0 else "TRAY_REVISION_CHANGED"
                if t["state"] == "RECIPIENT_RECHECK":
                    self._set_state(conn, t, "DOSE_CHECK", [block(code)])
                else:
                    self._set_blocks(conn, t["txn_id"], [block(code)])
            elif t and t["state"] in ("PERSON_CHECK", "EMPTY_TRAY_CHECK") and door and door[1] == 0:
                self._set_blocks(conn, t["txn_id"], [block("TRAY_DOOR_OPENED")])
        if door and door[1] == 1 and self.lock_when_closed:
            t = self.store.q1("SELECT * FROM transactions WHERE state='COMPLETE' ORDER BY updated_at DESC LIMIT 1")
            if t:
                await self._lock_latch(t)

    # ------------------------------------------------------------------ snapshot
    def snapshot(self) -> dict:
        t = self.active_txn()
        if t and t["demo"] != self.store.demo:
            t = None
        armed = self._is_armed()
        out = {"armed": armed, "busy": self.busy, "txn": None, "pending_operation": None}
        if self.pending:
            out["pending_operation"] = {k: self.pending[k] for k in ("command_id", "kind", "channel", "acked")}
        if not t:
            return out
        expected = json.loads(t["expected_json"])
        classes = self.store.class_by_id()
        label, pose, action = STAGES.get(t["state"], (t["state"], None, None))
        obs = {}
        for kind in ("FACE_MATCH", "TRAY_EMPTY", "CLASS_COUNTS"):
            o = self._latest_obs(t["txn_id"], kind)
            if o:
                age = self._obs_age_s(o)
                obs[kind] = {"result": o["result"], "reason_code": o["reason_code"], "message": o["details"].get("message"),
                             "observed": o["details"].get("observed"), "simulated": bool(o["simulated"]),
                             "age_s": None if age is None else round(age, 1), "tray_revision": o["tray_revision"]}
        doses = self.store.q(f"SELECT dose_id, label, strength_text, quantity, class_id, cartridge, schedule FROM doses WHERE dose_id IN ({_qs(_ids(t))})",
                             _ids(t))
        blocks = json.loads(t["blocks_json"] or "[]")
        release_available = False
        if t["state"] == "RECIPIENT_RECHECK":
            rb = self.release_blocks(t)
            release_available = not rb
            if rb and not blocks:
                blocks = rb
        out["txn"] = {
            "txn_id": t["txn_id"], "state": t["state"], "stage": label, "pose": pose, "next_action": action,
            "doses": doses, "expected": {"by_class": {c: expected["by_class"].get(c, 0) for c in classes},
                                         "by_channel": expected["by_channel"]},
            "class_labels": {c: v["label"] for c, v in classes.items()},
            "class_colors": {c: v["color"] for c, v in classes.items()},
            "observations": obs, "blocks": blocks, "release_available": release_available,
            "can_cancel": not t["dispense_reserved_at"], "created_at": t["created_at"],
        }
        return out


def _qs(items) -> str:
    return ",".join("?" * len(items))


def _ids(txn: dict) -> list[str]:
    return json.loads(txn["dose_ids_json"])


def _changed_to_fault(evt: dict) -> bool:
    ch = (evt.get("changes") or {}).get("fault")
    return bool(ch and ch[1] and not ch[0])
