"""One person per Healthbox, and giving it to someone else while keeping the old history."""
import json

from conftest import rid
from test_account import NAME, PW, PW2, account, create

NEW = "Robin Otter"


def persons(h):
    return h.c.get("/api/setup").json()["persons"]


def handover(h, password=PW, confirm_name=NAME, emptied=True, expect=200):
    return h.post("/api/account/handover", {"request_id": rid(), "password": password, "confirm_name": confirm_name,
                                            "emptied": emptied}, expect=expect)


def plan_with_device_entry(h):
    return h.post("/api/plans/confirm", {"request_id": rid(), "timezone": "America/New_York", "timezone_confirmed": True,
                                         "entries": [{"label": "Morning tablet", "quantity": 1, "delivery_mode": "DEVICE",
                                                      "class_id": "blue", "times": ["08:30", "20:00"], "confirmed": True}]})


def test_one_person_per_account_enrolling_again_retakes_the_same_face(harness):
    h = harness(require_label_photo=False)
    create(h)
    a = h.post("/api/enroll", {"request_id": rid(), "label": "Somebody Else", "consent": True})
    assert a["label"] == NAME and a["retaken"] is False  # the account's name, not the typed one
    b = h.post("/api/enroll", {"request_id": rid(), "label": "Another", "consent": True})
    assert b["person_id"] == a["person_id"] and b["retaken"] is True
    assert [p["label"] for p in persons(h)] == [NAME]
    # The plan always belongs to that one person, whatever the request says.
    r = plan_with_device_entry(h)
    store = h.c.app.state.svc.store
    assert store.plan_version(r["plan_version_id"])["person_id"] == a["person_id"]


def test_enrolling_is_refused_during_a_dose(harness):
    h = harness()
    card = h.demo_due()
    h.start(card)
    r = h.post("/api/enroll", {"request_id": rid(), "label": "T", "consent": True}, expect=409)
    assert r["reason_codes"] == ["HARDWARE_BUSY"]


def test_handover_needs_emptied_cartridges_the_typed_name_and_the_password(harness):
    h = harness()
    create(h)
    h.post("/api/enroll", {"request_id": rid(), "label": NAME, "consent": True})
    assert handover(h, emptied=False, expect=422)["reason_codes"] == ["VALIDATION"]
    assert "Type Zelda Quokka" in handover(h, confirm_name="Zelda", expect=422)["message"]
    assert handover(h, password=PW2, expect=401)["reason_codes"] == ["WRONG_PASSWORD"]
    assert account(h)["exists"] and len(persons(h)) == 1
    # The typed name is not case sensitive; the password is checked even when Setup is unlocked.
    r = handover(h, confirm_name=NAME.lower())
    assert r["exists"] is False and r["name"] is None


def test_handover_is_refused_during_a_dose(harness):
    h = harness()
    create(h)
    card = h.demo_due()
    h.start(card)
    assert handover(h, expect=409)["reason_codes"] == ["HARDWARE_BUSY"]


def test_handover_archives_the_person_keeps_history_and_starts_fresh(harness):
    h = harness()
    create(h)
    card = h.demo_due()  # enroll, demo mode, a due reminder, armed
    # One finished dose for history, then a second reminder left due.
    t = h.to_dose_check(card)
    h.check(t)
    h.post("/api/doses/release", {"request_id": rid(), "txn_id": t})
    h.wait(lambda s: s["workflow"]["txn"]["state"] == "COLLECTION_CHECK", msg="released")
    h.sim(action="open_door")
    h.sim(action="collect")
    h.check(t)
    h.sim(action="close_door")
    h.post("/api/demo/trigger", {"request_id": rid(), "entry_keys": ["demoA"]})
    h.advance(31)
    h.wait(lambda s: any(c["reminder"]["state"] == "DUE" for c in s["today"]), msg="second due")
    h.post("/api/sim/hardware", {"action": "place_token", "channel": "A", "n": 1})
    h.post("/api/cartridges/check-pill", {"request_id": rid(), "cartridge": "A"})
    h.post("/api/cartridges/fill", {"request_id": rid(), "cartridge": "A", "count": 20})
    h.post("/api/sim/hardware", {"action": "place_token", "channel": "A", "n": -9})
    svc = h.c.app.state.svc
    face = svc.store.current_person()["embedding_ref"]

    handover(h)

    st = h.status()
    assert st["account"]["exists"] is False and st["demo_mode"] is False
    assert st["workflow"]["armed"] is False
    assert st["today"] == [] and st["followups"] == [] and st["plan"] is None
    assert all(c["count"] is None for c in st["cartridges"])
    assert persons(h) == []
    old = svc.store.q("SELECT * FROM persons")
    assert len(old) == 1 and old[0]["archived_at"] and old[0]["embedding_ref"] is None and old[0]["label"] == NAME
    if face:
        assert not (svc.settings.data_dir / face).exists()
    assert svc.store.q("SELECT COUNT(*) AS n FROM reminders WHERE state NOT IN ('CLOSED')")[0]["n"] == 0
    assert svc.store.q1("SELECT close_reason FROM reminders ORDER BY rowid DESC LIMIT 1")["close_reason"] == "HANDED_OVER"
    # History stays, labelled with the old name (demo records show in demo mode).
    h.post("/api/demo/mode", {"request_id": rid(), "enabled": True})
    hist = h.c.get("/api/history").json()["doses"]
    assert hist and all(c["earlier_person"] == NAME for c in hist)
    assert any(c["collection"] == "OBSERVED" for c in hist)
    events = h.c.get("/api/export/events.csv").text
    assert "ACCOUNT_HANDED_OVER" in events and PW not in events

    # The new person starts from scratch: own account, own face, and none of the old limits.
    h.post("/api/demo/mode", {"request_id": rid(), "enabled": False})
    create(h, name=NEW, password=PW2)
    a = h.post("/api/enroll", {"request_id": rid(), "label": NEW, "consent": True})
    assert a["retaken"] is False and a["person_id"] != old[0]["person_id"]
    assert [p["label"] for p in persons(h)] == [NEW]
    h.post("/api/demo/mode", {"request_id": rid(), "enabled": True})
    st = h.status()
    assert st["plan"]["recipient"] == NEW
    need = next(i for i in st["as_needed"] if i["entry_key"] == "demoP")
    assert need["taken_last_24h"] == 0 and need["available"]


def test_as_needed_limits_ignore_the_previous_person(harness):
    h = harness()
    create(h)
    h.demo_due()
    st = h.status()
    item = next(i for i in st["as_needed"] if i["entry_key"] == "demoP")
    h.post("/api/doses/request", {"request_id": rid(), "entry_key": "demoP", "plan_version": item["plan_version_id"]})
    t = h.txn()["txn_id"]
    h.post("/api/doses/cancel", {"request_id": rid(), "txn_id": t})
    # A dose that counts: self-reported reminder-only style is not available for demoP, so use the store directly.
    svc = h.c.app.state.svc
    with svc.store.tx() as conn:
        conn.execute("UPDATE doses SET workflow_state='COMPLETE', outcome='RELEASED', collection='OBSERVED' WHERE entry_key='demoP'")
    assert next(i for i in h.status()["as_needed"] if i["entry_key"] == "demoP")["taken_last_24h"] == 1
    handover(h)
    create(h, name=NEW, password=PW2)
    h.post("/api/enroll", {"request_id": rid(), "label": NEW, "consent": True})
    h.post("/api/demo/mode", {"request_id": rid(), "enabled": True})
    assert next(i for i in h.status()["as_needed"] if i["entry_key"] == "demoP")["taken_last_24h"] == 0
