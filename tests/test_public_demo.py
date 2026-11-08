"""The hosted public demo: simulated only, one built-in guest nobody can take over, wiped regularly."""
import io

import cv2
import numpy as np

from app.config import Settings
from app.main import _public_demo_refresh
from conftest import rid

HOST = "healthbox-demo.example.com"


def demo(harness):
    return harness(public_demo=True, public_host=HOST, host="0.0.0.0")


def png():
    return cv2.imencode(".png", np.full((120, 200, 3), 255, np.uint8))[1].tobytes()


def test_public_binding_needs_the_demo_mode_and_simulated_hardware():
    assert any("loopback" in p for p in Settings.from_env(host="0.0.0.0").validate())
    bad = Settings.from_env(host="0.0.0.0", public_demo=True, public_host=HOST, hardware_mode="serial", serial_port="/dev/x")
    assert any("simulated" in p for p in bad.validate())
    assert any("PUBLIC_HOST" in p for p in Settings.from_env(host="0.0.0.0", public_demo=True, public_host="").validate())


def test_env_forces_simulation(monkeypatch):
    for k, v in {"PUBLIC_DEMO": "1", "PUBLIC_HOST": HOST, "HARDWARE_MODE": "serial", "CAMERA_MODE": "dual_camera", "TRAY_BYPASS": "1"}.items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    assert (s.hardware_mode, s.camera_mode, s.tray_bypass, s.laptop_sound, s.host) == ("fake", "simulated", False, False, "0.0.0.0")
    assert s.validate() == []


def test_visitor_starts_at_sign_up_on_an_armed_demo(harness):
    h = demo(harness)
    st = h.wait(lambda s: s["workflow"]["armed"], msg="armed")
    assert st["public_demo"] == 30 and st["demo_mode"] is True and st["account"]["exists"] is False
    h.post("/api/account/create", {"request_id": rid(), "first_name": "Judge", "last_name": "One", "password": "correct-horse-1"})
    assert h.status()["account"]["unlocked"] is True
    h.post("/api/enroll", {"request_id": rid(), "label": "x", "consent": True})
    assert h.status()["plan"]["recipient"]


def test_nobody_can_lock_out_the_others_or_leave_demo_mode(harness):
    h = demo(harness)
    for path in ("unlock", "lock", "password", "handover"):
        assert h.post(f"/api/account/{path}", {"request_id": rid()}, expect=403)["reason_codes"] == ["PUBLIC_DEMO"]
    assert h.post("/api/demo/mode", {"request_id": rid(), "enabled": False}, expect=403)["reason_codes"] == ["PUBLIC_DEMO"]


def test_start_over_goes_back_to_sign_up_from_any_state(harness):
    h = demo(harness)
    h.wait(lambda s: s["workflow"]["armed"], msg="armed")
    h.post("/api/account/create", {"request_id": rid(), "first_name": "Judge", "last_name": "One", "password": "correct-horse-1"})
    h.post("/api/enroll", {"request_id": rid(), "label": "x", "consent": True})
    h.post("/api/device/stop", {"request_id": rid()})
    h.post("/api/demo/reset", {"request_id": rid()})
    st = h.wait(lambda s: s["workflow"]["armed"], msg="armed again")
    assert st["account"]["exists"] is False and st["plan"]["recipient"] is None and st["demo_mode"] is True


def test_a_dose_comes_due_quickly(harness):
    h = demo(harness)
    h.wait(lambda s: s["workflow"]["armed"], msg="armed")
    h.post("/api/account/create", {"request_id": rid(), "first_name": "Judge", "last_name": "One", "password": "correct-horse-1"})
    h.post("/api/enroll", {"request_id": rid(), "label": "x", "consent": True})
    h.post("/api/demo/trigger", {"request_id": rid(), "entry_keys": ["demoA"]})
    h.advance(6)
    h.wait(lambda s: any(c["reminder"]["state"] == "DUE" for c in s["today"]), msg="due")


def test_public_host_and_origin_only(harness):
    h = demo(harness)
    assert h.c.get("/", headers={"host": HOST}).status_code == 200
    assert h.c.get("/", headers={"host": "evil.example.com"}).status_code == 400
    body = {"request_id": rid(), "kind": "face"}
    good = h.c.post("/api/practice/run", json=body, headers={**h.h, "origin": f"https://{HOST}"})
    assert good.status_code != 403, good.json()
    assert h.c.post("/api/practice/run", json=body, headers={**h.h, "origin": "https://evil.example.com"}).status_code == 403


def test_normal_install_stays_local_with_its_account_routes(harness):
    h = harness()
    assert h.c.get("/", headers={"host": HOST}).status_code == 400
    assert h.status()["public_demo"] is None
    assert h.status()["account"]["exists"] is False


def test_reset_removes_uploads_and_recovers_from_stop(harness):
    h = demo(harness)
    h.wait(lambda s: s["workflow"]["armed"], msg="armed")
    up = h.c.post("/api/labels/extract", headers=h.h, data={"request_id": rid()},
                  files={"file": ("l.png", io.BytesIO(png()), "image/png")}).json()
    svc = h.c.app.state.svc
    photo = svc.settings.data_dir / "labels" / f"{up['label_id']}_0.png"
    assert photo.exists()
    h.post("/api/settings/hand-check", {"request_id": rid(), "enabled": True})
    h.post("/api/device/stop", {"request_id": rid()})
    h.wait(lambda s: not s["workflow"]["armed"], msg="stopped")
    h.c.portal.call(_public_demo_refresh, svc)
    st = h.wait(lambda s: s["workflow"]["armed"], msg="armed again")
    assert not photo.exists() and svc.store.q1("SELECT COUNT(*) AS n FROM label_scans")["n"] == 0
    assert st["config"]["hand_check"] is False and st["device"]["fault"] in (None, "", "NONE")
