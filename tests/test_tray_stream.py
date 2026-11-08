"""The tray camera's preview video. It must make the screen smooth without ever becoming a source
of proof: guarded checks keep using /snapshot frames, and a stale or missing feed is not shown as live."""
import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from app.camera import CaptureManager, Esp32SnapshotCamera, Esp32StreamReader, MjpegParser, stream_url_for
from app.timeutil import Clock


def jpeg(shade: int) -> bytes:
    return cv2.imencode(".jpg", np.full((48, 64, 3), shade, np.uint8))[1].tobytes()


def part(body: bytes, seq: int, age_ms: int = 30) -> bytes:
    return (f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(body)}\r\nX-Boot-Id: b00tb00t\r\n"
            f"X-Frame-Seq: {seq}\r\nX-Capture-Age-Ms: {age_ms}\r\n\r\n").encode() + body + b"\r\n"


class FakeCamera:
    """Serves /snapshot like the firmware, and /stream unless `video` is False."""

    def __init__(self, video: bool = True, age_ms: int = 30):
        self.video, self.age_ms = video, age_ms
        self.sent: list[bytes] = []
        self.active_streams = 0
        self.stream_connections = 0
        self.snapshots = 0
        self.stopping = False
        cam = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/snapshot":
                    cam.snapshots += 1
                    body = jpeg(200)
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("X-Boot-Id", "b00tb00t")
                    self.send_header("X-Frame-Seq", str(cam.snapshots))
                    self.send_header("X-Capture-Age-Ms", "30")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path != "/stream" or not cam.video:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=frame")
                self.end_headers()
                cam.active_streams += 1
                cam.stream_connections += 1
                seq = 0
                try:
                    while not cam.stopping:
                        seq += 1
                        body = jpeg(seq % 200)
                        cam.sent.append(body)
                        self.wfile.write(part(body, seq, cam.age_ms))
                        self.wfile.flush()
                        time.sleep(0.02)
                except OSError:
                    pass
                finally:
                    cam.active_streams -= 1

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.snapshot_url, self.stream_url = f"{base}/snapshot", f"{base}/stream"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.stopping = True
        self.httpd.shutdown()


@pytest.fixture
def fake():
    made = []

    def make(**kw):
        made.append(FakeCamera(**kw))
        return made[-1]
    yield make
    for c in made:
        c.close()


def manager(cam: FakeCamera) -> CaptureManager:
    clock = Clock()
    m = CaptureManager(SimpleNamespace(camera_mode="dual_camera"), clock)
    m.tray_cam = Esp32SnapshotCamera(cam.snapshot_url, clock)
    m.tray_stream = Esp32StreamReader(cam.stream_url, clock)
    m.tray_stream.IDLE_GRACE_S = 0.0
    return m


async def next_parts(stream, count: int) -> list[bytes]:
    return [await asyncio.wait_for(stream.__anext__(), 5) for _ in range(count)]


async def until(check, seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        await asyncio.sleep(0.02)
    return check()


def test_stream_url_is_port_81_of_the_snapshot_host():
    assert stream_url_for("http://172.20.10.2/snapshot") == "http://172.20.10.2:81/stream"
    assert stream_url_for("http://doseguard-tray.local/snapshot") == "http://doseguard-tray.local:81/stream"


def test_parser_handles_parts_split_across_network_reads():
    bodies = [jpeg(10), jpeg(90)]
    data = b"".join(part(b, i) for i, b in enumerate(bodies, 1))
    parser, got = MjpegParser(), []
    for i in range(0, len(data), 7):
        got += parser.feed(data[i:i + 7])
    assert [body for _, body in got] == bodies
    assert [headers["x-frame-seq"] for headers, _ in got] == ["1", "2"]


def test_parser_skips_a_malformed_part():
    good = jpeg(50)
    got = MjpegParser().feed(b"--frame\r\nContent-Length: nope\r\n\r\n" + part(good, 1))
    assert [body for _, body in got] == [good]


def test_preview_passes_camera_video_through_unchanged_without_repeats(fake):
    cam = fake()

    async def go():
        m = manager(cam)
        stream = m.preview_stream("tray")
        parts = await next_parts(stream, 6)
        await stream.aclose()
        await m.tray_stream.stop()
        return parts

    parsed = MjpegParser().feed(b"".join(asyncio.run(go())))
    assert len(parsed) >= 5
    assert all(body in cam.sent for _, body in parsed)
    ids = [headers["x-frame-id"] for headers, _ in parsed]
    assert len(ids) == len(set(ids))


def test_viewers_share_one_connection_and_it_closes_when_nobody_watches(fake):
    cam = fake()

    async def go():
        m = manager(cam)
        first, second = m.preview_stream("tray"), m.preview_stream("tray")
        await next_parts(first, 2)
        await next_parts(second, 2)
        assert cam.stream_connections == 1 and m.tray_stream.viewers == 2
        await first.aclose()
        await next_parts(second, 1)
        assert m.tray_stream.viewers == 1
        await second.aclose()
        assert m.tray_stream.viewers == 0
        return await until(lambda: cam.active_streams == 0)

    assert asyncio.run(go())


def test_guarded_tray_frames_never_come_from_the_video(fake):
    cam = fake()

    async def go():
        m = manager(cam)
        stream = m.preview_stream("tray")
        await next_parts(stream, 3)
        frame = await m.frame("tray")
        await stream.aclose()
        await m.tray_stream.stop()
        return frame

    frame = asyncio.run(go())
    assert frame is not None and frame.source_id == "esp32cam"
    assert "preview_only" not in frame.meta and cam.snapshots == 1


def test_overlay_uses_the_video_while_watching_and_a_snapshot_otherwise(fake):
    cam = fake()

    async def go():
        m = manager(cam)
        before = await m.live_frame("tray")
        stream = m.preview_stream("tray")
        await next_parts(stream, 3)
        snapshots_before = cam.snapshots
        during = await m.live_frame("tray")
        await stream.aclose()
        await m.tray_stream.stop()
        return before, during, snapshots_before

    before, during, snapshots = asyncio.run(go())
    assert before.source_id == "esp32cam"
    assert during.source_id == "esp32cam-stream" and during.meta["preview_only"] is True
    assert cam.snapshots == snapshots


def test_a_stale_video_feed_is_not_treated_as_live(fake):
    cam = fake(age_ms=5000)

    async def go():
        m = manager(cam)
        stream = m.preview_stream("tray")
        await next_parts(stream, 2)
        frame = m.tray_stream.latest_frame()
        await stream.aclose()
        await m.tray_stream.stop()
        return frame

    assert asyncio.run(go()) is None


def test_preview_falls_back_to_snapshots_when_the_firmware_has_no_video(fake):
    cam = fake(video=False)

    async def go():
        m = manager(cam)
        stream = m.preview_stream("tray")
        waiting = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0.05)
        assert await m.frame("tray") is not None
        data = await asyncio.wait_for(waiting, 3)
        await stream.aclose()
        await m.tray_stream.stop()
        return data

    assert b"X-Frame-Id: esp32cam:" in asyncio.run(go())
