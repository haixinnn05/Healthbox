"""As-needed (PRN) entries: validation, server-side limits, the guarded workflow on request,
idempotency, and scheduled behaviour left unchanged."""
import sqlite3

from conftest import START, rid

from app.store import SCHEMA, Store
from app.timeutil import FakeClock, parse

NY = "America/New_York"


def prn(key="prn1", mode="DEVICE", cls="blue", interval=4, per_day=3, qty=1, **kw):
    e = {"entry_key": key, "label": f"Headache {key}", "quantity": qty, "delivery_mode": mode,
         "class_id": cls if mode == "DEVICE" else None, "schedule": "AS_NEEDED", "times": [],
         "min_interval_hours": interval, "max_per_day": per_day, "confirmed": True}
    e.update(kw)
    return e


def sched(key="sch1", times=("20:00",), mode="DEVICE", cls="orange"):
    return {"entry_key": key, "label": f"Med {key}", "quantity": 1, "delivery_mode": mode,
            "class_id": cls if mode == "DEVICE" else None, "times": list(times), "confirmed": True}


def setup(h, entries, arm=True):
    h.wait(lambda s: s["device"]["connected"], msg="device connected")
    person = h.post("/api/enroll", {"request_id": rid(), "label": "Teammate", "consent": True})["person_id"]
    plan = h.post("/api/plans/confirm", {"request_id": rid(), "person_id": person, "timezone": NY, "timezone_confirmed": True,
                                         "dst_reviewed": True, "entries": entries})
    if arm:
        h.post("/api/device/arm", {"request_id": rid(), "inspected": True})
    return plan["plan_version_id"]


def request(h, key, pv, expect=200, req=None):
    return h.post("/api/doses/request", {"request_id": req or rid(), "entry_key": key, "plan_version": pv}, expect=expect)


def item(h, key):
    return next(p for p in h.status()["as_needed"] if p["entry_key"] == key)


def run_to_release(h, txn_id):
    assert h.check(txn_id)["state"] == "EMPTY_TRAY_CHECK"
    h.check(txn_id)
    h.wait(lambda s: s["workflow"]["txn"]["state"] == "DOSE_CHECK", msg="DOSE_CHECK")
    assert h.check(txn_id)["state"] == "RECIPIENT_RECHECK"
    assert h.post("/api/doses/release", {"request_id": rid(), "txn_id": txn_id})["result"] == "pending"
    h.wait(lambda s: s["workflow"]["txn"]["state"] == "COLLECTION_CHECK", msg="released")
    h.sim(action="open_door")
    h.sim(action="collect")
    assert h.check(txn_id)["state"] == "COMPLETE"
    h.sim(action="close_door")
    h.wait(lambda s: s["device"]["servo_commanded"] == "locked", msg="relocked")


def commands(h):
    return h.c.app.state.svc.store.q("SELECT kind, channel FROM commands WHERE kind IN ('DISPENSE','RELEASE')")


def prn_doses(h, key):
    return h.c.app.state.svc.store.q("SELECT * FROM doses WHERE schedule='AS_NEEDED' AND entry_key=?", (key,))


# ---------------------------------------------------------------- validation
def test_validation_errors(harness):
    h = harness(require_label_photo=False)
    h.wait(lambda s: s["device"]["connected"])
    person = h.post("/api/enroll", {"request_id": rid(), "label": "T", "consent": True})["person_id"]

    def errors(entry):
        r = h.post("/api/plans/confirm", {"request_id": rid(), "person_id": person, "timezone": NY, "timezone_confirmed": True,
                                          "dst_reviewed": True, "entries": [entry]}, expect=422)
        return {e["field"]: e["message"] for e in r["errors"]}

    assert "times" in errors(prn(times=["08:00"]))
    assert "min_interval_hours" in errors(prn(interval=None))
    assert "min_interval_hours" in errors(prn(interval=0))
    assert "min_interval_hours" in errors(prn(interval=25))
    assert "min_interval_hours" in errors(prn(interval=4.5))  # never rounded
    assert "min_interval_hours" in errors(prn(interval=True))
    assert "max_per_day" in errors(prn(per_day=9))
    assert "max_per_day" in errors(prn(per_day="three"))
    assert "schedule" in errors(prn(schedule="SOMETIMES"))
    assert "class_id" in errors(prn(cls="nope"))
    assert h.status()["plan"] is None
    # Whole numbers sent as text are accepted; scheduled entries still need times.
    assert "times" in errors(sched(times=()))
    h.post("/api/plans/confirm", {"request_id": rid(), "person_id": person, "timezone": NY, "timezone_confirmed": True,
                                  "dst_reviewed": True, "entries": [prn(interval="6", per_day="2")]})
    e = h.status()["plan"]["entries"][0]
    assert (e["schedule"], e["min_interval_hours"], e["max_per_day"], e["times"]) == ("AS_NEEDED", 6, 2, [])


# ---------------------------------------------------------------- device flow
def test_request_runs_guarded_workflow_and_releases_once(harness):
    h = harness(require_label_photo=False)
    pv = setup(h, [prn()])
    assert item(h, "prn1")["available"] is True
    r = request(h, "prn1", pv)
    assert r["state"] == "PERSON_CHECK" and r["schedule"] == "AS_NEEDED" and r["txn_id"]
    assert commands(h) == []  # requesting never dispenses by itself
    txn = h.txn()
    assert txn["doses"][0]["dose_id"] == r["dose_id"] and txn["doses"][0]["schedule"] == "AS_NEEDED"
    assert txn["expected"]["by_class"]["blue"] == 1
    st = h.status()
    assert all(c["dose_id"] != r["dose_id"] for c in st["today"])  # never a reminder card
    assert item(h, "prn1")["blocked_reason"] == "AS_NEEDED_IN_PROGRESS"
    run_to_release(h, r["txn_id"])
    assert commands(h) == [{"kind": "DISPENSE", "channel": "A"}, {"kind": "RELEASE", "channel": ""}]
    h.post("/api/doses/release", {"request_id": rid(), "txn_id": r["txn_id"]}, expect=409)
    # History shows it, marked as needed, with no reminder.
    hist = h.c.get("/api/history").json()["doses"]
    row = next(c for c in hist if c["dose_id"] == r["dose_id"])
    assert row["as_needed"] is True and row["reminder"] is None and row["outcome"] == "RELEASED"
    # The released device dose can still be confirmed as taken (unverified).
    assert h.post("/api/doses/confirm", {"request_id": rid(), "dose_id": r["dose_id"]})["self_report"] == "TAKEN_UNVERIFIED"
    # It cannot be restarted through the scheduled start route.
    x = h.post("/api/doses/start", {"request_id": rid(), "dose_id": r["dose_id"], "plan_version": pv}, expect=409)
    assert x["reason_codes"] == ["INVALID_TRANSITION"]


def test_duplicate_request_id_returns_same_dose(harness):
    h = harness(require_label_photo=False)
    pv = setup(h, [prn()])
    req = rid()
    a = request(h, "prn1", pv, req=req)
    b = request(h, "prn1", pv, req=req)  # browser retry
    assert a["dose_id"] == b["dose_id"] and a["txn_id"] == b["txn_id"]
    # Even if the saved reply were lost, the occurrence derived from request_id returns the same dose.
    store = h.c.app.state.svc.store
    with store.tx() as conn:
        conn.execute("DELETE FROM requests WHERE request_id=?", (req,))
    c = request(h, "prn1", pv, req=req)
    assert c["dose_id"] == a["dose_id"] and c["txn_id"] == a["txn_id"]
    assert len(prn_doses(h, "prn1")) == 1
    # A request id used on another route is rejected.
    r = h.post("/api/doses/self-report", {"request_id": req, "dose_id": a["dose_id"]}, expect=409)
    assert r["reason_codes"] == ["REQUEST_ID_REUSED"]


def test_too_soon_after_release(harness):
    h = harness(require_label_photo=False)
    pv = setup(h, [prn(interval=4, per_day=3)])
    r = request(h, "prn1", pv)
    run_to_release(h, r["txn_id"])
    released_at = parse(h.c.app.state.svc.store.q1("SELECT MIN(ts) AS t FROM events WHERE kind='RELEASED'")["t"])
    assert parse(item(h, "prn1")["last_taken_at"]) == released_at  # measured from release, not from the request
    x = request(h, "prn1", pv, expect=409)
    assert x["reason_codes"] == ["TOO_SOON"]
    assert (parse(x["next_available_at"]) - released_at).total_seconds() == 4 * 3600
    assert x["message"].startswith("It is too soon since the last dose") and x["next_available_local"] in x["message"]
    it = item(h, "prn1")
    assert it["available"] is False and it["blocked_reason"] == "TOO_SOON" and it["taken_last_24h"] == 1
    assert it["next_available_at"] == x["next_available_at"]
    assert len(prn_doses(h, "prn1")) == 1  # a rejected request leaves no dose behind
    h.advance(4 * 3600 - 60)
    assert request(h, "prn1", pv, expect=409)["reason_codes"] == ["TOO_SOON"]
    h.advance(61)
    assert item(h, "prn1")["available"] is True


def test_daily_limit_rolling_24h(harness):
    h = harness(require_label_photo=False)
    pv = setup(h, [prn(key="p", mode="REMINDER_ONLY", interval=1, per_day=2)], arm=False)
    first = request(h, "p", pv)
    h.advance(3600)
    request(h, "p", pv)
    h.advance(3600)
    x = request(h, "p", pv, expect=409)
    assert x["reason_codes"] == ["DAILY_LIMIT"] and x["taken_last_24h"] == 2
    assert (parse(x["next_available_at"]) - parse(first["requested_at"])).total_seconds() == 24 * 3600
    it = item(h, "p")
    assert it["blocked_reason"] == "DAILY_LIMIT" and it["taken_last_24h"] == 2
    h.advance(22 * 3600 - 10)
    assert request(h, "p", pv, expect=409)["reason_codes"] == ["DAILY_LIMIT"]
    h.advance(11)
    assert item(h, "p")["available"] is True and item(h, "p")["taken_last_24h"] == 1
    request(h, "p", pv)


def test_cancel_before_dispense_does_not_count(harness):
    h = harness(require_label_photo=False)
    pv = setup(h, [prn(per_day=1)])
    r = request(h, "prn1", pv)
    h.check(r["txn_id"])  # recipient check passes; nothing reserved yet
    h.post("/api/doses/cancel", {"request_id": rid(), "txn_id": r["txn_id"]})
    h.wait(lambda s: s["workflow"]["txn"] is None, msg="cancelled")
    it = item(h, "prn1")
    assert it["available"] is True and it["taken_last_24h"] == 0 and it["last_taken_at"] is None
    assert it["last_dose"]["outcome"] == "CANCELLED_BEFORE_DISPENSE"
    hist = h.c.get("/api/history").json()["doses"]
    assert next(c for c in hist if c["dose_id"] == r["dose_id"])["outcome"] == "CANCELLED_BEFORE_DISPENSE"
    again = request(h, "prn1", pv)  # a new request, even with max_per_day=1
    assert again["dose_id"] != r["dose_id"] and again["state"] == "PERSON_CHECK"
    assert commands(h) == []


def test_uncertain_outcome_counts(harness):
    h = harness(require_label_photo=False)
    pv = setup(h, [prn(interval=2, per_day=3)])
    h.sim(action="feed_mode", channel="A", mode="stall")
    r = request(h, "prn1", pv)
    h.check(r["txn_id"])
    h.check(r["txn_id"])
    h.wait(lambda s: s["workflow"]["txn"]["state"] == "UNCERTAIN", msg="uncertain")
    h.post("/api/device/reset", {"request_id": rid(), "inspected": True})
    h.wait(lambda s: s["workflow"]["txn"] is None, msg="ended")
    h.wait(lambda s: s["device"]["fault"] is None, msg="controller rebooted")
    it = item(h, "prn1")
    assert it["taken_last_24h"] == 1 and it["blocked_reason"] == "TOO_SOON"
    assert it["last_dose"]["outcome"] == "UNRESOLVED_NEEDS_REVIEW"
    h.post("/api/device/arm", {"request_id": rid(), "inspected": True})
    x = request(h, "prn1", pv, expect=409)
    assert x["reason_codes"] == ["TOO_SOON"]


def test_reminder_only_never_touches_hardware(harness):
    h = harness(require_label_photo=False)
    pv = setup(h, [prn(key="r", mode="REMINDER_ONLY")], arm=False)
    rev = h.status()["device"]["tray_revision"]
    r = request(h, "r", pv)  # works unarmed: no hardware involved
    assert r["state"] == "SELF_REPORTED" and r["self_report"] == "TAKEN_UNVERIFIED" and r["txn_id"] is None
    st = h.status()
    assert st["workflow"]["txn"] is None and st["device"]["tray_revision"] == rev
    assert h.c.app.state.svc.store.q("SELECT * FROM commands") == []
    assert h.c.app.state.svc.store.q("SELECT * FROM observations") == []  # never counts as a camera check
    ev = h.c.app.state.svc.store.q1("SELECT details_json FROM events WHERE kind='SELF_REPORT'")
    assert '"verified":false' in ev["details_json"]
    hist = next(c for c in h.c.get("/api/history").json()["doses"] if c["dose_id"] == r["dose_id"])
    assert hist["outcome"] == "USER_REPORTED_TAKEN" and hist["as_needed"] is True
    h.post("/api/doses/self-report", {"request_id": rid(), "dose_id": r["dose_id"]}, expect=409)  # not twice
    assert item(h, "r")["blocked_reason"] == "TOO_SOON"


def test_rejections_stale_plan_unknown_and_scheduled(harness):
    h = harness(require_label_photo=False)
    pv = setup(h, [prn(), sched()])
    assert request(h, "prn1", pv + 1, expect=409)["reason_codes"] == ["PLAN_VERSION_MISMATCH"]
    assert request(h, "nope", pv, expect=409)["reason_codes"] == ["UNKNOWN_ENTRY"]
    assert request(h, "sch1", pv, expect=409)["reason_codes"] == ["NOT_AS_NEEDED"]
    # A newer plan version makes the old one stale.
    plan = h.c.app.state.svc.store.active_plan()
    h.post("/api/plans/confirm", {"request_id": rid(), "person_id": plan["person_id"], "timezone": NY, "timezone_confirmed": True,
                                  "dst_reviewed": True, "base_plan_version_id": pv, "entries": [prn(interval=6)]})
    assert request(h, "prn1", pv, expect=409)["reason_codes"] == ["PLAN_VERSION_MISMATCH"]
    assert prn_doses(h, "prn1") == []


def test_device_checks_still_apply(harness):
    h = harness(require_label_photo=False)
    pv = setup(h, [prn(), prn(key="prn2", cls="orange")], arm=False)
    assert request(h, "prn1", pv, expect=409)["reason_codes"][0] == "NOT_ARMED"
    assert prn_doses(h, "prn1") == []
    h.post("/api/device/arm", {"request_id": rid(), "inspected": True})
    request(h, "prn1", pv)
    assert request(h, "prn1", pv, expect=409)["reason_codes"] == ["AS_NEEDED_IN_PROGRESS"]
    assert request(h, "prn2", pv, expect=409)["reason_codes"] == ["ANOTHER_DOSE_ACTIVE"]
    assert len(prn_doses(h, "prn2")) == 0


# ---------------------------------------------------------------- scheduled unchanged
def test_scheduled_behaviour_unchanged(harness):
    h = harness(require_label_photo=False)
    setup(h, [prn(), sched(times=("08:30",))])
    store = h.c.app.state.svc.store
    doses = store.q("SELECT * FROM doses")
    assert doses and all(d["schedule"] == "SCHEDULED" and d["entry_key"] == "sch1" for d in doses)
    assert store.q("SELECT * FROM reminders r JOIN doses d ON d.dose_id=r.dose_id WHERE d.entry_key='prn1'") == []
    st = h.status()
    assert [c["label"] for c in st["today"]] == ["Med sch1"]
    assert st["next_dose"]["label"] == "Med sch1" and st["today"][0]["schedule"] == "SCHEDULED"
    h.advance(1800)  # 8:30 New York
    st = h.wait(lambda s: s["today"][0]["reminder"]["state"] == "DUE", msg="scheduled due")
    card = st["today"][0]
    h.wait(lambda s: s["device"]["connected"], msg="device back after the clock jump")
    h.post("/api/device/arm", {"request_id": rid(), "inspected": True})
    t = h.start(card)
    assert t["dose_ids"] == [card["dose_id"]]


def test_demo_entry_and_trigger_skip_as_needed(harness):
    h = harness(require_label_photo=False)
    h.wait(lambda s: s["device"]["connected"])
    h.post("/api/enroll", {"request_id": rid(), "label": "T", "consent": True})
    h.post("/api/demo/mode", {"request_id": rid(), "enabled": True})
    st = h.status()
    p = item(h, "demoP")
    assert p["label"].endswith("(fictional)") and p["min_interval_hours"] == 4 and p["max_per_day"] == 3
    assert p["delivery_mode"] == "DEVICE" and p["cartridge"] == "B" and p["available"] is True
    assert p["plan_version_id"] == st["plan"]["plan_version_id"]
    made = h.post("/api/demo/trigger", {"request_id": rid()})["entries"]
    assert "demoP" not in made and "demoA" in made


def test_assistant_answers_from_as_needed_facts(harness):
    h = harness(require_label_photo=False)
    pv = setup(h, [prn(key="r", mode="REMINDER_ONLY", interval=4)], arm=False)
    a = h.post("/api/assistant", {"request_id": rid(), "question": "Can I take my headache pill now?"})
    assert a["answer"]["supporting_fact_ids"] == ["F-prn-r"] and "within its limits now" in a["answer"]["answer_text"]
    request(h, "r", pv)
    a = h.post("/api/assistant", {"request_id": rid(), "question": "Can I take my headache pill now?"})
    assert "not available now" in a["answer"]["answer_text"] and "too soon" in a["answer"]["answer_text"]
    assert len(prn_doses(h, "r")) == 1  # asking never writes a dose
    a = h.post("/api/assistant", {"request_id": rid(), "question": "What is my next dose?"})
    assert a["answer"]["supporting_fact_ids"] == ["F-next"]  # unrelated questions are not about as-needed medicine


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.replace(",\n  schedule TEXT NOT NULL DEFAULT 'SCHEDULED'", ""))
    assert "schedule" not in {r[1] for r in conn.execute("PRAGMA table_info(doses)")}
    conn.execute("INSERT INTO doses (dose_id, occurrence_id, slot_id, local_date, local_time, entry_key, plan_version_id, label,"
                 " quantity, delivery_mode, due_at_utc, created_at, updated_at) VALUES"
                 " ('d1','o1','k@0800','2026-11-07','08:00','k',1,'Old',1,'DEVICE','x','x','x')")
    conn.execute("INSERT INTO plan_versions (plan_id, version, timezone, entries_json, confirmed_at) VALUES"
                 " ('p',1,'America/New_York','[{\"entry_key\":\"k\",\"times\":[\"08:00\"]}]','x')")
    conn.commit()
    conn.close()
    store = Store(path, FakeClock(START))
    assert store.q1("SELECT schedule FROM doses WHERE dose_id='d1'")["schedule"] == "SCHEDULED"
    assert store.active_plan(0)["entries"][0]["schedule"] == "SCHEDULED"
    Store(path, FakeClock(START))  # rerunning the migration is harmless
