"""Practice runs of the camera checks, and pass rates, for tuning on the real camera before a demo.

Practice checks never dispense, unlock or touch a dose: they run the same camera check as a dose step
and store the result as a PRACTICE_* observation outside any dose. The summary shows how often each
check passes (in practice and in real doses) and, for the face check, how far your match scores sit
above the threshold."""
from __future__ import annotations

import json
import statistics

KINDS = {"face": "PRACTICE_FACE", "hand": "PRACTICE_HAND", "tray": "PRACTICE_TRAY"}
DOSE_KINDS = [("FACE_MATCH", "Your face"), ("TRAY_EMPTY", "Empty tray"), ("CLASS_COUNTS", "Dose count"),
              ("HAND_CHECK", "Pills in your hand"), ("COLLECTED_BY", "Who emptied the tray"),
              ("ACTION_OBSERVED", "Hand to mouth")]


def _sims(rows: list[dict]) -> list[float]:
    out = []
    for r in rows:
        out.extend(float(x) for x in (json.loads(r["details_json"] or "{}").get("similarities") or []))
    return out


def face_advice(sims: list[float], threshold: float) -> str | None:
    if not sims:
        return None
    low = min(sims)
    if low < threshold:
        return (f"Some frames scored below the threshold ({low:.2f} < {threshold:.2f}). Retake your face in the "
                "light you will use, facing the camera, before changing FACE_MATCH_THRESHOLD.")
    if low - threshold < 0.08:
        return f"Matches pass, but close to the threshold (lowest {low:.2f}, threshold {threshold:.2f}). Better light helps."
    return f"Good margin: your lowest score {low:.2f} is well above the threshold {threshold:.2f}."


def summary(store, threshold: float) -> dict:
    practice = {}
    for kind, obs_kind in KINDS.items():
        rows = store.q("SELECT obs_id, result, reason_code, details_json, simulated, created_at FROM observations"
                       " WHERE kind=? ORDER BY obs_id DESC LIMIT 200", (obs_kind,))
        recent = []
        for r in rows[:8]:
            d = json.loads(r["details_json"] or "{}")
            sims = d.get("similarities") or []
            recent.append({"at": r["created_at"], "result": r["result"], "reason_code": r["reason_code"],
                           "source": d.get("source"), "observed": d.get("observed"), "expected": d.get("expected"),
                           "similarity": round(statistics.median(sims), 3) if sims else None,
                           "photo": d.get("photo"), "simulated": bool(r["simulated"])})
        entry = {"runs": len(rows), "passed": sum(r["result"] == "PASS" for r in rows), "recent": recent}
        if kind == "face":
            sims = _sims(rows)
            entry["similarity"] = None if not sims else {"min": round(min(sims), 3), "median": round(statistics.median(sims), 3),
                                                         "max": round(max(sims), 3), "threshold": threshold}
            entry["advice"] = face_advice(sims, threshold)
        practice[kind] = entry
    doses = []
    for kind, label in DOSE_KINDS:
        rows = store.q("SELECT txn_id, result, simulated FROM observations WHERE kind=? AND txn_id IS NOT NULL ORDER BY obs_id",
                       (kind,))
        first: dict[str, str] = {}
        for r in rows:
            first.setdefault(r["txn_id"], r["result"])
        doses.append({"kind": kind, "label": label, "checks": len(rows), "passed": sum(r["result"] == "PASS" for r in rows),
                      "doses": len(first), "first_try": sum(v == "PASS" for v in first.values()),
                      "simulated": sum(r["simulated"] for r in rows)})
    face_rows = store.q("SELECT details_json FROM observations WHERE kind='FACE_MATCH' AND simulated=0 ORDER BY obs_id DESC LIMIT 200")
    dose_sims = _sims(face_rows)
    return {"practice": practice, "doses": doses,
            "dose_face": None if not dose_sims else {"min": round(min(dose_sims), 3), "median": round(statistics.median(dose_sims), 3),
                                                     "threshold": threshold, "advice": face_advice(dose_sims, threshold)}}
