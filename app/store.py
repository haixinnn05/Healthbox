"""SQLite store. The authoritative local record. One serialized writer (a process lock
around every transaction); uniqueness constraints back up the workflow rules."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .timeutil import Clock, iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS persons (
  person_id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  consent_at TEXT NOT NULL,
  embedding_ref TEXT,
  simulated INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS token_classes (
  class_id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  cartridge TEXT UNIQUE,
  color TEXT NOT NULL,
  hsv_json TEXT NOT NULL,
  reference_image TEXT
);

CREATE TABLE IF NOT EXISTS plan_versions (
  plan_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  person_id TEXT,
  timezone TEXT NOT NULL,
  entries_json TEXT NOT NULL,
  confirmed_at TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  demo INTEGER NOT NULL DEFAULT 0,
  UNIQUE (plan_id, version)
);

CREATE TABLE IF NOT EXISTS doses (
  dose_id TEXT PRIMARY KEY,
  occurrence_id TEXT NOT NULL UNIQUE,
  slot_id TEXT NOT NULL,
  local_date TEXT NOT NULL,
  local_time TEXT NOT NULL,
  entry_key TEXT NOT NULL,
  plan_version_id INTEGER NOT NULL,
  label TEXT NOT NULL,
  strength_text TEXT,
  quantity INTEGER NOT NULL,
  class_id TEXT,
  cartridge TEXT,
  delivery_mode TEXT NOT NULL,
  due_at_utc TEXT NOT NULL,
  workflow_state TEXT NOT NULL DEFAULT 'NOT_STARTED',
  txn_id TEXT,
  outcome TEXT,
  collection TEXT NOT NULL DEFAULT 'NOT_OBSERVED',
  self_report TEXT,
  self_report_at TEXT,
  review_reason TEXT,
  demo INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  schedule TEXT NOT NULL DEFAULT 'SCHEDULED'
);
CREATE INDEX IF NOT EXISTS doses_due ON doses (demo, due_at_utc);

CREATE TABLE IF NOT EXISTS reminders (
  reminder_id TEXT PRIMARY KEY,
  occurrence_id TEXT NOT NULL UNIQUE,
  dose_id TEXT NOT NULL,
  state TEXT NOT NULL,
  due_at_utc TEXT NOT NULL,
  active_since TEXT,
  next_alert_at TEXT,
  snoozed_until TEXT,
  sound_repeat_count INTEGER NOT NULL DEFAULT 0,
  last_emitted_at TEXT,
  acknowledged_at TEXT,
  closed_at TEXT,
  close_reason TEXT,
  demo INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reminders_state ON reminders (state);

CREATE TABLE IF NOT EXISTS transactions (
  txn_id TEXT PRIMARY KEY,
  short_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  dose_ids_json TEXT NOT NULL,
  expected_json TEXT NOT NULL,
  state TEXT NOT NULL,
  controller_session TEXT,
  dispense_reserved_at TEXT,
  release_reserved_at TEXT,
  outcome TEXT,
  blocks_json TEXT,
  demo INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commands (
  command_id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT,
  session TEXT,
  txn_id TEXT,
  kind TEXT NOT NULL,
  channel TEXT,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE (txn_id, kind, channel)
);

CREATE TABLE IF NOT EXISTS requests (
  request_id TEXT PRIMARY KEY,
  route TEXT NOT NULL,
  status INTEGER NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
  obs_id INTEGER PRIMARY KEY AUTOINCREMENT,
  txn_id TEXT,
  kind TEXT NOT NULL,
  result TEXT NOT NULL,
  reason_code TEXT,
  method TEXT NOT NULL,
  capture_time TEXT,
  capture_mono REAL,
  frame_ids_json TEXT,
  tray_revision INTEGER,
  controller_session TEXT,
  plan_version_id INTEGER,
  details_json TEXT,
  simulated INTEGER NOT NULL DEFAULT 0,
  demo INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS obs_txn ON observations (txn_id, kind);

CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  dose_id TEXT,
  reminder_id TEXT,
  txn_id TEXT,
  reason_code TEXT,
  details_json TEXT,
  demo INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_dose ON events (dose_id);
CREATE INDEX IF NOT EXISTS events_kind ON events (kind, event_id);

CREATE TABLE IF NOT EXISTS label_scans (
  label_id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  source TEXT NOT NULL,
  sharpness REAL,
  fields_json TEXT,
  ai_status TEXT,
  demo INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS label_images (
  label_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  path TEXT NOT NULL,
  mime TEXT NOT NULL,
  sharpness REAL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (label_id, idx)
);

CREATE TABLE IF NOT EXISTS assistant_exchanges (
  exchange_id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  snapshot_version INTEGER,
  fact_ids_json TEXT,
  result TEXT NOT NULL,
  backend TEXT,
  model TEXT,
  question_text TEXT,
  answer_text TEXT,
  created_at TEXT NOT NULL
);
"""

DEFAULT_CLASSES = [
    # Two visible demo token classes. HSV ranges are starting points; calibrate on site.
    ("blue", "Blue", "A", "#2F6FD6", {"ranges": [[[95, 90, 60], [130, 255, 255]]]}),
    ("orange", "Orange", "B", "#E8822A", {"ranges": [[[5, 110, 90], [22, 255, 255]]]}),
]


def _entries(entries_json: str) -> list[dict]:
    """Plan entries saved before as-needed support have no schedule fields: they are SCHEDULED."""
    out = json.loads(entries_json)
    for e in out:
        e.setdefault("schedule", "SCHEDULED")
        e.setdefault("min_interval_hours", None)
        e.setdefault("max_per_day", None)
    return out


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)


class Store:
    def __init__(self, path: Path | str, clock: Clock):
        self.clock = clock
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.lock = threading.RLock()
        self._in_tx = False
        self.version = 0
        with self.lock:
            self.conn.executescript(SCHEMA)
            self._migrate()
        self._seed()

    def _migrate(self) -> None:
        """Additive changes for databases created by older versions. Each step is safe to rerun."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(doses)").fetchall()}
        if "schedule" not in cols:
            # Every dose before as-needed support came from a recurring schedule.
            self.conn.execute("ALTER TABLE doses ADD COLUMN schedule TEXT NOT NULL DEFAULT 'SCHEDULED'")
        self.conn.execute("CREATE INDEX IF NOT EXISTS doses_as_needed ON doses (schedule, entry_key, demo)")
        cmd_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(commands)").fetchall()}
        if "sent_at" not in cmd_cols:
            # When the command was actually written to the controller (reservation can be earlier,
            # e.g. cartridge B waits for A's motion). Older rows stay NULL.
            self.conn.execute("ALTER TABLE commands ADD COLUMN sent_at TEXT")
        # The local account that guards Setup (one per laptop). Older databases simply gain the
        # empty table, so Setup stays open until an account is made. password_hash holds
        # 'scrypt$n$r$p$salt$hash'; the password itself is never stored.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS account ("
            " account_id INTEGER PRIMARY KEY CHECK (account_id = 1),"
            " name TEXT NOT NULL,"
            " password_hash TEXT NOT NULL,"
            " created_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL)")
        # Who emptied the tray (RECIPIENT, OTHER_PERSON, NOT_SEEN), whether a hand-to-mouth movement was
        # seen (OBSERVED, NOT_OBSERVED), and when the "did you take it?" follow-up may show next.
        # These are camera observations only: none of them means the medicine was swallowed.
        for col in ("collected_by TEXT", "action TEXT", "followup_after TEXT"):
            if col.split()[0] not in cols:
                self.conn.execute(f"ALTER TABLE doses ADD COLUMN {col}")
        acols = {r["name"] for r in self.conn.execute("PRAGMA table_info(account)").fetchall()}
        if "first_name" not in acols:
            self.conn.execute("ALTER TABLE account ADD COLUMN first_name TEXT")  # for greetings; name is the full name
        # Handing Healthbox to someone else: the old person is archived (face removed, name kept for
        # history) and their plan versions are marked, so Today, reminders and as-needed limits ignore them.
        pcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(persons)").fetchall()}
        if "archived_at" not in pcols:
            self.conn.execute("ALTER TABLE persons ADD COLUMN archived_at TEXT")
        vcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(plan_versions)").fetchall()}
        for col in ("handed_over_at TEXT", "handed_over_name TEXT"):
            if col.split()[0] not in vcols:
                self.conn.execute(f"ALTER TABLE plan_versions ADD COLUMN {col}")
        # Estimated pills left in each cartridge. NULL count = never counted. It is an estimate:
        # set when the cartridge is filled, lowered by each finished dispense motion.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS cartridge_stock ("
            " cartridge TEXT PRIMARY KEY,"
            " count INTEGER,"
            " filled_at TEXT,"
            " fill_check_obs INTEGER,"
            " updated_at TEXT NOT NULL)")

    # -- primitives -------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            if self._in_tx:
                raise RuntimeError("nested transaction")
            self.conn.execute("BEGIN IMMEDIATE")
            self._in_tx = True
            before = self.conn.total_changes
            try:
                yield self.conn
                self.conn.execute("COMMIT")
                if self.conn.total_changes != before:
                    self.version += 1
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            finally:
                self._in_tx = False

    def q(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def q1(self, sql: str, params: tuple | list = ()) -> dict | None:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def now(self) -> str:
        return iso(self.clock.now())

    def event(self, conn: sqlite3.Connection, kind: str, *, dose_id=None, reminder_id=None,
              txn_id=None, reason=None, details=None, demo: int | None = None) -> int:
        if demo is None:
            demo = self.demo_flag(conn)
        cur = conn.execute(
            "INSERT INTO events (ts, kind, dose_id, reminder_id, txn_id, reason_code, details_json, demo)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (self.now(), kind, dose_id, reminder_id, txn_id, reason,
             dumps(details) if details is not None else None, demo),
        )
        return int(cur.lastrowid)

    def log(self, kind: str, **kw) -> int:
        with self.tx() as conn:
            return self.event(conn, kind, **kw)

    # -- meta ---------------------------------------------------------------
    def meta_get(self, key: str, default: str | None = None, conn=None) -> str | None:
        c = conn or self.conn
        with self.lock:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))

    def demo_flag(self, conn=None) -> int:
        return 1 if self.meta_get("demo_mode", "0", conn) == "1" else 0

    @property
    def demo(self) -> int:
        return self.demo_flag()

    # -- idempotency -----------------------------------------------------------
    def get_request(self, request_id: str) -> dict | None:
        return self.q1("SELECT * FROM requests WHERE request_id=?", (request_id,))

    def save_request(self, conn: sqlite3.Connection, request_id: str, route: str, status: int, response: dict) -> None:
        conn.execute("INSERT OR IGNORE INTO requests (request_id, route, status, response_json, created_at) VALUES (?,?,?,?,?)",
                     (request_id, route, status, dumps(response), self.now()))

    # -- reference data -------------------------------------------------------
    def _seed(self) -> None:
        with self.tx() as conn:
            for class_id, label, cart, color, hsv in DEFAULT_CLASSES:
                conn.execute(
                    "INSERT OR IGNORE INTO token_classes (class_id, label, cartridge, color, hsv_json) VALUES (?,?,?,?,?)",
                    (class_id, label, cart, color, dumps(hsv)),
                )

    def token_classes(self) -> list[dict]:
        rows = self.q("SELECT * FROM token_classes ORDER BY cartridge")
        for r in rows:
            r["hsv"] = json.loads(r.pop("hsv_json"))
        return rows

    def class_by_id(self) -> dict[str, dict]:
        return {c["class_id"]: c for c in self.token_classes()}

    def active_plan(self, demo: int | None = None) -> dict | None:
        demo = self.demo if demo is None else demo
        row = self.q1("SELECT * FROM plan_versions WHERE active=1 AND demo=? ORDER BY plan_version_id DESC LIMIT 1", (demo,))
        if row:
            row["entries"] = _entries(row.pop("entries_json"))
        return row

    def plan_version(self, plan_version_id: int) -> dict | None:
        row = self.q1("SELECT * FROM plan_versions WHERE plan_version_id=?", (plan_version_id,))
        if row:
            row["entries"] = _entries(row.pop("entries_json"))
        return row

    def person(self, person_id: str | None) -> dict | None:
        if not person_id:
            return None
        return self.q1("SELECT * FROM persons WHERE person_id=?", (person_id,))

    def persons(self) -> list[dict]:
        """People who use Healthbox now. Handed-over people are archived and never listed."""
        return self.q("SELECT * FROM persons WHERE archived_at IS NULL ORDER BY created_at")

    def current_person(self) -> dict | None:
        """The one person this Healthbox is for (the newest, if an older install enrolled several)."""
        return self.q1("SELECT * FROM persons WHERE archived_at IS NULL ORDER BY created_at DESC LIMIT 1")
