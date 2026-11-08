"""Capture manager: exactly one owner per configured camera. Every frame carries its
source, camera session, sequence number and capture time so vision can prove freshness.
Frames never leave this machine."""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

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


def stream_url_for(snapshot_url: str) -> str:
    """The firmware serves preview video on port 81 of the same host as /snapshot."""
    parts = urlsplit(snapshot_url)
    return f"{parts.scheme}://{parts.hostname}:81/stream"


class MjpegParser:
    """Splits the bytes of a multipart JPEG stream into (headers, jpeg) parts."""

    MAX_BUFFER = 2_000_000

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[dict[str, str], bytes]]:
        self._buf += data
        parts: list[tuple[dict[str, str], bytes]] = []
        while True:
            start = self._buf.find(b"--frame")
            end = self._buf.find(b"\r\n\r\n", start) if start >= 0 else -1
            if end < 0:
                break
            headers = {}
            for line in bytes(self._buf[start:end]).split(b"\r\n")[1:]:
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            try:
                length = int(headers.get("content-length", ""))
            except ValueError:
                del self._buf[:end + 4]  # malformed part: resynchronise on the next boundary
                continue
            body = end + 4
            if len(self._buf) < body + length:
                break
            parts.append((headers, bytes(self._buf[body:body + length])))
            del self._buf[:body + length]
        if len(self._buf) > self.MAX_BUFFER:
            self._buf.clear()
        return parts


class Esp32StreamReader:
    """The tray camera's video feed, for the on-screen preview and its overlay only.

    One connection is shared by all viewers and it stays open only while someone is watching.
    Verification never reads from here: it keeps using Esp32SnapshotCamera's provably fresh frames.
    """

    SOURCE_ID = "esp32cam-stream"
    IDLE_GRACE_S = 3.0
    FRESH_S = 1.0

    def __init__(self, url: str, clock: Clock):
        self.url = url
        self.clock = clock
        self.error: str | None = None
        self.viewers = 0
        self._idle_until = 0.0
        self._task: asyncio.Task | None = None
        self._latest: tuple[str, bytes, float, str, int] | None = None
        self._new_frame = asyncio.Event()
        self._decode_lock = threading.Lock()
        self._decoded: Frame | None = None

    def add_viewer(self) -> None:
        self.viewers += 1
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="tray-preview-stream")

    def remove_viewer(self) -> None:
        self.viewers = max(0, self.viewers - 1)
        if not self.viewers:
            self._idle_until = self.clock.monotonic() + self.IDLE_GRACE_S

    def _wanted(self) -> bool:
        return self.viewers > 0 or self.clock.monotonic() < self._idle_until

    async def _run(self) -> None:
        import httpx
        timeout = httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0)
        while self._wanted():
            try:
                async with httpx.AsyncClient(timeout=timeout) as client, client.stream("GET", self.url) as resp:
                    if resp.status_code != 200:
                        self.error = f"Tray preview stream returned HTTP {resp.status_code}."
                    else:
                        parser = MjpegParser()
                        async for chunk in resp.aiter_raw():
                            for headers, jpeg in parser.feed(chunk):
                                self._publish(headers, jpeg)
                            if not self._wanted():
                                return
            except Exception as exc:
                self.error = f"Tray preview stream unavailable: {type(exc).__name__}"
            await asyncio.sleep(0.5)

    def _publish(self, headers: dict[str, str], jpeg: bytes) -> None:
        try:
            seq, age_ms = int(headers.get("x-frame-seq", "")), int(headers.get("x-capture-age-ms", ""))
        except ValueError:
            self.error = "Tray preview frame lacks its headers."
            return
        boot = headers.get("x-boot-id", "")
        frame_id = f"{self.SOURCE_ID}:{boot[:6]}:{seq}"
        self.error = None
        self._latest = (frame_id, jpeg, self.clock.monotonic() - age_ms / 1000.0, boot, seq)
        waiting, self._new_frame = self._new_frame, asyncio.Event()
        waiting.set()

    async def wait_newer(self, last_id: str | None, timeout: float) -> tuple[str, bytes] | None:
        """The newest frame other than `last_id`; missed frames are dropped, never queued."""
        if self._latest is None or self._latest[0] == last_id:
            try:
                await asyncio.wait_for(self._new_frame.wait(), timeout)
            except asyncio.TimeoutError:
                return None
        latest = self._latest
        return None if latest is None or latest[0] == last_id else (latest[0], latest[1])

    def latest_frame(self) -> Frame | None:
        """The newest video frame decoded for the overlay, or None if the feed is not fresh."""
        import cv2
        import numpy as np
        latest = self._latest
        if latest is None or self.clock.monotonic() - latest[2] > self.FRESH_S:
            return None
        frame_id, jpeg, capture_mono, boot, seq = latest
        with self._decode_lock:
            if self._decoded is not None and self._decoded.frame_id == frame_id:
                return self._decoded
            img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                return None
            self._decoded = Frame(img, seq, self.SOURCE_ID, boot, "tray", capture_mono, self.clock.now(),
                                  meta={"preview_only": True})
            return self._decoded

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None


def _mjpeg_part(frame_id: str, data: bytes) -> bytes:
    return (b"--frame\r\nContent-Type: image/jpeg\r\n"
            + f"Content-Length: {len(data)}\r\nX-Frame-Id: {frame_id}\r\n\r\n".encode()
            + data + b"\r\n")


class CaptureManager:
    def __init__(self, settings: Settings, clock: Clock):
        self.settings = settings
        self.clock = clock
        self.mode = settings.camera_mode
        self.laptop: LaptopCamera | None = None
        self.tray_cam: Esp32SnapshotCamera | None = None
        self.tray_stream: Esp32StreamReader | None = None
        self._preview_lock = threading.Lock()
        self._previews: dict[str, tuple[str, bytes]] = {}

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
            self.tray_stream = Esp32StreamReader(stream_url_for(self.settings.tray_camera_url), self.clock)

    async def stop(self) -> None:
        if self.laptop:
            self.laptop.stop()
        if self.tray_stream:
            await self.tray_stream.stop()

    async def frame(self, role: str) -> Frame | None:
        """role: face or tray. In single_laptop mode both use the laptop camera (guided poses)."""
        if role == "tray" and self.tray_cam is not None:
            return await self.tray_cam.snapshot()
        if self.laptop is not None:
            return self.laptop.latest()
        return None

    async def live_frame(self, role: str) -> Frame | None:
        """A frame for the on-screen overlay only. Guarded checks must use frame(), whose tray frames
        are provably fresh. Using the video here also keeps the overlay from competing with them."""
        if role == "tray" and self.tray_stream is not None:
            f = await asyncio.to_thread(self.tray_stream.latest_frame)
            if f is not None:
                return f
        return await self.frame(role)

    def preview_frame(self, role: str) -> tuple[str, bytes] | None:
        """Encode each source frame once, shared by snapshots and stream viewers.

        Called off the event loop: JPEG work must not hold up status or STOP requests.
        The cache contains only the newest preview, never a queue of old frames.
        """
        import cv2
        src = self.tray_cam if (role == "tray" and self.tray_cam) else self.laptop
        f = src.latest() if src else None
        if f is None or self.clock.monotonic() - f.capture_mono > 1.0:
            return None
        with self._preview_lock:
            cached = self._previews.get(f.source_id)
            if cached is not None and cached[0] == f.frame_id:
                return cached
            img = f.image
            h, w = img.shape[:2]
            if w > 640:
                img = cv2.resize(img, (640, int(h * 640 / w)))
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if not ok:
                return None
            preview = (f.frame_id, buf.tobytes())
            self._previews[f.source_id] = preview
            return preview

    def preview_jpeg(self, role: str) -> bytes | None:
        preview = self.preview_frame(role)
        return preview[1] if preview else None

    async def preview_stream(self, role: str):
        """At most 24 fps, dropping missed frames instead of buffering latency."""
        if role == "tray" and self.tray_stream is not None:
            # aclosing: when the viewer leaves, the feed's viewer count must drop now, not at garbage collection.
            async with aclosing(self._tray_preview_stream(self.tray_stream)) as parts:
                async for part in parts:
                    yield part
            return
        last_id = None
        while True:
            preview = await asyncio.to_thread(self.preview_frame, role)
            if preview is not None and preview[0] != last_id:
                last_id, data = preview
                yield _mjpeg_part(last_id, data)
            await asyncio.sleep(1 / 24)

    async def _tray_preview_stream(self, reader: Esp32StreamReader):
        """The camera's own video, passed through without re-encoding. If the feed is missing (older
        firmware, WiFi trouble) it shows the latest verification snapshot instead."""
        reader.add_viewer()
        last_id = None
        try:
            while True:
                newest = await reader.wait_newer(last_id, 0.25)
                if newest is None:
                    newest = await asyncio.to_thread(self.preview_frame, "tray")
                    if newest is None or newest[0] == last_id:
                        continue
                last_id = newest[0]
                yield _mjpeg_part(newest[0], newest[1])
        finally:
            reader.remove_viewer()

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "simulated": self.simulated,
            "face": self.laptop.status() if self.laptop else None,
            "tray": (self.tray_cam.status() if self.tray_cam else (self.laptop.status() if self.laptop else None)),
        }
