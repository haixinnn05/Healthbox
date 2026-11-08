import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app import store as store_module
from app.timeutil import FakeClock

START = datetime(2026, 11, 7, 13, 0, tzinfo=timezone.utc)  # 8:00 AM New York
PRODUCT_CLASSES = store_module.DEFAULT_CLASSES


@pytest.fixture(autouse=True)
def blue_pills_in_test_pictures(monkeypatch):
    """The test pictures draw blue pills on a light tray, so tests use a blue type to match them.
    The product's own pill types are checked in test_pill_types.py."""
    blue = ("blue", "Blue", "A", "#2F6FD6", {"ranges": [[[95, 90, 60], [130, 255, 255]]]})
    monkeypatch.setattr(store_module, "DEFAULT_CLASSES", [blue, PRODUCT_CLASSES[1]])


def rid() -> str:
    return uuid.uuid4().hex


def fast_settings(tmp: Path, **kw) -> Settings:
    base = dict(db_path=tmp / "t.db", data_dir=tmp, check_min_span_s=0.0, fake_motion_s_per_unit=0.03,
                scheduler_tick_s=0.05, camera_mode="simulated", hardware_mode="fake", genai_backend="off",
                cycle_timeout_per_unit_s=2.0, watch_interval_s=0.02, setup_unlock_minutes=15, laptop_sound=False, hand_check=False, laptop_voice=False, tray_vision="markers")
    base.update(kw)
    return Settings.from_env(**base)


class Harness:
    def __init__(self, client: TestClient, clock: FakeClock):
        self.c = client
        self.clock = clock
        tok = re.search(r'name="dg-token" content="([^"]+)"', client.get("/").text).group(1)
        self.h = {"x-doseguard-token": tok}

    def post(self, path, body=None, expect=200):
        r = self.c.post(path, json=body or {}, headers=self.h)
        assert r.status_code == expect, (path, r.status_code, r.json())
        return r.json()

    def status(self):
        return self.c.get("/api/status").json()

    def wait(self, pred, timeout=5.0, msg="condition"):
        end = time.time() + timeout
        while time.time() < end:
            st = self.status()
            if pred(st):
                return st
            time.sleep(0.03)
        raise AssertionError(f"timed out waiting for {msg}: {self.status()['workflow']}")

    def advance(self, seconds: float, smooth: bool = False):
        """Advance the fake clock. A jump makes heartbeats look stale for one cycle, so let a
        ping round-trip happen afterwards; smooth mode steps slowly enough for the watchdog."""
        if smooth:
            steps = int(seconds / 0.1)
            for _ in range(steps):
                self.clock.advance(0.1)
                time.sleep(0.06)
        else:
            self.clock.advance(seconds)
        time.sleep(0.35)

    def txn(self):
        return self.status()["workflow"]["txn"]

    # -- common flows --------------------------------------------------------
    def demo_due(self):
        """Enroll, demo mode, trigger a reminder, advance the clock until it is due, arm."""
        self.wait(lambda s: s["device"]["connected"], msg="device connected")
        self.post("/api/enroll", {"request_id": rid(), "label": "Teammate", "consent": True})
        self.post("/api/demo/mode", {"request_id": rid(), "enabled": True})
        self.post("/api/demo/trigger", {"request_id": rid(), "entry_keys": ["demoA"]})
        self.advance(31)
        st = self.wait(lambda s: any(c["reminder"]["state"] == "DUE" for c in s["today"]), msg="due")
        self.post("/api/device/arm", {"request_id": rid(), "inspected": True})
        return next(c for c in st["today"] if c["reminder"]["state"] == "DUE")

    def start(self, card):
        return self.post("/api/doses/start", {"request_id": rid(), "dose_id": card["dose_id"],
                                              "plan_version": card["plan_version_id"]})

    def check(self, txn_id, expect=200):
        return self.post("/api/doses/check", {"request_id": rid(), "txn_id": txn_id}, expect=expect)

    def sim(self, **kw):
        return self.post("/api/sim/hardware", kw)

    def to_dose_check(self, card):
        t = self.start(card)["txn_id"]
        assert self.check(t)["state"] == "EMPTY_TRAY_CHECK"
        self.check(t)  # empty tray passes, dispensing starts
        self.wait(lambda s: s["workflow"]["txn"]["state"] == "DOSE_CHECK", msg="DOSE_CHECK")
        return t


@pytest.fixture
def harness(tmp_path):
    def make(**kw):
        clock = FakeClock(START)
        app = create_app(fast_settings(tmp_path, **kw), clock=clock, testing=True)
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        client.__enter__()
        made.append(client)
        return Harness(client, clock)
    made = []
    yield make
    for c in made:
        c.__exit__(None, None, None)
