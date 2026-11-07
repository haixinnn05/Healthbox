"""Camera performance paths must stay bounded and preserve verification evidence."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import cv2
import numpy as np

from app.camera import CaptureManager, Frame
from app.config import Settings
from app.store import Store
from app.timeutil import Clock, FakeClock
from app.vision import FaceEngine, VisionService


def manager():
    clock = FakeClock(datetime(2026, 11, 7, tzinfo=timezone.utc))
    camera = CaptureManager(Settings(camera_mode="single_laptop"), clock)
    frame = Frame(np.zeros((720, 1280, 3), np.uint8), 1, "laptop0", "session", "face",
                  clock.monotonic(), clock.now())
    source = SimpleNamespace(frame=frame)
    source.latest = lambda: source.frame
    camera.laptop = source
    return camera, source, clock


def test_preview_reuses_encoding_across_viewers_and_rejects_stale_frame(monkeypatch):
    camera, source, clock = manager()
    encode = cv2.imencode
    calls = []

    def counted(*args):
        calls.append(args[1].shape)
        return encode(*args)

    monkeypatch.setattr(cv2, "imencode", counted)
    first = camera.preview_frame("face")
    assert camera.preview_frame("tray") is first  # one physical camera, two roles
    assert calls == [(360, 640, 3)]
    source.frame.seq += 1
    assert camera.preview_frame("face")[0] != first[0]
    assert len(calls) == 2
    clock.advance(1.1)
    assert camera.preview_frame("face") is None
    assert len(calls) == 2


def test_stream_sends_only_distinct_latest_frames():
    async def run():
        camera, source, _ = manager()
        stream = camera.preview_stream("face")
        try:
            first = await anext(stream)
            assert b"Content-Type: image/jpeg\r\n" in first
            next_frame = asyncio.create_task(anext(stream))
            await asyncio.sleep(0.1)
            assert not next_frame.done()  # don't replay the same JPEG at 24 fps
            source.frame.seq = 10  # intermediate frames were dropped by the capture owner
            second = await asyncio.wait_for(next_frame, 1)
            assert b"X-Frame-Id: laptop0:sessio:10\r\n" in second
        finally:
            await stream.aclose()

    asyncio.run(run())


class CameraStub:
    simulated = False

    def __init__(self, clock, repeat=False, restart=False):
        self.clock, self.seq, self.repeat, self.restart = clock, 0, repeat, restart

    async def frame(self, role):
        self.seq += 1
        return Frame(np.zeros((32, 32, 3), np.uint8), 1 if self.repeat else self.seq, "test",
                     "new" if self.restart and self.seq > 1 else "session", role,
                     self.clock.monotonic(), self.clock.now())


def service(tmp_path, **settings):
    clock = Clock()
    s = Settings(data_dir=tmp_path, db_path=tmp_path / "camera.db", camera_mode="single_laptop", **settings)
    store = Store(s.db_path, clock)
    return VisionService(s, store, CameraStub(clock), SimpleNamespace(fields={"rev": 0}, session="test"), clock)


def test_positioning_hints_share_detection_and_yield_to_identity_check(tmp_path):
    async def run():
        svc = service(tmp_path)
        calls = []

        def detect(img):
            calls.append(img.shape)
            return {"faces": 1, "score": 0.95}

        # No embedding method: a positioning hint must never require recognition.
        svc.face = SimpleNamespace(available=lambda: True, detect=detect)
        try:
            a, b = await asyncio.gather(svc.face_preview(), svc.face_preview())
            assert a == b == {"available": True, "faces": 1, "score": 0.95}
            assert len(calls) == 1
            async with svc._face_operation:
                assert (await svc.face_preview())["busy"] is True
            assert len(calls) == 1
        finally:
            svc.store.conn.close()

    asyncio.run(run())


def test_detection_does_not_compute_an_embedding():
    eng = FaceEngine(Settings().models_dir)
    eng._det = SimpleNamespace(setInputSize=lambda size: None,
                              detect=lambda img: (None, np.array([[0] * 14 + [0.95]], np.float32)))
    # An absent recognizer makes accidental embedding work fail loudly.
    assert eng.detect(np.zeros((32, 32, 3), np.uint8))["faces"] == 1


def test_partial_collection_timeout_cannot_pass_a_check(tmp_path):
    async def run():
        svc = service(tmp_path, check_frames=10, check_min_span_s=0.1, check_timeout_s=0.12)
        svc.camera.repeat = True
        try:
            obs = await svc.check_tray({}, "TRAY_EMPTY")
            assert obs.result == "NOT_OBSERVED"
            assert obs.reason_code == "CAMERA_STALE"
            assert len(obs.frame_ids) == 1
        finally:
            svc.store.conn.close()

    asyncio.run(run())


def test_camera_restart_invalidates_multiframe_collection(tmp_path):
    async def run():
        svc = service(tmp_path, check_frames=10, check_min_span_s=0.1, check_timeout_s=0.2)
        svc.camera.restart = True
        try:
            frames, error = await svc._collect("face", lambda img: {"faces": 1})
            assert len(frames) == 1
            assert error == "CAMERA_STALE"
        finally:
            svc.store.conn.close()

    asyncio.run(run())
