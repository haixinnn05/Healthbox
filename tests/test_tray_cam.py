"""The app's ESP32-CAM client and scripts/tray_cam_check.py against a fake tray camera that
speaks the firmware's contract (firmware/doseguard_tray_cam): a fresh JPEG per request with
X-Boot-Id, X-Frame-Seq and X-Capture-Age-Ms. Failure modes are what the app must reject."""
import asyncio
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.camera import Esp32SnapshotCamera
from app.timeutil import Clock

ROOT = Path(__file__).resolve().parent.parent


def tray_image(rng) -> np.ndarray:
    """A white tray with the four corner markers (ids 0..3), plus a little sensor noise."""
    img = np.full((480, 640, 3), 235, np.uint8)
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for mid, (x, y) in enumerate([(30, 30), (530, 30), (530, 370), (30, 370)]):
        m = cv2.aruco.generateImageMarker(d, mid, 80)
        img[y:y + 80, x:x + 80] = cv2.cvtColor(m, cv2.COLOR_GRAY2BGR)
    noise = rng.integers(0, 6, img.shape, dtype=np.uint8)
    return cv2.add(img, noise)


class FakeTrayCam:
    def __init__(self, mode: str = "good"):
        self.mode = mode
        self.seq = 0
        self.rng = np.random.default_rng(7)
        cam = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if cam.mode == "down":
                    self.send_response(503)
                    self.end_headers()
                    return
                if cam.mode != "stuck":
                    cam.seq += 1
                img = tray_image(np.random.default_rng(1) if cam.mode == "stuck" else cam.rng)
                body = cv2.imencode(".jpg", img)[1].tobytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                if cam.mode != "no_headers":
                    self.send_header("X-Boot-Id", "a1b2c3d4")
                    self.send_header("X-Frame-Seq", str(max(cam.seq, 1)))
                    self.send_header("X-Capture-Age-Ms", "900" if cam.mode == "old" else "40")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/snapshot"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def cam():
    made = []

    def make(mode="good"):
        c = FakeTrayCam(mode)
        made.append(c)
        return c
    yield make
    for c in made:
        c.close()


def snap(client):
    return asyncio.run(client.snapshot())


def test_fresh_frames_are_accepted_with_their_capture_time(cam):
    c = cam()
    client = Esp32SnapshotCamera(c.url, Clock())
    f1, f2 = snap(client), snap(client)
    assert f1 is not None and f2 is not None, client.error
    assert (f1.seq, f2.seq) == (1, 2) and f1.camera_session == "a1b2c3d4" and f1.role == "tray"
    assert f1.image.shape == (480, 640, 3)
    assert f1.meta["capture_age_ms"] == 40 and f1.capture_mono < Clock().monotonic()


@pytest.mark.parametrize("mode, error", [
    ("no_headers", "freshness headers"),
    ("old", "too old"),
    ("down", "freshness headers"),
])
def test_unprovable_or_stale_frames_are_rejected(cam, mode, error):
    client = Esp32SnapshotCamera(cam(mode).url, Clock())
    assert snap(client) is None
    assert error in client.error


def test_a_repeated_frame_number_is_rejected(cam):
    client = Esp32SnapshotCamera(cam("stuck").url, Clock())
    assert snap(client) is not None
    assert snap(client) is None and "repeated" in client.error


def test_unreachable_camera_is_reported(cam):
    c = cam()
    url = c.url
    c.close()
    client = Esp32SnapshotCamera(url, Clock(), timeout_s=0.5)
    assert snap(client) is None and "unreachable" in client.error


def run_check(url, *extra, tmp_path):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "tray_cam_check.py"), "--url", url, "--count", "12",
                           "--save", str(tmp_path / "last.jpg"), *extra],
                          capture_output=True, text=True, timeout=60, cwd=ROOT)


def test_tray_cam_check_passes_a_good_camera_and_sees_the_markers(cam, tmp_path):
    out = run_check(cam().url, "--tray", tmp_path=tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "FAIL" not in out.stdout and "all four markers" in out.stdout
    assert (tmp_path / "last.jpg").exists()


def test_tray_cam_check_fails_a_stuck_camera(cam, tmp_path):
    out = run_check(cam("stuck").url, tmp_path=tmp_path)
    assert out.returncode == 1
    assert "FAIL  fresh" in out.stdout and "repeated" in out.stdout
