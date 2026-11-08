"""Real (non-simulated) vision code on synthetic images: tray markers, counting, failure cases,
and that the face models load and run on this OpenCV build."""
import cv2
import numpy as np
import pytest

from app.config import ROOT, Settings
from app.store import DEFAULT_CLASSES
from app.vision import FaceEngine, TrayInspector

CLASSES = [{"class_id": c[0], "label": c[1], "cartridge": c[2], "hsv": c[4]} for c in DEFAULT_CLASSES]
BLUE, ORANGE = (214, 111, 47), (42, 130, 232)  # BGR, matching the demo token colours


def tray_image(tokens, drop_marker=None, tilt=True, blur=0, bg=(205, 210, 200), first_id=0):
    """tokens: (x, y, bgr) round, or (x, y, bgr, "capsule", angle_deg) in a 640x480 tray area."""
    W, H, M = 800, 640, 60
    img = np.full((H, W, 3), bg, np.uint8)  # matte tray
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    corners = [(M, M), (W - M, M), (W - M, H - M), (M, H - M)]
    for i, (cx, cy) in enumerate(corners):
        if i == drop_marker:
            continue
        m = cv2.cvtColor(cv2.aruco.generateImageMarker(d, first_id + i, 70), cv2.COLOR_GRAY2BGR)
        pad = cv2.copyMakeBorder(m, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=(255, 255, 255))
        s = pad.shape[0] // 2
        img[cy - s:cy + s, cx - s:cx + s] = pad
    for t in tokens:
        x, y, color = t[:3]
        cx, cy = int(M + 40 + x), int(M + 40 + y)
        if len(t) > 3 and t[3] == "capsule":
            a = np.deg2rad(t[4])
            dx, dy = int(30 * np.cos(a)), int(30 * np.sin(a))
            cv2.line(img, (cx - dx, cy - dy), (cx + dx, cy + dy), color, 36, cv2.LINE_AA)  # round caps
        else:
            cv2.circle(img, (cx, cy), 22, color, -1, cv2.LINE_AA)
    if tilt:
        src = np.float32([[0, 0], [W, 0], [W, H], [0, H]])
        dst = np.float32([[30, 20], [W - 10, 0], [W - 40, H - 10], [0, H - 30]])
        img = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (W, H), borderValue=(90, 90, 90))
    if blur:
        img = cv2.GaussianBlur(img, (blur, blur), 0)
    # mild sensor noise so the sharpness test sees texture
    noise = np.random.default_rng(1).normal(0, 6, img.shape)
    return np.clip(img + noise, 0, 255).astype(np.uint8)


@pytest.fixture
def inspector():
    return TrayInspector(Settings(tray_token_area_px=900, blur_threshold=20.0))


def test_counts_each_class(inspector):
    r = inspector.analyze(tray_image([(100, 100, BLUE), (300, 200, ORANGE), (450, 120, ORANGE)]), CLASSES)
    assert r["status"] == "ok", r
    assert r["counts"] == {"blue": 1, "orange": 2}


def test_empty_tray(inspector):
    r = inspector.analyze(tray_image([]), CLASSES)
    assert r["status"] == "ok" and r["counts"] == {"blue": 0, "orange": 0}


def test_missing_marker_is_reported(inspector):
    r = inspector.analyze(tray_image([(100, 100, BLUE)], drop_marker=2), CLASSES)
    assert r["status"] == "markers_missing"


def test_touching_tokens_are_flagged(inspector):
    r = inspector.analyze(tray_image([(200, 200, BLUE), (232, 200, BLUE)]), CLASSES)
    assert r["status"] in ("touching", "shape_mismatch")  # uncalibrated: blocked either way


def test_blurry_image_is_flagged(inspector):
    r = inspector.analyze(tray_image([(200, 200, BLUE)], blur=31), CLASSES)
    assert r["status"] in ("blurry", "markers_missing")  # either way: no PASS


def test_face_models_run_on_this_opencv():
    eng = FaceEngine(ROOT / "models")
    if not eng.available():
        pytest.skip("face models not downloaded")
    blank = np.full((480, 640, 3), 128, np.uint8)
    assert eng.analyze(blank)["faces"] == 0


class StubCamera:
    """Feeds synthetic frames with increasing sequence numbers, like a live camera owner."""
    simulated = False

    def __init__(self, clock, image):
        self.clock, self.image, self.seq = clock, image, 0

    async def frame(self, role):
        import asyncio
        from app.camera import Frame
        await asyncio.sleep(0.02)
        self.seq += 1
        return Frame(self.image, self.seq, "stub", "sess", role, self.clock.monotonic(), self.clock.now())


class StubHardware:
    fields = {"rev": 7}
    session = "abcd"

    def sim_tray(self):
        return None


def _service(tmp_path, image):
    from app.store import Store
    from app.timeutil import Clock
    from app.vision import VisionService
    clock = Clock()
    s = Settings(db_path=tmp_path / "v.db", data_dir=tmp_path, check_frames=10, check_min_span_s=0.2, blur_threshold=20.0)
    store = Store(s.db_path, clock)
    return VisionService(s, store, StubCamera(clock, image), StubHardware(), clock)


def test_multiframe_tray_check_passes_and_fails(tmp_path):
    import asyncio
    svc = _service(tmp_path, tray_image([(150, 150, BLUE)]))
    ok = asyncio.run(svc.check_tray({"blue": 1}, "CLASS_COUNTS"))
    assert ok.result == "PASS" and len(ok.frame_ids) == 10 and ok.tray_revision == 7
    assert ok.details["span_s"] >= 0.2
    bad = asyncio.run(svc.check_tray({"blue": 1, "orange": 1}, "CLASS_COUNTS"))
    assert bad.result == "FAIL" and bad.message.startswith("Expected 1 orange token and observed 0.")


YELLOW, WHITE = (40, 200, 230), (245, 245, 245)


def calibrated(inspector, img, class_id, label):
    m = inspector.measure_single(img)
    assert m["status"] == "ok", m
    return {"class_id": class_id, "label": label, "cartridge": None, "hsv": {"ranges": m["ranges"], "shape": m["shape"]}}, m


def test_calibrate_round_and_capsule_then_count(inspector):
    rnd, m1 = calibrated(inspector, tray_image([(200, 200, BLUE)]), "blue", "Blue round")
    cap, m2 = calibrated(inspector, tray_image([(200, 200, YELLOW, "capsule", 20)]), "yellow", "Yellow capsule")
    assert m1["shape"]["kind"] == "round" and m2["shape"]["kind"] == "capsule"
    assert m2["shape"]["elongation"] > 1.6
    # Capsules at different angles count as one each; round tablets too.
    img = tray_image([(80, 80, BLUE), (300, 90, YELLOW, "capsule", 0), (420, 250, YELLOW, "capsule", 75), (150, 300, BLUE)])
    r = inspector.analyze(img, [rnd, cap])
    assert r["status"] == "ok", r
    assert r["counts"] == {"blue": 2, "yellow": 2}


def test_capsules_touching_end_to_end_are_uncertain(inspector):
    cap, _ = calibrated(inspector, tray_image([(200, 200, YELLOW, "capsule", 0)]), "yellow", "Yellow capsule")
    r = inspector.analyze(tray_image([(200, 200, YELLOW, "capsule", 0), (290, 200, YELLOW, "capsule", 0)]), [cap])
    assert r["status"] == "touching", r


def test_round_pill_of_capsule_colour_is_shape_mismatch(inspector):
    cap, _ = calibrated(inspector, tray_image([(200, 200, YELLOW, "capsule", 0)]), "yellow", "Yellow capsule")
    r = inspector.analyze(tray_image([(200, 200, YELLOW)]), [cap])
    assert r["status"] in ("shape_mismatch", "fragment"), r


def test_white_pill_on_dark_tray(inspector):
    dark = (45, 45, 50)
    white, m = calibrated(inspector, tray_image([(200, 200, WHITE)], bg=dark), "white", "White round")
    assert m["sample_hsv"][1] < 60  # recognised as a pale pill
    r = inspector.analyze(tray_image([(100, 100, WHITE), (350, 250, WHITE)], bg=dark), [white])
    assert r["status"] == "ok" and r["counts"] == {"white": 2}, r


def test_calibration_refuses_two_objects(inspector):
    m = inspector.measure_single(tray_image([(100, 100, BLUE), (300, 200, BLUE)]))
    assert m["status"] == "multiple_objects"



def test_tray_bypass_is_refused_with_real_hardware():
    from app.config import Settings
    real = Settings.from_env(hardware_mode="serial", serial_port="/dev/cu.test", tray_bypass=True, genai_backend="off")
    assert any("TRAY_BYPASS" in p for p in real.validate())
    fake = Settings.from_env(hardware_mode="fake", tray_bypass=True, genai_backend="off")
    assert not any("TRAY_BYPASS" in p for p in fake.validate())


def test_tray_bypass_marks_tray_results_simulated_and_reports_itself(harness):
    h = harness(tray_bypass=True)
    card = h.demo_due()
    t = h.to_dose_check(card)
    r = h.check(t)
    assert r["result"] == "PASS"
    obs = h.c.get(f"/api/history/{card['dose_id']}").json()["observations"]
    assert all(o["simulated"] for o in obs if o["kind"] in ("TRAY_EMPTY", "CLASS_COUNTS"))


def test_real_tray_check_keeps_a_circled_photo_of_the_tray_only(tmp_path):
    """The photo proof is the warped tray area (never the whole frame), with each counted pill ringed."""
    import asyncio
    img = tray_image([(150, 150, BLUE), (350, 220, ORANGE)])
    svc = _service(tmp_path, img)
    obs = asyncio.run(svc.check_tray({"blue": 1, "orange": 1}, "CLASS_COUNTS"))
    assert obs.result == "PASS"
    photo = cv2.imread(str(tmp_path / obs.details["photo"]))
    assert photo.shape[:2] == (488, 688)  # 344x244 tray area, doubled
    plain = TrayInspector(svc.settings)._warp(img)[0]
    plain = cv2.resize(plain, (688, 488))
    assert np.abs(photo.astype(int) - plain.astype(int)).max() > 100  # rings and tags were drawn
    # No photo at all when the markers (and so the tray area) are not found.
    svc2 = _service(tmp_path / "b", tray_image([(150, 150, BLUE)], drop_marker=2))
    (tmp_path / "b").mkdir(exist_ok=True)
    obs2 = asyncio.run(svc2.check_tray({"blue": 1}, "CLASS_COUNTS"))
    assert obs2.reason_code == "TRAY_MARKERS_MISSING" and "photo" not in obs2.details



# ---------------------------------------------------------------- pill dish and live view
def dish_image(tokens):
    """The pill dish: same layout as the tray but markers 4-7 and a dark floor."""
    return tray_image(tokens, bg=(45, 45, 50), first_id=4)


def test_dish_is_counted_and_never_mistaken_for_the_tray(tmp_path):
    svc = _service(tmp_path, dish_image([(120, 120, BLUE), (300, 200, BLUE)]))
    classes = svc.store.token_classes()
    r = svc._hand_analyze(dish_image([(120, 120, BLUE), (300, 200, BLUE)]), [c for c in classes if c["class_id"] == "blue"])
    assert r["status"] == "ok" and r["source"] == "dish" and r["counts"] == {"blue": 2}
    # The tray's markers are not the dish, and the dish's markers are not the tray.
    assert svc.tray._warp(dish_image([]))[1]["status"] == "markers_missing"
    assert svc.tray._warp(tray_image([]), marker_ids=(4, 5, 6, 7))[1]["status"] == "markers_missing"


def test_hand_check_uses_the_dish_and_keeps_its_photo(tmp_path):
    import asyncio
    svc = _service(tmp_path, dish_image([(200, 150, BLUE)]))
    obs = asyncio.run(svc.check_hand({"blue": 1}))
    assert obs.result == "PASS" and obs.details["source"] == "dish" and obs.method == "dish-markers+hsv"
    assert (tmp_path / obs.details["photo"]).exists()
    bad = asyncio.run(svc.check_hand({"blue": 2}))
    assert bad.result == "FAIL" and bad.reason_code == "HAND_COUNT_MISMATCH" and "Expected 2 blue and saw 1" in bad.message


def test_live_view_rings_tray_pills_in_frame_coordinates(tmp_path):
    import asyncio
    img = tray_image([(150, 150, BLUE)])
    svc = _service(tmp_path, img)
    r = asyncio.run(svc.live("tray", {"blue": 1}))
    assert r["available"] and r["ready"] is True and r["counts"] == {"blue": 1, "orange": 0}
    assert len(r["quad"]) == 4 and all(0 <= x <= 1 and 0 <= y <= 1 for x, y in r["quad"])
    (p,) = r["pills"]
    assert 0.15 < p["x"] < 0.6 and 0.15 < p["y"] < 0.6 and p["r"] > 0.01
    assert asyncio.run(svc.live("tray", {"blue": 2}))["ready"] is False
    empty = asyncio.run(svc.live("tray", {}))
    assert empty["ready"] is False  # a pill is still there


def test_live_view_of_the_dish(tmp_path):
    import asyncio
    svc = _service(tmp_path, dish_image([(150, 150, BLUE)]))
    r = asyncio.run(svc.live("hand", {"blue": 1}))
    assert r["source"] == "dish" and r["ready"] is True and len(r["pills"]) == 1


def test_a_moving_hand_asks_to_hold_still(tmp_path, monkeypatch):
    """Real practice photos of a moving hand measured 3-6 sharpness; they are not counted (a smeared pill
    would read as 'saw 0'), the person is asked to hold still instead."""
    import asyncio
    svc = _service(tmp_path, tray_image([]))
    monkeypatch.setattr(svc, "_hand_analyze", lambda img, classes, geometry=False: {"status": "blurry", "source": "palm", "sharpness": 4.0})
    obs = asyncio.run(svc.check_hand({"blue": 1}))
    assert obs.result == "NOT_OBSERVED" and obs.reason_code == "HAND_BLURRY" and "Hold your hand still" in obs.message
    sharp = cv2.cvtColor(tray_image([(100, 100, BLUE)]), cv2.COLOR_BGR2BGRA)[:, :, :3]
    assert svc._sharpness(sharp) > 8 > svc._sharpness(cv2.GaussianBlur(sharp, (41, 41), 0))
