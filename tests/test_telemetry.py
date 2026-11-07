"""Reminder delivery delay and dispense cycle duration, measured from the local event store."""
from conftest import rid


def test_metrics_measure_reminder_delay_and_dispense_cycle(harness):
    h = harness()
    card = h.demo_due()  # the clock jumps 31 s past the trigger, so the reminder fires about 1 s late
    h.to_dose_check(card)
    m = h.c.get("/api/metrics").json()
    assert m["demo"] is True and m["simulated_hardware"] is True
    rd = m["reminder_delay"]
    assert rd["count"] == 1 and rd["median_ms"] is not None and 0 <= rd["median_ms"] < 60000
    a = m["dispense_cycles"]["A"]
    assert a["commands"] == 1 and a["count"] == 1 and a["outcomes"] == {"DONE": 1}
    assert a["median_ms"] is not None and a["median_ms"] >= 0
    assert m["dispense_cycles"]["B"]["commands"] == 0


def test_metrics_csv_lists_both_series_separately(harness):
    h = harness()
    card = h.demo_due()
    h.to_dose_check(card)
    csv = h.c.get("/api/export/metrics.csv").text.strip().splitlines()
    assert csv[0].startswith("metric,id,channel")
    kinds = [line.split(",")[0] for line in csv[1:]]
    assert kinds.count("reminder_delivery_delay") == 1 and kinds.count("dispense_cycle_duration") == 1


def test_metrics_record_a_failed_cycle_without_a_duration(harness):
    h = harness(fake_motion_s_per_unit=0.5)
    card = h.demo_due()
    t = h.start(card)["txn_id"]
    h.check(t)
    h.sim(action="feed_mode", channel="A", mode="stall")
    h.check(t)
    h.wait(lambda s: s["workflow"]["txn"]["state"] == "UNCERTAIN", msg="stall makes the outcome uncertain")
    a = h.c.get("/api/metrics").json()["dispense_cycles"]["A"]
    assert a["commands"] == 1 and a["count"] == 0 and "DONE" not in a["outcomes"]


def test_metrics_are_read_only(harness):
    h = harness()
    before = h.status()["state_version"]
    h.c.get("/api/metrics")
    h.c.get("/api/export/metrics.csv")
    assert h.status()["state_version"] == before
    # and they are plain GETs: no command route, no token needed
    assert h.c.post("/api/metrics", json={"request_id": rid()}).status_code in (403, 405)


def test_second_cartridge_is_timed_from_its_own_send_not_the_reservation(tmp_path):
    # A and B are reserved together at T0; B is only written to the controller after A's motion.
    from app import telemetry
    from app.store import Store
    from app.timeutil import Clock
    store = Store(tmp_path / "m.db", Clock())
    t0 = "2026-11-07T20:00:00.000+00:00"
    store.conn.execute("INSERT INTO transactions (txn_id, short_id, request_id, dose_ids_json, expected_json, state, demo,"
                       " created_at, updated_at) VALUES ('x', 'x1', 'r', '[]', '{}', 'DOSE_CHECK', 0, ?, ?)", (t0, t0))
    rows = [("A", t0, "2026-11-07T20:00:00.100+00:00", "2026-11-07T20:00:00.900+00:00"),
            ("B", t0, "2026-11-07T20:00:01.000+00:00", "2026-11-07T20:00:01.500+00:00")]
    for ch, created, sent, done in rows:
        store.conn.execute("INSERT INTO commands (txn_id, kind, channel, payload_json, state, created_at, sent_at, completed_at)"
                           " VALUES ('x', 'DISPENSE', ?, '{}', 'DONE', ?, ?, ?)", (ch, created, sent, done))
    # An older row without sent_at gets no duration rather than a wrong one.
    store.conn.execute("INSERT INTO transactions (txn_id, short_id, request_id, dose_ids_json, expected_json, state, demo,"
                       " created_at, updated_at) VALUES ('y', 'y1', 'r2', '[]', '{}', 'COMPLETE', 0, ?, ?)", (t0, t0))
    store.conn.execute("INSERT INTO commands (txn_id, kind, channel, payload_json, state, created_at, completed_at)"
                       " VALUES ('y', 'DISPENSE', 'B', '{}', 'DONE', ?, '2026-11-07T20:00:09.000+00:00')", (t0,))
    m = telemetry.metrics(store, simulated=True)["dispense_cycles"]
    assert m["A"]["median_ms"] == 800
    assert m["B"]["count"] == 1 and m["B"]["median_ms"] == 500 and m["B"]["commands"] == 2


def test_sent_at_is_recorded_when_a_dispense_is_written(harness):
    h = harness()
    card = h.demo_due()
    h.to_dose_check(card)
    cycles = h.c.get("/api/metrics").json()["recent_cycles"]
    assert cycles and all(c["started_at_utc"] for c in cycles)
