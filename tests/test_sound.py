"""Due reminders chime through the laptop itself, so no click in the browser is needed."""
from app.sound import LaptopSound
from conftest import rid


def test_each_reminder_alert_plays_the_laptop_chime(harness):
    h = harness(sound_interval_s=60, sound_max=3)
    played = []
    h.c.app.state.svc.reminders.on_alert = lambda r: played.append(r["reminder_id"])
    h.wait(lambda s: s["device"]["connected"], msg="device")
    h.post("/api/enroll", {"request_id": rid(), "label": "T", "consent": True})
    h.post("/api/demo/mode", {"request_id": rid(), "enabled": True})
    h.post("/api/demo/trigger", {"request_id": rid(), "entry_keys": ["demoA"]})
    h.advance(31)
    h.wait(lambda s: any(c["reminder"]["state"] == "DUE" for c in s["today"]), msg="due")
    assert len(played) == 1
    h.advance(61)
    h.wait(lambda s: len(played) == 2, msg="second chime")


def test_a_broken_sound_never_changes_reminders(harness):
    h = harness()

    def boom(r):
        raise RuntimeError("no speaker")
    h.c.app.state.svc.reminders.on_alert = boom
    card = h.demo_due()  # becomes DUE and alerts despite the failing sound
    assert card["reminder"]["state"] == "DUE"


def test_sound_is_off_in_tests_and_on_other_systems(harness, tmp_path):
    h = harness()
    assert h.status()["laptop_sound"] == {"enabled": False}
    assert h.post("/api/sound/test", {"request_id": rid()})["played"] is False
    assert LaptopSound(True, str(tmp_path / "missing.aiff")).enabled is False
