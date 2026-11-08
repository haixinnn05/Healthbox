"""Reminder sound played by the server through this laptop's speakers.

Browsers only play sound after someone clicks the page, and forget that after every reload, so a
reminder could stay silent. Healthbox runs on the same laptop as its page, so the server plays the
chime itself with macOS `afplay`: no click needed, and it works with the tab in the background or
the page closed. It also reads the laptop's volume (osascript) so the page can warn when the
laptop is muted or nearly silent. Off on other systems and in tests (LAPTOP_SOUND=0)."""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("doseguard.sound")

QUIET_VOLUME = 15  # percent; below this a reminder is easy to miss


class LaptopSound:
    def __init__(self, enabled: bool, sound_file: str, repeats: int = 2):
        self.file = Path(sound_file)
        self.repeats = max(1, repeats)
        self.enabled = bool(enabled) and sys.platform == "darwin" and shutil.which("afplay") is not None and self.file.exists()
        self._state: dict = {"muted": None, "volume": None}
        self._checked = 0.0
        self._checking = False
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.played = 0

    # -- playing ---------------------------------------------------------------
    def play(self) -> bool:
        """Play the chime `repeats` times without blocking the caller."""
        if not self.enabled:
            return False
        threading.Thread(target=self._play, name="reminder-sound", daemon=True).start()
        self.played += 1
        return True

    def _play(self) -> None:
        for _ in range(self.repeats):
            try:
                subprocess.run(["afplay", str(self.file)], timeout=10, check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.last_error = None
            except Exception as exc:  # never let a sound problem touch reminders
                self.last_error = f"{type(exc).__name__}"
                log.warning("reminder sound failed: %s", exc)
                return
            time.sleep(0.15)

    # -- laptop volume ----------------------------------------------------------------
    def _refresh(self) -> None:
        try:
            out = subprocess.run(["osascript", "-e", "get volume settings"], capture_output=True, text=True, timeout=3).stdout
            parts = dict(p.strip().split(":", 1) for p in out.split(",") if ":" in p)
            vol = parts.get("output volume", "").strip()
            muted = parts.get("output muted", "").strip()
            state = {"volume": int(vol) if vol.isdigit() else None,
                     "muted": True if muted == "true" else False if muted == "false" else None}
        except Exception:
            state = {"muted": None, "volume": None}
        with self._lock:
            self._state, self._checked, self._checking = state, time.monotonic(), False

    def status(self) -> dict:
        """Cheap: answers from a cache refreshed in the background at most every 10 seconds."""
        if not self.enabled:
            return {"enabled": False}
        with self._lock:
            stale = time.monotonic() - self._checked > 10 and not self._checking
            if stale:
                self._checking = True
            state = dict(self._state)
        if stale:
            threading.Thread(target=self._refresh, name="volume-check", daemon=True).start()
        vol, muted = state["volume"], state["muted"]
        return {"enabled": True, "muted": muted, "volume": vol,
                "quiet": vol is not None and vol < QUIET_VOLUME, "error": self.last_error, "played": self.played}
