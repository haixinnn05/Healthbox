"""The Tiger Data copy is one-way, carries no personal data, and labels simulated runs."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from conftest import rid

spec = importlib.util.spec_from_file_location("export_tiger", Path(__file__).parent.parent / "scripts" / "export_tiger.py")
tiger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tiger)


def finished_dose(h):
    card = h.demo_due()
    t = h.to_dose_check(card)
    h.check(t)
    h.post("/api/doses/release", {"request_id": rid(), "txn_id": t})
    h.wait(lambda s: s["workflow"]["txn"]["state"] == "COLLECTION_CHECK", msg="released")
    return card


def test_rows_are_complete_and_labelled_simulated(harness, tmp_path):
    finished_dose(harness())
    rows = tiger.collect(tmp_path / "t.db", "bench")
    assert {r[4] for r in rows["healthbox_checks"]} >= {"FACE_MATCH", "TRAY_EMPTY", "CLASS_COUNTS"}
    assert rows["healthbox_cycles"] and all(r[6] == "simulated" for r in rows["healthbox_cycles"])
    assert all(r[7] is True and r[8] == "simulated" for r in rows["healthbox_checks"])
    assert rows["healthbox_reminders"] and rows["healthbox_events"]


def test_nothing_personal_is_copied(harness, tmp_path):
    h = harness()
    h.post("/api/account/create", {"request_id": rid(), "first_name": "Zebulon", "last_name": "Quagmire", "password": "correct-horse-1"})
    finished_dose(h)
    text = repr(tiger.collect(tmp_path / "t.db", "bench"))
    for secret in ("Zebulon", "Quagmire", "Demo token", "evidence/", "similarities"):
        assert secret not in text


def test_sqlite_is_opened_read_only(harness, tmp_path):
    harness()
    ro = tiger.ReadOnly(tmp_path / "t.db")
    with pytest.raises(sqlite3.OperationalError):
        ro.conn.execute("DELETE FROM events")


def test_hardware_mode_follows_each_app_start():
    class Log:
        def q(self, *_):
            return [{"ts": "2026-11-08T01:00", "details_json": '{"hardware": "fake"}'},
                    {"ts": "2026-11-08T02:00", "details_json": '{"hardware": "serial"}'}]
    at = tiger.HardwareAt(Log())
    assert (at("2026-11-08T00:30"), at("2026-11-08T01:30"), at("2026-11-08T02:30")) == ("unknown", "simulated", "real")


def test_password_is_never_printed():
    assert "hunter2" not in tiger.scrub("could not connect to postgres://user:hunter2@host:5432/db\nmore")


def test_app_never_imports_the_export():
    for f in (Path(__file__).parent.parent / "app").glob("*.py"):
        assert "export_tiger" not in f.read_text()
