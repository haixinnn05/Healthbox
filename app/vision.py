"""Vision: recipient recognition and tray counting. Produces observations only; it never
moves hardware. Uses pretrained YuNet (detection) and SFace (embedding) plus ordinary
image processing for the tray. No model training.

Observation results: PASS, FAIL, UNCERTAIN, NOT_OBSERVED."""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import evidence
from .camera import CaptureManager, Frame
from .config import Settings
from .evidence import simulated_tray
from .store import dumps
import json
from .hardware import HardwareAdapter
from .reasons import count_mismatch_sentence, message
from .store import Store
from .taking import ActionTracker, PalmDetector, Sample, Watch
from .timeutil import Clock, iso

log = logging.getLogger("doseguard.vision")

YUNET_FILE = "face_detection_yunet_2023mar.onnx"
SFACE_FILE = "face_recognition_sface_2021dec.onnx"
SIM_FACE = ("recipient", "unknown", "two_faces", "none")
SIM_TRAY = ("auto", "touching", "markers_missing", "blurry")
SIM_ACTION = ("taken", "not_seen")  # simulated hand-to-mouth movement after release
SIM_HAND = ("shown", "none", "wrong")  # simulated "show the pills in your hand"


@dataclass
class Observation:
    kind: str  # FACE_MATCH | TRAY_EMPTY | CLASS_COUNTS
    result: str
    method: str
    reason_code: str | None = None
    message: str | None = None
    details: dict = field(default_factory=dict)
    frame_ids: list[str] = field(default_factory=list)
    capture_time: datetime | None = None  # wall time of the oldest frame used
    capture_mono: float | None = None  # monotonic time of the oldest frame used
    tray_revision: int | None = None
    controller_session: str | None = None
    simulated: bool = False


# ------------------------------------------------------------------------------------
# Face
# ------------------------------------------------------------------------------------
class FaceEngine:
    def __init__(self, models_dir: Path):
        self.models_dir = models_dir
        self._det = None
        self._rec = None
        self._lock = threading.Lock()
        self.error: str | None = None

    def available(self) -> bool:
        return (self.models_dir / YUNET_FILE).exists() and (self.models_dir / SFACE_FILE).exists()

    def _load(self) -> None:
        if self._det is not None:
            return
        import cv2
        # Score 0.7: measured ~0.92 for a clear face at 640 px wide, stray detections stay below 0.45.
        self._det = cv2.FaceDetectorYN.create(str(self.models_dir / YUNET_FILE), "", (320, 320), 0.7, 0.3, 50)
        self._rec = cv2.FaceRecognizerSF.create(str(self.models_dir / SFACE_FILE), "")

    DETECT_WIDTH = 640  # YuNet is far more confident (0.92 vs 0.70) and faster on small inputs

    def _detect(self, img):
        import cv2
        self._load()
        h, w = img.shape[:2]
        if w > self.DETECT_WIDTH:
            img = cv2.resize(img, (self.DETECT_WIDTH, int(h * self.DETECT_WIDTH / w)), interpolation=cv2.INTER_AREA)
            h, w = img.shape[:2]
        self._det.setInputSize((w, h))
        _, faces = self._det.detect(img)
        return img, faces

    def detect(self, img) -> dict:
        """Positioning hints need detection, not an identity embedding."""
        with self._lock:
            _, faces = self._detect(img)
            n = 0 if faces is None else len(faces)
            return {"faces": n, "score": float(faces[0][-1]) if n == 1 else 0.0}

    def analyze(self, img) -> dict:
        import numpy as np
        # YuNet input size and SFace buffers are mutable; do not run them concurrently.
        with self._lock:
            img, faces = self._detect(img)
            n = 0 if faces is None else len(faces)
            if n != 1:
                return {"faces": n}
            aligned = self._rec.alignCrop(img, faces[0])
            feat = self._rec.feature(aligned).flatten().astype("float32")
            feat /= (np.linalg.norm(feat) + 1e-9)
            return {"faces": 1, "feature": feat, "score": float(faces[0][-1])}

    @staticmethod
    def best_similarity(feat, refs) -> float:
        import numpy as np
        refs = np.asarray(refs, dtype="float32")
        refs = refs / (np.linalg.norm(refs, axis=1, keepdims=True) + 1e-9)
        return float(np.max(refs @ feat))


# ------------------------------------------------------------------------------------
# Tray
# ------------------------------------------------------------------------------------
WARP_W, WARP_H, BORDER = 400, 300, 28
TRAY_IDS = (0, 1, 2, 3)  # tray corner markers: top-left, top-right, bottom-right, bottom-left
DISH_IDS = (4, 5, 6, 7)  # the small pill dish has its own markers, so it is never mistaken for the tray


class TrayInspector:
    """Four ArUco markers (DICT_4X4_50, ids 0..3 = top-left, top-right, bottom-right,
    bottom-left) define the tray. The view is warped to a fixed rectangle, then each
    token class is segmented by colour and every blob is compared with that class's
    calibrated shape profile (area, elongation, solidity). Round tablets, capsules and
    oblong tablets all work, as long as each class is calibrated with one real sample."""

    def __init__(self, settings: Settings):
        self.settings = settings

    # -- geometry ----------------------------------------------------------------
    def _warp(self, img, marker_ids: tuple = TRAY_IDS, geometry: bool = False):
        """Find the four corner markers `marker_ids` and return the area inside them, perspective-corrected.
        With geometry=True, info["_geom"] = (corner points in the image, transform) for overlays."""
        import cv2
        import numpy as np
        if marker_ids == TRAY_IDS and self.settings.tray_vision == "outline":
            return self._warp_by_colour(img, geometry)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50),
                                           cv2.aruco.DetectorParameters())
        corners, ids, _ = detector.detectMarkers(gray)
        found = {}
        if ids is not None:
            for c, i in zip(corners, ids.flatten()):
                if int(i) in marker_ids:
                    found[int(i)] = c.reshape(4, 2).mean(axis=0)
        if len(found) < 4:
            return None, {"status": "markers_missing", "markers": sorted(found)}
        src = np.array([found[i] for i in marker_ids], dtype="float32")
        dst = np.array([[0, 0], [WARP_W, 0], [WARP_W, WARP_H], [0, WARP_H]], dtype="float32")
        M = cv2.getPerspectiveTransform(src, dst)
        warped = cv2.warpPerspective(img, M, (WARP_W, WARP_H))
        # Focus is measured over the whole warped view: marker edges give texture even when
        # the matte tray floor has none.
        sharp = float(cv2.Laplacian(cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
        if sharp < self.settings.blur_threshold:
            return None, {"status": "blurry", "sharpness": round(sharp, 1)}
        info = {"sharpness": round(sharp, 1)}
        if geometry:
            info["_geom"] = (src, M)
        return warped[BORDER:WARP_H - BORDER, BORDER:WARP_W - BORDER], info

    # -- tray by colour, objects by outline -------------------------------------------
    def _find_tray_quad(self, img):
        """The four corners of the tray, found as the large single-coloured shape in the middle of the
        view. None unless the whole tray is in view with its edges showing against what surrounds it."""
        import cv2
        import numpy as np
        h, w = img.shape[:2]
        lab = cv2.cvtColor(cv2.GaussianBlur(img, (7, 7), 0), cv2.COLOR_BGR2LAB).astype(np.int16)
        centre = lab[int(h * 0.3):int(h * 0.7), int(w * 0.3):int(w * 0.7)].reshape(-1, 3)
        l0, a0, b0 = (float(np.median(centre[:, i])) for i in range(3))
        near = (np.abs(lab[..., 0] - l0) < 45) & (np.abs(lab[..., 1] - a0) < 14) & (np.abs(lab[..., 2] - b0) < 14)
        mask = cv2.morphologyEx(near.astype(np.uint8) * 255, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        if n < 2:
            return None
        best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        contours, _ = cv2.findContours((labels == best).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        hull = cv2.convexHull(max(contours, key=cv2.contourArea))
        area = float(cv2.contourArea(hull))
        x, y, bw, bh = cv2.boundingRect(hull)
        if not 0.15 * w * h <= area <= 0.95 * w * h or x <= 2 or y <= 2 or x + bw >= w - 2 or y + bh >= h - 2:
            return None  # too small, or running off the edge of the view: not the whole tray
        (_, (rw, rh), _) = rect = cv2.minAreaRect(hull)
        if area / max(1.0, rw * rh) < 0.85:
            return None  # not rectangular
        filled = np.zeros((h, w), np.uint8)
        cv2.drawContours(filled, [hull], -1, 255, -1)
        ring = cv2.dilate(filled, np.ones((25, 25), np.uint8)) & ~cv2.dilate(filled, np.ones((9, 9), np.uint8))
        outside = lab[ring > 0]
        if not len(outside) or max(abs(float(np.median(outside[:, 0])) - l0), abs(float(np.median(outside[:, 1])) - a0),
                                   abs(float(np.median(outside[:, 2])) - b0)) < 18:
            return None  # the surroundings look the same: a plain wall or table, not a tray
        approx = cv2.approxPolyDP(hull, 0.03 * cv2.arcLength(hull, True), True)
        pts = approx.reshape(-1, 2).astype("float32") if len(approx) == 4 else cv2.boxPoints(rect).astype("float32")
        s, d = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
        quad = np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], dtype="float32")
        if np.linalg.norm(quad[1] - quad[0]) < np.linalg.norm(quad[3] - quad[0]):
            quad = np.roll(quad, 1, axis=0)  # keep the long side across
        return quad

    def _warp_by_colour(self, img, geometry: bool):
        import cv2
        import numpy as np
        src = self._find_tray_quad(img)
        if src is None:
            return None, {"status": "markers_missing", "markers": []}
        # Unlike markers (which sit on the tray), these corners are the tray's own: keep nearly all of it.
        m = BORDER - 8
        dst = np.array([[m, m], [WARP_W - m, m], [WARP_W - m, WARP_H - m], [m, WARP_H - m]], dtype="float32")
        M = cv2.getPerspectiveTransform(src, dst)
        warped = cv2.warpPerspective(img, M, (WARP_W, WARP_H))
        info = {"sharpness": round(float(cv2.Laplacian(cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()), 1)}
        if geometry:
            info["_geom"] = (src, M)
        return warped[BORDER:WARP_H - BORDER, BORDER:WARP_W - BORDER], info

    def _find_objects(self, roi) -> list[dict]:
        """Everything on the tray that differs from the tray colour, by outline. Soft glare follows the
        slowly-changing background and drops out. Touching round pills count separately."""
        import cv2
        import numpy as np
        lab = cv2.cvtColor(cv2.GaussianBlur(roi, (5, 5), 0), cv2.COLOR_BGR2LAB)
        L = lab[..., 0].astype(np.float32)
        k = np.ones((3, 3), np.uint8)
        # An object has a hard edge; glare fades out gradually, however bright it is. So objects are
        # the shapes enclosed by strong edges, which also keeps glare from merging with a pill beside it.
        grad = sum(np.abs(cv2.Sobel(lab[..., i], cv2.CV_32F, dx, 1 - dx)) for i in range(3) for dx in (0, 1))
        edges = (grad > 45).astype(np.uint8) * 255
        rh, rw = edges.shape
        edges[:4, :] = edges[-4:, :] = 0
        edges[:, :4] = edges[:, -4:] = 0
        # Paper corner markers are black and white together: not objects.
        tray_l = float(np.median(L))
        for y0, x0 in ((0, 0), (0, rw - int(rw * 0.22)), (rh - int(rh * 0.28), 0), (rh - int(rh * 0.28), rw - int(rw * 0.22))):
            y1, x1 = y0 + int(rh * 0.28), x0 + int(rw * 0.22)
            patch = L[y0:y1, x0:x1]
            if (patch < tray_l - 35).mean() > 0.04 and (patch > tray_l + 35).mean() > 0.02:
                edges[y0:y1, x0:x1] = 0
        sealed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        outlines, _ = cv2.findContours(sealed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        mask = np.zeros(edges.shape, np.uint8)
        cv2.drawContours(mask, outlines, -1, 255, -1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        found = []
        for c in contours:
            area = float(cv2.contourArea(c))
            if area < self.settings.tray_min_area_px:
                continue
            one = np.zeros(mask.shape, np.uint8)
            cv2.drawContours(one, [c], -1, 255, -1)
            inside = cv2.erode(one, k, iterations=2)
            sample = cv2.cvtColor(cv2.GaussianBlur(roi, (5, 5), 0), cv2.COLOR_BGR2HSV)[inside > 0]
            hsv = [int(np.median(sample[:, i])) for i in range(3)] if len(sample) else [0, 0, 0]
            if area > 0.25 * mask.size:
                (x, y), r = cv2.minEnclosingCircle(c)
                found.append({"x": float(x), "y": float(y), "r": float(r), "hsv": hsv, "verdict": "shape_mismatch", "area": area})
                continue  # far too big for a pill: a hand or something covering the tray
            dist = cv2.distanceTransform(one, cv2.DIST_L2, 5)
            n, labels = cv2.connectedComponents((dist >= 0.78 * dist.max()).astype(np.uint8))
            for i in range(1, n):
                ys, xs = np.nonzero(labels == i)
                found.append({"x": float(xs.mean()), "y": float(ys.mean()), "r": float(dist[labels == i].max()), "hsv": hsv,
                              "verdict": "ok", "area": area / (n - 1)})
        return found

    def _count_outlines(self, roi, classes: list[dict], expected: dict | None) -> dict:
        """Count objects by outline. An object in a known pill colour goes to that type. One in no known
        colour (white pills on an uncalibrated type) fills the types this dose expects, in order."""
        objects = self._find_objects(roi)
        counts = {c["class_id"]: 0 for c in classes}
        by_id = {c["class_id"]: c for c in classes}
        problems, blobs, unknown = [], [], []
        for ob in objects:
            hit = next((c["class_id"] for c in classes if _in_ranges(ob["hsv"], (c.get("hsv") or {}).get("ranges") or [])), None)
            if ob["verdict"] == "ok" and hit is None:
                unknown.append(ob)
                continue
            ob["class_id"] = hit or (classes[0]["class_id"] if classes else "")
            blobs.append(ob)
        wanted = [cid for cid, n in (expected or {}).items() for _ in range(int(n))]
        taken = {cid: sum(b["class_id"] == cid for b in blobs if b["verdict"] == "ok") for cid in counts}
        fill = []
        for cid in wanted:
            if taken.get(cid, 0) > 0:
                taken[cid] -= 1
            else:
                fill.append(cid)
        overflow = next((cid for cid, n in (expected or {}).items() if n > 0), None) or (classes[0]["class_id"] if classes else "")
        for ob in unknown:
            cid = fill.pop(0) if fill else overflow
            if (by_id.get(cid, {}).get("hsv") or {}).get("sample_hsv"):
                ob["verdict"] = "shape_mismatch"  # this type was calibrated and the object isn't that colour
            ob["class_id"] = cid
            blobs.append(ob)
        for ob in blobs:
            if ob["verdict"] == "ok":
                counts[ob["class_id"]] = counts.get(ob["class_id"], 0) + 1
            else:
                problems.append({"class_id": ob["class_id"], "verdict": ob["verdict"], "area": round(ob["area"]), "elongation": 0})
        return {"status": "shape_mismatch" if problems else "ok", "counts": counts, "problems": problems[:6],
                "blobs": [{"class_id": b["class_id"], "x": round(b["x"], 1), "y": round(b["y"], 1), "r": round(b["r"], 1),
                           "verdict": b["verdict"]} for b in blobs[:24]]}

    def count_loose(self, roi, classes: list[dict]) -> tuple[dict, list]:
        """Count pills by colour without the tray's size rules (the dish and the hand are a different size
        from the tray, so calibrated areas don't apply). Returns (counts, blobs in roi pixels)."""
        import cv2
        hsv = cv2.cvtColor(cv2.GaussianBlur(roi, (5, 5), 0), cv2.COLOR_BGR2HSV)
        area = roi.shape[0] * roi.shape[1]
        counts, blobs = {}, []
        for cls in classes:
            contours, _ = cv2.findContours(self._mask(hsv, cls["hsv"]["ranges"]), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            n = 0
            for c in contours:
                a = cv2.contourArea(c)
                if max(self.settings.tray_min_area_px * 0.5, 0.002 * area) <= a <= 0.2 * area:
                    (x, y), r = cv2.minEnclosingCircle(c)
                    blobs.append({"class_id": cls["class_id"], "x": float(x), "y": float(y), "r": float(r)})
                    n += 1
            counts[cls["class_id"]] = n
        return counts, blobs

    @staticmethod
    def _shape(c) -> dict:
        import cv2
        area = float(cv2.contourArea(c))
        hull = float(cv2.contourArea(cv2.convexHull(c))) or 1.0
        (_, _), (rw, rh), _ = cv2.minAreaRect(c)
        return {"area": area, "elongation": max(rw, rh) / max(1.0, min(rw, rh)), "solidity": area / hull}

    def profile(self, cls: dict) -> dict:
        p = (cls.get("hsv") or {}).get("shape")
        if p:
            return p
        return {"kind": "round", "area": float(self.settings.tray_token_area_px), "elongation": 1.0, "solidity": 0.95}

    def classify_blob(self, blob: dict, prof: dict) -> str:
        """ok | touching | fragment | shape_mismatch, relative to the class's own profile."""
        ratio = blob["area"] / max(1.0, prof["area"])
        el_tol = max(0.35, 0.25 * prof["elongation"])
        if ratio < 0.45:
            return "fragment"  # broken pill or partly hidden
        if ratio > 1.6 or blob["solidity"] < prof["solidity"] - 0.10:
            return "touching"  # two pills merged, or stacked
        if abs(blob["elongation"] - prof["elongation"]) > el_tol:
            return "touching" if blob["elongation"] > prof["elongation"] and ratio > 1.3 else "shape_mismatch"
        return "ok"

    def _mask(self, hsv, ranges):
        import cv2
        import numpy as np
        mask = np.zeros(hsv.shape[:2], np.uint8)
        for lo, hi in ranges:
            mask |= cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
        k = np.ones((3, 3), np.uint8)
        return cv2.morphologyEx(cv2.morphologyEx(mask, cv2.MORPH_OPEN, k), cv2.MORPH_CLOSE, k)

    # -- counting ---------------------------------------------------------------
    def _inspect(self, roi, classes: list[dict], expected: dict | None = None) -> dict:
        """Count each class in the tray area. Also returns where every counted object is, so the
        photo proof can circle exactly what was counted."""
        import cv2
        if self.settings.tray_vision == "outline":
            return self._count_outlines(roi, classes, expected)
        hsv = cv2.cvtColor(cv2.GaussianBlur(roi, (5, 5), 0), cv2.COLOR_BGR2HSV)
        counts, problems, blobs = {}, [], []
        for cls in classes:
            prof = self.profile(cls)
            contours, _ = cv2.findContours(self._mask(hsv, cls["hsv"]["ranges"]), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            n = 0
            for c in contours:
                blob = self._shape(c)
                if blob["area"] < max(self.settings.tray_min_area_px, 0.2 * prof["area"]):
                    continue  # noise
                verdict = self.classify_blob(blob, prof)
                if verdict != "ok":
                    problems.append({"class_id": cls["class_id"], "verdict": verdict,
                                     "area": round(blob["area"]), "elongation": round(blob["elongation"], 2)})
                (x, y), r = cv2.minEnclosingCircle(c)
                blobs.append({"class_id": cls["class_id"], "x": round(float(x), 1), "y": round(float(y), 1),
                              "r": round(float(r), 1), "verdict": verdict})
                n += 1
            counts[cls["class_id"]] = n
        status = "ok"
        for v in ("touching", "fragment", "shape_mismatch"):
            if any(p["verdict"] == v for p in problems):
                status = v
                break
        return {"status": status, "counts": counts, "problems": problems[:6], "blobs": blobs[:24]}

    def analyze(self, img, classes: list[dict], expected: dict | None = None) -> dict:
        roi, info = self._warp(img)
        if roi is None:
            return info
        return {**self._inspect(roi, classes, expected), **info}

    def render_evidence(self, img, classes: list[dict], expected: dict | None = None):
        """The tray area of one frame with each counted pill circled and tagged (A1, B1...).
        Returns None when the tray area cannot be found, so nothing outside the tray is ever kept."""
        import cv2
        from .evidence import SCALE, draw_marks
        roi, _ = self._warp(img)
        if roi is None:
            return None
        res = self._inspect(roi, classes, expected)
        out = cv2.resize(roi, (roi.shape[1] * SCALE, roi.shape[0] * SCALE), interpolation=cv2.INTER_CUBIC)
        by_id = {c["class_id"]: c for c in classes}
        seen: dict[str, int] = {}
        marks = []
        for b in res["blobs"]:
            cls = by_id.get(b["class_id"], {})
            seen[b["class_id"]] = seen.get(b["class_id"], 0) + 1
            marks.append({"x": b["x"] * SCALE, "y": b["y"] * SCALE, "r": b["r"] * SCALE, "color": cls.get("color"),
                          "tag": f"{cls.get('cartridge') or '?'}{seen[b['class_id']]}", "ok": b["verdict"] == "ok"})
        draw_marks(out, marks)
        return out

    # -- calibration --------------------------------------------------------------
    def measure_single(self, img) -> dict:
        """With exactly one pill in the tray, learn its colour range and shape profile.
        Coloured pills are found by saturation; white or pale pills by brightness against
        a darker tray (use a dark matte tray for them)."""
        import cv2
        import numpy as np
        roi, info = self._warp(img)
        if roi is None:
            return info
        hsv = cv2.cvtColor(cv2.GaussianBlur(roi, (5, 5), 0), cv2.COLOR_BGR2HSV)
        H, Sat, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        tray_v = float(np.median(V))
        colored = (Sat > 70) & (V > 50)
        pale = (Sat < 60) & (V > tray_v + 60)
        mask = ((colored | pale).astype(np.uint8)) * 255
        k = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(cv2.morphologyEx(mask, cv2.MORPH_OPEN, k), cv2.MORPH_CLOSE, k)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        blobs = [c for c in contours if cv2.contourArea(c) >= self.settings.tray_min_area_px]
        if not blobs:
            return {"status": "no_object"}
        if len(blobs) > 1:
            return {"status": "multiple_objects", "objects": len(blobs)}
        c = blobs[0]
        one = np.zeros(mask.shape, np.uint8)
        cv2.drawContours(one, [c], -1, 255, -1)
        one = cv2.erode(one, k, iterations=2)  # sample the inside, not the edge
        h, sat, v = (float(np.median(ch[one > 0])) for ch in (H, Sat, V))
        if sat < 60:  # white / pale pill
            ranges = [[[0, 0, int(max(0, v - 50))], [179, 70, 255]]]
        else:
            lo_s, lo_v = int(max(50, sat - 70)), int(max(40, v - 90))
            hlo, hhi = int(h) - 10, int(h) + 10
            if hlo < 0:
                ranges = [[[0, lo_s, lo_v], [hhi, 255, 255]], [[180 + hlo, lo_s, lo_v], [179, 255, 255]]]
            elif hhi > 179:
                ranges = [[[hlo, lo_s, lo_v], [179, 255, 255]], [[0, lo_s, lo_v], [hhi - 180, 255, 255]]]
            else:
                ranges = [[[hlo, lo_s, lo_v], [hhi, 255, 255]]]
        shape = self._shape(c)
        kind = "round" if shape["elongation"] < 1.3 else ("capsule" if shape["solidity"] > 0.9 else "oblong")
        bgr = cv2.cvtColor(np.uint8([[[h, sat, v]]]), cv2.COLOR_HSV2BGR)[0][0]
        return {"status": "ok", "ranges": ranges,
                "shape": {"kind": kind, "area": round(shape["area"], 1), "elongation": round(shape["elongation"], 3),
                          "solidity": round(shape["solidity"], 3)},
                "color": "#{:02X}{:02X}{:02X}".format(int(bgr[2]), int(bgr[1]), int(bgr[0])),
                "sample_hsv": [round(h), round(sat), round(v)]}


# ------------------------------------------------------------------------------------
# Look-alike pill types
# ------------------------------------------------------------------------------------
def _in_ranges(hsv: list, ranges: list) -> bool:
    h, sat, v = (int(x) for x in hsv)
    return any(lo[0] <= h <= hi[0] and lo[1] <= sat <= hi[1] and lo[2] <= v <= hi[2] for lo, hi in ranges)


def typical_hsv(cls: dict) -> list | None:
    """The colour a pill of this type usually shows: the calibrated sample, or the middle of
    its starting range for a type that was never calibrated."""
    hsv = cls.get("hsv") or {}
    if hsv.get("sample_hsv"):
        return list(hsv["sample_hsv"])
    ranges = hsv.get("ranges") or []
    if not ranges:
        return None
    lo, hi = ranges[0]
    return [(lo[i] + hi[i]) // 2 for i in range(3)]


def _skin_like(hsv: list | None) -> bool:
    """Colours a camera confuses with skin on a palm: orange, beige, pink and pale/white pills."""
    if not hsv:
        return False
    h, s, v = hsv
    return (h <= 25 or h >= 170) and s <= 190 and v >= 60 or (s < 60 and v > 150)


def look_alike_pairs(classes: list[dict]) -> list[dict]:
    """Pill types the tray camera cannot tell apart. Counting is by colour, so if one type's
    usual colour falls inside another type's colour range, a single pill is counted as both
    and every dose check would fail (or worse, a swap could go unnoticed). Setup must fix it."""
    out = []
    for i, a in enumerate(classes):
        for b in classes[i + 1:]:
            ha, hb = typical_hsv(a), typical_hsv(b)
            if ha is None or hb is None:
                continue
            if _in_ranges(ha, b["hsv"].get("ranges") or []) or _in_ranges(hb, a["hsv"].get("ranges") or []):
                out.append({"a": a["class_id"], "b": b["class_id"], "a_label": a["label"], "b_label": b["label"],
                            "message": f"{a['label']} (cartridge {a.get('cartridge')}) and {b['label']} (cartridge {b.get('cartridge')}) "
                                       "look too alike for the tray camera: their colours overlap. Use pills of clearly "
                                       "different colours, then calibrate both again."})
    return out


# ------------------------------------------------------------------------------------
# Service
# ------------------------------------------------------------------------------------
class VisionService:
    def __init__(self, settings: Settings, store: Store, camera: CaptureManager, hardware: HardwareAdapter, clock: Clock):
        self.settings = settings
        self.store = store
        self.camera = camera
        self.hardware = hardware
        self.clock = clock
        self.face = FaceEngine(settings.models_dir)
        self.tray = TrayInspector(settings)
        self.sim = {"face": "recipient", "tray": "auto", "action": "taken", "hand": "shown"}
        self.palm = PalmDetector(settings.models_dir)
        self.watch: Watch | None = None
        self._watch_task: asyncio.Task | None = None
        self._face_operation = asyncio.Lock()
        self._hint_lock = asyncio.Lock()
        self._hint: tuple[str, float, dict] | None = None
        self._live: tuple | None = None

    @property
    def simulated(self) -> bool:
        return self.camera.simulated

    @property
    def tray_simulated(self) -> bool:
        # TRAY_BYPASS (testing, fake hardware only) takes tray results from the simulator while the face check stays real.
        return self.simulated or self.settings.tray_bypass

    def status(self) -> dict:
        return {"simulated": self.simulated, "tray_bypass": bool(self.settings.tray_bypass) and not self.simulated,
                "face_models": self.face.available(), "palm_model": self.palm.available(),
                "sim": dict(self.sim) if self.tray_simulated else None}

    def set_sim(self, face: str | None = None, tray: str | None = None, action: str | None = None,
                hand: str | None = None) -> None:
        if hand in SIM_HAND:
            self.sim["hand"] = hand
        if face in SIM_FACE:
            self.sim["face"] = face
        if tray in SIM_TRAY:
            self.sim["tray"] = tray
        if action in SIM_ACTION:
            self.sim["action"] = action

    # -- collection watch: who empties the tray, and a hand-to-mouth movement ----------------
    def start_watch(self, txn_id: str, person: dict | None) -> Watch:
        """Start sampling the face camera for one released dose. Replaces any earlier watch."""
        self.stop_watch()
        s = self.settings
        tracker = ActionTracker(s.action_near_ratio, s.action_far_ratio, s.action_min_near, s.action_near_window_s)
        refs = None
        if not self.simulated and person and not person.get("simulated") and self.face.available():
            refs = self._load_refs(person)
        available = self.simulated or refs is not None
        w = Watch(txn_id, person["person_id"] if person else None, self.clock.monotonic(), tracker, available,
                  self.simulated or self.palm.available(), self.simulated, run_needed=s.collector_run_samples)
        self.watch = w
        if available:
            self._watch_task = asyncio.create_task(self._watch_loop(w, refs), name=f"watch-{txn_id}")
        return w

    def stop_watch(self) -> None:
        if self.watch:
            self.watch.stopped = True
        if self._watch_task:
            self._watch_task.cancel()
        self._watch_task = None

    def watch_view(self, txn_id: str | None = None) -> dict | None:
        w = self.watch
        if not w or (txn_id and w.txn_id != txn_id):
            return None
        return w.view(self.settings.collector_min_samples)

    async def _watch_loop(self, w: Watch, refs) -> None:
        i, last_key = 0, None
        while not w.stopped:
            try:
                if w.simulated:
                    sample = self._sim_sample(i)
                else:
                    sample, last_key = await self._real_sample(refs, last_key, w.palms_available)
                if sample is not None and not w.stopped:
                    w.add(sample, iso(self.clock.now()))
                    i += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("collection watch sample failed")
            await asyncio.sleep(self.settings.watch_interval_s)

    def _sim_sample(self, i: int) -> Sample:
        face = {"recipient": "match", "unknown": "unknown", "two_faces": "multi", "none": "none"}[self.sim["face"]]
        recipient = face == "match"
        palms = []
        if self.sim["action"] == "taken" and recipient:
            if i < 3:
                palms = [{"score": 0.9, "box": [40.0, 380.0, 60.0, 60.0]}]  # hand down by the tray
            elif i < 6:
                palms = [{"score": 0.9, "box": [300.0, 250.0, 60.0, 60.0]}]  # hand at the mouth
        return Sample(t=self.clock.monotonic(), face=face, recipient=recipient,
                      mouth=(320.0, 240.0) if recipient else None, face_w=100.0 if recipient else None, palms=palms)

    async def _real_sample(self, refs, last_key, palms_on: bool):
        f = await self.camera.frame("face")
        if f is None or self.clock.monotonic() - f.capture_mono > 0.5:
            return None, last_key
        key = (f.source_id, f.camera_session, f.seq)
        if key == last_key:
            return None, last_key
        sample = await asyncio.to_thread(self._watch_analyze, f.image, refs, palms_on)
        sample.t = f.capture_mono
        return sample, key

    def _watch_analyze(self, img, refs, palms_on: bool) -> Sample:
        import numpy as np
        fe, thr = self.face, self.settings.face_match_threshold
        best = None
        with fe._lock:
            small, faces = fe._detect(img)
            rows = [] if faces is None else list(faces)
            for row in rows[:3]:
                feat = fe._rec.feature(fe._rec.alignCrop(small, row)).flatten().astype("float32")
                feat /= (np.linalg.norm(feat) + 1e-9)
                sim = fe.best_similarity(feat, refs)
                if sim >= thr and (best is None or sim > best[1]):
                    best = (row, sim)
        n = len(rows)
        face = "none" if n == 0 else "multi" if n > 1 else ("match" if best else "unknown")
        mouth = fw = None
        if best is not None:
            r = best[0]
            mouth = (float(r[10] + r[12]) / 2, float(r[11] + r[13]) / 2)
            fw = float(r[2])
        palms = self.palm.detect(small) if palms_on else []
        return Sample(t=0.0, face=face, recipient=best is not None, mouth=mouth, face_w=fw, palms=palms,
                      palms_checked=palms_on)

    async def face_preview(self) -> dict:
        if self.simulated or not self.face.available():
            return {"available": False}
        # Let enrollment and guarded identity checks own inference while active.
        if self._face_operation.locked():
            return {"available": True, "faces": None, "busy": True}
        async with self._hint_lock:
            f = await self.camera.frame("face")
            now = self.clock.monotonic()
            if f is None or now - f.capture_mono > 0.5:
                return {"available": True, "faces": None}
            if self._hint and (self._hint[0] == f.frame_id or now - self._hint[1] < 0.15):
                return self._hint[2]
            if self._face_operation.locked():
                return {"available": True, "faces": None, "busy": True}
            r = await asyncio.to_thread(self.face.detect, f.image)
            result = {"available": True, "faces": r["faces"], "score": round(r.get("score", 0.0), 2)}
            self._hint = (f.frame_id, now, result)
            return result

    # -- frame collection --------------------------------------------------------
    async def _collect(self, role: str, analyze) -> tuple[list[tuple[Frame, dict]], str | None]:
        s = self.settings
        start = self.clock.monotonic()
        out: list[tuple[Frame, dict]] = []
        last_key = None
        interval = s.check_min_span_s / max(1, s.check_frames - 1)
        while True:
            if self.clock.monotonic() - start > s.check_timeout_s:
                return out, "CAMERA_STALE"
            f = await self.camera.frame(role)
            if f is None:
                await asyncio.sleep(0.05)
                continue
            key = (f.source_id, f.camera_session, f.seq)
            if key == last_key or self.clock.monotonic() - f.capture_mono > 0.5:
                await asyncio.sleep(0.03)
                continue
            if out and (f.source_id, f.camera_session) != (out[0][0].source_id, out[0][0].camera_session):
                return out, "CAMERA_STALE"
            if out and f.capture_mono - out[-1][0].capture_mono < interval:
                await asyncio.sleep(0.01)
                continue
            last_key = key
            res = await asyncio.to_thread(analyze, f.image)
            out.append((f, res))
            if len(out) >= s.check_frames and out[-1][0].capture_mono - out[0][0].capture_mono >= s.check_min_span_s:
                return out, None

    def _frame_meta(self, obs: Observation, frames: list[tuple[Frame, dict]]) -> None:
        if not frames:
            return
        obs.frame_ids = [f.frame_id for f, _ in frames]
        oldest = frames[0][0]
        obs.capture_time = oldest.capture_wall
        obs.capture_mono = oldest.capture_mono
        obs.details["frames"] = len(frames)
        obs.details["span_s"] = round(frames[-1][0].capture_mono - oldest.capture_mono, 2)

    async def _sim_wait(self) -> tuple[float, datetime]:
        start_mono, start_wall = self.clock.monotonic(), self.clock.now()
        if self.settings.check_min_span_s:
            await asyncio.sleep(self.settings.check_min_span_s)
        return start_mono, start_wall

    # -- face -------------------------------------------------------------------------
    async def check_face(self, person: dict | None) -> Observation:
        async with self._face_operation:
            return await self._check_face(person)

    async def _check_face(self, person: dict | None) -> Observation:
        if not person:
            return Observation("FACE_MATCH", "NOT_OBSERVED", "none", "NOT_ENROLLED", message("NOT_ENROLLED"))
        if self.simulated:
            mono, wall = await self._sim_wait()
            mode = self.sim["face"]
            result, code = {"recipient": ("PASS", None), "unknown": ("FAIL", "UNKNOWN_PERSON"),
                            "two_faces": ("FAIL", "MULTIPLE_FACES"), "none": ("NOT_OBSERVED", "NO_FACE")}[mode]
            return Observation("FACE_MATCH", result, "simulated", code, message(code) if code else None,
                               details={"scenario": mode, "person_id": person["person_id"]},
                               frame_ids=[f"sim:{i}" for i in range(self.settings.check_frames)],
                               capture_time=wall, capture_mono=mono, simulated=True)
        if person.get("simulated"):
            return Observation("FACE_MATCH", "NOT_OBSERVED", "none", "NOT_ENROLLED",
                               "The recipient was enrolled in simulated mode. Enroll again with the camera.")
        if not self.face.available():
            return Observation("FACE_MATCH", "NOT_OBSERVED", "yunet+sface", "CAMERA_UNAVAILABLE",
                               "Face models are missing. Run scripts/download_models.py.")
        refs = self._load_refs(person)
        if refs is None:
            return Observation("FACE_MATCH", "NOT_OBSERVED", "yunet+sface", "NOT_ENROLLED", message("NOT_ENROLLED"))
        frames, err = await self._collect("face", self.face.analyze)
        obs = Observation("FACE_MATCH", "UNCERTAIN", "yunet+sface")
        self._frame_meta(obs, frames)
        if err or not frames:
            obs.result, obs.reason_code = "NOT_OBSERVED", err or "CAMERA_STALE"
            obs.message = message(obs.reason_code)
            return obs
        thr = self.settings.face_match_threshold
        sims, per = [], []
        for _, r in frames:
            if r["faces"] == 0:
                per.append("none")
            elif r["faces"] > 1:
                per.append("multi")
            else:
                sim = self.face.best_similarity(r["feature"], refs)
                sims.append(round(sim, 3))
                per.append("match" if sim >= thr else "unknown")
        obs.details.update({"similarities": sims, "threshold": thr, "per_frame": per})
        if "multi" in per:
            obs.result, obs.reason_code = "FAIL", "MULTIPLE_FACES"
        elif all(p == "match" for p in per):
            obs.result = "PASS"
        elif per.count("unknown") > len(per) / 2:
            obs.result, obs.reason_code = "FAIL", "UNKNOWN_PERSON"
        elif per.count("none") > len(per) / 2:
            obs.result, obs.reason_code = "NOT_OBSERVED", "NO_FACE"
        else:
            obs.result, obs.reason_code = "UNCERTAIN", "FACE_UNSTABLE"
        obs.message = message(obs.reason_code) if obs.reason_code else None
        return obs

    def _load_refs(self, person: dict):
        import numpy as np
        ref = person.get("embedding_ref")
        if not ref:
            return None
        path = self.settings.data_dir / ref
        if not path.exists():
            return None
        return np.load(path)

    async def enroll(self, person_id: str, samples: int = 5) -> tuple[str | None, str | None]:
        """Capture reference embeddings from several poses. Returns (relative path, error)."""
        async with self._face_operation:
            return await self._enroll(person_id, samples)

    async def _enroll(self, person_id: str, samples: int) -> tuple[str | None, str | None]:
        import numpy as np
        if self.simulated:
            return None, None
        if not self.face.available():
            return None, "Face models are missing. Run scripts/download_models.py."
        feats = []
        seen = {"no_face": 0, "multiple": 0}
        start = self.clock.monotonic()
        last_key = None
        while len(feats) < samples and self.clock.monotonic() - start < 15:
            f = await self.camera.frame("face")
            if f is None or self.clock.monotonic() - f.capture_mono > 0.5:
                await asyncio.sleep(0.05)
                continue
            key = (f.source_id, f.camera_session, f.seq)
            if key == last_key:
                await asyncio.sleep(0.05)
                continue
            last_key = key
            r = await asyncio.to_thread(self.face.analyze, f.image)
            if r["faces"] == 1:
                feats.append(r["feature"])
                await asyncio.sleep(0.4)  # spread samples so the person can vary pose slightly
            elif r["faces"] == 0:
                seen["no_face"] += 1
            else:
                seen["multiple"] += 1
        if len(feats) < samples:
            why = ("another face was in view" if seen["multiple"] > seen["no_face"]
                   else "no face was found in most frames")
            return None, (f"Captured {len(feats)} of {samples} samples; {why}. Face the camera alone, "
                          "about an arm's length away, with light in front of you, and try again.")
        rel = f"embeddings/{person_id}.npy"
        path = self.settings.data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(path, np.stack(feats))
        return rel, None

    # -- tray -------------------------------------------------------------------------
    async def check_tray(self, expected: dict[str, int], kind: str) -> Observation:
        classes = self.store.token_classes()
        labels = {c["class_id"]: c["label"].lower() for c in classes}
        expected = {c["class_id"]: int(expected.get(c["class_id"], 0)) for c in classes}
        rev_before = self.hardware.fields.get("rev")
        session = self.hardware.session
        if self.tray_simulated:
            mono, wall = await self._sim_wait()
            obs = Observation(kind, "UNCERTAIN", "simulated" if self.simulated else "tray-bypass (testing)", simulated=True,
                              frame_ids=[f"sim:{i}" for i in range(self.settings.check_frames)],
                              capture_time=wall, capture_mono=mono)
            scenario = self.sim["tray"]
            tray = self.hardware.sim_tray()
            if tray is None:
                observed = dict(expected)  # no simulator: assume the plan (clearly labeled)
                obs.details["assumed_from_plan"] = True
            else:
                by_cart = {c["cartridge"]: c["class_id"] for c in classes}
                observed = {c["class_id"]: 0 for c in classes}
                for cart, n in tray.items():
                    if cart in by_cart:
                        observed[by_cart[cart]] += n
            obs.details.update({"scenario": scenario, "expected": expected})
            if scenario == "touching" and sum(observed.values()) > 0:
                obs.reason_code = "OBJECTS_TOUCHING"
            elif scenario == "markers_missing":
                obs.reason_code = "TRAY_MARKERS_MISSING"
            elif scenario == "blurry":
                obs.reason_code = "IMAGE_BLURRY"
            else:
                obs.details["observed"] = observed
                self._judge(obs, expected, observed, labels)
            if obs.reason_code and obs.result == "UNCERTAIN":
                obs.message = message(obs.reason_code)
            if scenario != "markers_missing":
                shown = obs.details.get("observed") or (observed if scenario in ("touching", "blurry") else None)
                img = await asyncio.to_thread(simulated_tray, classes, shown, scenario)
                self._keep_photo(obs, img)
        else:
            frames, err = await self._collect("tray", lambda img: self.tray.analyze(img, classes, expected))
            obs = Observation(kind, "UNCERTAIN", "tray-colour+outline" if self.settings.tray_vision == "outline" else "aruco+hsv-contours")
            self._frame_meta(obs, frames)
            obs.details["expected"] = expected
            if err or not frames:
                obs.result, obs.reason_code = "NOT_OBSERVED", err or "CAMERA_STALE"
            else:
                statuses = [r["status"] for _, r in frames]
                bad = [p for _, r in frames for p in r.get("problems", [])]
                if bad:
                    obs.details["problems"] = bad[:4]
                if any(st == "markers_missing" for st in statuses):
                    obs.reason_code = "TRAY_MARKERS_MISSING"
                elif any(st == "blurry" for st in statuses):
                    obs.reason_code = "IMAGE_BLURRY"
                elif any(st == "touching" for st in statuses):
                    obs.reason_code = "OBJECTS_TOUCHING"
                elif any(st == "fragment" for st in statuses):
                    obs.reason_code = "OBJECT_FRAGMENT"
                elif any(st == "shape_mismatch" for st in statuses):
                    obs.reason_code = "SHAPE_MISMATCH"
                else:
                    counts = [r["counts"] for _, r in frames]
                    if any(c != counts[0] for c in counts):
                        obs.reason_code = "COUNT_UNSTABLE"
                        obs.details["per_frame_counts"] = counts
                    else:
                        obs.details["observed"] = counts[0]
                        self._judge(obs, expected, counts[0], labels)
            if obs.result != "PASS" and obs.reason_code and not obs.message:
                obs.message = message(obs.reason_code)
            if frames:
                # The newest frame of the check, cropped to the tray, every counted pill circled.
                img = await asyncio.to_thread(self.tray.render_evidence, frames[-1][0].image, classes, expected)
                if img is not None:
                    self._keep_photo(obs, img)
        rev_after = self.hardware.fields.get("rev")
        if rev_before != rev_after or session != self.hardware.session:
            obs.result, obs.reason_code = "UNCERTAIN", "TRAY_REVISION_CHANGED"
            obs.message = message("TRAY_REVISION_CHANGED")
        obs.tray_revision = rev_after
        obs.controller_session = self.hardware.session
        return obs

    # -- live view: what the camera sees right now, drawn over the preview ------------------------
    async def live(self, kind: str, expected: dict[str, int] | None, busy: bool = False) -> dict:
        """kind: face | hand | tray. Returns shapes in 0..1 frame coordinates (x across, y down) plus a
        `ready` flag (exactly what this step needs is in view). Display only: the guarded check that
        follows takes its own frames and decides."""
        out = {"kind": kind, "ready": False}
        if busy:
            return {**out, "busy": True}
        if self.simulated:
            return {**out, "simulated": True, "ready": self._sim_ready(kind, expected or {})}
        role = "tray" if kind == "tray" else "face"
        async with self._hint_lock:
            f = await self.camera.live_frame(role)
            now = self.clock.monotonic()
            if f is None or now - f.capture_mono > 1.0:
                return {**out, "available": False}
            key = (kind, f.frame_id, dumps(expected or {}))
            if self._live and self._live[0] == key:
                return self._live[1]
            classes = self.store.token_classes()
            res = await asyncio.to_thread(self._live_analyze, kind, f.image, classes, expected or {})
            res = {**out, **res, "available": True}
            self._live = (key, res)
            return res

    def _sim_ready(self, kind: str, expected: dict) -> bool:
        if kind == "face":
            return self.sim["face"] == "recipient"
        if kind == "hand":
            return self.sim["hand"] == "shown"
        tray = self.hardware.sim_tray()
        if tray is None or self.sim["tray"] != "auto":
            return self.sim["tray"] == "auto"
        by_cart = {c["cartridge"]: c["class_id"] for c in self.store.token_classes()}
        seen: dict[str, int] = {}
        for cart, n in tray.items():
            if cart in by_cart:
                seen[by_cart[cart]] = seen.get(by_cart[cart], 0) + n
        return all(seen.get(k, 0) == int(v) for k, v in expected.items()) and \
            all(n == 0 or int(expected.get(k, 0)) > 0 for k, n in seen.items())

    def _live_analyze(self, kind: str, img, classes: list[dict], expected: dict) -> dict:
        import cv2
        import numpy as np
        H, W = img.shape[:2]
        frame = {"w": W, "h": H}
        colors = {c["class_id"]: c["color"] for c in classes}

        def back(points, M):  # warped tray-area pixels -> 0..1 frame coordinates
            pts = np.array([[[p[0] + BORDER, p[1] + BORDER]] for p in points], dtype="float32")
            inv = cv2.perspectiveTransform(pts, np.linalg.inv(M)).reshape(-1, 2) if len(pts) else []
            return [[float(x) / W, float(y) / H] for x, y in inv]

        def warped_pills(blobs, M):
            centres = back([(b["x"], b["y"]) for b in blobs], M)
            if not blobs:
                return []
            (ax, ay), (bx, by) = back([(0, 0), (10, 0)], M)
            px_per_unit = float(np.hypot((bx - ax) * W, (by - ay) * H)) / 10.0  # frame pixels per warped pixel
            return [{"x": c[0], "y": c[1], "r": max(0.012, b["r"] * px_per_unit / W), "class_id": b["class_id"],
                     "color": colors.get(b["class_id"])} for c, b in zip(centres, blobs)]

        if kind == "face":
            with self.face._lock:
                small, faces = self.face._detect(img)
            sh, sw = small.shape[:2]
            boxes = [{"x": float(r[0]) / sw, "y": float(r[1]) / sh, "w": float(r[2]) / sw, "h": float(r[3]) / sh}
                     for r in ([] if faces is None else faces)]
            return {"frame": frame, "boxes": boxes, "faces": len(boxes), "ready": len(boxes) == 1}

        if kind == "tray":
            roi, info = self.tray._warp(img, TRAY_IDS, geometry=True)
            if roi is None:
                return {"frame": frame, "hint": info.get("status")}
            src, M = info["_geom"]
            want = {c["class_id"]: int(expected.get(c["class_id"], 0)) for c in classes}
            res = self.tray._inspect(roi, classes, want)
            pills = warped_pills(res["blobs"], M)
            ready = res["status"] == "ok" and all(res["counts"].get(k, 0) == v for k, v in want.items())
            return {"frame": frame, "quad": [[float(x) / W, float(y) / H] for x, y in src], "pills": pills,
                    "counts": res["counts"], "expected": want, "ready": ready, "hint": None if res["status"] == "ok" else res["status"]}

        # hand: only this dose's pill types (other colours also match skin)
        dose = [c for c in classes if int(expected.get(c["class_id"], 0)) > 0]
        want = {c["class_id"]: int(expected[c["class_id"]]) for c in dose}
        r = self._hand_analyze(img, dose, geometry=True)
        if r.get("status") != "ok":
            return {"frame": frame, "hint": r.get("status"), "source": r.get("source")}
        ready = all(r["counts"].get(k, 0) == v for k, v in want.items())
        base = {"frame": frame, "source": r["source"], "counts": r["counts"], "expected": want, "ready": ready}
        if r["source"] == "dish":
            _, blobs, (src, M) = r["_dish"]
            return {**base, "quad": [[float(x) / W, float(y) / H] for x, y in src], "pills": warped_pills(blobs, M)}
        (sh, sw, _), (x0, y0, x1, y1), blobs = r["_palm"]
        return {**base, "box": {"x": x0 / sw, "y": y0 / sh, "w": (x1 - x0) / sw, "h": (y1 - y0) / sh},
                "pills": [{"x": (x0 + b["x"]) / sw, "y": (y0 + b["y"]) / sh, "r": max(0.012, b["r"] / sw),
                           "class_id": b["class_id"], "color": colors.get(b["class_id"])} for b in blobs]}

    # -- pills in the hand ---------------------------------------------------------------
    async def check_hand(self, expected: dict[str, int]) -> Observation:
        """The person holds the pills on an open palm toward the face camera. A palm is found with the
        palm detector, and the pills on and around it are counted by each pill type's colour. Colour on
        skin is harder than on the tray (white or skin-coloured pills may not count), so a person who
        can't pass it can say so; it never blocks a dose on its own."""
        all_classes = self.store.token_classes()
        labels = {c["class_id"]: c["label"].lower() for c in all_classes}
        # Only this dose's pill types are counted: the tray check already showed nothing else came out, and
        # other colour ranges (orange especially) also match skin.
        classes = [c for c in all_classes if int(expected.get(c["class_id"], 0)) > 0]
        expected = {c["class_id"]: int(expected[c["class_id"]]) for c in classes}
        hard = [c["label"] for c in classes if _skin_like(typical_hsv(c))]
        w = self.watch
        if w:
            w.paused = True  # a hand held up to the camera is not a hand going to the mouth
        try:
            if self.simulated:
                mono, wall = await self._sim_wait()
                obs = Observation("HAND_CHECK", "UNCERTAIN", "simulated", simulated=True, capture_time=wall, capture_mono=mono,
                                  frame_ids=[f"sim:{i}" for i in range(self.settings.check_frames)])
                scenario = self.sim["hand"]
                obs.details.update({"scenario": scenario, "expected": expected})
                if scenario == "none":
                    obs.result, obs.reason_code = "NOT_OBSERVED", "NO_HAND"
                else:
                    seen = dict(expected) if scenario == "shown" else {k: max(0, v - 1) for k, v in expected.items()}
                    self._judge_hand(obs, expected, seen, labels)
            else:
                frames, err = await self._collect("face", lambda img: self._hand_analyze(img, classes))
                obs = Observation("HAND_CHECK", "UNCERTAIN", "dish-or-palm+hsv")
                self._frame_meta(obs, frames)
                obs.details["expected"] = expected
                with_hand = [(f, r) for f, r in frames if r.get("status") == "ok"]
                blurry = [r for _, r in frames if r.get("status") == "blurry"]
                if blurry:
                    obs.details["blurry_frames"] = len(blurry)
                if err and not frames:
                    obs.result, obs.reason_code = "NOT_OBSERVED", err
                elif len(blurry) * 2 >= max(1, len(frames)):
                    obs.result, obs.reason_code = "NOT_OBSERVED", "HAND_BLURRY"
                elif len(with_hand) < max(1, len(frames) // 2):
                    obs.result = "NOT_OBSERVED"
                    obs.reason_code = "NO_HAND" if self.palm.available() or any(r.get("source") == "dish" for _, r in frames) \
                        else "HAND_MODEL_MISSING"
                else:
                    tally: dict[str, int] = {}
                    for _, r in with_hand:
                        key = dumps(r["counts"])
                        tally[key] = tally.get(key, 0) + 1
                    best, n = max(tally.items(), key=lambda kv: kv[1])
                    obs.details["frames_with_hand"] = len(with_hand)
                    sources = [r.get("source") for _, r in with_hand]
                    obs.details["source"] = max(set(sources), key=sources.count)
                    if obs.details["source"] == "dish":
                        obs.method = "dish-markers+hsv"
                        hard = []  # the dark dish shows any pill colour
                    if n * 2 < len(with_hand):
                        obs.result, obs.reason_code = "UNCERTAIN", "HAND_UNSTABLE"
                    else:
                        self._judge_hand(obs, expected, json.loads(best), labels)
                        frame = next(f for f, r in with_hand if dumps(r["counts"]) == best)
                        img = await asyncio.to_thread(self._hand_photo, frame.image, classes)
                        if img is not None:
                            self._keep_photo(obs, img)
            if obs.reason_code and not obs.message:
                obs.message = message(obs.reason_code)
            if hard:
                obs.details["hard_on_skin"] = hard
                if obs.result != "PASS":
                    obs.message = (obs.message or "") + (f" {', '.join(hard)} pills are hard for the camera to see on skin."
                                                         " If it keeps failing, choose I can't show them.")
            return obs
        finally:
            if w:
                w.paused = False
                w.tracker.seen_away = False  # the hand must go back down before a movement to the mouth counts
                w.tracker.near.clear()

    @staticmethod
    def _judge_hand(obs: Observation, expected: dict, seen: dict, labels: dict) -> None:
        obs.details["observed"] = seen
        if all(int(seen.get(k, 0)) == v for k, v in expected.items()):
            obs.result, obs.reason_code = "PASS", None
            return
        obs.result, obs.reason_code = "FAIL", "HAND_COUNT_MISMATCH"
        parts = [f"Expected {v} {labels.get(k, k)} and saw {int(seen.get(k, 0))}." for k, v in expected.items()
                 if int(seen.get(k, 0)) != v]
        obs.message = " ".join(parts) + " Hold the pills flat on your open palm, closer to the camera, and check again."

    def _hand_region(self, img):
        """Downscale like the face detector, find the largest palm, and return (small image, region box)."""
        import cv2
        h, w = img.shape[:2]
        small = cv2.resize(img, (640, int(h * 640 / w))) if w > 640 else img
        palms = self.palm.detect(small)
        if not palms:
            return small, None
        x, y, bw, bh = max(palms, key=lambda p: p["box"][2] * p["box"][3])["box"]
        side = 1.9 * max(bw, bh)  # the palm box leaves out the fingers; pills sit on both
        cx, cy = x + bw / 2, y + bh / 2
        H, W = small.shape[:2]
        x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
        x1, y1 = int(min(W, cx + side / 2)), int(min(H, cy + side / 2))
        return small, (x0, y0, x1, y1) if x1 - x0 > 20 and y1 - y0 > 20 else None

    @staticmethod
    def _sharpness(roi) -> float:
        import cv2
        if roi.size == 0:
            return 0.0
        g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (320, max(1, int(g.shape[0] * 320 / max(1, g.shape[1])))))
        return float(cv2.Laplacian(g, cv2.CV_64F).var())

    def _hand_blobs(self, roi, classes):
        import cv2
        hsv = cv2.cvtColor(cv2.GaussianBlur(roi, (5, 5), 0), cv2.COLOR_BGR2HSV)
        area = roi.shape[0] * roi.shape[1]
        counts, blobs = {}, []
        for cls in classes:
            contours, _ = cv2.findContours(self.tray._mask(hsv, cls["hsv"]["ranges"]), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            n = 0
            for c in contours:
                a = cv2.contourArea(c)
                if 0.0015 * area <= a <= 0.06 * area:  # pill-sized next to a palm; skin patches are larger
                    (x, y), r = cv2.minEnclosingCircle(c)
                    blobs.append({"class_id": cls["class_id"], "x": x, "y": y, "r": r})
                    n += 1
            counts[cls["class_id"]] = n
        return counts, blobs

    def _hand_analyze(self, img, classes, geometry: bool = False) -> dict:
        """The pill dish first (its own markers, any pill colour), then an open palm (colour on skin)."""
        roi, info = self.tray._warp(img, DISH_IDS, geometry=geometry)
        if roi is not None:
            counts, blobs = self.tray.count_loose(roi, classes)
            out = {"status": "ok", "source": "dish", "counts": counts}
            if geometry:
                out["_dish"] = (roi.shape, blobs, info["_geom"])
            return out
        if info.get("status") == "blurry":
            return {"status": "blurry", "source": "dish"}
        if not self.palm.available():
            return {"status": "no_hand"}
        small, box = self._hand_region(img)
        if box is None:
            return {"status": "no_hand"}
        x0, y0, x1, y1 = box
        roi = small[y0:y1, x0:x1]
        sharp = self._sharpness(roi)
        if sharp < self.settings.hand_blur_threshold:
            # A moving hand smears small pills into the skin: don't count it, ask to hold still.
            return {"status": "blurry", "source": "palm", "sharpness": round(sharp, 1)}
        counts, blobs = self._hand_blobs(roi, classes)
        out = {"status": "ok", "source": "palm", "counts": counts, "sharpness": round(sharp, 1)}
        if geometry:
            out["_palm"] = (small.shape, box, blobs)
        return out

    def _hand_photo(self, img, classes):
        """The dish (or the hand region) with each counted pill ringed. A hand photo is not kept if a face
        overlaps it; the dish photo is only the area inside its markers."""
        import cv2
        from .evidence import SCALE, draw_marks
        roi, _ = self.tray._warp(img, DISH_IDS)
        if roi is not None:
            _, blobs = self.tray.count_loose(roi, classes)
            out = cv2.resize(roi, (roi.shape[1] * SCALE, roi.shape[0] * SCALE), interpolation=cv2.INTER_CUBIC)
            by_id, seen, marks = {c["class_id"]: c for c in classes}, {}, []
            for b in blobs:
                cls = by_id.get(b["class_id"], {})
                seen[b["class_id"]] = seen.get(b["class_id"], 0) + 1
                marks.append({"x": b["x"] * SCALE, "y": b["y"] * SCALE, "r": b["r"] * SCALE, "color": cls.get("color"),
                              "tag": f"{cls.get('cartridge') or '?'}{seen[b['class_id']]}"})
            draw_marks(out, marks)
            return out
        if not self.palm.available():
            return None
        small, box = self._hand_region(img)
        if box is None:
            return None
        x0, y0, x1, y1 = box
        with self.face._lock:
            _, faces = self.face._detect(small)
        for f in ([] if faces is None else faces):
            fx, fy, fw, fh = f[:4]
            if fx < x1 and fx + fw > x0 and fy < y1 and fy + fh > y0:
                return None  # never keep a face
        roi = small[y0:y1, x0:x1].copy()
        _, blobs = self._hand_blobs(roi, classes)
        scale = 320 / max(1, roi.shape[1])
        out = cv2.resize(roi, (320, max(1, int(roi.shape[0] * scale))))
        by_id = {c["class_id"]: c for c in classes}
        seen: dict[str, int] = {}
        marks = []
        for b in blobs:
            cls = by_id.get(b["class_id"], {})
            seen[b["class_id"]] = seen.get(b["class_id"], 0) + 1
            marks.append({"x": b["x"] * scale, "y": b["y"] * scale, "r": b["r"] * scale, "color": cls.get("color"),
                          "tag": f"{cls.get('cartridge') or '?'}{seen[b['class_id']]}"})
        draw_marks(out, marks)
        return out

    def _keep_photo(self, obs: Observation, img) -> None:
        rel = evidence.save(self.settings.data_dir, img, self.settings.evidence_keep)
        if rel:
            obs.details["photo"] = rel

    async def calibrate_class(self) -> tuple[dict | None, str | None]:
        """Measure one pill over several frames; returns (profile, error)."""
        import statistics
        if self.simulated:
            return None, "Calibration needs the real camera (CAMERA_MODE is simulated)."
        frames, err = await self._collect("tray", self.tray.measure_single)
        if err or not frames:
            return None, message(err or "CAMERA_STALE")
        results = [r for _, r in frames]
        bad = [r["status"] for r in results if r["status"] != "ok"]
        if bad:
            worst = max(set(bad), key=bad.count)
            return None, {
                "markers_missing": message("TRAY_MARKERS_MISSING"),
                "blurry": message("IMAGE_BLURRY"),
                "no_object": "No pill found. Put exactly one pill of this kind in the tray, in view.",
                "multiple_objects": "More than one object found. Leave exactly one pill in the tray.",
            }.get(worst, "Calibration was not stable. Keep the tray still and try again.")
        mid = results[len(results) // 2]
        shape = {k: round(statistics.median(r["shape"][k] for r in results), 3) for k in ("area", "elongation", "solidity")}
        shape["kind"] = mid["shape"]["kind"]
        return {"ranges": mid["ranges"], "shape": shape, "color": mid["color"], "sample_hsv": mid["sample_hsv"]}, None

    @staticmethod
    def _judge(obs: Observation, expected: dict, observed: dict, labels: dict) -> None:
        if all(int(observed.get(k, 0)) == v for k, v in expected.items()) and \
                all(k in expected or v == 0 for k, v in observed.items()):
            obs.result, obs.reason_code = "PASS", None
            return
        obs.result = "FAIL"
        if obs.kind == "TRAY_EMPTY":
            obs.reason_code = "TRAY_NOT_EMPTY"
            obs.message = message("TRAY_NOT_EMPTY")
        else:
            obs.reason_code = "COUNT_MISMATCH"
            obs.message = count_mismatch_sentence(expected, observed, labels)
