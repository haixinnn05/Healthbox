"""Signed out, Healthbox shows only what a due dose needs; signed in, everything."""
from conftest import rid
from test_account import PW, create, lock, unlock


def setup_due(h):
    """Account, face, demo plan, one dose due now and the device armed. Returns the due card."""
    h.wait(lambda s: s["device"]["connected"], msg="device")
    create(h)
    card = h.demo_due()
    return card


def test_signed_out_shows_only_the_due_dose(harness):
    h = harness()
    card = setup_due(h)
    full = h.status()
    assert full["signed_out"] is False and full["plan"] and full["as_needed"]
    lock(h)
    st = h.status()
    assert st["signed_out"] is True and st["account"]["unlocked"] is False
    assert st["plan"] is None and st["as_needed"] == [] and st["next_dose"] is None and st["cartridges"] == []
    assert [c["dose_id"] for c in st["today"]] == [card["dose_id"]]
    # The due dose can be taken without signing in, all the way to release.
    t = h.to_dose_check(card)
    assert h.check(t)["state"] == "RECIPIENT_RECHECK"
    h.post("/api/doses/release", {"request_id": rid(), "txn_id": t})
    st = h.wait(lambda s: s["workflow"]["txn"]["state"] == "COLLECTION_CHECK", msg="released")
    assert st["signed_out"] and [c["dose_id"] for c in st["today"]] == [card["dose_id"]]
    # Signing in brings everything back.
    unlock(h)
    st = h.status()
    assert st["signed_out"] is False and st["plan"] and st["as_needed"]


def test_signed_out_blocks_records_photos_ask_and_as_needed(harness):
    h = harness()
    setup_due(h)
    item = h.status()["as_needed"][0]
    lock(h)
    for path in ("/api/history", "/api/history/abc", "/api/setup", "/api/metrics", "/api/export/events.csv",
                 "/api/export/metrics.csv", "/api/evidence/0123456789abcdef.jpg", "/api/labels/0123456789ab/image"):
        r = h.c.get(path)
        assert r.status_code == 423 and r.json()["reason_codes"] == ["SIGNED_OUT"], path
    r = h.post("/api/doses/request", {"request_id": rid(), "entry_key": item["entry_key"], "plan_version": item["plan_version_id"]},
               expect=423)
    assert r["reason_codes"] == ["SIGNED_OUT"]
    h.post("/api/assistant", {"request_id": rid(), "question": "What is next?"}, expect=423)
    unlock(h)
    assert h.c.get("/api/history").status_code == 200


def test_no_account_means_nothing_is_hidden(harness):
    h = harness()
    h.demo_due()
    st = h.status()
    assert st["signed_out"] is False and st["plan"]
    assert h.c.get("/api/history").status_code == 200


def test_stays_signed_in_until_sign_out_by_default(harness):
    h = harness(setup_unlock_minutes=0)
    r = create(h)
    assert r["unlocked"] and r["unlocked_until"] is None
    h.advance(24 * 3600)
    assert h.status()["account"]["unlocked"] is True
    lock(h)
    assert h.status()["account"]["unlocked"] is False
    assert unlock(h, PW)["unlocked_until"] is None


def test_first_and_last_name(harness):
    h = harness()
    r = h.post("/api/account/create", {"request_id": rid(), "first_name": " Ada ", "last_name": "Lovelace", "password": PW})
    assert r["name"] == "Ada Lovelace" and r["first_name"] == "Ada"


def test_last_name_is_required_with_a_first_name(harness):
    h = harness()
    r = h.post("/api/account/create", {"request_id": rid(), "first_name": "Ada", "last_name": " ", "password": PW}, expect=422)
    assert r["errors"][0]["field"] == "last_name"


def test_one_name_field_greets_with_the_first_word(harness):
    h = harness()
    assert create(h, name="Grace Hopper")["first_name"] == "Grace"
