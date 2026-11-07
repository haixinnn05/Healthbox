"""The real SerialAdapter (pyserial + reader thread) against the simulated controller on a
pseudo-terminal. The in-process fake adapter never touches this code path."""
import subprocess
from dataclasses import replace
import sys
import time
from pathlib import Path

import pytest

from app.config import Settings
from app.virtual_controller import VirtualSerialController
from conftest import rid

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pty")


@pytest.fixture
def vc():
    settings = Settings.from_env(hardware_mode="fake", genai_backend="off", fake_motion_s_per_unit=0.3)
    v = VirtualSerialController(settings)
    v.start_in_thread()
    yield v
    v.unplug()


def set_motion(vc, seconds):
    vc.call(lambda: setattr(vc.controller, "settings", replace(vc.controller.settings, fake_motion_s_per_unit=seconds)))


def serial_harness(harness, vc, **kw):
    return harness(hardware_mode="serial", serial_port=vc.path, **kw)


def test_serial_link_reports_session_and_status(harness, vc):
    h = serial_harness(harness, vc)
    st = h.wait(lambda s: s["device"]["connected"] and s["device"]["session"], msg="serial connected")
    dev = st["device"]
    assert dev["mode"] == "serial" and dev["simulated"] is False  # the adapter is real; the controller is not
    assert dev["session"] == vc.controller.session
    h.wait(lambda s: s["device"]["firmware"] == "doseguard-sim-0.4", msg="firmware name via STATUS")
    assert dev["motor"] == {"A": "idle", "B": "idle"} and dev["door_closed"] is True
    assert dev["lid_closed_measured"] is None and dev["servo_measured"] is None


def test_serial_full_dose_releases_once(harness, vc):
    h = serial_harness(harness, vc)
    card = h.demo_due()
    t = h.to_dose_check(card)
    assert vc.controller.tray["A"] == 1  # the DISPENSE really crossed the pty
    r = h.check(t)
    assert r["result"] == "PASS" and r["state"] == "RECIPIENT_RECHECK"
    h.post("/api/doses/release", {"request_id": rid(), "txn_id": t})
    st = h.wait(lambda s: s["workflow"]["txn"]["state"] == "COLLECTION_CHECK", msg="released over serial")
    assert st["device"]["servo_commanded"] == "unlocked" and vc.controller.servo == "U"
    assert len(vc.controller.released) == 1
    h.post("/api/doses/release", {"request_id": rid(), "txn_id": t}, expect=409)
    assert len(vc.controller.released) == 1


def test_serial_unplug_marks_uncertain_and_disconnects(harness, vc):
    set_motion(vc, 1.0)
    h = serial_harness(harness, vc)
    card = h.demo_due()
    t = h.start(card)["txn_id"]
    h.check(t)
    h.check(t)
    h.wait(lambda s: s["workflow"]["txn"]["state"] == "DISPENSING")
    vc.unplug()
    st = h.wait(lambda s: s["workflow"]["txn"]["state"] == "UNCERTAIN", msg="uncertain after unplug")
    assert st["device"]["connected"] is False
    assert st["workflow"]["armed"] is False


def test_serial_controller_reboot_is_a_new_session(harness, vc):
    h = serial_harness(harness, vc)
    h.wait(lambda s: s["device"]["connected"] and s["device"]["session"])
    h.post("/api/enroll", {"request_id": rid(), "label": "Teammate", "consent": True})
    h.post("/api/device/arm", {"request_id": rid(), "inspected": True})
    old = h.status()["device"]["session"]

    def power_cycle():
        vc.controller._stop(None)
        vc.controller._boot()
    vc.call(power_cycle)
    st = h.wait(lambda s: s["device"]["session"] not in (None, old), msg="new session")
    assert st["workflow"]["armed"] is False


def test_bench_check_passes_against_virtual_controller(vc):
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "bench_check.py"), "--port", vc.path, "--quick"],
                         capture_output=True, text=True, timeout=90, cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "FAIL" not in out.stdout


def test_bench_check_catches_a_lost_command(vc):
    vc.controller.feed_mode["A"] = "no_ack"
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "bench_check.py"), "--port", vc.path, "--quick"],
                         capture_output=True, text=True, timeout=90, cwd=ROOT)
    assert out.returncode == 1
    assert "FAIL" in out.stdout and "dispense_A" in out.stdout


def test_bench_trials_write_csv(vc, tmp_path):
    csv_path = tmp_path / "trials.csv"
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "bench_check.py"), "--port", vc.path,
                          "--trials", "3", "--channel", "B", "--csv", str(csv_path)],
                         capture_output=True, text=True, timeout=90, cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr
    rows = csv_path.read_text().strip().splitlines()
    assert rows[0].startswith("trial,channel") and len(rows) == 4
    assert "3/3 cycles completed" in out.stdout
    time.sleep(0.1)
    assert vc.controller.tray["B"] == 3


def test_bench_watchdog_check(vc):
    set_motion(vc, 0.8)
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "bench_check.py"), "--port", vc.path,
                          "--only", "link", "ready", "watchdog"],
                         capture_output=True, text=True, timeout=90, cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "HEARTBEAT_LOST" in out.stdout


def test_bench_check_refuses_a_port_the_app_owns(harness, vc):
    h = serial_harness(harness, vc)
    h.wait(lambda s: s["device"]["connected"])
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "bench_check.py"), "--port", vc.path, "--quick"],
                         capture_output=True, text=True, timeout=30, cwd=ROOT)
    assert out.returncode == 2 and "still holding the port" in out.stdout
    assert h.status()["device"]["connected"] is True
