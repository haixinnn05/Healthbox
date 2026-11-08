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
    tray_max_age_s: float = 15.0
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
    gemini_model: str = "gemini-2.5-flash"
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
    setup_unlock_minutes: int = 15

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        _load_dotenv()
        data_dir = Path(_str("DATA_DIR", str(ROOT / "data")))
        s = cls(
            host=_str("HOST", "127.0.0.1"),
            port=_int("PORT", 8000),
            data_dir=data_dir,
            db_path=Path(_str("DB_PATH", str(data_dir / "doseguard.db"))),
            timezone=_str("TIMEZONE", "America/New_York"),
            hardware_mode=_str("HARDWARE_MODE", "fake").lower(),
            serial_port=_str("SERIAL_PORT", ""),
            serial_baud=_int("SERIAL_BAUD", 115200),
            camera_mode=_str("CAMERA_MODE", "simulated").lower(),
            face_camera_index=_int("FACE_CAMERA_INDEX", 0),
            tray_camera_url=_str("TRAY_CAMERA_URL", ""),
            face_max_age_s=_float("FACE_MAX_AGE_S", 2.0),
            tray_max_age_s=_float("TRAY_MAX_AGE_S", 15.0),
            face_match_threshold=_float("FACE_MATCH_THRESHOLD", 0.363),
            tray_token_area_px=_int("TRAY_TOKEN_AREA_PX", 900),
            snooze_minutes=_int("SNOOZE_MINUTES", 5),
            sound_interval_s=_int("SOUND_INTERVAL_S", 60),
            sound_max=_int("SOUND_MAX", 3),
            needs_review_minutes=_int("NEEDS_REVIEW_MINUTES", 15),
            genai_backend=_str("GENAI_BACKEND", "off").lower(),
            gemini_model=_str("GEMINI_MODEL", "gemini-2.5-flash"),
            gemini_api_key=_str("GEMINI_API_KEY", _str("GOOGLE_API_KEY", "")),
            google_cloud_project=_str("GOOGLE_CLOUD_PROJECT", ""),
            google_cloud_location=_str("GOOGLE_CLOUD_LOCATION", "us-central1"),
            ai_request_limit=_int("AI_REQUEST_LIMIT", 100),
            require_label_photo=_str("REQUIRE_LABEL_PHOTO", "1") not in ("0", "false", "no"),
            setup_unlock_minutes=_int("SETUP_UNLOCK_MINUTES", 15),
        )
        return replace(s, **overrides) if overrides else s

    def validate(self) -> list[str]:
        problems = []
        if self.hardware_mode not in ("fake", "serial"):
            problems.append("HARDWARE_MODE must be fake or serial")
        if self.hardware_mode == "serial" and not self.serial_port:
            problems.append("SERIAL_PORT is required when HARDWARE_MODE=serial")
        if self.camera_mode not in ("simulated", "single_laptop", "dual_camera"):
            problems.append("CAMERA_MODE must be simulated, single_laptop or dual_camera")
        if self.camera_mode == "dual_camera" and not self.tray_camera_url:
            problems.append("TRAY_CAMERA_URL is required when CAMERA_MODE=dual_camera")
        if self.genai_backend not in ("off", "developer_api", "google_cloud"):
            problems.append("GENAI_BACKEND must be off, developer_api or google_cloud")
        if not 1 <= self.setup_unlock_minutes <= 1440:
            problems.append("SETUP_UNLOCK_MINUTES must be between 1 and 1440")
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            problems.append("HOST must be a loopback address; the command API is local only")
        return problems
