"""Photo proof, who emptied the tray, the hand-to-mouth observation, the "did you take it?"
follow-up, look-alike pill types, and cartridge fills with a pill count."""
import json
import math

import pytest

from app import evidence
from app.taking import ActionTracker, Sample, collector, decode_palms, palm_anchors
from app.vision import look_alike_pairs
from conftest import rid


def release(h, card):
    t = h.to_dose_check(card)
    assert h.check(t)["state"] == "RECIPIENT_RECHECK"
    h.post("/api/doses/release", {"request_id": rid(), "txn_id": t})
    h.wait(lambda s: s["workflow"]["txn"]["state"] == "COLLECTION_CHECK", msg="released")
    return t


def collect(h, t):
    h.sim(action="open_door")
    h.sim(action="collect")
    r = h.check(t)
    assert r["state"] == "COMPLETE"
    h.sim(action="close_door")


def detail(h, dose_id):
    return h.c.get(f"/api/history/{dose_id}").json()


def obs_of(det, kind):
    return [o for o in det["observations"] if o["kind"] == kind]


# ---------------------------------------------------------------- photo proof
def test_every_tray_check_keeps_a_photo_of_the_tray(harness):
    h = harness()
    card = h.demo_due()
    t = h.to_dose_check(card)
    h.check(t)
    tx = h.txn()
    for kind in ("TRAY_EMPTY", "CLASS_COUNTS"):
        photo = tx["observations"][kind]["photo"]
        assert photo and photo.startswith("evidence/")
        r = h.c.get(f"/api/{photo}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    # Only names this module writes are served.
    for bad in ("../t.db", "x.jpg", "0123456789abcdef.png", "%2e%2e%2ft.db"):
        assert h.c.get(f"/api/evidence/{bad}").status_code == 404


def test_markers_hidden_keeps_no_photo(harness):
    h = harness()
    card = h.demo_due()
    t = h.to_dose_check(card)
    h.post("/api/sim/vision", {"tray": "markers_missing"})
    h.check(t)
    assert h.txn()["observations"]["CLASS_COUNTS"]["photo"] is None


def test_old_photos_are_pruned(tmp_path):
    import numpy as np
    img = np.zeros((20, 20, 3), np.uint8)
    names = [evidence.save(tmp_path, img, keep=3) for _ in range(5)]
    left = sorted(p.name for p in (tmp_path / "evidence").iterdir())
    assert len(left) == 3 and all(n for n in names)


def test_demo_reset_removes_demo_photos(harness):
    h = harness()
    card = h.demo_due()
    t = h.to_dose_check(card)
    h.check(t)
    photo = h.txn()["observations"]["CLASS_COUNTS"]["photo"]
    data_dir = h.c.app.state.svc.settings.data_dir
    assert (data_dir / photo).exists()
    # Finish the dose so the device is idle, then reset demo records.
    h.post("/api/doses/release", {"request_id": rid(), "txn_id": t})
    h.wait(lambda s: s["workflow"]["txn"]["state"] == "COLLECTION_CHECK", msg="released")
    collect(h, t)
    h.post("/api/demo/reset", {"request_id": rid()})
    assert not (data_dir / photo).exists()


# ---------------------------------------------------------------- collection and taking
def test_collection_records_who_and_the_hand_to_mouth_movement(harness):
    h = harness()
    card = h.demo_due()
    t = release(h, card)
    st = h.wait(lambda s: (s["workflow"]["txn"]["watch"] or {}).get("action_observed"), msg="action seen")
    w = st["workflow"]["txn"]["watch"]
    assert w["simulated"] and w["collector_so_far"] == "RECIPIENT" and w["counts"]["match"] >= 3
    collect(h, t)
    det = detail(h, card["dose_id"])
    assert det["dose"]["collected_by"] == "RECIPIENT"
    assert det["dose"]["action"] == "OBSERVED"
    (by,) = obs_of(det, "COLLECTED_BY")
    assert by["result"] == "PASS" and by["details"]["collector"] == "RECIPIENT"
    (act,) = obs_of(det, "ACTION_OBSERVED")
    assert act["result"] == "PASS" and act["details"]["ingestion"] == "UNVERIFIED"
    assert "swallow" in act["details"]["message"]  # it says plainly that swallowing is not shown
    # The person's own report is still separate and still possible.
    assert det["dose"]["self_report"] is None
    h.post("/api/doses/confirm", {"request_id": rid(), "dose_id": card["dose_id"]})
    h.advance(400)
    assert h.status()["followups"] == []


def test_someone_else_at_the_tray_is_recorded(harness):
    h = harness()
    card = h.demo_due()
    t = release(h, card)
    h.post("/api/sim/vision", {"face": "unknown"})
    h.wait(lambda s: (s["workflow"]["txn"]["watch"] or {}).get("collector_so_far") == "OTHER_PERSON", msg="other person")
    collect(h, t)
    det = detail(h, card["dose_id"])
    assert det["dose"]["collected_by"] == "OTHER_PERSON"
    (by,) = obs_of(det, "COLLECTED_BY")
    assert by["result"] == "FAIL" and by["reason_code"] == "COLLECTED_BY_OTHER"


def test_no_movement_seen_then_follow_up(harness):
    h = harness(followup_minutes=5)
    card = h.demo_due()
    h.post("/api/sim/vision", {"action": "not_seen"})
    t = release(h, card)
    collect(h, t)
    assert h.status()["followups"] == []  # not yet: it waits FOLLOWUP_MINUTES after release
    h.advance(181)  # past the watch window
    st = h.wait(lambda s: any(c["dose_id"] == card["dose_id"] and c["action"] == "NOT_OBSERVED" for c in s["today"]),
                msg="action not observed")
    h.advance(120)
    st = h.status()
    (f,) = st["followups"]
    assert f["dose_id"] == card["dose_id"] and f["followup"] == "taken"
    det = detail(h, card["dose_id"])
    (act,) = obs_of(det, "ACTION_OBSERVED")
    assert act["result"] == "NOT_OBSERVED" and act["reason_code"] == "ACTION_NOT_SEEN"
    # "Not yet" hides it for FOLLOWUP_MINUTES; "I took it" ends it.
    h.post("/api/doses/followup-later", {"request_id": rid(), "dose_id": card["dose_id"]})
    assert h.status()["followups"] == []
    h.advance(301)
    assert len(h.status()["followups"]) == 1
    h.post("/api/doses/confirm", {"request_id": rid(), "dose_id": card["dose_id"]})
    assert h.status()["followups"] == []


def test_uncollected_dose_follow_up_asks_to_collect(harness):
    h = harness()
    card = h.demo_due()
    h.post("/api/sim/vision", {"action": "not_seen"})
    release(h, card)
    h.advance(301)
    (f,) = h.status()["followups"]
    assert f["followup"] == "collect"


def test_follow_up_later_refuses_doses_not_released(harness):
    h = harness()
    card = h.demo_due()
    h.post("/api/doses/followup-later", {"request_id": rid(), "dose_id": card["dose_id"]}, expect=409)


# ---------------------------------------------------------------- tracker rules
def S(t, near=None, face="match", mouth=(100.0, 100.0), palms_checked=True):
    palms = [] if near is None else [{"box": [100.0 + near * 50, 100.0, 10.0, 10.0]}]
    return Sample(t=t, face=face, recipient=face == "match", mouth=mouth if face == "match" else None,
                  face_w=50.0, palms=palms, palms_checked=palms_checked)


def tracker():
    return ActionTracker(near_ratio=0.6, far_ratio=1.5, min_near=2, near_window_s=2.0)


def test_tracker_needs_away_then_near_twice():
    tr = tracker()
    assert not tr.feed(S(0.0, near=3.0))  # hand away
    assert not tr.feed(S(0.3, near=0.2))  # one sample at the mouth is not enough
    assert tr.feed(S(0.6, near=0.1))
    assert tr.observed_at == 0.6 and tr.details["near_samples"] == 2


def test_tracker_ignores_a_hand_that_was_always_at_the_face():
    tr = tracker()
    for i in range(10):
        assert not tr.feed(S(i * 0.2, near=0.1))


def test_tracker_reuses_mouth_when_the_hand_hides_the_face():
    tr = tracker()
    tr.feed(S(0.0, near=None))  # no hand in view counts as away
    tr.feed(S(0.2, near=0.1, face="none", mouth=None))
    assert tr.feed(S(0.4, near=0.1, face="none", mouth=None))


def test_tracker_does_not_trust_a_stale_mouth_or_a_stranger():
    tr = tracker()
    tr.feed(S(0.0, near=3.0))
    tr.feed(S(2.0, near=0.1, face="none", mouth=None))  # mouth last seen 2 s ago
    assert not tr.feed(S(2.2, near=0.1, face="none", mouth=None))
    tr2 = tracker()
    for i in range(6):  # an unknown face never gives a mouth to measure from
        assert not tr2.feed(S(i * 0.2, near=3.0 if i < 2 else 0.1, face="unknown"))


def test_tracker_without_the_hand_model_never_observes():
    tr = tracker()
    for i in range(6):
        assert not tr.feed(S(i * 0.2, near=3.0 if i < 2 else 0.1, palms_checked=False))


def test_collector_rules():
    assert collector({"match": 5}, 3) == "RECIPIENT"
    assert collector({"match": 5, "multi": 6}, 3, {"multi": 6, "unknown": 0}) == "OTHER_PERSON"  # a second face stayed
    assert collector({"match": 2, "unknown": 6}, 3, {"multi": 0, "unknown": 6}) == "OTHER_PERSON"
    assert collector({"match": 6, "unknown": 3}, 3) == "RECIPIENT"  # an odd angle now and then is not a stranger
    assert collector({"none": 20}, 3) == "NOT_SEEN"


def test_scattered_two_face_blips_are_not_another_person():
    """From a real dose: you matched in 170 of 183 samples; 13 scattered frames briefly found a second
    'face' (a hand or something behind). That is detector noise, not someone else."""
    from app.taking import Watch
    w = Watch("t", "p", 0.0, tracker(), True, True, False, run_needed=5)
    pattern = (["match"] * 13 + ["multi"]) * 13 + ["match"] * 1
    for i, face in enumerate(pattern):
        w.add(Sample(t=i * 0.2, face=face, recipient=face == "match"), "now")
    assert w.counts["multi"] == 13 and w.runs["multi"] == 1
    assert w.view(3)["collector_so_far"] == "RECIPIENT"
    for i in range(5):  # a real second person stays in view
        w.add(Sample(t=100 + i * 0.2, face="multi"), "now")
    assert w.view(3)["collector_so_far"] == "OTHER_PERSON"


def test_palm_decode_places_a_box_at_its_anchor():
    import numpy as np
    anchors = palm_anchors()
    assert anchors.shape == (2016, 2)
    reg = np.zeros((1, 2016, 18), np.float32)
    logits = np.full((1, 2016, 1), -20.0, np.float32)
    i = 100  # anchor in the 24x24 grid
    reg[0, i, 2:4] = 48.0  # 48 of 192 input pixels wide and high
    logits[0, i, 0] = 5.0
    # A 400x400 image: no padding, scale 400.
    out = decode_palms([reg, logits], anchors, (400, 400), 192 / 400, (0, 0), 0.6)
    assert len(out) == 1
    cx, cy = anchors[i] * 400
    x, y, w, hgt = out[0]["box"]
    assert math.isclose(x + w / 2, cx, abs_tol=0.5) and math.isclose(y + hgt / 2, cy, abs_tol=0.5)
    assert math.isclose(w, 100, abs_tol=0.5)


# ---------------------------------------------------------------- look-alike pill types
def cls(class_id, cart, ranges, sample=None, label=None):
    hsv = {"ranges": ranges}
    if sample:
        hsv["sample_hsv"] = sample
    return {"class_id": class_id, "label": label or class_id, "cartridge": cart, "color": "#FFFFFF", "hsv": hsv}


def test_look_alike_pairs():
    white = [[[0, 0, 150], [179, 70, 255]]]
    assert look_alike_pairs([cls("a", "A", white, [0, 20, 230]), cls("b", "B", white, [10, 30, 220])])
    blue = cls("blue", "A", [[[95, 90, 60], [130, 255, 255]]])
    orange = cls("orange", "B", [[[5, 110, 90], [22, 255, 255]]])
    assert look_alike_pairs([blue, orange]) == []
    # A pill whose colour falls inside another type's range collides even if the reverse is not true.
    wide_orange = cls("o", "B", [[[0, 50, 50], [40, 255, 255]]], [15, 200, 200])
    narrow = cls("n", "A", [[[20, 150, 150], [30, 255, 255]]], [25, 200, 200])
    assert len(look_alike_pairs([narrow, wide_orange])) == 1


def test_look_alike_types_block_starting_a_dose(harness, monkeypatch):
    from app.vision import VisionService
    h = harness()
    card = h.demo_due()
    store = h.c.app.state.svc.store
    white = json.dumps({"ranges": [[[0, 0, 150], [179, 70, 255]]], "sample_hsv": [0, 20, 230]})
    with store.tx() as conn:
        conn.execute("UPDATE token_classes SET hsv_json=?", (white,))
    st = h.status()
    assert st["look_alike"] and st["look_alike_blocks"] is False  # simulated tray: the camera is not used to count
    monkeypatch.setattr(VisionService, "tray_simulated", property(lambda self: False))
    r = h.post("/api/doses/start", {"request_id": rid(), "dose_id": card["dose_id"], "plan_version": card["plan_version_id"]},
               expect=409)
    assert r["reason_codes"][0] == "LOOK_ALIKE_PILLS" and "too alike" in r["message"]


# ---------------------------------------------------------------- cartridges
def cart(h, letter):
    return next(c for c in h.status()["cartridges"] if c["cartridge"] == letter)


def check_pill(h, letter, expect=200):
    return h.post("/api/cartridges/check-pill", {"request_id": rid(), "cartridge": letter}, expect=expect)


def clear_tray(h):
    for ch in ("A", "B"):
        h.sim(action="place_token", channel=ch, n=-9)


def test_fill_needs_a_matching_pill_check(harness):
    h = harness()
    h.wait(lambda s: s["device"]["connected"], msg="device")
    r = h.post("/api/cartridges/fill", {"request_id": rid(), "cartridge": "A", "count": 30}, expect=409)
    assert r["reason_codes"][0] == "FILL_CHECK_REQUIRED"
    r = check_pill(h, "A")
    assert r["result"] == "FAIL" and r["reason_code"] == "FILL_NO_PILL"
    h.sim(action="place_token", channel="B", n=1)
    r = check_pill(h, "A")
    assert r["reason_code"] == "FILL_WRONG_PILL" and "Orange" in r["message"]
    clear_tray(h)
    h.sim(action="place_token", channel="A", n=2)
    assert check_pill(h, "A")["reason_code"] == "FILL_TOO_MANY"
    h.post("/api/cartridges/fill", {"request_id": rid(), "cartridge": "A", "count": 30}, expect=409)
    clear_tray(h)
    h.sim(action="place_token", channel="A", n=1)
    r = check_pill(h, "A")
    assert r["result"] == "PASS" and r["photo"] and "matches" in r["message"]
    h.post("/api/cartridges/fill", {"request_id": rid(), "cartridge": "A", "count": 30})
    c = cart(h, "A")
    assert c["count"] == 30 and c["filled_at"]
    # The check expires.
    h.advance(901)
    h.post("/api/cartridges/fill", {"request_id": rid(), "cartridge": "A", "count": 31}, expect=409)


def test_dispensing_lowers_the_estimated_count_and_warns_when_low(harness):
    h = harness()
    card = h.demo_due()
    assert cart(h, "A")["count"] is None  # never counted
    h.sim(action="place_token", channel="A", n=1)
    check_pill(h, "A")
    h.post("/api/cartridges/fill", {"request_id": rid(), "cartridge": "A", "count": 4})
    clear_tray(h)
    assert cart(h, "A")["low"] is False
    t = h.to_dose_check(card)
    c = cart(h, "A")
    assert c["count"] == 3 and c["low"] is True and c["used"] is True
    assert cart(h, "B")["count"] is None and cart(h, "B")["low"] is False
    det = h.c.get("/api/export/events.csv").text
    assert "CARTRIDGE_FILLED" in det and "FILL_CHECK" in det


def test_fill_is_refused_while_a_dose_is_active(harness):
    h = harness()
    card = h.demo_due()
    h.start(card)
    r = check_pill(h, "A", expect=409)
    assert r["reason_codes"][0] == "HARDWARE_BUSY"


# ---------------------------------------------------------------- pills in the hand
def released(h):
    card = h.demo_due()
    return card, release(h, card)


def hand(h, t, expect=200):
    return h.post("/api/doses/hand", {"request_id": rid(), "txn_id": t}, expect=expect)


def test_tray_check_waits_for_the_hand_check(harness):
    h = harness(hand_check=True)
    card, t = released(h)
    assert h.txn()["hand_done"] is False
    h.sim(action="open_door")
    h.sim(action="collect")
    assert h.check(t, expect=409)["reason_codes"] == ["HAND_CHECK_FIRST"]
    r = hand(h, t)
    assert r["result"] == "PASS" and r["state"] == "COLLECTION_CHECK"
    assert h.txn()["hand_done"] is True and h.txn()["observations"]["HAND_CHECK"]["result"] == "PASS"
    assert h.check(t)["state"] == "COMPLETE"
    det = detail(h, card["dose_id"])
    assert [o["result"] for o in obs_of(det, "HAND_CHECK")] == ["PASS"]


def test_wrong_count_in_the_hand_blocks_until_checked_again_or_skipped(harness):
    h = harness(hand_check=True)
    card, t = released(h)
    h.post("/api/sim/vision", {"hand": "wrong"})
    r = hand(h, t)
    assert r["result"] == "FAIL" and r["reason_codes"] == ["HAND_COUNT_MISMATCH"] and "Expected 1 blue and saw 0" in r["message"]
    assert h.txn()["blocks"][0]["code"] == "HAND_COUNT_MISMATCH" and h.txn()["hand_done"] is False
    h.post("/api/sim/vision", {"hand": "none"})
    assert hand(h, t)["reason_codes"] == ["NO_HAND"]
    # "I can't show them" is recorded as not seen, and the dose can finish.
    h.post("/api/doses/hand-skip", {"request_id": rid(), "txn_id": t})
    assert h.txn()["hand_done"] is True and h.txn()["blocks"] == []
    h.sim(action="open_door")
    h.sim(action="collect")
    assert h.check(t)["state"] == "COMPLETE"
    skipped = [o for o in obs_of(detail(h, card["dose_id"]), "HAND_CHECK") if o["reason_code"] == "HAND_SKIPPED"]
    assert len(skipped) == 1 and skipped[0]["result"] == "NOT_OBSERVED"


def test_hand_check_only_after_release(harness):
    h = harness(hand_check=True)
    card = h.demo_due()
    t = h.start(card)["txn_id"]
    assert hand(h, t, expect=409)["reason_codes"] == ["INVALID_TRANSITION"]


def test_showing_the_hand_does_not_count_as_hand_to_mouth(harness):
    """The watch pauses during the hand check, and the hand must go down again before a movement counts."""
    from app.taking import Watch
    w = Watch("t", "p", 0.0, tracker(), True, True, False)
    w.add(S(0.0, near=3.0), "now")  # hand away
    w.paused = True
    for i in range(5):
        assert not w.add(S(0.2 + i * 0.2, near=0.1), "now")  # hand held up near the face while paused
    w.paused = False
    w.tracker.seen_away = False
    assert not w.add(S(2.0, near=0.1), "now") and not w.add(S(2.2, near=0.1), "now")
    w.add(S(2.4, near=3.0), "now")  # hand down
    w.add(S(2.6, near=0.1), "now")
    assert w.add(S(2.8, near=0.1), "now")  # now a real movement to the mouth
