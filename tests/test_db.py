import time

import pytest

from utils import database as db
from utils.roster import build_roster


def test_schema_is_versioned_and_reopen_is_idempotent(q):
    version = q.read(lambda c: c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])
    assert version == db.LATEST_SCHEMA_VERSION
    assert q.call(db.ensure_schema) == db.LATEST_SCHEMA_VERSION


def test_newer_database_is_refused(q):
    q.call(lambda c: c.execute("INSERT INTO schema_migrations VALUES (999, 0)"))
    with pytest.raises(db.SchemaVersionError):
        q.call(db.ensure_schema)


def test_reclicking_in_keeps_your_place(q, make_session):
    sid = make_session(time.time() + 86400)
    q.call(db.set_status, sid, "a", "A", db.IN, now=1)
    q.call(db.set_status, sid, "b", "B", db.IN, now=2)
    q.call(db.set_status, sid, "a", "A", db.IN, now=3)
    assert [s["user_id"] for s in q.read(db.list_signups, sid)] == ["a", "b"]


def test_out_then_in_goes_to_the_back(q, make_session):
    sid = make_session(time.time() + 86400)
    q.call(db.set_status, sid, "a", "A", db.IN, now=1)
    q.call(db.set_status, sid, "b", "B", db.IN, now=2)
    q.call(db.set_status, sid, "a", "A", db.OUT, now=3)
    q.call(db.set_status, sid, "a", "A", db.IN, now=4)
    roster = build_roster(q.read(db.list_signups, sid), 10)
    assert [s["user_id"] for s in roster.confirmed] == ["b", "a"]


def test_dropout_promotes_waitlist_head(q, make_session):
    sid = make_session(time.time() + 86400, slots=2)
    for i, u in enumerate("abc"):
        q.call(db.set_status, sid, u, u, db.IN, now=i)
    q.call(db.set_status, sid, "a", "a", db.OUT, now=10)
    roster = build_roster(q.read(db.list_signups, sid), 2)
    assert [s["user_id"] for s in roster.confirmed] == ["b", "c"]
    assert roster.waitlist == []


def test_guest_requires_sponsor_in_and_respects_limit(q, make_session):
    sid = make_session(time.time() + 86400)
    with pytest.raises(ValueError):
        q.call(db.add_guest, sid, "a", "A")
    q.call(db.set_status, sid, "a", "A", db.IN, now=1)
    q.call(db.add_guest, sid, "a", "A", now=2)
    with pytest.raises(ValueError):
        q.call(db.add_guest, sid, "a", "A", max_guests=1)
    roster = build_roster(q.read(db.list_signups, sid), 10)
    assert [s["display_name"] for s in roster.confirmed] == ["A", "A's +1"]


def test_going_out_drops_your_guests(q, make_session):
    sid = make_session(time.time() + 86400)
    q.call(db.set_status, sid, "a", "A", db.IN)
    q.call(db.add_guest, sid, "a", "A")
    q.call(db.set_status, sid, "a", "A", db.OUT)
    assert build_roster(q.read(db.list_signups, sid), 10).count == 0


def test_cancelled_session_refuses_signups(q, make_session):
    sid = make_session(time.time() + 86400)
    q.call(db.set_session_status, sid, db.CANCELLED)
    with pytest.raises(db.SessionClosed):
        q.call(db.set_status, sid, "a", "A", db.IN)


def test_alert_claim_is_exclusive_and_failed_retries_are_bounded(q, make_session):
    sid = make_session(time.time() + 86400)
    assert q.call(db.claim_alert, sid, "daily", "2026-10-05") is True
    assert q.call(db.claim_alert, sid, "daily", "2026-10-05") is False  # in flight
    for _ in range(db.MAX_ALERT_ATTEMPTS - 1):
        q.call(db.finish_alert, sid, "daily", "2026-10-05", db.ALERT_FAILED, "HTTP 500")
        assert q.call(db.claim_alert, sid, "daily", "2026-10-05") is True
    q.call(db.finish_alert, sid, "daily", "2026-10-05", db.ALERT_FAILED, "HTTP 500")
    assert q.call(db.claim_alert, sid, "daily", "2026-10-05") is False


def test_crash_mid_send_becomes_ambiguous_and_is_never_resent(q, make_session):
    sid = make_session(time.time() + 86400)
    q.call(db.claim_alert, sid, "daily", "d")
    assert q.call(db.recover_stale_sends) == 1
    assert q.read(db.alert_state, sid, "daily", "d")["status"] == db.ALERT_AMBIGUOUS
    assert q.call(db.claim_alert, sid, "daily", "d") is False


def test_channel_sessions_are_numbered_soonest_first_and_scoped_to_channel(q):
    now = time.time()
    friday = q.call(db.create_session, guild_id=1, channel_id=2, title="Fri", start_at=now + 5 * 86400, created_by=7)
    tonight = q.call(db.create_session, guild_id=1, channel_id=2, title="Tonight", start_at=now + 3600, created_by=7)
    q.call(db.create_session, guild_id=1, channel_id=3, title="Elsewhere", start_at=now + 60, created_by=7)
    assert [s["id"] for s in q.read(db.channel_open_sessions, 2)] == [tonight, friday]


def test_cancel_if_open_only_cancels_once(q, make_session):
    sid = make_session(time.time() + 86400)
    assert q.call(db.cancel_if_open, sid) is True
    assert q.call(db.cancel_if_open, sid) is False
    assert q.read(db.channel_open_sessions, 2) == []


def test_open_signup_limits_are_enforced_atomically(q):
    import threading
    now = time.time()
    errors = []
    barrier = threading.Barrier(10)

    def start(n):
        barrier.wait()
        try:
            q.call(db.create_session, guild_id=1, channel_id=2, title=f"t{n}", start_at=now + 3600,
                   created_by=n, max_per_channel=5, max_per_user=2)
        except db.LimitReached as exc:
            errors.append(exc)

    threads = [threading.Thread(target=start, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(q.read(db.channel_open_sessions, 2)) == 5
    assert len(errors) == 5


def test_per_user_limit(q):
    now = time.time()
    for _ in range(2):
        q.call(db.create_session, guild_id=1, channel_id=2, title="t", start_at=now + 3600, created_by=7,
               max_per_user=2)
    with pytest.raises(db.LimitReached, match="you already have 2"):
        q.call(db.create_session, guild_id=1, channel_id=9, title="t", start_at=now + 3600, created_by=7,
               max_per_user=2)


@pytest.mark.parametrize("title, slots", [("", 10), ("x" * 81, 10), ("ok", 1), ("ok", 41)])
def test_create_session_backstop_rejects_bad_values(q, title, slots):
    with pytest.raises(ValueError):
        q.call(db.create_session, guild_id=1, channel_id=2, title=title, start_at=time.time() + 60,
               created_by=7, slots=slots)


def test_going_maybe_also_drops_your_guests(q, make_session):
    sid = make_session(time.time() + 86400)
    q.call(db.set_status, sid, "a", "A", db.IN)
    q.call(db.add_guest, sid, "a", "A")
    q.call(db.set_status, sid, "a", "A", db.MAYBE)
    roster = build_roster(q.read(db.list_signups, sid), 10)
    assert roster.count == 0 and [r["display_name"] for r in roster.maybe] == ["A"]
