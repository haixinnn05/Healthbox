"""Print-ready ArUco corner markers (DICT_4X4_50) for the tray and for the pill dish.

  .venv/bin/python scripts/make_tray_markers.py   ->  docs/tray-markers.png  (ids 0-3)
                                                      docs/dish-markers.png  (ids 4-7)

Tray: stick id 0 top-left, 1 top-right, 2 bottom-right, 3 bottom-left around the tray floor, as seen
from the camera. Dish: the same order with ids 4 (top-left), 5, 6, 7 around a small dark, matte dish
(about 8 x 6 cm inside the markers); pour the pills in and hold it up to the laptop camera. The dish
uses different ids, so it is never mistaken for the tray. Keep markers flat and uncovered."""
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)


def sheet(first_id: int, size: int, name: str, title: str) -> Path:
    page = np.full((size * 2 + 220, size * 2 + 180), 255, np.uint8)
    cv2.putText(page, title, (60, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 0, 2)
    for k, (r, c) in enumerate([(0, 0), (0, 1), (1, 1), (1, 0)]):
        mid = first_id + k
        m = cv2.aruco.generateImageMarker(dictionary, mid, size)
        y, x = 100 + r * (size + 60), 60 + c * (size + 60)
        page[y:y + size, x:x + size] = m
        cv2.putText(page, f"id {mid}", (x, y + size + 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 0, 2)
    out = ROOT / "docs" / name
    cv2.imwrite(str(out), page)
    return out


print("wrote", sheet(0, 220, "tray-markers.png", "Tray corners (print each about 3 cm)"))
print("wrote", sheet(4, 220, "dish-markers.png", "Pill dish corners (print each about 1.5 cm)"))
