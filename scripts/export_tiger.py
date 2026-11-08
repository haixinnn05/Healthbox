#!/usr/bin/env python3
"""One-way copy of Healthbox reliability telemetry to Tiger Data (PostgreSQL + TimescaleDB), plus a report.

Stand-alone on purpose: the app never imports this file, the SQLite database is opened read-only, and Tiger
Data can't send anything back, so it can't affect a dose or the dispenser. SQLite stays the source of truth.

Only reliability facts are copied: times, check kinds, results, reason codes and durations. No names, medicine
labels, photos, face data or free-text details leave the laptop. Every row says whether its hardware and camera
were real, so simulated runs can't pass for real ones.

  pip install -r requirements-tiger.txt
  Put TIGER_DATABASE_URL=postgres://... in .env, then:
  .venv/bin/python scripts/export_tiger.py --data-dir data/live-demo          # copy once and print the report
  .venv/bin/python scripts/export_tiger.py --data-dir data/live-demo --watch 30
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.telemetry import dispense_cycles, reminder_delays  # noqa: E402

TABLES = ("healthbox_checks", "healthbox_cycles", "healthbox_reminders", "healthbox_events")

SCHEMA = """
CREATE TABLE IF NOT EXISTS healthbox_checks (
  time timestamptz NOT NULL, box text NOT NULL, obs_id bigint NOT NULL, txn_id text, kind text NOT NULL,
  result text NOT NULL, reason_code text, camera_simulated boolean NOT NULL, hardware text NOT NULL, demo boolean NOT NULL,
  PRIMARY KEY (time, box, obs_id));
CREATE TABLE IF NOT EXISTS healthbox_cycles (
  time timestamptz NOT NULL, box text NOT NULL, command_id bigint NOT NULL, channel text, outcome text NOT NULL,
  duration_ms integer, hardware text NOT NULL, demo boolean NOT NULL,
  PRIMARY KEY (time, box, command_id));
CREATE TABLE IF NOT EXISTS healthbox_reminders (
  time timestamptz NOT NULL, box text NOT NULL, reminder_id text NOT NULL, delay_ms integer NOT NULL, demo boolean NOT NULL,
  PRIMARY KEY (time, box, reminder_id));
CREATE TABLE IF NOT EXISTS healthbox_events (
  time timestamptz NOT NULL, box text NOT NULL, event_id bigint NOT NULL, kind text NOT NULL, reason_code text,
  hardware text NOT NULL, demo boolean NOT NULL,
  PRIMARY KEY (time, box, event_id));
"""

HOURLY = """
CREATE MATERIALIZED VIEW IF NOT EXISTS healthbox_checks_hourly WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', time) AS hour, box, kind, hardware, count(*) AS checks, count(*) FILTER (WHERE result = 'PASS') AS passed
FROM healthbox_checks GROUP BY 1, 2, 3, 4 WITH NO DATA
"""

INSERTS = {
    "healthbox_checks": "INSERT INTO healthbox_checks VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
    "healthbox_cycles": "INSERT INTO healthbox_cycles VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (time, box, command_id)"
                        " DO UPDATE SET outcome = EXCLUDED.outcome, duration_ms = EXCLUDED.duration_ms",
    "healthbox_reminders": "INSERT INTO healthbox_reminders VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
    "healthbox_events": "INSERT INTO healthbox_events VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
}

REPORT = [
    ("Dispense time by cartridge", """
SELECT hardware, channel, count(*) AS cycles, count(*) FILTER (WHERE outcome <> 'DONE') AS not_done,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)) AS p50_ms,
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)) AS p95_ms
FROM healthbox_cycles GROUP BY 1, 2 ORDER BY 1, 2"""),
    ("First-try pass rate of each dose check", """
SELECT hardware, kind, count(*) AS doses, round(100.0 * count(*) FILTER (WHERE result = 'PASS') / count(*), 1) AS first_try_pct
FROM (SELECT DISTINCT ON (box, txn_id, kind) * FROM healthbox_checks WHERE txn_id IS NOT NULL ORDER BY box, txn_id, kind, time) f
GROUP BY 1, 2 ORDER BY 1, 2"""),
    ("Why checks fail", """
SELECT hardware, kind, reason_code, count(*) AS times FROM healthbox_checks
WHERE result <> 'PASS' GROUP BY 1, 2, 3 ORDER BY times DESC LIMIT 10"""),
    ("Reminder delivery delay", """
SELECT count(*) AS reminders, round(percentile_cont(0.5) WITHIN GROUP (ORDER BY delay_ms)) AS p50_ms,
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY delay_ms)) AS p95_ms,
       count(*) FILTER (WHERE delay_ms > 60000) AS late_over_60s
FROM healthbox_reminders"""),
]


class ReadOnly:
    """Just enough of app.store.Store for app.telemetry, on a connection that cannot write."""

    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        self.conn.row_factory = sqlite3.Row

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]


class HardwareAt:
    """Which dispenser mode the app was started in at a given moment (from its APP_STARTED events)."""

    def __init__(self, ro: ReadOnly):
        rows = ro.q("SELECT ts, details_json FROM events WHERE kind = 'APP_STARTED' ORDER BY ts")
        self.times = [r["ts"] for r in rows]
        self.modes = [json.loads(r["details_json"] or "{}").get("hardware", "unknown") for r in rows]

    def __call__(self, ts: str) -> str:
        i = bisect.bisect_right(self.times, ts) - 1
        if i < 0:
            return "unknown"
        return "real" if self.modes[i] == "serial" else "simulated" if self.modes[i] == "fake" else "unknown"


def collect(db_path: Path, box: str) -> dict[str, list[tuple]]:
    """Every row to copy, as column tuples. Field by field, so nothing personal can come along by accident."""
    ro = ReadOnly(db_path)
    try:
        hw = HardwareAt(ro)
        checks = [(r["created_at"], box, r["obs_id"], r["txn_id"], r["kind"], r["result"], r["reason_code"],
                   bool(r["simulated"]), hw(r["created_at"]), bool(r["demo"]))
                  for r in ro.q("SELECT obs_id, txn_id, kind, result, reason_code, simulated, demo, created_at FROM observations")]
        cycles, reminders = [], []
        for demo in (0, 1):
            cycles += [(c["started_at_utc"], box, c["command_id"], c["channel"], c["outcome"], c["duration_ms"],
                        hw(c["started_at_utc"]), bool(demo)) for c in dispense_cycles(ro, demo, 100000) if c["started_at_utc"]]
            for d in reminder_delays(ro, demo, 100000):
                reminders.append((d["due_at_utc"], box, d["reminder_id"], d["delay_ms"], bool(demo)))
        events = [(r["ts"], box, r["event_id"], r["kind"], r["reason_code"], hw(r["ts"]), bool(r["demo"]))
                  for r in ro.q("SELECT event_id, ts, kind, reason_code, demo FROM events")]
        return {"healthbox_checks": checks, "healthbox_cycles": cycles, "healthbox_reminders": reminders, "healthbox_events": events}
    finally:
        ro.conn.close()


def scrub(text: str) -> str:
    """Connection errors can quote the URL; never print a password."""
    return re.sub(r"(postgres(?:ql)?://[^:/@\s]+):[^@\s]+@", r"\1:***@", str(text)).splitlines()[0][:300]


def prepare(pg) -> bool:
    """Create tables. Returns whether TimescaleDB features are on."""
    pg.execute(SCHEMA)
    try:
        pg.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        for table in TABLES:
            pg.execute("SELECT create_hypertable(%s, 'time', if_not_exists => TRUE, migrate_data => TRUE)", (table,))
        pg.execute(HOURLY)
        return True
    except Exception as exc:
        print(f"WARNING: TimescaleDB features are off ({type(exc).__name__}: {scrub(exc)})")
        return False


def push(pg, rows: dict[str, list[tuple]]) -> None:
    with pg.cursor() as cur:
        for table, values in rows.items():
            if values:
                cur.executemany(INSERTS[table], values)


def report(pg, timescale: bool) -> None:
    sections = list(REPORT)
    if timescale:
        pg.execute("CALL refresh_continuous_aggregate('healthbox_checks_hourly', NULL, NULL)")
        sections.append(("Checks per hour (continuous aggregate)",
                         "SELECT hour, hardware, kind, checks, passed FROM healthbox_checks_hourly ORDER BY hour DESC, kind LIMIT 12"))
    for title, sql in sections:
        cur = pg.execute(sql)
        names = [d.name for d in cur.description]
        print(f"\n{title}\n  " + " | ".join(names))
        for row in cur.fetchall():
            print("  " + " | ".join("-" if v is None else str(v) for v in row))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--box", help="name for this box in Tiger Data (default: the data folder's name)")
    ap.add_argument("--watch", type=int, metavar="SECONDS", help="keep copying every SECONDS seconds")
    args = ap.parse_args()
    data_dir = Path(args.data_dir).resolve()
    db = data_dir / "doseguard.db"
    if not db.exists():
        print(f"No database at {db}")
        return 1
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    url = os.environ.get("TIGER_DATABASE_URL", "").strip()
    if not url:
        print("Set TIGER_DATABASE_URL in .env (the connection string from your Tiger Data service).")
        return 1
    try:
        import psycopg
    except ImportError:
        print("Install the driver first: .venv/bin/pip install -r requirements-tiger.txt")
        return 1
    box = args.box or data_dir.name
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=15) as pg:
            timescale = prepare(pg)
            while True:
                rows = collect(db, box)
                push(pg, rows)
                print(f"Copied to Tiger Data: " + ", ".join(f"{len(v)} {k.split('_')[1]}" for k, v in rows.items()))
                if not args.watch:
                    report(pg, timescale)
                    return 0
                time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0
    except psycopg.Error as exc:
        print(f"Tiger Data error: {type(exc).__name__}: {scrub(exc)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
