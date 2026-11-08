"""Download the pretrained OpenCV Zoo models: YuNet (face detector), SFace (face recognizer)
and the MediaPipe palm detector (used only to notice a hand moving to the mouth after release).

Run once:  .venv/bin/python scripts/download_models.py
Files go to models/. Nothing is trained; these are used as-is."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
BASE = "https://github.com/opencv/opencv_zoo/raw/main/models"
FILES = {
    "face_detection_yunet_2023mar.onnx": f"{BASE}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx": f"{BASE}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    "palm_detection_mediapipe_2023feb.onnx": f"{BASE}/palm_detection_mediapipe/palm_detection_mediapipe_2023feb.onnx",
}


def main() -> int:
    MODELS.mkdir(exist_ok=True)
    for name, url in FILES.items():
        dest = MODELS / name
        if dest.exists() and dest.stat().st_size > 100_000:
            print(f"ok      {name} (already present)")
            continue
        print(f"fetch   {name}")
        tmp = dest.with_suffix(".part")
        import httpx  # ships certifi; python.org builds of Python may lack system CA certs
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_bytes():
                    fh.write(chunk)
        if tmp.stat().st_size < 100_000:
            tmp.unlink()
            print(f"failed  {name}: download too small (check the URL or Git LFS)")
            return 1
        tmp.rename(dest)
        print(f"saved   {name}  sha256={hashlib.sha256(dest.read_bytes()).hexdigest()[:16]}…")
    # Smoke test: make sure this OpenCV build can load both models.
    import cv2
    cv2.FaceDetectorYN.create(str(MODELS / "face_detection_yunet_2023mar.onnx"), "", (320, 320))
    cv2.FaceRecognizerSF.create(str(MODELS / "face_recognition_sface_2021dec.onnx"), "")
    cv2.dnn.readNet(str(MODELS / "palm_detection_mediapipe_2023feb.onnx"))
    print(f"loaded all three models with OpenCV {cv2.__version__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
