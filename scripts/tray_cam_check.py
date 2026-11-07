"""Bench check for the ESP32-CAM tray camera (CAMERA_MODE=dual_camera).

    .venv/bin/python scripts/tray_cam_check.py --url http://doseguard-tray.local/snapshot

It fetches frames exactly the way the app does (the app's own Esp32SnapshotCamera), so a PASS
means the app will accept them. Then it checks what the app needs over a real verification:
    fresh        every frame is younger than 500 ms and the reply comes within 1.5 s
    sequence     one boot ID, frame numbers strictly increasing, no repeats
    distinct     no two frames are byte-identical (a stuck buffer would be)
    rate         enough frames for a 10-frame check spanning at least 1 s
    tray view    (with --tray) all four corner markers seen and the picture sharp enough

The last frame is saved to data/bench/ so you can look at the framing and lighting.
Run it on the same WiFi the demo will use; event networks often block device-to-device traffic.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.camera import Esp32SnapshotCamera  # noqa: E402
from app.config import Settings  # noqa: E402
from app.timeutil import Clock  # noqa: E402


async def run(a) -> int:
    clock = Clock()
    cam = Esp32SnapshotCamera(a.url, clock, max_age_ms=a.max_age_ms, timeout_s=a.timeout_s)
    frames, errors, hashes, req_ms, ages = [], [], set(), [], []
    boots = set()
    duplicates = 0
    t0 = time.monotonic()
    for i in range(a.count):
        f = await cam.snapshot()
        if f is None:
            errors.append(cam.error or "no frame")
            print(f"  frame {i + 1:>2}: REJECTED  {cam.error}", flush=True)
        else:
            frames.append(f)
            boots.add(f.camera_session)
            req_ms.append(f.meta["request_ms"])
            ages.append(f.meta["capture_age_ms"])
            digest = hashlib.sha1(f.image.tobytes()).hexdigest()
            if digest in hashes:
                duplicates += 1
            hashes.add(digest)
            h, w = f.image.shape[:2]
            if a.verbose:
                print(f"  frame {i + 1:>2}: seq {f.seq} age {f.meta['capture_age_ms']} ms, "
                      f"request {f.meta['request_ms']} ms, {w}x{h}", flush=True)
        if a.interval:
            await asyncio.sleep(a.interval)
    elapsed = time.monotonic() - t0

    failed = 0

    def report(ok: bool, name: str, detail: str) -> None:
        nonlocal failed
        failed += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<10} {detail}", flush=True)

    print()
    report(not errors, "fresh", f"{len(frames)}/{a.count} frames accepted by the app's checks"
           + (f"; first problem: {errors[0]}" if errors else ""))
    if frames:
        seqs = [f.seq for f in frames]
        increasing = all(b > a_ for a_, b in zip(seqs, seqs[1:]))
        report(len(boots) == 1 and increasing, "sequence",
               f"boot {', '.join(sorted(boots))}; frame numbers {seqs[0]}..{seqs[-1]}"
               + ("" if increasing else " (not strictly increasing)") + ("" if len(boots) == 1 else " (camera rebooted)"))
        report(duplicates == 0, "distinct", "every frame differs" if not duplicates else f"{duplicates} repeated frame(s)")
        fps = len(frames) / elapsed if elapsed else 0.0
        # The app's tray check wants 10 distinct frames spanning at least 1 s.
        report(fps >= 2.0, "rate", f"{fps:.1f} frames/s; request median {statistics.median(req_ms):.0f} ms, "
               f"max {max(req_ms)} ms; capture age median {statistics.median(ages):.0f} ms, max {max(ages)} ms")
        last = frames[-1].image
        h, w = last.shape[:2]
        report(w >= 320 and h >= 240, "size", f"{w}x{h}")
        out = Path(a.save) if a.save else ROOT / "data" / "bench" / f"tray-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        import cv2
        cv2.imwrite(str(out), last)
        if a.tray:
            from app.vision import TrayInspector
            settings = Settings.from_env(genai_backend="off")
            warped, info = TrayInspector(settings)._warp(last)
            if warped is None and info.get("status") == "markers_missing":
                report(False, "tray view", f"corner markers seen: {info.get('markers')} (need 0, 1, 2, 3). "
                       "Reframe so all four printed squares show.")
            elif warped is None:
                report(False, "tray view", f"too blurry (sharpness {info.get('sharpness')}, need "
                       f"{settings.blur_threshold}). Refocus the lens or add diffuse light.")
            else:
                report(True, "tray view", f"all four markers, sharpness {info.get('sharpness')}")
        print(f"\n  Last frame saved to {out}")
    print(f"\n{'All checks passed.' if not failed else f'{failed} check(s) failed.'}")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="e.g. http://doseguard-tray.local/snapshot")
    ap.add_argument("--count", type=int, default=20, help="frames to fetch (default 20)")
    ap.add_argument("--interval", type=float, default=0.0, help="seconds to wait between requests")
    ap.add_argument("--tray", action="store_true", help="also check the corner markers and focus in the last frame")
    ap.add_argument("--save", help="where to save the last frame")
    ap.add_argument("--max-age-ms", type=int, default=500, help="the app's limit (default 500)")
    ap.add_argument("--timeout-s", type=float, default=1.5, help="the app's limit (default 1.5)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    print(f"DoseGuard tray camera check: {a.url}")
    return asyncio.run(run(a))


if __name__ == "__main__":
    sys.exit(main())
