"""scripts/start_box.py: what it decides from the network, the camera and the motor board."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import start_box  # noqa: E402
from app.config import Settings  # noqa: E402
from app.virtual_controller import VirtualSerialController  # noqa: E402
from test_tray_cam import FakeTrayCam  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pty")


@pytest.fixture
def vc():
    v = VirtualSerialController(Settings.from_env(hardware_mode="fake", genai_backend="off", fake_motion_s_per_unit=0.3))
    v.start_in_thread()
    yield v
    v.unplug()


@pytest.mark.parametrize("address, host, expected", [
    ("172.20.10.9", "172.20.10.2", None),
    ("172.20.10.14", "172.20.10.2", None),
    ("192.0.0.2", "172.20.10.2", "ipv6_only"),
    ("10.207.74.139", "172.20.10.2", "other_network"),
    (None, "172.20.10.2", "no_wifi"),
    ("192.168.1.20", "192.168.1.42", None),
    ("192.168.1.20", "doseguard-tray.local", None),
])
def test_network_problem(address, host, expected):
    assert start_box.network_problem(address, host) == expected


def test_motor_board_is_found_past_a_port_that_does_not_answer(vc, tmp_path):
    silent = str(tmp_path / "no-such-port")
    found = start_box.find_motor_board([silent, vc.path], wait_s=3)
    assert found is not None and found[0] == vc.path and found[1]["event"] == "PONG"


def test_no_motor_board_found():
    assert start_box.find_motor_board(["/dev/cu.not-a-real-port"], wait_s=0.2) is None


def test_likely_motor_port_is_tried_first(monkeypatch):
    monkeypatch.setattr(start_box.glob, "glob", lambda p: ["/dev/cu.usbserial-10", "/dev/cu.usbserial-0001"] if "usbserial" in p else [])
    assert start_box.serial_ports()[0] == "/dev/cu.usbserial-0001"


@pytest.mark.parametrize("change, phrase", [
    ({"fault": "STOPPED"}, "fault"),
    ({"door": 0}, "door"),
    ({"hA": 0}, "home"),
])
def test_motor_problems_are_reported(change, phrase):
    healthy = {"door": 1, "hA": 1, "hB": 1, "fault": None}
    assert start_box.motor_problems(healthy) == []
    assert phrase in start_box.motor_problems({**healthy, **change})[0][0]


def serve_status(body: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"127.0.0.1:{httpd.server_address[1]}"


def test_camera_status_recognises_only_the_tray_camera():
    good, good_host = serve_status({"fw": "doseguard-tray-0.11"})
    other, other_host = serve_status({"fw": "something-else"})
    try:
        assert start_box.camera_status(good_host)["fw"] == "doseguard-tray-0.11"
        assert start_box.camera_status(other_host) is None
    finally:
        good.shutdown()
        other.shutdown()


def test_fresh_pictures_uses_the_apps_rules():
    good, old = FakeTrayCam(), FakeTrayCam("old")
    try:
        assert start_box.fresh_pictures(good.url, attempts=3) == (3, None)
        count, error = start_box.fresh_pictures(old.url, attempts=2)
        assert count == 0 and "too old" in error
    finally:
        good.close()
        old.close()
