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


def test_visitor_lands_on_a_ready_demo(harness):
    h = demo(harness)
    st = h.wait(lambda s: s["workflow"]["armed"], msg="armed")
    assert st["public_demo"] == 30 and st["demo_mode"] is True
    assert st["account"]["exists"] and st["account"]["unlocked"] and not st["signed_out"]


def test_nobody_can_take_over_the_guest_or_leave_demo_mode(harness):
    h = demo(harness)
    for path in ("create", "unlock", "lock", "password", "handover"):
        assert h.post(f"/api/account/{path}", {"request_id": rid()}, expect=403)["reason_codes"] == ["PUBLIC_DEMO"]
    assert h.post("/api/demo/mode", {"request_id": rid(), "enabled": False}, expect=403)["reason_codes"] == ["PUBLIC_DEMO"]
    assert h.status()["account"]["unlocked"] is True


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
