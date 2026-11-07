"""Reason codes and the plain, deterministic sentences shown for them.

These sentences are the authoritative explanation. The assistant may add help
around them, but never replaces them."""
from __future__ import annotations

MESSAGES: dict[str, str] = {
    # People
    "UNKNOWN_PERSON": "Unknown person. Only the enrolled recipient can continue.",
    "MULTIPLE_FACES": "More than one face is in view. Only the recipient should be in frame.",
    "NO_FACE": "No face in view. Face the laptop camera.",
    "NOT_ENROLLED": "No recipient is enrolled for this plan. Enroll in Setup first.",
    "FACE_UNSTABLE": "The face match was not stable across frames. Hold still and try again.",
    # Tray
    "TRAY_MARKERS_MISSING": "Tray markers missing. The camera must see all four corner markers.",
    "OBJECTS_TOUCHING": "Objects touching or stacked. Counts are uncertain.",
    "IMAGE_BLURRY": "The tray image is blurry. Hold the camera steady.",
    "OBJECT_FRAGMENT": "Something smaller than a whole pill is in the tray (broken or partly hidden). Counts are uncertain.",
    "SHAPE_MISMATCH": "An object has the right colour but the wrong shape for its pill type. Counts are uncertain.",
    "TRAY_NOT_EMPTY": "The tray is not empty. It must be empty before dispensing.",
    "COUNT_MISMATCH": "The tray does not match the confirmed dose. Access remains locked.",
    "COUNT_UNSTABLE": "Counts changed between frames. Keep the tray still and recheck.",
    "TRAY_NOT_COLLECTED": "Tokens are still in the tray.",
    "INSPECTION_EXPIRED": "Inspection expired. Check the tray again.",
    "INSPECTION_MISSING": "The tray has not been inspected for this dose.",
    "TRAY_REVISION_CHANGED": "The tray changed since it was inspected. Check it again.",
    "TRAY_DOOR_OPENED": "Tray door opened. The inspected contents are no longer trusted.",
    "DOOR_OPEN": "The access door is open. Close it to continue.",
    # Camera
    "CAMERA_UNAVAILABLE": "Camera unavailable. Check the connection and camera permission.",
    "CAMERA_STALE": "The camera image is stale. Check position, lighting or permission.",
    "CAMERA_POSE": "Point the camera at the requested view.",
    # Device
    "DEVICE_DISCONNECTED": "Device disconnected.",
    "DEVICE_FAULT": "The device reports a fault. Use Device controls for supervised recovery.",
    "DEVICE_RESTARTED": "The controller restarted. The session is new and unarmed.",
    "NOT_ARMED": "The device is not armed. Inspect the enclosure and arm it in Device controls.",
    "NOT_HOMED": "A rotor is not at home. Use supervised recovery to rehome.",
    "MOTOR_BUSY": "A motor is still moving.",
    "STOPPED": "STOP was pressed. Inspect the device, then use supervised recovery.",
    "OUTCOME_UNCERTAIN": "Outcome uncertain. Nothing will be repeated automatically. Inspect the device.",
    "COMMAND_REJECTED": "The controller rejected the command.",
    "RESTART_DURING_MOTION": "The app restarted while a motion may have run. Supervised inspection is required.",
    "PENDING_OPERATION": "Waiting for the device to finish the current operation.",
    # Workflow
    "INVALID_TRANSITION": "That action is not available at this stage.",
    "NOT_DUE": "This dose is not due yet.",
    "ALREADY_STARTED": "This dose has already started or finished. It cannot run again.",
    "REMINDER_ONLY": "This entry is reminder only. It never uses the dispenser.",
    "PLAN_VERSION_MISMATCH": "The plan changed. Refresh and try again.",
    "ANOTHER_DOSE_ACTIVE": "Another dose is being verified. Finish it first.",
    "QUANTITY_LIMIT": "The requested quantity is outside the allowed demo range.",
    "INSPECTION_REQUIRED": "Confirm that you physically inspected the enclosure.",
    "DEMO_MISMATCH": "This record belongs to the other mode (demo or ordinary).",
    "HARDWARE_BUSY": "Hardware must be idle for this change.",
    "REQUEST_ID_REUSED": "This request ID was already used for a different action.",
}


def message(code: str, **details) -> str:
    if code == "COUNT_MISMATCH" and details.get("expected") is not None:
        return count_mismatch_sentence(details["expected"], details.get("observed") or {}, details.get("labels") or {})
    return MESSAGES.get(code, code.replace("_", " ").capitalize() + ".")


def _plural(n: int, word: str) -> str:
    return f"{n} {word} token" + ("" if n == 1 else "s")


def count_mismatch_sentence(expected: dict, observed: dict, labels: dict) -> str:
    parts = []
    for cls in sorted(set(expected) | set(observed)):
        e, o = int(expected.get(cls, 0)), int(observed.get(cls, 0))
        if e != o:
            name = labels.get(cls, cls)
            parts.append(f"Expected {_plural(e, name)} and observed {o}.")
    if not parts:
        return MESSAGES["COUNT_MISMATCH"]
    return " ".join(parts) + " Access remains locked. Use Device controls for supervised recovery."


def block(code: str, **details) -> dict:
    return {"code": code, "message": message(code, **details), "details": details or None}
