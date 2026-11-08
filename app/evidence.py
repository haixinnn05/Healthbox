"""Photo proof for tray checks. Each tray check keeps one picture of the tray, with every pill
the camera counted circled, so a person can see what "Dose check passed" was based on.

Only the tray region is ever stored (the perspective-corrected area inside the four markers),
never a face. Photos live in data/evidence on this laptop, which is never uploaded, and the
oldest are removed once there are more than EVIDENCE_KEEP."""
from __future__ import annotations

import logging
import re
import secrets
from pathlib import Path

log = logging.getLogger("doseguard.evidence")

DIRNAME = "evidence"
_NAME = re.compile(r"^[0-9a-f]{16}\.jpg$")
SCALE = 2  # stored photos are the 344x244 tray area, doubled so the rings and numbers read clearly


def evidence_dir(data_dir: Path) -> Path:
    return data_dir / DIRNAME


def path_for(data_dir: Path, name: str) -> Path | None:
    """The file for a photo name, or None when the name is not one this module writes."""
    if not _NAME.match(name or ""):
        return None
    p = evidence_dir(data_dir) / name
    return p if p.is_file() else None


def save(data_dir: Path, img, keep: int) -> str | None:
    """Write a BGR image as JPEG; returns 'evidence/<name>.jpg' (relative to data_dir)."""
    import cv2
    d = evidence_dir(data_dir)
    try:
        d.mkdir(parents=True, exist_ok=True)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return None
        name = f"{secrets.token_hex(8)}.jpg"
        (d / name).write_bytes(buf.tobytes())
        prune(data_dir, keep)
        return f"{DIRNAME}/{name}"
    except OSError:
        log.exception("could not save a tray photo")
        return None


def prune(data_dir: Path, keep: int) -> int:
    d = evidence_dir(data_dir)
    if keep <= 0 or not d.is_dir():
        return 0
    files = sorted((p for p in d.iterdir() if _NAME.match(p.name)), key=lambda p: p.stat().st_mtime)
    removed = 0
    for p in files[: max(0, len(files) - keep)]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def delete(data_dir: Path, rels: list[str]) -> None:
    for rel in rels:
        name = (rel or "").removeprefix(f"{DIRNAME}/")
        p = path_for(data_dir, name)
        if p:
            try:
                p.unlink()
            except OSError:
                pass


def hex_to_bgr(color: str) -> tuple[int, int, int]:
    c = (color or "#888888").lstrip("#")
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except (ValueError, IndexError):
        r, g, b = 136, 136, 136
    return b, g, r


def draw_marks(img, marks: list[dict]) -> None:
    """Draw one ring and a short tag (cartridge letter and number, e.g. A1) per counted pill.
    marks: {x, y, r, color, tag, ok} in image pixels. Problem objects get a red ring."""
    import cv2
    for m in marks:
        x, y, r = int(m["x"]), int(m["y"]), max(8, int(m["r"]))
        ring = hex_to_bgr(m["color"]) if m.get("ok", True) else (24, 35, 180)
        cv2.circle(img, (x, y), r + 7, (255, 255, 255), 5, cv2.LINE_AA)
        cv2.circle(img, (x, y), r + 7, ring if m.get("ok", True) else (24, 35, 180), 3, cv2.LINE_AA)
        if not m.get("ok", True):
            cv2.circle(img, (x, y), r + 13, (24, 35, 180), 2, cv2.LINE_AA)
        tag = str(m.get("tag") or "")[:4]
        if tag:
            (tw, th), _ = cv2.getTextSize(tag, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
            tx, ty = x - tw // 2, y - r - 14
            ty = max(th + 4, ty)
            cv2.rectangle(img, (tx - 4, ty - th - 4), (tx + tw + 4, ty + 4), (255, 255, 255), -1)
            cv2.putText(img, tag, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (79, 48, 22), 2, cv2.LINE_AA)


def banner(img, text: str) -> None:
    """A small label in the corner, e.g. SIMULATED. ASCII only (OpenCV's fonts)."""
    import cv2
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
    cv2.rectangle(img, (8, 8), (8 + tw + 14, 8 + th + 14), (255, 255, 255), -1)
    cv2.putText(img, text, (15, 8 + th + 7), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (79, 48, 22), 2, cv2.LINE_AA)


def simulated_tray(classes: list[dict], observed: dict[str, int] | None, scenario: str = "auto"):
    """A drawn stand-in for the tray photo when the camera is simulated. It is always labelled
    SIMULATED; it shows what the simulator reported, not what a camera saw."""
    import cv2
    import numpy as np
    w, h = 344 * SCALE, 244 * SCALE
    img = np.full((h, w, 3), (226, 221, 216), np.uint8)
    cv2.rectangle(img, (6, 6), (w - 7, h - 7), (190, 182, 175), 2)
    rng = np.random.default_rng(7)
    slots = [(int(w * (0.18 + 0.16 * (i % 5))), int(h * (0.36 + 0.3 * (i // 5)))) for i in range(10)]
    marks, k = [], 0
    for cls in classes:
        n = int((observed or {}).get(cls["class_id"], 0))
        shape = (cls.get("hsv") or {}).get("shape") or {}
        elong = float(shape.get("elongation", 1.0))
        for j in range(min(n, 10 - k)):
            cx, cy = slots[k]
            cx += int(rng.integers(-12, 12))
            cy += int(rng.integers(-10, 10))
            if scenario == "touching" and k > 0:
                cx, cy = slots[k - 1][0] + 30, slots[k - 1][1]
            ax = 26
            cv2.ellipse(img, (cx, cy), (int(ax * min(2.2, elong)), ax), 0, 0, 360, hex_to_bgr(cls["color"]), -1, cv2.LINE_AA)
            cv2.ellipse(img, (cx, cy), (int(ax * min(2.2, elong)), ax), 0, 0, 360, (90, 90, 90), 1, cv2.LINE_AA)
            marks.append({"x": cx, "y": cy, "r": int(ax * min(2.2, elong)), "color": cls["color"],
                          "tag": f"{cls.get('cartridge') or '?'}{j + 1}", "ok": scenario != "touching"})
            k += 1
    draw_marks(img, marks)
    banner(img, "SIMULATED")
    if scenario == "blurry":
        img = cv2.GaussianBlur(img, (21, 21), 0)
    return img
