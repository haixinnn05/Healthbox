"""Reminder scheduling, snooze, dismissal, review, restart and plan edits (no hardware)."""
import uuid
from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.errors import WorkflowError
from app.plans import confirm_plan
from app.reminders import ReminderService
from app.store import Store
from app.timeutil import FakeClock, resolve_local
from datetime import date, time as dtime
from zoneinfo import ZoneInfo

NY = "America/New_York"


def rid():
    return uuid.uuid4().hex


@pytest.fixture
def env(tmp_path):
    clock = FakeClock(datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc))  # 7:00 AM New York
    settings = Settings(db_path=tmp_path / "r.db", data_dir=tmp_path)
    store = Store(settings.db_path, clock)
    rem = ReminderService(store, settings)
    with store.tx() as conn:
        conn.execute("INSERT INTO persons (person_id, label, consent_at, simulated, created_at) VALUES ('p1','T','x',1,'x')")
    return clock, settings, store, rem


def entry(key="blue1", times=("08:00",), mode="DEVICE", qty=1, cls="blue"):
    return {"entry_key": key, "label": f"Med {key}", "quantity": qty, "delivery_mode": mode,
            "class_id": cls if mode == "DEVICE" else None, "times": list(times), "confirmed": True}


def confirm(store, rem, entries, base=None):
    return confirm_plan(store, rem, max_per_channel=3, request={
        "timezone": NY, "timezone_confirmed": True, "person_id": "p1", "entries": entries,
        "base_plan_version_id": base, "dst_reviewed": True})


def reminder(store, local_time="08:00"):
    return store.q1("SELECT r.* FROM reminders r JOIN doses d ON d.dose_id=r.dose_id WHERE d.local_time=? AND d.local_date='2026-11-07'",
                    (local_time,))


def alerts(store, reminder_id):
    return store.q("SELECT * FROM events WHERE kind='REMINDER_ALERT' AND reminder_id=?", (reminder_id,))


def test_plan_requires_confirmation_of_each_entry(env):
    clock, s, store, rem = env
    e = entry()
    e["confirmed"] = False
    with pytest.raises(WorkflowError) as ex:
        confirm(store, rem, [e])
    assert ex.value.status == 422
    assert store.active_plan() is None


def test_due_sound_repeats_three_times_then_needs_review(env):
    clock, s, store, rem = env
    confirm(store, rem, [entry()])
    rem.tick()
    r = reminder(store)
    assert r["state"] == "SCHEDULED"
    clock.advance(3600)  # 8:00
    rem.tick()
    r = reminder(store)
    assert r["state"] == "DUE" and len(alerts(store, r["reminder_id"])) == 1
    for _ in range(5):
        clock.advance(60)
        rem.tick()
    assert len(alerts(store, r["reminder_id"])) == 3  # capped
    clock.advance(15 * 60)
    rem.tick()
    assert reminder(store)["state"] == "NEEDS_REVIEW"


def test_snooze_returns_after_five_minutes_and_never_moves_hardware(env):
    clock, s, store, rem = env
    confirm(store, rem, [entry()])
    clock.advance(3600)
    rem.tick()
    r = reminder(store)
    rem.snooze(rid(), r["reminder_id"])
    assert reminder(store)["state"] == "SNOOZED"
    clock.advance(4 * 60)
    rem.tick()
    assert reminder(store)["state"] == "SNOOZED"
    clock.advance(61)
    rem.tick()
    r2 = reminder(store)
    assert r2["state"] == "DUE" and r2["sound_repeat_count"] == 1
    assert store.q("SELECT * FROM commands") == []
    d = store.q1("SELECT * FROM doses WHERE dose_id=?", (r["dose_id"],))
    assert d["workflow_state"] == "NOT_STARTED" and d["self_report"] is None


def test_dismiss_only_acknowledges(env):
    clock, s, store, rem = env
    confirm(store, rem, [entry()])
    clock.advance(3600)
    rem.tick()
    r = reminder(store)
    rem.acknowledge(rid(), r["reminder_id"])
    for _ in range(3):
        clock.advance(60)
        rem.tick()
    assert reminder(store)["state"] == "ACKNOWLEDGED"
    assert len(alerts(store, r["reminder_id"])) == 1
    d = store.q1("SELECT * FROM doses WHERE dose_id=?", (r["dose_id"],))
    assert d["outcome"] is None and d["collection"] == "NOT_OBSERVED" and d["self_report"] is None


def test_idempotent_requests(env):
    clock, s, store, rem = env
    confirm(store, rem, [entry()])
    clock.advance(3600)
    rem.tick()
    r = reminder(store)
    req = rid()
    a = rem.snooze(req, r["reminder_id"])
    b = rem.snooze(req, r["reminder_id"])  # retry with same request_id
    assert a == b
    with pytest.raises(WorkflowError) as ex:
        rem.acknowledge(req, r["reminder_id"])  # same id, different action
    assert ex.value.code == "REQUEST_ID_REUSED"


def test_restart_restores_without_duplicates_or_burst(env, tmp_path):
    clock, s, store, rem = env
    confirm(store, rem, [entry()])
    clock.advance(3600)
    rem.tick()
    r = reminder(store)
    n_doses = store.q1("SELECT COUNT(*) AS n FROM doses")["n"]
    clock.advance(200)  # app "down" past two sound slots
    rem2 = ReminderService(store, s)
    rem2.restore()
    rem2.tick()
    rem2.tick()
    assert store.q1("SELECT COUNT(*) AS n FROM doses")["n"] == n_doses
    assert len(alerts(store, r["reminder_id"])) == 1  # no burst replay
    clock.advance(61)
    rem2.tick()
    assert len(alerts(store, r["reminder_id"])) == 2


def test_late_start_marks_needs_review_without_sound(env):
    clock, s, store, rem = env
    confirm(store, rem, [entry()])  # confirmed at 7:00
    clock.advance(3600 + 20 * 60)  # first tick only at 8:20
    rem.tick()
    r = reminder(store)
    assert r["state"] == "NEEDS_REVIEW"
    assert alerts(store, r["reminder_id"]) == []


def test_plan_edit_replaces_only_unissued_future_occurrences(env):
    clock, s, store, rem = env
    p1 = confirm(store, rem, [entry(times=("08:00", "20:00"))])
    clock.advance(3600)
    rem.tick()
    issued = reminder(store, "08:00")
    assert issued["state"] == "DUE"
    confirm(store, rem, [entry(times=("08:00", "21:00"), qty=2)], base=p1["plan_version_id"])
    # The issued 8:00 occurrence keeps its original version and quantity.
    kept = store.q1("SELECT * FROM doses WHERE dose_id=?", (issued["dose_id"],))
    assert kept["plan_version_id"] == p1["plan_version_id"] and kept["quantity"] == 1
    # 20:00 is gone, 21:00 exists with the new quantity.
    times = {d["local_time"]: d for d in store.q("SELECT * FROM doses WHERE local_date='2026-11-07'")}
    assert "20:00" not in times and times["21:00"]["quantity"] == 2


def test_stale_plan_edit_rejected(env):
    clock, s, store, rem = env
    confirm(store, rem, [entry()])
    with pytest.raises(WorkflowError) as ex:
        confirm(store, rem, [entry(times=("09:00",))], base=None)
    assert ex.value.code == "PLAN_VERSION_MISMATCH"


def test_reminder_only_self_report_is_unverified(env):
    clock, s, store, rem = env
    confirm(store, rem, [entry(key="vit", mode="REMINDER_ONLY")])
    clock.advance(3600)
    rem.tick()
    r = reminder(store)
    rem.mark_taken_self_report(rid(), r["dose_id"])
    d = store.q1("SELECT * FROM doses WHERE dose_id=?", (r["dose_id"],))
    assert d["self_report"] == "TAKEN_UNVERIFIED"
    assert reminder(store)["state"] == "CLOSED"
    assert store.q("SELECT * FROM observations") == []  # never counts as a camera check


def test_device_dose_cannot_self_report(env):
    clock, s, store, rem = env
    confirm(store, rem, [entry()])
    clock.advance(3600)
    rem.tick()
    with pytest.raises(WorkflowError):
        rem.mark_taken_self_report(rid(), reminder(store)["dose_id"])


def test_dst_gap_and_overlap_detected():
    tz = ZoneInfo(NY)
    assert resolve_local(date(2027, 3, 14), dtime(2, 30), tz)[1] == "NONEXISTENT"
    assert resolve_local(date(2026, 11, 1), dtime(1, 30), tz)[1] == "AMBIGUOUS"
    assert resolve_local(date(2026, 11, 7), dtime(8, 0), tz)[1] is None


def test_dst_times_need_review_before_confirm(env):
    clock, s, store, rem = env
    clock._now = datetime(2027, 3, 1, 12, 0, tzinfo=timezone.utc)  # two weeks before spring forward
    with pytest.raises(WorkflowError) as ex:
        confirm_plan(store, rem, max_per_channel=3, request={
            "timezone": NY, "timezone_confirmed": True, "person_id": "p1",
            "entries": [entry(times=("02:30",))], "base_plan_version_id": None, "dst_reviewed": False})
    assert ex.value.code == "DST_REVIEW"
    assert ex.value.extra["dst_issues"][0] == {"date": "2027-03-14", "time": "02:30", "issue": "NONEXISTENT"}


def test_quantity_limit_per_channel_at_same_time(env):
    clock, s, store, rem = env
    with pytest.raises(WorkflowError) as ex:
        confirm(store, rem, [entry(key="a", qty=2), entry(key="b", qty=2)])
    assert ex.value.status == 422
