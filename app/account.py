"""The local account. One account per laptop, for one person: a name and a password.

The password is hashed with scrypt and never leaves the laptop: it is not logged, stored in
plain text, returned or shown to the assistant.

Signed in ("unlocked"), everything is available. Signed out, only what is needed to take a due
dose is: the due dose itself, the dose steps, reminders, follow-ups and STOP (the face check
guards the dose). Medicines, history, photos, Ask and Setup need the password (SETUP_ROUTES and
SIGNED_IN_READS below). A forgotten password must never block a needed dose.

Sign-in state and the wrong-password counter live in memory only, so restarting the server
signs out. SETUP_UNLOCK_MINUTES > 0 signs out after that many minutes without a Setup change;
0 (the default) keeps you signed in until Sign out. A forgotten password is reset with scripts/reset_account.py, which needs
physical access to the laptop."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import unicodedata
from datetime import datetime, timedelta

from .errors import WorkflowError
from .timeutil import iso

# Setup actions. Everything else (STOP, doses, reminders, assistant, simulator, every GET) stays open.
SETUP_ROUTES = frozenset({
    "/api/enroll", "/api/plans/confirm",
    "/api/device/arm", "/api/device/reset",
    "/api/tray/calibrate", "/api/tray/rename",
    "/api/demo/mode", "/api/demo/trigger", "/api/demo/reset",
})
SETUP_PREFIXES = ("/api/labels/", "/api/cartridges/")  # every label POST (capture, extract, read); cartridge fills
# Signed out, these reveal the person's medicines or records, so they need sign-in too.
SIGNED_IN_POSTS = frozenset({"/api/assistant", "/api/doses/request"})
SIGNED_IN_READS = ("/api/history", "/api/export/", "/api/setup", "/api/metrics", "/api/evidence/", "/api/labels/")


def needs_sign_in(method: str, path: str) -> bool:
    """Routes that need sign-in apart from Setup changes. Taking a due dose never does."""
    if method == "POST":
        return path in SIGNED_IN_POSTS
    return method == "GET" and path.startswith(SIGNED_IN_READS)

NAME_MAX = 60
PASSWORD_MIN, PASSWORD_MAX = 8, 128
MAX_FAILURES = 5
FAILURE_WINDOW = timedelta(minutes=5)
LOCKOUT = timedelta(seconds=60)

# scrypt parameters for new hashes. Stored with each hash, so they can change later.
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN, SALT_BYTES = 2 ** 14, 8, 1, 32, 16


def is_setup_route(method: str, path: str) -> bool:
    return method == "POST" and (path in SETUP_ROUTES or path.startswith(SETUP_PREFIXES))


def _pw_bytes(password: str) -> bytes:
    return unicodedata.normalize("NFC", password).encode("utf-8")


def hash_password(password: str) -> str:
    """'scrypt$n$r$p$salt_hex$hash_hex'."""
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.scrypt(_pw_bytes(password), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.scrypt(_pw_bytes(password), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p),
                            dklen=len(expected), maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


def _check_password(password: str, field: str) -> None:
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise WorkflowError(422, "VALIDATION", f"The password must be {PASSWORD_MIN} to {PASSWORD_MAX} characters.",
                            errors=[{"field": field, "message": f"Use {PASSWORD_MIN} to {PASSWORD_MAX} characters."}])


class AccountService:
    def __init__(self, store, settings):
        self.store = store
        self.clock = store.clock
        self.unlock_for = timedelta(minutes=settings.setup_unlock_minutes) if settings.setup_unlock_minutes > 0 else None
        self._signed_in = False
        self._unlocked_until: datetime | None = None  # None while signed in = until Sign out
        self._locked_out_until: datetime | None = None
        self._failures: list[datetime] = []
        self._lock = asyncio.Lock()  # one password check at a time

    # -- reading ----------------------------------------------------------------
    def _name(self) -> str | None:
        row = self.store.q1("SELECT name FROM account WHERE account_id=1")
        return row["name"] if row else None

    def _first(self) -> str | None:
        row = self.store.q1("SELECT name, first_name FROM account WHERE account_id=1")
        return (row["first_name"] or row["name"].split()[0]) if row else None

    def _unlocked(self, exists: bool, now: datetime) -> bool:
        return exists and self._signed_in and (self._unlocked_until is None or now < self._unlocked_until)

    def _lockout(self, now: datetime) -> datetime | None:
        return self._locked_out_until if self._locked_out_until and now < self._locked_out_until else None

    def view(self) -> dict:
        name, now = self._name(), self.clock.now()
        unlocked = self._unlocked(name is not None, now)
        lockout = self._lockout(now)
        return {"exists": name is not None, "name": name, "first_name": self._first(), "unlocked": unlocked,
                "unlocked_until": iso(self._unlocked_until) if unlocked and self._unlocked_until else None,
                "locked_out_until": iso(lockout) if lockout else None}

    def brief(self) -> dict:
        """For /api/status (polled every 500 ms): one tiny query."""
        name = self._name()
        return {"exists": name is not None, "name": name, "first_name": self._first(),
                "unlocked": self._unlocked(name is not None, self.clock.now())}

    def setup_locked(self) -> bool:
        """True when an account exists and Setup is not unlocked. With no account Setup stays open
        (first run, and installs from before accounts keep working until one is made)."""
        name = self._name()
        return name is not None and not self._unlocked(True, self.clock.now())

    def signed_out(self) -> bool:
        """An account exists and nobody is signed in: show only what a due dose needs."""
        return self.setup_locked()

    def touch(self) -> None:
        """A protected Setup action succeeded: slide the sign-in window forward (when one is set)."""
        now = self.clock.now()
        if self.unlock_for and self._unlocked_until is not None and now < self._unlocked_until:
            self._unlocked_until = now + self.unlock_for

    # -- helpers ----------------------------------------------------------------------
    def _unlock_now(self) -> None:
        self._signed_in = True
        self._unlocked_until = self.clock.now() + self.unlock_for if self.unlock_for else None
        self._failures.clear()

    def _event(self, kind: str, details: dict | None = None) -> None:
        # Account events belong to the laptop, not to demo mode, and never carry the password or hash.
        self.store.log(kind, details=details, demo=0)

    async def _verify(self, password: str) -> None:
        """Check the password against the stored hash, with the wrong-password limit. Caller holds _lock."""
        now = self.clock.now()
        lockout = self._lockout(now)
        if lockout:
            raise WorkflowError(429, "TOO_MANY_ATTEMPTS", locked_out_until=iso(lockout))
        row = self.store.q1("SELECT password_hash FROM account WHERE account_id=1")
        if not row:
            raise WorkflowError(409, "NO_ACCOUNT")
        good = isinstance(password, str) and len(password) <= PASSWORD_MAX and \
            await asyncio.to_thread(verify_password, password, row["password_hash"])
        if good:
            return
        now = self.clock.now()
        self._failures = [t for t in self._failures if now - t < FAILURE_WINDOW] + [now]
        left = max(0, MAX_FAILURES - len(self._failures))
        extra: dict = {"attempts_left": left}
        if left == 0:
            self._locked_out_until = now + LOCKOUT
            self._failures.clear()
            extra["locked_out_until"] = iso(self._locked_out_until)
        self._event("ACCOUNT_UNLOCK_FAILED", {"attempts_left": left})
        raise WorkflowError(401, "WRONG_PASSWORD", **extra)

    async def _once(self, request_id: str, route: str, fn):
        """Idempotency for account commands. A replayed request_id never runs again and returns the
        CURRENT state (with replayed: true), not the cached one: replaying an old unlock after a lock
        does not unlock, and does not claim to."""
        async with self._lock:
            prev = self.store.get_request(request_id)
            if prev:
                if prev["route"] != route:
                    raise WorkflowError(409, "REQUEST_ID_REUSED")
                return dict(self.view(), replayed=True)
            result = await fn()
            with self.store.tx() as conn:
                self.store.save_request(conn, request_id, route, 200, result)
            return dict(result)

    # -- commands -----------------------------------------------------------------------
    async def create(self, request_id: str, name: str, password: str, first_name: str = "", last_name: str = "") -> dict:
        async def fn():
            if self._name() is not None:
                raise WorkflowError(409, "ACCOUNT_EXISTS")
            first = first_name.strip() if isinstance(first_name, str) else ""
            last = last_name.strip() if isinstance(last_name, str) else ""
            full = name
            if first or last:
                if not first or not last:
                    raise WorkflowError(422, "VALIDATION", "Enter your first and last name.",
                                        errors=[{"field": "first_name" if not first else "last_name", "message": "Required."}])
                full = f"{first} {last}"
            clean = " ".join((full or "").split()) if isinstance(full, str) else ""
            if not 1 <= len(clean) <= NAME_MAX:
                raise WorkflowError(422, "VALIDATION", f"Your name must be 1 to {NAME_MAX} characters.",
                                    errors=[{"field": "name", "message": f"Use 1 to {NAME_MAX} characters."}])
            _check_password(password, "password")
            hashed = await asyncio.to_thread(hash_password, password)
            now = self.store.now()
            with self.store.tx() as conn:
                if conn.execute("SELECT 1 FROM account WHERE account_id=1").fetchone():
                    raise WorkflowError(409, "ACCOUNT_EXISTS")
                conn.execute("INSERT INTO account (account_id, name, first_name, password_hash, created_at, updated_at)"
                             " VALUES (1,?,?,?,?,?)", (clean, first or None, hashed, now, now))
                self.store.event(conn, "ACCOUNT_CREATED", demo=0)
            self._locked_out_until = None
            self._unlock_now()
            return self.view()
        return await self._once(request_id, "account/create", fn)

    async def unlock(self, request_id: str, password: str) -> dict:
        async def fn():
            await self._verify(password)
            self._unlock_now()
            self._event("ACCOUNT_UNLOCKED")
            return self.view()
        return await self._once(request_id, "account/unlock", fn)

    async def lock(self, request_id: str) -> dict:
        """Locking is always safe, so it always applies, even for a replayed request_id."""
        async with self._lock:
            prev = self.store.get_request(request_id)
            if prev and prev["route"] != "account/lock":
                raise WorkflowError(409, "REQUEST_ID_REUSED")
            was = self._unlocked(True, self.clock.now())
            self._signed_in = False
            self._unlocked_until = None
            if was:
                self._event("ACCOUNT_LOCKED")
            result = self.view()
            if not prev:
                with self.store.tx() as conn:
                    self.store.save_request(conn, request_id, "account/lock", 200, result)
            return dict(result)

    async def handover(self, request_id: str, password: str, confirm_name: str, emptied: bool, wipe) -> dict:
        """Give Healthbox to someone else. Needs the password, the current name typed out, and the
        cartridges emptied (the old medicine must never reach the new person). `wipe(conn, name)` archives
        the old person inside the same transaction that removes the account; the next visit starts the
        welcome setup. History stays, labelled with the old name."""
        async def fn():
            name = self._name()
            if name is None:
                raise WorkflowError(409, "NO_ACCOUNT")
            if not emptied:
                raise WorkflowError(422, "VALIDATION", "Empty both cartridges and the tray first, and tick the box.",
                                    errors=[{"field": "emptied", "message": "Empty both cartridges and the tray first."}])
            if not isinstance(confirm_name, str) or confirm_name.strip().casefold() != name.casefold():
                raise WorkflowError(422, "VALIDATION", f"Type {name} exactly to confirm.",
                                    errors=[{"field": "confirm_name", "message": f"Type {name} to confirm."}])
            await self._verify(password)
            with self.store.tx() as conn:
                after = wipe(conn, name)
                conn.execute("DELETE FROM account WHERE account_id=1")
                self.store.event(conn, "ACCOUNT_HANDED_OVER", details={"from": name}, demo=0)
            self._signed_in = False
            self._unlocked_until = None
            self._failures.clear()
            if after:
                after()
            return self.view()
        return await self._once(request_id, "account/handover", fn)

    async def change_password(self, request_id: str, current_password: str, new_password: str) -> dict:
        async def fn():
            lockout = self._lockout(self.clock.now())
            if lockout:
                raise WorkflowError(429, "TOO_MANY_ATTEMPTS", locked_out_until=iso(lockout))
            if self._name() is None:
                raise WorkflowError(409, "NO_ACCOUNT")
            _check_password(new_password, "new_password")
            await self._verify(current_password)
            hashed = await asyncio.to_thread(hash_password, new_password)
            with self.store.tx() as conn:
                conn.execute("UPDATE account SET password_hash=?, updated_at=? WHERE account_id=1",
                             (hashed, self.store.now()))
                self.store.event(conn, "ACCOUNT_PASSWORD_CHANGED", demo=0)
            self._unlock_now()  # the current password was just proven
            return self.view()
        return await self._once(request_id, "account/password", fn)
