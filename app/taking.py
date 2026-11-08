"""Taking observation: after the tray unlocks, watch the face camera for (1) who empties the
tray and (2) a hand moving to the recipient's mouth.

What this can and cannot say: a palm seen at the mouth of a face that matches the enrolled
person, after the hand was away from the mouth. That is a hand-to-mouth movement, recorded as
ACTION_OBSERVED. It is never proof of swallowing, and it is stored separately from the
person's own "I took it" (USER_CONFIRMED).

Models: the pretrained MediaPipe palm detector as published in the OpenCV Zoo
(palm_detection_mediapipe_2023feb.onnx), run with cv2.dnn, plus the YuNet face landmarks and
SFace match already used for the face check. Nothing is trained."""
from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

PALM_FILE = "palm_detection_mediapipe_2023feb.onnx"
PALM_INPUT = 192


def palm_anchors():
    """SSD anchor centres for the 192x192 palm detector: a 24x24 grid with 2 anchors per cell,
    then a 12x12 grid with 6 per cell (2016 in total, fixed size, so only centres matter)."""
    import numpy as np
    out = []
    for stride, per_cell in ((8, 2), (16, 6)):
        g = PALM_INPUT // stride
        for y in range(g):
            for x in range(g):
                out.extend([((x + 0.5) / g, (y + 0.5) / g)] * per_cell)
    return np.array(out, dtype=np.float32)


class PalmDetector:
    def __init__(self, models_dir: Path, score_threshold: float = 0.6):
        self.path = models_dir / PALM_FILE
        self.score_threshold = score_threshold
        self._net = None
        self._anchors = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self.path.exists() and self.path.stat().st_size > 100_000

    def _load(self) -> None:
        if self._net is None:
            import cv2
            self._net = cv2.dnn.readNet(str(self.path))
            self._anchors = palm_anchors()

    def detect(self, img) -> list[dict]:
        """Palms in a BGR image: [{score, box: [x, y, w, h], keypoints: [[x, y] * 7]}] in image pixels."""
        import cv2
        import numpy as np
        with self._lock:
            self._load()
            h, w = img.shape[:2]
            ratio = min(PALM_INPUT / h, PALM_INPUT / w)
            rh, rw = max(1, int(h * ratio)), max(1, int(w * ratio))
            top, left = (PALM_INPUT - rh) // 2, (PALM_INPUT - rw) // 2
            square = cv2.copyMakeBorder(cv2.resize(img, (rw, rh)), top, PALM_INPUT - rh - top, left, PALM_INPUT - rw - left,
                                        cv2.BORDER_CONSTANT, value=(0, 0, 0))
            blob = cv2.cvtColor(square, cv2.COLOR_BGR2RGB).astype(np.float32)[None] / 255.0  # NHWC
            self._net.setInput(blob)
            outs = self._net.forward(self._net.getUnconnectedOutLayersNames())
            return decode_palms(outs, self._anchors, (h, w), ratio, (left, top), self.score_threshold)


def decode_palms(outs, anchors, shape, ratio, pad, score_threshold) -> list[dict]:
    import cv2
    import numpy as np
    reg = next(o for o in outs if o.shape[-1] == 18)[0]
    logits = next(o for o in outs if o.shape[-1] == 1)[0, :, 0].astype(np.float64)
    score = 1.0 / (1.0 + np.exp(-np.clip(logits, -50, 50)))
    h, w = shape
    scale = max(h, w)
    pad_px = np.array([pad[0] / ratio, pad[1] / ratio])
    centre = reg[:, :2] / PALM_INPUT + anchors
    size = reg[:, 2:4] / PALM_INPUT
    xy1 = (centre - size / 2) * scale - pad_px
    wh = size * scale
    boxes = np.concatenate([xy1, wh], axis=1)
    keep = cv2.dnn.NMSBoxes(boxes.tolist(), score.tolist(), score_threshold, 0.3, top_k=4)
    out = []
    for i in np.array(keep).flatten():
        kp = (reg[i, 4:].reshape(7, 2) / PALM_INPUT + anchors[i]) * scale - pad_px
        out.append({"score": round(float(score[i]), 3), "box": [round(float(v), 1) for v in boxes[i]],
                    "keypoints": [[round(float(x), 1), round(float(y), 1)] for x, y in kp]})
    return out


def box_distance(point, box) -> float:
    """Distance from a point to a box (0 inside it)."""
    x, y = point
    bx, by, bw, bh = box
    dx = max(bx - x, 0.0, x - (bx + bw))
    dy = max(by - y, 0.0, y - (by + bh))
    return math.hypot(dx, dy)


@dataclass
class Sample:
    t: float  # monotonic seconds
    face: str  # match | unknown | multi | none
    recipient: bool = False  # the enrolled face was among the faces in view
    mouth: tuple[float, float] | None = None  # recipient's mouth centre, if seen
    face_w: float | None = None
    palms: list[dict] = field(default_factory=list)
    palms_checked: bool = True  # False when the palm model is missing


class ActionTracker:
    """Decides when a hand-to-mouth movement was seen.

    Rules: the hand must first be seen away from the mouth (or not in view), then a palm must be
    near the recipient's mouth in at least `min_near` samples within `near_window_s`, and the
    enrolled face must have matched within that window (a hand over the mouth often hides the
    face, so the last mouth position is reused for up to 1.5 s)."""

    MOUTH_MEMORY_S = 1.5

    def __init__(self, near_ratio: float, far_ratio: float, min_near: int, near_window_s: float):
        self.near_ratio, self.far_ratio = near_ratio, far_ratio
        self.min_near, self.near_window_s = min_near, near_window_s
        self.seen_away = False
        self.near: deque[float] = deque()
        self.last_mouth: tuple[float, tuple[float, float], float] | None = None
        self.last_match_t: float | None = None
        self.observed_at: float | None = None
        self.details: dict = {}
        self.closest: float | None = None

    def feed(self, s: Sample) -> bool:
        """Returns True on the sample where the movement is first seen."""
        if self.observed_at is not None:
            return False
        if s.recipient:
            self.last_match_t = s.t
            if s.mouth and s.face_w:
                self.last_mouth = (s.t, s.mouth, s.face_w)
        if not s.palms_checked:
            return False
        ref = self.last_mouth if self.last_mouth and s.t - self.last_mouth[0] <= self.MOUTH_MEMORY_S else None
        if ref is None:
            return False
        _, mouth, fw = ref
        dist = min((box_distance(mouth, p["box"]) for p in s.palms), default=math.inf) / max(fw, 1.0)
        if math.isfinite(dist):
            self.closest = dist if self.closest is None else min(self.closest, dist)
        if dist >= self.far_ratio:
            self.seen_away = True
            self.near.clear()
            return False
        if dist > self.near_ratio or not self.seen_away:
            return False
        self.near.append(s.t)
        while self.near and s.t - self.near[0] > self.near_window_s:
            self.near.popleft()
        recent_match = self.last_match_t is not None and s.t - self.last_match_t <= self.near_window_s + self.MOUTH_MEMORY_S
        if len(self.near) >= self.min_near and recent_match:
            self.observed_at = s.t
            self.details = {"near_samples": len(self.near), "closest_face_widths": round(self.closest or dist, 2),
                            "near_ratio": self.near_ratio, "far_ratio": self.far_ratio}
            return True
        return False


def collector(counts: dict[str, int], min_samples: int, runs: dict[str, int] | None = None, run_needed: int = 5) -> str:
    """Who emptied the tray, from face samples taken while the tray was unlocked.
    OTHER_PERSON: a second face stayed in view (run_needed samples in a row, about a second), or an
    unknown face stayed in view and was seen more than the enrolled one. Scattered single frames are
    detector noise (a hand or a poster briefly read as a face), not a person.
    RECIPIENT: the enrolled face was seen enough. NOT_SEEN: neither."""
    match, unknown, multi = counts.get("match", 0), counts.get("unknown", 0), counts.get("multi", 0)
    runs = runs if runs is not None else {"multi": multi, "unknown": unknown}
    if runs.get("multi", 0) >= run_needed or (runs.get("unknown", 0) >= run_needed and unknown > match):
        return "OTHER_PERSON"
    if match >= min_samples:
        return "RECIPIENT"
    return "NOT_SEEN"


@dataclass
class Watch:
    txn_id: str
    person_id: str | None
    started_mono: float
    tracker: ActionTracker
    available: bool  # face models present (or simulated)
    palms_available: bool
    simulated: bool
    counts: dict = field(default_factory=lambda: {"match": 0, "unknown": 0, "multi": 0, "none": 0})
    runs: dict = field(default_factory=lambda: {"multi": 0, "unknown": 0})  # longest unbroken stretch of each
    _run: tuple = ("", 0)
    samples: int = 0
    run_needed: int = 5
    paused: bool = False  # while the person shows the pills in their hand, the hand is not "going to the mouth"
    action_at_wall: str | None = None
    action_saved: bool = False
    last_face: str | None = None
    stopped: bool = False

    def add(self, s: Sample, wall_iso: str) -> bool:
        self.samples += 1
        self.counts[s.face] = self.counts.get(s.face, 0) + 1
        self.last_face = s.face
        kind, n = self._run
        n = n + 1 if kind == s.face else 1
        self._run = (s.face, n)
        if s.face in self.runs:
            self.runs[s.face] = max(self.runs[s.face], n)
        if self.paused:
            return False
        hit = self.tracker.feed(s)
        if hit:
            self.action_at_wall = wall_iso
        return hit

    def view(self, min_samples: int) -> dict:
        return {"txn_id": self.txn_id, "active": not self.stopped, "available": self.available, "palms_available": self.palms_available,
                "simulated": self.simulated, "samples": self.samples, "counts": dict(self.counts),
                "face_now": self.last_face, "collector_so_far": collector(self.counts, min_samples, self.runs, self.run_needed),
                "action_observed": self.tracker.observed_at is not None, "action_at": self.action_at_wall}
