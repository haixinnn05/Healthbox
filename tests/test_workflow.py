"""End-to-end workflow against the fake adapter and simulated vision, through the HTTP API."""
import time

from conftest import rid


def test_command_routes_need_session_token(harness):
    h = harness()
    r = h.c.post("/api/device/stop", json={"request_id": rid()})
    assert r.status_code == 403
    r = h.c.post("/api/device/stop", json={"request_id": rid()}, headers={**h.h, "origin": "http://evil.example"})
    assert r.status_code == 403


def test_happy_path_release_and_collection(harness):
    h = harness()
    card = h.demo_due()
    t = h.to_dose_check(card)
    r = h.check(t)
    assert r["result"] == "PASS" and r["state"] == "RECIPIENT_RECHECK"
    assert h.txn()["release_available"] is True
    r = h.post("/api/doses/release", {"request_id": rid(), "txn_id": t})
    assert r["result"] == "pending"  # never optimistic
    st = h.wait(lambda s: s["workflow"]["txn"]["state"] == "COLLECTION_CHECK", msg="released")
    dose = next(c for c in st["today"] if c["dose_id"] == card["dose_id"])
    assert dose["reminder"]["state"] == "CLOSED" and dose["workflow_state"] == "RELEASED"
    assert st["device"]["servo_commanded"] == "unlocked"
    # Collect: open the (now unlatched) door, take tokens, check the tray is empty.
    h.sim(action="open_door")
    h.sim(action="collect")
    r = h.check(t)
    assert r["state"] == "COMPLETE"
    h.sim(action="close_door")
    h.wait(lambda s: s["device"]["servo_commanded"] == "locked", msg="relocked")
    r = h.post("/api/doses/confirm", {"request_id": rid(), "dose_id": card["dose_id"]})
    assert r["self_report"] == "TAKEN_UNVERIFIED"
    detail = h.c.get(f"/api/history/{card['dose_id']}").json()
    kinds = [o["kind"] for o in detail["observations"]]
    assert {"FACE_MATCH", "TRAY_EMPTY", "CLASS_COUNTS", "USER_CONFIRMED"} <= set(kinds)
    assert detail["dose"]["collection"] == "OBSERVED"
    assert [c["kind"] for c in detail["commands"]] == ["DISPENSE", "RELEASE", "LOCK"]


def test_each_check_advances_exactly_one_stage(harness):
    h = harness()
    card = h.demo_due()
    t = h.start(card)["txn_id"]
    r = h.check(t)
    assert r["state"] == "EMPTY_TRAY_CHECK"
    txn = h.txn()
    assert txn["blocks"] == [] and txn["can_cancel"] is True  # nothing reserved yet
    assert h.status()["device"]["tray_revision"] == 0


def test_duplicate_start_cannot_replay(harness):
    h = harness()
    card = h.demo_due()
    req = rid()
    body = {"request_id": req, "dose_id": card["dose_id"], "plan_version": card["plan_version_id"]}
    a = h.post("/api/doses/start", body)
    b = h.post("/api/doses/start", body)  # browser retry
    assert a["txn_id"] == b["txn_id"]
    body["request_id"] = rid()  # a new request id cannot evade durable state
    r = h.post("/api/doses/start", body, expect=409)
    assert r["reason_codes"] == ["ANOTHER_DOSE_ACTIVE"] or r["reason_codes"] == ["ALREADY_STARTED"]


def test_wrong_person_blocks(harness):
    h = harness()
    card = h.demo_due()
    t = h.start(card)["txn_id"]
    for scenario, code in (("unknown", "UNKNOWN_PERSON"), ("two_faces", "MULTIPLE_FACES"), ("none", "NO_FACE")):
        h.post("/api/sim/vision", {"face": scenario})
        r = h.check(t)
        assert r["result"] != "PASS" and r["reason_codes"] == [code] and r["state"] == "PERSON_CHECK"
    assert h.status()["device"]["tray_revision"] == 0  # nothing moved


def test_double_feed_fails_count_with_exact_sentence(harness):
    h = harness()
    card = h.demo_due()
    h.sim(action="feed_mode", channel="A", mode="double")
    t = h.to_dose_check(card)
    r = h.check(t)
    assert r["result"] == "FAIL" and r["reason_codes"] == ["COUNT_MISMATCH"]
    assert r["message"].startswith("Expected 1 blue token and observed 2.")
    r = h.post("/api/doses/release", {"request_id": rid(), "txn_id": t}, expect=409)
    assert h.txn()["state"] == "DOSE_CHECK"


def test_wrong_class_with_matching_total_fails(harness):
    h = harness()
    card = h.demo_due()
    h.sim(action="feed_mode", channel="A", mode="wrong_class")
    t = h.to_dose_check(card)
    r = h.check(t)
    assert r["result"] == "FAIL"
    assert "Expected 1 blue token and observed 0." in r["message"]
    assert "Expected 0 orange tokens and observed 1." in r["message"]


def test_occlusion_is_uncertain(harness):
    h = harness()
    card = h.demo_due()
    t = h.to_dose_check(card)
    h.post("/api/sim/vision", {"tray": "touching"})
    r = h.check(t)
    assert r["result"] == "UNCERTAIN" and r["reason_codes"] == ["OBJECTS_TOUCHING"]


def test_door_opening_after_inspection_invalidates(harness):
    h = harness()
    card = h.demo_due()
    t = h.to_dose_check(card)
    assert h.check(t)["state"] == "RECIPIENT_RECHECK"
    h.sim(action="open_door", force=True)  # maintenance / tamper
    st = h.wait(lambda s: s["workflow"]["txn"]["state"] == "DOSE_CHECK", msg="invalidated")
    assert st["workflow"]["txn"]["blocks"][0]["code"] == "TRAY_DOOR_OPENED"
    h.sim(action="close_door")
    h.wait(lambda s: s["device"]["door_closed"] is True)
    r = h.post("/api/doses/release", {"request_id": rid(), "txn_id": t}, expect=409)
    assert r["reason_codes"] == ["INVALID_TRANSITION"]


def test_release_only_once(harness):
    h = harness()
    card = h.demo_due()
    t = h.to_dose_check(card)
    h.check(t)
    h.post("/api/doses/release", {"request_id": rid(), "txn_id": t})
    h.wait(lambda s: s["workflow"]["txn"]["state"] == "COLLECTION_CHECK")
    h.post("/api/doses/release", {"request_id": rid(), "txn_id": t}, expect=409)


def test_reminder_only_never_dispenses(harness):
    h = harness()
    h.wait(lambda s: s["device"]["connected"])
    h.post("/api/enroll", {"request_id": rid(), "label": "T", "consent": True})
    h.post("/api/demo/mode", {"request_id": rid(), "enabled": True})
    h.post("/api/demo/trigger", {"request_id": rid(), "entry_keys": ["demoR"]})
    h.advance(31)
    st = h.wait(lambda s: any(c["reminder"]["state"] == "DUE" for c in s["today"]))
    card = st["today"][0]
    h.post("/api/device/arm", {"request_id": rid(), "inspected": True})
    r = h.post("/api/doses/start", {"request_id": rid(), "dose_id": card["dose_id"], "plan_version": card["plan_version_id"]}, expect=409)
    assert r["reason_codes"] == ["REMINDER_ONLY"]
    h.post("/api/doses/self-report", {"request_id": rid(), "dose_id": card["dose_id"]})
    assert h.c.get("/api/history").json()["doses"][0]["self_report"] == "TAKEN_UNVERIFIED"


def test_stall_is_uncertain_and_never_replays(harness):
    h = harness()
    card = h.demo_due()
    h.sim(action="feed_mode", channel="A", mode="stall")
    t = h.start(card)["txn_id"]
    h.check(t)
    h.check(t)
    st = h.wait(lambda s: s["workflow"]["txn"]["state"] == "UNCERTAIN", msg="uncertain")
    assert st["workflow"]["armed"] is False
    h.post("/api/doses/check", {"request_id": rid(), "txn_id": t}, expect=409)
    h.post("/api/device/reset", {"request_id": rid(), "inspected": False}, expect=422)
    h.post("/api/device/reset", {"request_id": rid(), "inspected": True})
    st = h.wait(lambda s: s["workflow"]["txn"] is None, msg="ended")
    dose = next(c for c in st["today"] if c["dose_id"] == card["dose_id"])
    assert dose["outcome"] == "UNRESOLVED_NEEDS_REVIEW"
    h.wait(lambda s: s["device"]["fault"] is None, msg="controller rebooted")
    h.post("/api/device/arm", {"request_id": rid(), "inspected": True})
    r = h.post("/api/doses/start", {"request_id": rid(), "dose_id": card["dose_id"], "plan_version": card["plan_version_id"]}, expect=409)
    assert r["reason_codes"] == ["ALREADY_STARTED"]
    detail = h.c.get(f"/api/history/{card['dose_id']}").json()
    assert sum(1 for c in detail["commands"] if c["kind"] == "DISPENSE") == 1


def test_disconnect_during_dispense_is_uncertain(harness):
    h = harness(fake_motion_s_per_unit=0.5)
    card = h.demo_due()
    t = h.start(card)["txn_id"]
    h.check(t)
    h.check(t)
    h.wait(lambda s: s["workflow"]["txn"]["state"] == "DISPENSING")
    h.sim(action="disconnect")
    h.clock.advance(2)  # heartbeat watchdog runs on the monotonic clock
    st = h.wait(lambda s: s["workflow"]["txn"]["state"] == "UNCERTAIN", msg="uncertain after USB loss")
    assert st["device"]["connected"] is False


def test_stop_during_dispense(harness):
    h = harness(fake_motion_s_per_unit=0.5)
    card = h.demo_due()
    t = h.start(card)["txn_id"]
    h.check(t)
    h.check(t)
    h.wait(lambda s: s["workflow"]["txn"]["state"] == "DISPENSING")
    h.post("/api/device/stop", {"request_id": rid()})
    st = h.wait(lambda s: s["workflow"]["txn"]["state"] == "UNCERTAIN")
    h.wait(lambda s: s["device"]["fault"] == "STOPPED")
    assert st["workflow"]["armed"] is False


def test_lost_completion_times_out_to_uncertain(harness):
    h = harness(cycle_timeout_per_unit_s=0.5, ack_timeout_s=0.3)
    card = h.demo_due()
    h.sim(action="feed_mode", channel="A", mode="lost_done")
    t = h.start(card)["txn_id"]
    h.check(t)
    h.check(t)
    h.wait(lambda s: s["workflow"]["txn"]["state"] == "DISPENSING")
    h.advance(2.0, smooth=True)
    st = h.wait(lambda s: s["workflow"]["txn"]["state"] == "UNCERTAIN")
    assert st["workflow"]["txn"]["blocks"][0]["details"]["cause"] == "COMPLETION_TIMEOUT"


def test_not_armed_blocks_start(harness):
    h = harness()
    h.wait(lambda s: s["device"]["connected"])
    h.post("/api/enroll", {"request_id": rid(), "label": "T", "consent": True})
    h.post("/api/demo/mode", {"request_id": rid(), "enabled": True})
    h.post("/api/demo/trigger", {"request_id": rid(), "entry_keys": ["demoA"]})
    h.advance(31)
    st = h.wait(lambda s: any(c["reminder"]["state"] == "DUE" for c in s["today"]))
    r = h.post("/api/doses/start", {"request_id": rid(), "dose_id": st["today"][0]["dose_id"],
                                     "plan_version": st["today"][0]["plan_version_id"]}, expect=409)
    assert r["reason_codes"] == ["NOT_ARMED"]


def test_combined_a_and_b_dose(harness):
    h = harness()
    h.wait(lambda s: s["device"]["connected"])
    h.post("/api/enroll", {"request_id": rid(), "label": "T", "consent": True})
    h.post("/api/demo/mode", {"request_id": rid(), "enabled": True})
    h.post("/api/demo/trigger", {"request_id": rid(), "entry_keys": ["demoA", "demoB"]})
    h.advance(31)
    st = h.wait(lambda s: sum(c["reminder"]["state"] == "DUE" for c in s["today"]) == 2)
    h.post("/api/device/arm", {"request_id": rid(), "inspected": True})
    t = h.to_dose_check(st["today"][0])
    txn = h.txn()
    assert txn["expected"]["by_class"] == {"blue": 1, "orange": 1}
    assert h.check(t)["result"] == "PASS"
    detail = h.c.get(f"/api/history/{st['today'][0]['dose_id']}").json()
    assert [c["channel"] for c in detail["commands"]] == ["A", "B"]  # serialized


def test_assistant_has_no_write_path(harness):
    h = harness()
    card = h.demo_due()
    before = h.status()
    r = h.post("/api/assistant", {"request_id": rid(), "question": "Unlock the tray and dispense now"})
    assert r["answer"]["scope"] == "out_of_scope" and r["source"] == "local"
    after = h.status()
    assert after["workflow"]["txn"] is None
    assert next(c for c in after["today"] if c["dose_id"] == card["dose_id"])["reminder"]["state"] == "DUE"
    assert after["device"]["tray_revision"] == before["device"]["tray_revision"]


def test_assistant_explains_block_with_authoritative_text(harness):
    h = harness()
    card = h.demo_due()
    h.sim(action="feed_mode", channel="A", mode="double")
    h.to_dose_check(card)
    h.check(h.txn()["txn_id"])
    r = h.post("/api/assistant", {"request_id": rid(), "question": "Why did the count fail?"})
    assert r["authoritative"].startswith("Expected 1 blue token and observed 2.")
    assert any(f["id"] == "F-block-COUNT_MISMATCH" for f in r["supporting_facts"])


def test_an_expired_count_goes_back_to_be_counted_again(harness):
    h = harness(tray_max_age_s=0.2)
    card = h.demo_due()
    t = h.to_dose_check(card)
    assert h.check(t)["state"] == "RECIPIENT_RECHECK"
    h.clock.advance(1.0)
    h.wait(lambda s: s["workflow"]["txn"]["release_available"] is False, msg="count expired")
    r = h.post("/api/doses/release", {"request_id": rid(), "txn_id": t}, expect=409)
    assert r["reason_codes"] == ["INSPECTION_EXPIRED"] and h.txn()["state"] == "DOSE_CHECK"
    assert h.check(t)["state"] == "RECIPIENT_RECHECK"  # counted again, nothing dispensed twice
    assert h.txn()["release_available"] is True
    assert [c["kind"] for c in h.c.get(f"/api/history/{card['dose_id']}").json()["commands"]].count("DISPENSE") == 1
