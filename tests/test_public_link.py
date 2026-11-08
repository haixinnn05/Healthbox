"""The public link to the real box: each browser signs in for itself, and nothing is open without it."""
from conftest import rid

HOST = "abc-def.trycloudflare.com"
CODE = "a1b2c3d4"
PASSWORD = "correct-horse-battery"


def link(harness):
    return harness(remote_host=HOST, remote_setup_code=CODE)


def visitor(h, cookie=None):
    headers = {"host": HOST, "origin": f"https://{HOST}", **h.h}
    if cookie:
        headers["cookie"] = f"healthbox_session={cookie}"
    return headers


def send(h, path, body=None, cookie=None):
    h.c.cookies.clear()
    return h.c.post(path, json={"request_id": rid(), **(body or {})}, headers=visitor(h, cookie))


def get(h, path, cookie=None):
    h.c.cookies.clear()
    return h.c.get(path, headers=visitor(h, cookie))


def make_account(h):
    r = send(h, "/api/account/create", {"first_name": "Avery", "last_name": "Test", "password": PASSWORD, "setup_code": CODE})
    assert r.status_code == 200, r.text
    return r.cookies.get("healthbox_session") or r.headers["set-cookie"].split("healthbox_session=")[1].split(";")[0]


def test_creating_the_account_over_the_link_needs_the_code(harness):
    h = link(harness)
    assert get(h, "/api/status").json()["account"]["needs_code"] is True
    r = send(h, "/api/account/create", {"first_name": "A", "last_name": "B", "password": PASSWORD, "setup_code": "wrong"})
    assert r.status_code == 403 and r.json()["reason_codes"] == ["SETUP_CODE"]
    assert h.status()["account"]["exists"] is False


def test_session_cookie_is_locked_down(harness):
    h = link(harness)
    r = send(h, "/api/account/create", {"first_name": "A", "last_name": "B", "password": PASSWORD, "setup_code": CODE})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie


def test_visitor_without_sign_in_reaches_nothing(harness):
    h = link(harness)
    token = make_account(h)
    h.c.post("/api/enroll", json={"request_id": rid(), "label": "x", "consent": True}, headers={**visitor(h, token)})
    for path in ("/api/camera/frame", "/api/camera/stream", "/api/camera/live", "/api/setup", "/api/history", "/api/practice"):
        assert get(h, path).status_code == 423, path
    for path in ("/api/doses/start", "/api/doses/release", "/api/doses/check", "/api/device/stop", "/api/device/arm",
                 "/api/sim/hardware", "/api/speak", "/api/enroll", "/api/assistant"):
        assert send(h, path).status_code == 423, path
    st = get(h, "/api/status").json()
    assert st["today"] == [] and st["workflow"]["txn"] is None and st["plan"] is None and st["signed_out"] is True
    assert st["account"]["name"] is None and st["account"]["first_name"] is None


def test_each_browser_signs_in_for_itself(harness):
    h = link(harness)
    a = make_account(h)
    assert get(h, "/api/status", a).json()["account"]["unlocked"] is True
    assert get(h, "/api/status").json()["account"]["unlocked"] is False  # another browser
    assert send(h, "/api/account/unlock", {"password": "wrong-password"}).status_code == 401
    b = send(h, "/api/account/unlock", {"password": PASSWORD}).headers["set-cookie"].split("healthbox_session=")[1].split(";")[0]
    assert b != a
    out = send(h, "/api/account/lock", {}, a)
    assert out.status_code == 200 and "healthbox_session" in out.headers["set-cookie"]
    assert get(h, "/api/status", a).json()["account"]["unlocked"] is False
    assert get(h, "/api/status", b).json()["account"]["unlocked"] is True


def test_signing_in_over_the_link_does_not_sign_in_the_laptop(harness):
    h = link(harness)
    make_account(h)
    h.c.cookies.clear()
    assert h.status()["account"]["unlocked"] is False  # 127.0.0.1


def test_tunnel_header_counts_as_the_link_even_with_a_local_host(harness):
    h = link(harness)
    r = h.c.get("/api/history", headers={"cf-connecting-ip": "203.0.113.9"})
    assert r.status_code == 423


def test_laptop_keeps_its_own_rules(harness):
    h = link(harness)
    r = h.post("/api/account/create", {"request_id": rid(), "first_name": "A", "last_name": "B", "password": PASSWORD})
    assert r["unlocked"] is True and h.status()["account"]["unlocked"] is True


def test_handover_ends_every_session(harness):
    h = link(harness)
    a = make_account(h)
    send(h, "/api/enroll", {"label": "x", "consent": True}, a)
    r = send(h, "/api/account/handover", {"password": PASSWORD, "confirm_name": "Avery Test", "emptied": True}, a)
    assert r.status_code == 200, r.text
    assert get(h, "/api/history", a).status_code == 423


def test_link_needs_a_code(harness, tmp_path):
    from app.config import Settings
    assert any("REMOTE_SETUP_CODE" in p for p in Settings.from_env(remote_host=HOST, remote_setup_code="").validate())
