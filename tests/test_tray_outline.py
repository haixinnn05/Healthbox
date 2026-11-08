"""Tray by colour, objects by outline: the check for a camera too soft to read corner markers."""
import cv2
import numpy as np
import pytest

from app.config import Settings
from app.vision import TrayInspector

CLASSES = [{"class_id": "blue", "cartridge": "A", "color": "#2F6FD6", "hsv": {"ranges": [[[100, 80, 50], [130, 255, 255]]]}},
           {"class_id": "orange", "cartridge": "B", "color": "#E8822A", "hsv": {"ranges": [[[5, 100, 80], [22, 255, 255]]]}}]
ONE_BLUE = {"blue": 1, "orange": 0}


@pytest.fixture
def tray():
    return TrayInspector(Settings(tray_vision="outline"))


def scene(pills=(), glare=True, markers=False, tilt=2.0, box=(70, 60, 570, 420), seed=3):
    """Like the real camera: gray tray on a pale table, soft focus, a glare patch and sensor noise."""
    img = np.full((480, 640, 3), (225, 228, 226), np.uint8)
    x0, y0, x1, y1 = box
    cv2.rectangle(img, (x0, y0), (x1, y1), (106, 110, 104), -1)
    if glare:
        yy, xx = np.mgrid[0:480, 0:640]
        g = 80 * np.exp(-(((xx - 260) / 90.0) ** 2 + ((yy - 300) / 60.0) ** 2))
        inside = np.zeros((480, 640), bool)
        inside[y0:y1, x0:x1] = True
        img = np.clip(img.astype(np.float32) + (g * inside)[..., None], 0, 255).astype(np.uint8)
    if markers:
        for mx, my in ((x0 + 6, y0 + 6), (x1 - 56, y0 + 6), (x1 - 56, y1 - 56), (x0 + 6, y1 - 56)):
            cv2.rectangle(img, (mx, my), (mx + 50, my + 50), (15, 15, 15), -1)
            cv2.rectangle(img, (mx + 12, my + 12), (mx + 30, my + 26), (240, 240, 240), -1)
            cv2.rectangle(img, (mx + 22, my + 30), (mx + 40, my + 40), (240, 240, 240), -1)
    for p in pills:
        kind, cx, cy = p[:3]
        color = p[3] if len(p) > 3 else (238, 240, 238)
        if kind == "round":
            cv2.circle(img, (cx, cy), 26, color, -1)
        elif kind == "capsule":
            cv2.ellipse(img, (cx, cy), (46, 16), 20, 0, 360, color, -1)
        elif kind == "hand":
            cv2.ellipse(img, (cx, cy), (170, 120), 0, 0, 360, (120, 150, 215), -1)
    M = cv2.getRotationMatrix2D((320, 240), tilt, 1.0)
    img = cv2.warpAffine(img, M, (640, 480), borderValue=(225, 228, 226))
    img = cv2.GaussianBlur(img, (0, 0), 1.6)
    noise = np.random.default_rng(seed).normal(0, 3, img.shape)
    return np.clip(img + noise, 0, 255).astype(np.uint8)


def n(result):
    return sum(result["counts"].values())


@pytest.mark.parametrize("pills,want", [
    ([], 0),
    ([("round", 320, 200)], 1),
    ([("round", 200, 170), ("round", 440, 320)], 2),
    ([("round", 180, 150), ("round", 320, 330), ("round", 470, 180)], 3),
])
def test_counts_white_pills_on_the_gray_tray(tray, pills, want):
    r = tray.analyze(scene(pills), CLASSES, {"blue": want, "orange": 0})
    assert r["status"] == "ok" and n(r) == want


def test_pill_inside_the_glare_still_counts_and_glare_alone_does_not(tray):
    assert n(tray.analyze(scene(), CLASSES, ONE_BLUE)) == 0
    assert n(tray.analyze(scene([("round", 260, 300)]), CLASSES, ONE_BLUE)) == 1


def test_two_touching_pills_count_as_two(tray):
    r = tray.analyze(scene([("round", 290, 220), ("round", 342, 220)]), CLASSES, {"blue": 2, "orange": 0})
    assert r["status"] == "ok" and n(r) == 2


def test_a_capsule_counts_as_one(tray):
    assert n(tray.analyze(scene([("capsule", 320, 220)]), CLASSES, ONE_BLUE)) == 1


def test_corner_markers_on_the_tray_are_ignored(tray):
    assert n(tray.analyze(scene(markers=True), CLASSES, ONE_BLUE)) == 0
    assert n(tray.analyze(scene([("round", 320, 220)], markers=True), CLASSES, ONE_BLUE)) == 1


def test_a_dark_pill_on_the_tray_counts(tray):
    assert n(tray.analyze(scene([("round", 320, 220, (25, 25, 30))]), CLASSES, ONE_BLUE)) == 1


def test_two_pills_when_one_is_expected_is_not_a_match(tray):
    r = tray.analyze(scene([("round", 200, 170), ("round", 440, 320)]), CLASSES, ONE_BLUE)
    assert r["counts"] != ONE_BLUE and n(r) == 2


def test_same_looking_pills_fill_a_two_cartridge_dose(tray):
    want = {"blue": 1, "orange": 1}
    r = tray.analyze(scene([("round", 200, 170), ("round", 440, 320)]), CLASSES, want)
    assert r["counts"] == want


def test_a_known_colour_goes_to_its_own_type(tray):
    r = tray.analyze(scene([("round", 320, 220, (42, 130, 232))]), CLASSES, ONE_BLUE)
    assert r["counts"] == {"blue": 0, "orange": 1}


def test_a_pill_of_the_wrong_colour_fails_a_calibrated_type(tray):
    white = [{"class_id": "white", "cartridge": "A", "color": "#fff",
              "hsv": {"ranges": [[[0, 0, 200], [179, 40, 255]]], "sample_hsv": [0, 5, 235]}}]
    r = tray.analyze(scene([("round", 320, 220, (200, 90, 60))]), white, {"white": 1})
    assert r["status"] != "ok"


def test_a_hand_over_the_tray_is_not_a_count(tray):
    r = tray.analyze(scene([("hand", 320, 240)]), CLASSES, ONE_BLUE)
    assert r["status"] != "ok" or n(r) != 1


@pytest.mark.parametrize("box", [(-40, 60, 570, 420), (70, 60, 700, 420), (70, 140, 570, 520)])
def test_a_tray_running_out_of_view_is_refused(tray, box):
    assert tray.analyze(scene(box=box, tilt=0), CLASSES, ONE_BLUE)["status"] == "markers_missing"


def test_a_plain_surface_is_not_a_tray(tray):
    wall = np.full((480, 640, 3), (120, 122, 118), np.uint8)
    assert tray.analyze(wall, CLASSES, ONE_BLUE)["status"] == "markers_missing"
    assert tray.analyze(np.zeros((480, 640, 3), np.uint8), CLASSES, ONE_BLUE)["status"] == "markers_missing"


def test_soft_focus_is_not_rejected_by_the_marker_blur_rule(tray):
    img = cv2.GaussianBlur(scene([("round", 320, 220)]), (0, 0), 2.0)
    r = tray.analyze(img, CLASSES, ONE_BLUE)
    assert r["status"] == "ok" and n(r) == 1


def test_proof_photo_is_cropped_to_the_tray(tray):
    img = tray.render_evidence(scene([("round", 320, 220)]), CLASSES, ONE_BLUE)
    assert img is not None and img.shape[0] > 100
    assert tray.render_evidence(np.zeros((480, 640, 3), np.uint8), CLASSES, ONE_BLUE) is None


def test_outline_is_the_default_for_the_real_app(monkeypatch):
    monkeypatch.delenv("TRAY_VISION", raising=False)
    assert Settings.from_env().tray_vision == "outline"
    assert any("TRAY_VISION" in p for p in Settings.from_env(tray_vision="nope").validate())
