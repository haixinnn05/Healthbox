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
    "TRAY_MARKERS_MISSING": "The camera can't see the whole tray. Keep all of it in view, with nothing covering it.",
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
    "SKIP_NOT_ALLOWED": "This medicine is needed, so it can't be skipped. Take it now, or ask your doctor or pharmacist what to do.",
    "NOT_HOMED": "A rotor's position is not known (it was stopped or faulted mid-motion). Use supervised recovery to rehome.",
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
    # As needed
    "TOO_SOON": "It is too soon since the last dose of this medicine.",
    "DAILY_LIMIT": "The most doses allowed in 24 hours have already been taken.",
    "AS_NEEDED_IN_PROGRESS": "A dose of this medicine is already being prepared. Finish or cancel it first.",
    "UNKNOWN_ENTRY": "This medicine is not in the confirmed plan. Refresh and try again.",
    "NOT_AS_NEEDED": "This medicine follows a schedule. Take it when its reminder is due.",
    # Collection and taking (camera observations only; never proof of swallowing)
    "COLLECTED_BY_OTHER": "Someone other than you was in view while the tray was open.",
    "COLLECTOR_NOT_SEEN": "Your face was not in view while the tray was open, so it is not known who took the pills out.",
    "NO_HAND": "No open hand was seen. Hold the pills flat on your open palm, facing the camera, about 30 cm away.",
    "HAND_BLURRY": "Your hand was moving, so the picture was blurry. Hold your hand still, about 30 to 40 cm from the camera.",
    "HAND_UNSTABLE": "The pills in your hand were not counted the same way twice. Keep your hand still and check again.",
    "HAND_COUNT_MISMATCH": "The pills in your hand do not match your dose.",
    "HAND_MODEL_MISSING": "The hand model is missing, so the camera can't check your hand. Run scripts/download_models.py.",
    "SETUP_CODE": "That setup code is wrong. It is shown in the terminal on the laptop with the pill box.",
    "PUBLIC_DEMO": "This is a shared public demo, so this can't be changed here.",
    "HAND_CHECK_FIRST": "First show the pills in your hand to the camera, or choose I can't show them.",
    "ACTION_NOT_SEEN": "No hand-to-mouth movement was seen. If you took your medicine, press I took it.",
    "ACTION_UNAVAILABLE": "The camera could not watch for taking (simulated face, missing models or no enrolled face).",
    # Pill types and cartridges
    "LOOK_ALIKE_PILLS": "Two pill types look too alike for the tray camera. Calibrate them with clearly different pills in Setup, Dispenser.",
    "FILL_CHECK_REQUIRED": "Check one pill from the bottle with the camera before recording a fill.",
    "FILL_NO_PILL": "No pill found in the tray. Put one pill from the bottle in the tray, in view of the camera.",
    "FILL_TOO_MANY": "More than one pill is in the tray. Leave exactly one pill from the bottle.",
    "FILL_WRONG_PILL": "This pill does not match the pill type set up for this cartridge. Do not fill the cartridge with it.",
    # Account (guards Setup only)
    "SETUP_LOCKED": "Setup is locked. Enter your password to make changes.",
    "SIGNED_OUT": "Sign in to see this. A dose that is due can still be taken without signing in.",
    "WRONG_PASSWORD": "That password is not right. Try again.",
    "TOO_MANY_ATTEMPTS": "Too many wrong passwords. Wait a minute, then try again.",
    "ACCOUNT_EXISTS": "An account already exists on this laptop.",
    "NO_ACCOUNT": "No account has been made on this laptop yet.",
}

AS_NEEDED_WAIT = ("TOO_SOON", "DAILY_LIMIT")


def message(code: str, **details) -> str:
    if code == "COUNT_MISMATCH" and details.get("expected") is not None:
        return count_mismatch_sentence(details["expected"], details.get("observed") or {}, details.get("labels") or {})
    if code in AS_NEEDED_WAIT and details.get("when"):
        return f"{MESSAGES[code]} The next dose can be taken {_at(details['when'])}."
    return MESSAGES.get(code, code.replace("_", " ").capitalize() + ".")


def _at(when: str) -> str:
    """'6:15 PM' -> 'at 6:15 PM'; 'tomorrow at 6:15 PM' stays as it is."""
    return when if " at " in when else f"at {when}"


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
