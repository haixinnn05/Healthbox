"""Label photos are required for every plan entry; Gemini is optional."""
import io

import cv2
import numpy as np

from conftest import rid


def png_bytes():
    img = np.full((240, 320, 3), 255, np.uint8)
    cv2.putText(img, "DEMO TOKEN A 5mg", (10, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
    return cv2.imencode(".png", img)[1].tobytes()


def plan_body(person_id, label_id=None):
    return {"request_id": rid(), "timezone": "America/New_York", "timezone_confirmed": True, "person_id": person_id,
            "entries": [{"label": "Demo token A", "quantity": 1, "delivery_mode": "DEVICE", "class_id": "blue",
                         "times": ["20:00"], "confirmed": True, "label_id": label_id}]}


def test_plan_requires_label_photo_and_accepts_upload(harness):
    h = harness()
    person = h.post("/api/enroll", {"request_id": rid(), "label": "T", "consent": True})["person_id"]
    r = h.post("/api/plans/confirm", plan_body(person), expect=422)
    assert any(e["field"] == "label_id" for e in r["errors"])

    up = h.c.post("/api/labels/extract", headers=h.h, data={"request_id": rid()},
                  files={"file": ("label.png", io.BytesIO(png_bytes()), "image/png")})
    assert up.status_code == 200, up.json()
    j = up.json()
    assert j["photos"] == 1 and h.c.get(j["image_urls"][0]).status_code == 200
    read = h.post("/api/labels/read", {"request_id": rid(), "label_id": j["label_id"], "container": "flat"})
    assert read["fields"] is None and read["ai_status"] == "off" and "Type them" in read["note"]

    h.post("/api/plans/confirm", plan_body(person, j["label_id"]))
    entry = h.status()["plan"]["entries"][0]
    assert entry["label_id"] == j["label_id"]


def upload(h, label_id=None):
    data = {"request_id": rid()}
    if label_id:
        data["label_id"] = label_id
    return h.c.post("/api/labels/extract", headers=h.h, data=data,
                    files={"file": ("label.png", io.BytesIO(png_bytes()), "image/png")})


def test_round_bottle_takes_several_angles_of_one_label(harness):
    h = harness()
    first = upload(h).json()
    lid = first["label_id"]
    for n in (2, 3, 4):
        r = upload(h, lid).json()
        assert r["label_id"] == lid and r["photos"] == n
    assert upload(h, lid).status_code == 409  # at most 4 photos per label
    for i in range(4):
        assert h.c.get(f"/api/labels/{lid}/image?i={i}").status_code == 200
    read = h.post("/api/labels/read", {"request_id": rid(), "label_id": lid, "container": "bottle"})
    assert read["photos"] == 4


def test_unknown_label_id_is_rejected(harness):
    h = harness()
    person = h.post("/api/enroll", {"request_id": rid(), "label": "T", "consent": True})["person_id"]
    r = h.post("/api/plans/confirm", plan_body(person, "deadbeef0000"), expect=422)
    assert any(e["field"] == "label_id" for e in r["errors"])


def test_camera_capture_needs_a_real_camera(harness):
    h = harness()
    r = h.post("/api/labels/capture", {"request_id": rid()}, expect=409)
    assert "Upload" in r["message"]


def test_label_requirement_can_be_turned_off(harness):
    h = harness(require_label_photo=False)
    person = h.post("/api/enroll", {"request_id": rid(), "label": "T", "consent": True})["person_id"]
    h.post("/api/plans/confirm", plan_body(person))


def test_glare_and_blur_checks():
    from app.main import check_label_photo
    rng = np.random.default_rng(0)
    label = np.full((720, 1280, 3), 235, np.uint8)
    for y in range(200, 520, 40):  # lines of "text"
        cv2.putText(label, "DEMO TOKEN A 5 MG TAKE ONE DAILY", (200, y), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (20, 20, 20), 2)
    sharp, problem = check_label_photo(label, 30.0)
    assert problem is None and sharp > 30
    blurry = cv2.GaussianBlur(label, (41, 41), 0)
    assert "blurry" in check_label_photo(blurry, 30.0)[1]
    glare = label.copy()
    glare[260:460, 500:780] = 255  # bright reflection stripe across the label
    assert "glare" in check_label_photo(glare, 30.0)[1].lower()
