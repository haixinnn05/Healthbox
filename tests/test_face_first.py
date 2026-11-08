"""The face comes first: no medicine, dispenser or reminder-only, can be saved before a face is enrolled."""
import pytest
from conftest import rid

NY = "America/New_York"


def plan_body(person, mode):
    entry = {"entry_key": "med1", "label": "Med", "quantity": 1, "delivery_mode": mode,
             "class_id": "blue" if mode == "DEVICE" else None, "times": ["20:00"], "confirmed": True}
    return {"request_id": rid(), "person_id": person, "timezone": NY, "timezone_confirmed": True,
            "dst_reviewed": True, "entries": [entry]}


@pytest.mark.parametrize("mode", ["DEVICE", "REMINDER_ONLY"])
def test_a_medicine_cannot_be_saved_before_a_face_is_enrolled(harness, mode):
    h = harness(require_label_photo=False)
    refused = h.post("/api/plans/confirm", plan_body(None, mode), expect=422)
    assert "NOT_ENROLLED" in str(refused)
    assert not h.status()["plan"]


def test_a_reminder_only_medicine_can_be_saved_once_the_face_is_enrolled(harness):
    h = harness(require_label_photo=False)
    person = h.post("/api/enroll", {"request_id": rid(), "label": "Teammate", "consent": True})["person_id"]
    h.post("/api/plans/confirm", plan_body(person, "REMINDER_ONLY"))
    assert h.status()["plan"]
