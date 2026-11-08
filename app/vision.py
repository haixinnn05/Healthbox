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

from .camera import CaptureManager, Frame
from .config import Settings
from .hardware import HardwareAdapter
from .reasons import count_mismatch_sentence, message
from .store import Store
from .timeutil import Clock

log = logging.getLogger("doseguard.vision")

YUNET_FILE = "face_detection_yunet_2023mar.onnx"
SFACE_FILE = "face_recognition_sface_2021dec.onnx"
SIM_FACE = ("recipient", "unknown", "two_faces", "none")
SIM_TRAY = ("auto", "touching", "markers_missing", "blurry")


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


class TrayInspector:
    """Four ArUco markers (DICT_4X4_50, ids 0..3 = top-left, top-right, bottom-right,
    bottom-left) define the tray. The view is warped to a fixed rectangle, then each
    token class is segmented by colour and every blob is compared with that class's
    calibrated shape profile (area, elongation, solidity). Round tablets, capsules and
    oblong tablets all work, as long as each class is calibrated with one real sample."""

    def __init__(self, settings: Settings):
        self.settings = settings

    # -- geometry ----------------------------------------------------------------
    def _warp(self, img):
        import cv2
        import numpy as np
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50),
                                           cv2.aruco.DetectorParameters())
        corners, ids, _ = detector.detectMarkers(gray)
        found = {}
        if ids is not None:
            for c, i in zip(corners, ids.flatten()):
                if int(i) in (0, 1, 2, 3):
                    found[int(i)] = c.reshape(4, 2).mean(axis=0)
        if len(found) < 4:
            return None, {"status": "markers_missing", "markers": sorted(found)}
        src = np.array([found[0], found[1], found[2], found[3]], dtype="float32")
        dst = np.array([[0, 0], [WARP_W, 0], [WARP_W, WARP_H], [0, WARP_H]], dtype="float32")
        warped = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (WARP_W, WARP_H))
        # Focus is measured over the whole warped view: marker edges give texture even when
        # the matte tray floor has none.
        sharp = float(cv2.Laplacian(cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
        if sharp < self.settings.blur_threshold:
            return None, {"status": "blurry", "sharpness": round(sharp, 1)}
        return warped[BORDER:WARP_H - BORDER, BORDER:WARP_W - BORDER], {"sharpness": round(sharp, 1)}

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
    def analyze(self, img, classes: list[dict]) -> dict:
        import cv2
        roi, info = self._warp(img)
        if roi is None:
            return info
        hsv = cv2.cvtColor(cv2.GaussianBlur(roi, (5, 5), 0), cv2.COLOR_BGR2HSV)
        counts, problems = {}, []
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
                n += 1
            counts[cls["class_id"]] = n
        status = "ok"
        for v in ("touching", "fragment", "shape_mismatch"):
            if any(p["verdict"] == v for p in problems):
                status = v
                break
        return {"status": status, "counts": counts, "problems": problems[:6], **info}

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
        self.sim = {"face": "recipient", "tray": "auto"}
        self._face_operation = asyncio.Lock()
        self._hint_lock = asyncio.Lock()
        self._hint: tuple[str, float, dict] | None = None

    @property
    def simulated(self) -> bool:
        return self.camera.simulated

    @property
    def tray_simulated(self) -> bool:
        # TRAY_BYPASS (testing, fake hardware only) takes tray results from the simulator while the face check stays real.
        return self.simulated or self.settings.tray_bypass

    def status(self) -> dict:
        return {"simulated": self.simulated, "tray_bypass": bool(self.settings.tray_bypass) and not self.simulated,
                "face_models": self.face.available(), "sim": dict(self.sim) if self.tray_simulated else None}

    def set_sim(self, face: str | None = None, tray: str | None = None) -> None:
        if face in SIM_FACE:
            self.sim["face"] = face
        if tray in SIM_TRAY:
            self.sim["tray"] = tray

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
        else:
            frames, err = await self._collect("tray", lambda img: self.tray.analyze(img, classes))
            obs = Observation(kind, "UNCERTAIN", "aruco+hsv-contours")
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
        rev_after = self.hardware.fields.get("rev")
        if rev_before != rev_after or session != self.hardware.session:
            obs.result, obs.reason_code = "UNCERTAIN", "TRAY_REVISION_CHANGED"
            obs.message = message("TRAY_REVISION_CHANGED")
        obs.tray_revision = rev_after
        obs.controller_session = self.hardware.session
        return obs

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
