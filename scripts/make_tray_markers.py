"""Print-ready ArUco corner markers for the tray (DICT_4X4_50, ids 0-3).

  .venv/bin/python scripts/make_tray_markers.py   ->  docs/tray-markers.png

Stick id 0 top-left, 1 top-right, 2 bottom-right, 3 bottom-left around the tray floor,
as seen from the camera. Keep them flat and uncovered; the tray area is inside them."""
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SIZE = 220  # pixels per marker; print at about 3 cm
dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
sheet = np.full((SIZE * 2 + 180, SIZE * 2 + 180), 255, np.uint8)
for i, (r, c) in enumerate([(0, 0), (0, 1), (1, 1), (1, 0)]):
    m = cv2.aruco.generateImageMarker(dictionary, i, SIZE)
    y, x = 60 + r * (SIZE + 60), 60 + c * (SIZE + 60)
    sheet[y:y + SIZE, x:x + SIZE] = m
    cv2.putText(sheet, f"id {i}", (x, y + SIZE + 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 0, 2)
out = ROOT / "docs" / "tray-markers.png"
cv2.imwrite(str(out), sheet)
print(f"wrote {out}")
