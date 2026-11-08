"""Runtime configuration, read once from the environment (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(ROOT / ".env")


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    # Server
    host: str = "127.0.0.1"
    port: int = 8000
    public_demo: bool = False  # hosted public demo: simulated hardware only, one built-in guest, wiped regularly
    public_host: str = ""  # the demo's public domain, e.g. healthbox.up.railway.app
    demo_reset_minutes: int = 30
    tray_vision: str = "markers"  # markers: ArUco corners + calibrated colours; outline: tray by colour, objects by outline
    remote_host: str = ""  # public link to the real box, e.g. abc.trycloudflare.com; the server stays on loopback
    remote_setup_code: str = ""  # needed to create the account over the link; shown only in the laptop's terminal
    data_dir: Path = ROOT / "data"
    db_path: Path = ROOT / "data" / "doseguard.db"
    models_dir: Path = ROOT / "models"
    timezone: str = "America/New_York"

    # Hardware
    hardware_mode: str = "fake"  # fake | serial
    serial_port: str = ""
    serial_baud: int = 115200
    max_tokens_per_channel: int = 3
    ack_timeout_s: float = 2.0
    cycle_timeout_per_unit_s: float = 8.0
    release_timeout_s: float = 5.0
    ping_interval_s: float = 0.25
    watchdog_s: float = 1.0
    fake_motion_s_per_unit: float = 0.8

    # Camera / vision
    camera_mode: str = "simulated"  # simulated | single_laptop | dual_camera
    face_camera_index: int = 0
    tray_camera_url: str = ""
    check_frames: int = 10
    check_min_span_s: float = 1.0
    check_timeout_s: float = 5.0
    face_max_age_s: float = 2.0
    tray_max_age_s: float = 60.0  # the box is latched and any door or rotor change voids the count; 15 s was too short to use
    face_match_threshold: float = 0.363  # SFace cosine similarity, calibrate on site
    tray_token_area_px: int = 900  # area of one token in the 400x300 warped tray
    tray_min_area_px: int = 150
    blur_threshold: float = 25.0  # Laplacian variance of the warped tray; calibrate on site

    # Reminders
    snooze_minutes: int = 5
    sound_interval_s: int = 60
    sound_max: int = 3
    needs_review_minutes: int = 15
    scheduler_tick_s: float = 1.0

    # AI
    genai_backend: str = "off"  # off | developer_api | google_cloud
    gemini_model: str = "gemini-3.5-flash"
    # off: no thinking (fast, enough for reading a label or explaining a block); low; default: the model's own.
    gemini_thinking: str = "off"
    gemini_api_key: str = field(default="", repr=False)
    google_cloud_project: str = ""
    google_cloud_location: str = "us-central1"
    ai_request_limit: int = 100
    ai_timeout_s: float = 10.0
    require_label_photo: bool = True  # every plan entry needs a photo of its label
    label_min_sharpness: float = 30.0
    max_upload_bytes: int = 4_000_000
    max_question_chars: int = 500

    # Account: how long Setup stays unlocked after the last Setup action (sliding)
    setup_unlock_minutes: int = 0  # 0 = stay signed in until Sign out (or a server restart)
    # Testing only: tray checks come from the simulated dispenser while the face check stays real.
    # Refused with real hardware, where it would release pills nobody counted.
    tray_bypass: bool = False

    # Reminder sound played by the server through this laptop's speakers (macOS), no click needed
    laptop_sound: bool = True
    laptop_sound_file: str = "/System/Library/Sounds/Glass.aiff"

    # Proof and taking observation
    evidence_keep: int = 400  # tray photos kept in data/evidence (oldest are removed first)
    watch_interval_s: float = 0.2  # how often the collection watch samples the face camera
    action_watch_s: float = 180.0  # keep watching for the hand-to-mouth movement this long after collection
    action_near_ratio: float = 0.6  # palm within this many face widths of the mouth counts as "at the mouth"
    action_far_ratio: float = 1.5  # palm farther than this (or no palm) counts as "away from the mouth"
    action_min_near: int = 2  # samples at the mouth needed, within action_near_window_s
    action_near_window_s: float = 2.0
    collector_min_samples: int = 3  # face samples needed to say who emptied the tray
    collector_run_samples: int = 5  # a second face must stay this many samples in a row (about a second) to count
    hand_blur_threshold: float = 8.0  # palm region sharpness (Laplacian variance at 320 px); measured 3-6 for a moving hand
    # After release, ask the person to show the pills in their hand (or the dish) before the tray check. Off by
    # default: holding several pills up is hard for many people, and the tray was already counted. The Setup
    # switch (Dispenser, "Dose steps") overrides this.
    hand_check: bool = False
    # Check the tray is empty right after the hand check passes (one step). None = only with the in-box
    # tray camera (CAMERA_MODE=dual_camera), where the tray is in view while the person faces the laptop.
    auto_tray_after_hand: bool | None = None
    laptop_voice: bool = True  # speak each dose step through the laptop (macOS say), no click needed
    followup_minutes: int = 5  # ask "did you take it?" this long after release when no taking was seen
    fill_check_max_age_s: float = 900.0  # a pill check must be this recent to record a cartridge fill

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        _load_dotenv()
        data_dir = Path(_str("DATA_DIR", str(ROOT / "data")))
        public = _str("PUBLIC_DEMO", "0") in ("1", "true", "yes")
        s = cls(
            host=_str("HOST", "0.0.0.0" if public else "127.0.0.1"),
            port=_int("PORT", 8000),
            public_demo=public,
            public_host=_str("PUBLIC_HOST", _str("RAILWAY_PUBLIC_DOMAIN", "")),
            demo_reset_minutes=_int("DEMO_RESET_MINUTES", 30),
            tray_vision=_str("TRAY_VISION", "outline").lower(),
            remote_host=_str("REMOTE_HOST", ""),
            remote_setup_code=_str("REMOTE_SETUP_CODE", ""),
            data_dir=data_dir,
            db_path=Path(_str("DB_PATH", str(data_dir / "doseguard.db"))),
            timezone=_str("TIMEZONE", "America/New_York"),
            hardware_mode="fake" if public else _str("HARDWARE_MODE", "fake").lower(),
            serial_port=_str("SERIAL_PORT", ""),
            serial_baud=_int("SERIAL_BAUD", 115200),
            camera_mode="simulated" if public else _str("CAMERA_MODE", "simulated").lower(),
            face_camera_index=_int("FACE_CAMERA_INDEX", 0),
            tray_camera_url=_str("TRAY_CAMERA_URL", ""),
            face_max_age_s=_float("FACE_MAX_AGE_S", 2.0),
            tray_max_age_s=_float("TRAY_MAX_AGE_S", 60.0),
            face_match_threshold=_float("FACE_MATCH_THRESHOLD", 0.363),
            tray_token_area_px=_int("TRAY_TOKEN_AREA_PX", 900),
            snooze_minutes=_int("SNOOZE_MINUTES", 5),
            sound_interval_s=_int("SOUND_INTERVAL_S", 60),
            sound_max=_int("SOUND_MAX", 3),
            needs_review_minutes=_int("NEEDS_REVIEW_MINUTES", 15),
            genai_backend=_str("GENAI_BACKEND", "off").lower(),
            gemini_model=_str("GEMINI_MODEL", "gemini-3.5-flash"),
            gemini_thinking=_str("GEMINI_THINKING", "off").lower(),
            gemini_api_key=_str("GEMINI_API_KEY", _str("GOOGLE_API_KEY", "")),
            google_cloud_project=_str("GOOGLE_CLOUD_PROJECT", ""),
            google_cloud_location=_str("GOOGLE_CLOUD_LOCATION", "us-central1"),
            ai_request_limit=_int("AI_REQUEST_LIMIT", 100),
            require_label_photo=_str("REQUIRE_LABEL_PHOTO", "1") not in ("0", "false", "no"),
            setup_unlock_minutes=_int("SETUP_UNLOCK_MINUTES", 0),
            tray_bypass=not public and _str("TRAY_BYPASS", "0") in ("1", "true", "yes"),
            evidence_keep=_int("EVIDENCE_KEEP", 400),
            laptop_sound=not public and _str("LAPTOP_SOUND", "1") not in ("0", "false", "no"),
            laptop_sound_file=_str("LAPTOP_SOUND_FILE", "/System/Library/Sounds/Glass.aiff"),
            action_watch_s=_float("ACTION_WATCH_S", 180.0),
            followup_minutes=_int("FOLLOWUP_MINUTES", 5),
            hand_check=_str("HAND_CHECK", "0") in ("1", "true", "yes"),
            hand_blur_threshold=_float("HAND_BLUR_THRESHOLD", 8.0),
            auto_tray_after_hand={"1": True, "true": True, "0": False, "false": False}.get(_str("AUTO_TRAY_AFTER_HAND", "").lower()),
            laptop_voice=_str("LAPTOP_VOICE", "1") not in ("0", "false", "no"),
        )
        return replace(s, **overrides) if overrides else s

    def validate(self) -> list[str]:
        problems = []
        if self.tray_bypass and self.hardware_mode != "fake":
            problems.append("TRAY_BYPASS is for testing with the simulated dispenser only; it is refused with HARDWARE_MODE=serial")
        if self.hardware_mode not in ("fake", "serial"):
            problems.append("HARDWARE_MODE must be fake or serial")
        if self.hardware_mode == "serial" and not self.serial_port:
            problems.append("SERIAL_PORT is required when HARDWARE_MODE=serial")
        if self.camera_mode not in ("simulated", "single_laptop", "dual_camera"):
            problems.append("CAMERA_MODE must be simulated, single_laptop or dual_camera")
        if self.camera_mode == "dual_camera" and not self.tray_camera_url:
            problems.append("TRAY_CAMERA_URL is required when CAMERA_MODE=dual_camera")
        if self.gemini_thinking not in ("off", "low", "default"):
            problems.append("GEMINI_THINKING must be off, low or default")
        if self.genai_backend not in ("off", "developer_api", "google_cloud"):
            problems.append("GENAI_BACKEND must be off, developer_api or google_cloud")
        if not 0 <= self.setup_unlock_minutes <= 1440:
            problems.append("SETUP_UNLOCK_MINUTES must be 0 (stay signed in until Sign out) to 1440")
        if self.tray_vision not in ("outline", "markers"):
            problems.append("TRAY_VISION must be outline or markers")
        if self.remote_host and self.public_demo:
            problems.append("REMOTE_HOST (the real box) and PUBLIC_DEMO (the simulated site) can't be used together")
        if self.remote_host and len(self.remote_setup_code) < 8:
            problems.append("REMOTE_HOST needs REMOTE_SETUP_CODE, at least 8 characters")
        if self.public_demo:
            if self.hardware_mode != "fake" or self.camera_mode != "simulated":
                problems.append("PUBLIC_DEMO runs only with the simulated dispenser and cameras")
            if not self.public_host:
                problems.append("PUBLIC_DEMO needs PUBLIC_HOST (or RAILWAY_PUBLIC_DOMAIN), the demo's public domain")
            if not 5 <= self.demo_reset_minutes <= 1440:
                problems.append("DEMO_RESET_MINUTES must be 5 to 1440")
        elif self.host not in ("127.0.0.1", "localhost", "::1"):
            problems.append("HOST must be a loopback address; the command API is local only")
        return problems
