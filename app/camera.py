"""Capture manager: exactly one owner per configured camera. Every frame carries its
source, camera session, sequence number and capture time so vision can prove freshness.
Frames never leave this machine."""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from .config import Settings
from .timeutil import Clock

log = logging.getLogger("doseguard.camera")


@dataclass
class Frame:
    image: object  # numpy array (BGR)
    seq: int
    source_id: str
    camera_session: str
    role: str
    capture_mono: float
    capture_wall: datetime
    meta: dict = field(default_factory=dict)

    @property
    def frame_id(self) -> str:
        return f"{self.source_id}:{self.camera_session[:6]}:{self.seq}"


class LaptopCamera:
    """Continuous capture on a background thread; keeps only the newest frame."""

    def __init__(self, index: int, clock: Clock):
        self.index = index
        self.clock = clock
        self.source_id = f"laptop{index}"
        self.session = uuid.uuid4().hex
        self._latest: Frame | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None
        self.seq = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"camera-{self.source_id}", daemon=True)
        self._thread.start()

    def probe_on_main_thread(self) -> None:
        """macOS only shows the camera permission prompt when the camera is first opened
        from the main thread. Open and release it once here so the prompt can appear."""
        import cv2
        try:
            cap = cv2.VideoCapture(self.index)
            ok = cap.isOpened()
            cap.release()
            log.info("camera %s probe on main thread: %s", self.index, "opened" if ok else "not opened yet")
        except Exception:
            log.exception("camera probe failed")

    def _run(self) -> None:
        import cv2
        # Keep trying: permission may be granted after start, or the camera may be busy.
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.index)
            if not cap.isOpened():
                cap.release()
                self.error = ("Camera could not be opened. Allow camera access for your terminal app in "
                              "System Settings > Privacy & Security > Camera, then quit and reopen the terminal.")
                self._stop.wait(2.0)
                continue
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)  # enough for face and tray checks, and faster
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            self.error = None
            self.session = uuid.uuid4().hex  # a reopened camera is a new capture session
            log.info("camera %s opened", self.index)
            failures = 0
            while not self._stop.is_set():
                ok, img = cap.read()
                if not ok or img is None:
                    failures += 1
                    if failures > 40:
                        self.error = "Camera stopped delivering frames. Reconnecting."
                        break
                    self._stop.wait(0.05)
                    continue
                failures = 0
                self.error = None
                self.seq += 1
                frame = Frame(img, self.seq, self.source_id, self.session, "laptop", self.clock.monotonic(), self.clock.now())
                with self._lock:
                    self._latest = frame
            cap.release()

    def latest(self) -> Frame | None:
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        f = self.latest()
        return {
            "source_id": self.source_id, "error": self.error,
            "age_ms": None if f is None else round((self.clock.monotonic() - f.capture_mono) * 1000),
            "seq": None if f is None else f.seq,
        }


class Esp32SnapshotCamera:
    """Request-driven snapshot from the ESP32-CAM. The firmware must return headers
    X-Boot-Id, X-Frame-Seq and X-Capture-Age-Ms; receiving a JPEG now does not prove it
    was captured now, so frames without provable freshness are rejected."""

    def __init__(self, url: str, clock: Clock, max_age_ms: int = 500, timeout_s: float = 1.5):
        self.url = url
        self.clock = clock
        self.max_age_ms = max_age_ms
        self.timeout_s = timeout_s
        self.source_id = "esp32cam"
        self.boot_id: str | None = None
        self.last_seq: int | None = None
        self.error: str | None = None
        self._last: Frame | None = None

    async def snapshot(self) -> Frame | None:
        import cv2
        import httpx
        import numpy as np
        sent = self.clock.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.get(self.url)
        except Exception as exc:
            self.error = f"Tray camera unreachable: {type(exc).__name__}"
            return None
        received = self.clock.monotonic()
        boot, seq, age = resp.headers.get("X-Boot-Id"), resp.headers.get("X-Frame-Seq"), resp.headers.get("X-Capture-Age-Ms")
        if resp.status_code != 200 or not boot or seq is None or age is None:
            self.error = "Tray camera response lacks freshness headers."
            return None
        try:
            seq_i, age_ms = int(seq), int(age)
        except ValueError:
            self.error = "Tray camera freshness headers are malformed."
            return None
        if boot == self.boot_id and self.last_seq is not None and seq_i <= self.last_seq:
            self.error = "Tray camera repeated an old frame."
            return None
        if age_ms > self.max_age_ms or (received - sent) * 1000 > self.timeout_s * 1000:
            self.error = "Tray camera frame is too old."
            return None
        img = cv2.imdecode(np.frombuffer(resp.content, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            self.error = "Tray camera sent an unreadable image."
            return None
        self.boot_id, self.last_seq, self.error = boot, seq_i, None
        capture_mono = received - age_ms / 1000.0
        frame = Frame(img, seq_i, self.source_id, boot, "tray", capture_mono, self.clock.now(),
                      meta={"request_ms": round((received - sent) * 1000), "capture_age_ms": age_ms})
        self._last = frame
        return frame

    def latest(self) -> Frame | None:
        return self._last

    def status(self) -> dict:
        f = self._last
        return {"source_id": self.source_id, "error": self.error, "boot_id": self.boot_id,
                "age_ms": None if f is None else round((self.clock.monotonic() - f.capture_mono) * 1000)}


class CaptureManager:
    def __init__(self, settings: Settings, clock: Clock):
        self.settings = settings
        self.clock = clock
        self.mode = settings.camera_mode
        self.laptop: LaptopCamera | None = None
        self.tray_cam: Esp32SnapshotCamera | None = None

    @property
    def simulated(self) -> bool:
        return self.mode == "simulated"

    async def start(self) -> None:
        if self.mode in ("single_laptop", "dual_camera"):
            self.laptop = LaptopCamera(self.settings.face_camera_index, self.clock)
            self.laptop.probe_on_main_thread()
            self.laptop.start()
        if self.mode == "dual_camera":
            self.tray_cam = Esp32SnapshotCamera(self.settings.tray_camera_url, self.clock)

    async def stop(self) -> None:
        if self.laptop:
            self.laptop.stop()

    async def frame(self, role: str) -> Frame | None:
        """role: face or tray. In single_laptop mode both use the laptop camera (guided poses)."""
        if role == "tray" and self.tray_cam is not None:
            return await self.tray_cam.snapshot()
        if self.laptop is not None:
            return self.laptop.latest()
        return None

    def preview_jpeg(self, role: str) -> bytes | None:
        import cv2
        src = self.tray_cam if (role == "tray" and self.tray_cam) else self.laptop
        f = src.latest() if src else None
        if f is None:
            return None
        img = f.image
        h, w = img.shape[:2]
        if w > 640:
            img = cv2.resize(img, (640, int(h * 640 / w)))
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return buf.tobytes() if ok else None

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "simulated": self.simulated,
            "face": self.laptop.status() if self.laptop else None,
            "tray": (self.tray_cam.status() if self.tray_cam else (self.laptop.status() if self.laptop else None)),
        }
