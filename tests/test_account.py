"""The local account: a password guards Setup only. Doses, reminders, STOP and the assistant never need it."""
import io
import json
import sqlite3
from pathlib import Path

import pytest

from conftest import rid
from test_labels import png_bytes

NAME, PW, PW2 = "Zelda Quokka", "correct horse 42", "battery staple 99"
ACCOUNT_KEYS = {"exists", "name", "first_name", "unlocked", "unlocked_until", "locked_out_until"}

# (path, body) for every Setup route. Bodies are minimal; the lock is checked before the body is read.
SETUP = [
    ("/api/enroll", {"label": "T", "consent": True}),
    ("/api/plans/confirm", {"timezone": "America/New_York", "entries": []}),
    ("/api/device/arm", {"inspected": True}),
    ("/api/device/reset", {"inspected": True}),
    ("/api/tray/calibrate", {"class_id": "blue"}),
    ("/api/tray/rename", {"class_id": "blue", "label": "Blue"}),
    ("/api/demo/mode", {"enabled": True}),
    ("/api/demo/trigger", {"entry_keys": ["demoA"]}),
    ("/api/demo/reset", {}),
    ("/api/labels/capture", {}),
    ("/api/labels/read", {"label_id": "deadbeef0000"}),
    ("/api/cartridges/check-pill", {"cartridge": "A"}),
    ("/api/cartridges/fill", {"cartridge": "A", "count": 10}),
]


def post(h, path, body=None):
    return h.c.post(path, json={"request_id": rid(), **(body or {})}, headers=h.h)


def create(h, name=NAME, password=PW, expect=200):
    return h.post("/api/account/create", {"request_id": rid(), "name": name, "password": password}, expect=expect)


def unlock(h, password=PW, expect=200, request_id=None):
    return h.post("/api/account/unlock", {"request_id": request_id or rid(), "password": password}, expect=expect)


def lock(h):
    return h.post("/api/account/lock", {"request_id": rid()})


def upload(h):
    return h.c.post("/api/labels/extract", headers=h.h, data={"request_id": rid()},
                    files={"file": ("label.png", io.BytesIO(png_bytes()), "image/png")})


def account(h):
    return h.c.get("/api/account").json()


# ---------------------------------------------------------------- create, unlock, lock
def test_no_account_keeps_setup_open(harness):
    h = harness()
    a = account(h)
    assert a == {"exists": False, "name": None, "first_name": None, "unlocked": False, "unlocked_until": None, "locked_out_until": None}
    assert h.status()["account"] == {"exists": False, "name": None, "first_name": None, "unlocked": False}
    h.post("/api/enroll", {"request_id": rid(), "label": "T", "consent": True})
    assert upload(h).status_code == 200
    h.post("/api/tray/rename", {"request_id": rid(), "class_id": "blue", "label": "Blue"})


def test_create_unlocks_and_status_shows_account(harness):
    h = harness()
    r = create(h, name="  " + NAME + "  ")
    assert r["result"] == "ok" and r["exists"] and r["name"] == NAME and r["unlocked"]
    assert ACCOUNT_KEYS <= set(r) and r["locked_out_until"] is None
    a = account(h)
    assert set(a) == ACCOUNT_KEYS and a["unlocked"] and a["unlocked_until"].startswith("2026-11-07T13:15:00")
    st = h.status()["account"]
    assert st == {"exists": True, "name": NAME, "first_name": "Zelda", "unlocked": True}


def test_duplicate_create_is_409(harness):
    h = harness()
    create(h)
    r = create(h, name="Someone else", password="another password", expect=409)
    assert r["reason_codes"] == ["ACCOUNT_EXISTS"]
    assert account(h)["name"] == NAME


@pytest.mark.parametrize("name,password,field", [
    ("   ", PW, "name"), ("x" * 61, PW, "name"), (NAME, "short7!", "password"), (NAME, "y" * 129, "password")])
def test_create_validation(harness, name, password, field):
    h = harness()
    r = h.c.post("/api/account/create", json={"request_id": rid(), "name": name, "password": password}, headers=h.h)
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == field
    assert password not in r.text
    assert account(h)["exists"] is False


def test_schema_errors_never_echo_the_password(harness):
    h = harness()
    # request_id too short: FastAPI would normally echo the whole body as "input".
    r = h.c.post("/api/account/create", json={"request_id": "x", "name": NAME, "password": PW}, headers=h.h)
    assert r.status_code == 422 and PW not in r.text and "input" not in r.text
    r = h.c.post("/api/account/unlock", json={"password": PW}, headers=h.h)
    assert r.status_code == 422 and PW not in r.text


def test_commands_need_the_session_token(harness):
    h = harness()
    r = h.c.post("/api/account/create", json={"request_id": rid(), "name": NAME, "password": PW})
    assert r.status_code == 403
    r = h.c.post("/api/account/create", json={"request_id": rid(), "name": NAME, "password": PW},
                 headers={**h.h, "origin": "http://evil.example"})
    assert r.status_code == 403
    assert account(h)["exists"] is False


def test_lock_and_unlock(harness):
    h = harness()
    create(h)
    r = lock(h)
    assert r["unlocked"] is False and r["unlocked_until"] is None
    assert h.status()["account"]["unlocked"] is False
    r = unlock(h)
    assert r["unlocked"] and r["exists"] and r["name"] == NAME
    assert h.status()["account"]["unlocked"] is True


def test_wrong_password_counts_down_then_locks_out(harness):
    h = harness()
    create(h)
    lock(h)
    for left in (4, 3, 2, 1):
        r = unlock(h, "wrong password", expect=401)
        assert r["reason_codes"] == ["WRONG_PASSWORD"] and r["attempts_left"] == left
    r = unlock(h, "wrong password", expect=401)
    assert r["attempts_left"] == 0 and r["locked_out_until"].startswith("2026-11-07T13:01:00")
    # During the lockout even the right password is refused, and nothing is unlocked.
    r = unlock(h, PW, expect=429)
    assert r["reason_codes"] == ["TOO_MANY_ATTEMPTS"] and r["locked_out_until"].startswith("2026-11-07T13:01:00")
    a = account(h)
    assert a["unlocked"] is False and a["locked_out_until"].startswith("2026-11-07T13:01:00")
    h.post("/api/account/password", {"request_id": rid(), "current_password": PW, "new_password": PW2}, expect=429)
    h.clock.advance(61)
    assert account(h)["locked_out_until"] is None
    assert unlock(h)["unlocked"]
    # A success resets the count.
    lock(h)
    assert unlock(h, "wrong password", expect=401)["attempts_left"] == 4


def test_wrong_passwords_spread_over_more_than_five_minutes_do_not_lock_out(harness):
    h = harness()
    create(h)
    lock(h)
    for _ in range(4):
        unlock(h, "wrong password", expect=401)
    h.clock.advance(301)
    assert unlock(h, "wrong password", expect=401)["attempts_left"] == 4
    assert unlock(h)["unlocked"]


def test_password_change(harness):
    h = harness()
    create(h)
    lock(h)
    r = h.post("/api/account/password", {"request_id": rid(), "current_password": "nope nope", "new_password": PW2},
               expect=401)
    assert r["attempts_left"] == 4
    r = h.post("/api/account/password", {"request_id": rid(), "current_password": PW, "new_password": "short"},
               expect=422)
    assert r["errors"][0]["field"] == "new_password"
    r = h.post("/api/account/password", {"request_id": rid(), "current_password": PW, "new_password": PW2})
    assert set(r) >= ACCOUNT_KEYS and r["unlocked"]
    lock(h)
    unlock(h, PW, expect=401)
    assert unlock(h, PW2)["unlocked"]


def test_no_account_unlock_and_password_are_409(harness):
    h = harness()
    assert unlock(h, expect=409)["reason_codes"] == ["NO_ACCOUNT"]
    h.post("/api/account/password", {"request_id": rid(), "current_password": PW, "new_password": PW2}, expect=409)
    assert lock(h)["unlocked"] is False


def test_replayed_unlock_does_not_unlock_after_lock(harness):
    h = harness()
    create(h)
    lock(h)
    rid_unlock = rid()
    assert unlock(h, request_id=rid_unlock)["unlocked"]
    lock(h)
    r = unlock(h, request_id=rid_unlock)  # a retried or replayed request
    assert r["unlocked"] is False and r["replayed"] is True
    assert post(h, "/api/tray/rename", {"class_id": "blue", "label": "Blue"}).status_code == 423
    # A request_id from another route is refused.
    r = h.post("/api/account/lock", {"request_id": rid_unlock}, expect=409)
    assert r["reason_codes"] == ["REQUEST_ID_REUSED"]


def test_lock_always_applies_even_when_replayed(harness):
    h = harness()
    create(h)
    rid_lock = rid()
    h.post("/api/account/lock", {"request_id": rid_lock})
    unlock(h)
    r = h.post("/api/account/lock", {"request_id": rid_lock})
    assert r["unlocked"] is False and account(h)["unlocked"] is False


# ---------------------------------------------------------------- protection
@pytest.mark.parametrize("path,body", SETUP)
def test_setup_routes_are_423_when_locked(harness, path, body):
    h = harness()
    create(h)
    lock(h)
    r = post(h, path, body)
    assert r.status_code == 423, (path, r.json())
    j = r.json()
    assert j["result"] == "rejected" and j["reason_codes"] == ["SETUP_LOCKED"]
    assert j["message"] == "Setup is locked. Enter your password to make changes."
    unlock(h)
    assert post(h, path, body).status_code != 423


def test_label_upload_is_423_when_locked_and_works_when_unlocked(harness):
    h = harness()
    create(h)
    lock(h)
    r = upload(h)
    assert r.status_code == 423 and r.json()["reason_codes"] == ["SETUP_LOCKED"]
    assert h.c.app.state.svc.store.q1("SELECT COUNT(*) AS n FROM label_images")["n"] == 0
    unlock(h)
    assert upload(h).status_code == 200


def test_setup_actions_succeed_when_unlocked(harness):
    h = harness()
    create(h)
    lock(h)
    unlock(h)
    h.post("/api/enroll", {"request_id": rid(), "label": "T", "consent": True})
    h.post("/api/demo/mode", {"request_id": rid(), "enabled": True})
    h.post("/api/demo/trigger", {"request_id": rid(), "entry_keys": ["demoA"]})
    h.post("/api/tray/rename", {"request_id": rid(), "class_id": "blue", "label": "Blue"})
    h.wait(lambda s: s["device"]["connected"], msg="device connected")
    h.post("/api/device/arm", {"request_id": rid(), "inspected": True})
    h.post("/api/demo/reset", {"request_id": rid()})


def test_everyday_actions_never_need_the_password(harness):
    h = harness()
    h.wait(lambda s: s["device"]["connected"], msg="device connected")
    create(h)  # unlocked: set things up first
    h.post("/api/enroll", {"request_id": rid(), "label": "T", "consent": True})
    h.post("/api/demo/mode", {"request_id": rid(), "enabled": True})
    h.post("/api/demo/trigger", {"request_id": rid(), "entry_keys": ["demoA", "demoR"]})
    h.advance(31)
    h.wait(lambda s: sum(c["reminder"]["state"] == "DUE" for c in s["today"]) == 2, msg="both due")
    h.post("/api/device/arm", {"request_id": rid(), "inspected": True})
    lock(h)
    st = h.status()
    assert st["account"]["unlocked"] is False
    rem = next(c for c in st["today"] if c["delivery_mode"] == "REMINDER_ONLY")
    dev = next(c for c in st["today"] if c["delivery_mode"] == "DEVICE")
    h.post("/api/reminders/snooze", {"request_id": rid(), "reminder_id": rem["reminder"]["reminder_id"]})
    h.post("/api/reminders/ack", {"request_id": rid(), "reminder_id": dev["reminder"]["reminder_id"]})
    t = h.start(dev)["txn_id"]
    assert h.check(t)["state"] == "EMPTY_TRAY_CHECK"
    # Ask shows the schedule, so it needs sign-in; the dose itself never does.
    r = h.post("/api/assistant", {"request_id": rid(), "question": "What is next?"}, expect=423)
    assert r["reason_codes"] == ["SIGNED_OUT"]
    # Simulator only, never guarded (the door is already closed, so this one is refused, but not locked).
    r = h.c.post("/api/sim/hardware", json={"action": "close_door"}, headers=h.h)
    assert r.status_code != 423 and r.json()["reason_codes"] != ["SETUP_LOCKED"]
    h.post("/api/sim/vision", {"face": "match"})
    h.post("/api/device/stop", {"request_id": rid()})
    h.post("/api/doses/self-report", {"request_id": rid(), "dose_id": rem["dose_id"]})
    for path in ("/api/status", "/api/account"):
        assert h.c.get(path).status_code == 200, path
    # Signed out, the person's records stay hidden.
    for path in ("/api/setup", "/api/history", "/api/metrics", "/api/export/events.csv"):
        r = h.c.get(path)
        assert r.status_code == 423 and r.json()["reason_codes"] == ["SIGNED_OUT"], path
    assert account(h)["unlocked"] is False


# ---------------------------------------------------------------- expiry and restart
def test_unlock_slides_with_each_setup_action(harness):
    h = harness(setup_unlock_minutes=15)
    create(h)
    rename = {"class_id": "blue", "label": "Blue"}
    h.clock.advance(10 * 60)
    assert post(h, "/api/tray/rename", rename).status_code == 200  # extends to +15 min from now
    assert account(h)["unlocked_until"].startswith("2026-11-07T13:25:00")
    h.clock.advance(10 * 60)  # 20 min after unlocking, 10 after the last action
    assert account(h)["unlocked"]
    # A failed Setup action does not extend the window.
    assert post(h, "/api/tray/rename", {"class_id": "blue", "label": " "}).status_code == 422
    assert account(h)["unlocked_until"].startswith("2026-11-07T13:25:00")
    # Neither do everyday actions or reads.
    h.post("/api/assistant", {"request_id": rid(), "question": "hello"})
    assert account(h)["unlocked_until"].startswith("2026-11-07T13:25:00")
    h.clock.advance(5 * 60 + 1)
    a = account(h)
    assert a["unlocked"] is False and a["unlocked_until"] is None
    assert h.status()["account"]["unlocked"] is False
    assert post(h, "/api/tray/rename", rename).status_code == 423


def test_unlock_minutes_setting(harness):
    h = harness(setup_unlock_minutes=2)
    create(h)
    h.clock.advance(119)
    assert account(h)["unlocked"]
    h.clock.advance(2)
    assert account(h)["unlocked"] is False


def test_restart_locks_setup(harness):
    h1 = harness()
    create(h1)
    assert account(h1)["unlocked"]
    h2 = harness()  # a new server on the same database
    a = account(h2)
    assert a["exists"] and a["name"] == NAME and a["unlocked"] is False
    assert post(h2, "/api/enroll", {"label": "T", "consent": True}).status_code == 423
    assert unlock(h2)["unlocked"]


# ---------------------------------------------------------------- secrets stay secret
def test_password_and_hash_never_leave_the_account_table(harness):
    h = harness()
    texts = []
    r = h.c.post("/api/account/create", json={"request_id": rid(), "name": NAME, "password": PW}, headers=h.h)
    texts.append(r.text)
    for body in ({"password": "wrong password"}, {"password": PW}):
        texts.append(h.c.post("/api/account/unlock", json={"request_id": rid(), **body}, headers=h.h).text)
    texts.append(h.c.post("/api/account/password", headers=h.h, json={
        "request_id": rid(), "current_password": PW, "new_password": PW2}).text)
    texts.append(h.c.post("/api/account/lock", json={"request_id": rid()}, headers=h.h).text)
    texts += [h.c.get(p).text for p in ("/api/account", "/api/status", "/api/setup", "/api/export/events.csv",
                                        "/api/history", "/api/export/metrics.csv")]
    store = h.c.app.state.svc.store
    stored = store.q1("SELECT password_hash FROM account")["password_hash"]
    assert stored.startswith("scrypt$16384$8$1$")
    _, salt, digest = stored.rsplit("$", 2)
    secrets_ = [PW, PW2, stored, salt, digest]
    for t in texts:
        for s in secrets_:
            assert s not in t
    tables = [r["name"] for r in store.q("SELECT name FROM sqlite_master WHERE type='table'") if r["name"] != "account"]
    dump = json.dumps({t: store.q(f"SELECT * FROM {t}") for t in tables}, default=str)
    for s in secrets_:
        assert s not in dump
    kinds = [e["kind"] for e in store.q("SELECT kind FROM events WHERE kind LIKE 'ACCOUNT%' ORDER BY event_id")]
    assert kinds == ["ACCOUNT_CREATED", "ACCOUNT_UNLOCK_FAILED", "ACCOUNT_UNLOCKED", "ACCOUNT_PASSWORD_CHANGED",
                     "ACCOUNT_LOCKED"]


def test_assistant_sees_no_account_data(harness):
    h = harness()
    create(h)
    lock(h)
    unlock(h, "wrong password", expect=401)
    unlock(h)  # Ask needs sign-in; even then it never sees account data
    svc = h.c.app.state.svc
    seen = {}

    async def fake_ask(question, snippets, facts, history):
        seen["payload"] = json.dumps({"snippets": snippets, "facts": facts, "history": history})
        return {"answer_text": "ok", "supporting_fact_ids": [], "suggested_page": None,
                "needs_more_information": False, "scope": "in_scope"}
    svc.ai.ask = fake_ask
    h.post("/api/assistant", {"request_id": rid(), "question": "Is setup locked? What is my account name?"})
    payload = seen["payload"].lower()
    assert NAME.lower() not in payload and "account" not in payload and "password" not in payload


def test_hash_format_and_verify():
    from app.account import hash_password, verify_password
    a, b = hash_password(PW), hash_password(PW)
    assert a != b  # random salt
    algo, n, r, p, salt, digest = a.split("$")
    assert (algo, n, r, p) == ("scrypt", "16384", "8", "1") and len(bytes.fromhex(salt)) == 16
    assert len(bytes.fromhex(digest)) == 32
    assert verify_password(PW, a) and not verify_password(PW2, a)
    assert not verify_password(PW, "garbage") and not verify_password(PW, "md5$1$2$3$00$00")


def test_old_database_gains_the_account_table(tmp_path):
    from datetime import datetime, timezone
    from app.store import Store
    from app.timeutil import FakeClock
    db = tmp_path / "old.db"
    s = Store(db, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    with s.tx() as conn:
        conn.execute("DROP TABLE account")
        s.event(conn, "OLD_EVENT")
    s.conn.close()
    s = Store(db, FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    cols = {r["name"] for r in s.q("PRAGMA table_info(account)")}
    assert cols == {"account_id", "name", "first_name", "password_hash", "created_at", "updated_at"}
    assert s.q1("SELECT COUNT(*) AS n FROM account")["n"] == 0
    assert s.q1("SELECT kind FROM events WHERE kind='OLD_EVENT'")
    s.conn.close()


# ---------------------------------------------------------------- reset script
def _reset_script():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "scripts" / "reset_account.py"
    spec = importlib.util.spec_from_file_location("reset_account_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_reset_script_removes_only_the_account(harness, capsys):
    h = harness()
    create(h)
    h.post("/api/enroll", {"request_id": rid(), "label": "T", "consent": True})
    db = h.c.app.state.svc.settings.db_path
    lock(h)
    script = _reset_script()
    assert script.main(["--db", str(db)], ask=lambda _: "no") == 1
    assert account(h)["exists"]
    assert script.main(["--db", str(db)], ask=lambda _: "RESET") == 0
    assert "removed" in capsys.readouterr().out
    # The running server sees no account: Setup is open again, and nothing else was lost.
    assert account(h)["exists"] is False
    assert post(h, "/api/tray/rename", {"class_id": "blue", "label": "Blue"}).status_code == 200
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='ACCOUNT_RESET'").fetchone()[0] == 1
    con.close()
    create(h, name="New name", password=PW2)
    assert account(h)["name"] == "New name"
    assert script.main(["--db", str(db / "missing.db")], ask=lambda _: "RESET") == 1


def test_every_post_route_is_classified(harness):
    """New POST routes must be put on one side deliberately."""
    from app.account import is_setup_route
    h = harness()
    posts = {r.path for r in h.c.app.routes if "POST" in getattr(r, "methods", set())}
    guarded = {p for p in posts if is_setup_route("POST", p)}
    assert guarded == {p for p, _ in SETUP} | {"/api/labels/extract"}
    assert posts - guarded == {
        "/api/account/create", "/api/account/unlock", "/api/account/lock", "/api/account/password",
        "/api/account/handover",
        "/api/reminders/snooze", "/api/reminders/ack", "/api/reminders/close",
        "/api/doses/self-report", "/api/doses/start", "/api/doses/request", "/api/doses/check",
        "/api/doses/release", "/api/doses/finish", "/api/doses/cancel", "/api/doses/confirm",
        "/api/doses/followup-later", "/api/device/stop", "/api/assistant", "/api/sim/hardware", "/api/sim/vision"}
    assert not is_setup_route("GET", "/api/labels/abc/image")
