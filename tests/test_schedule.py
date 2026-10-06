import sqlite3
import time
from datetime import datetime, timedelta

import pytest

from tests.conftest import ET
from utils import alerts
from utils import database as db
from utils.db_queue import DBQueue
from utils.roster import build_roster
from utils.webhook import DELIVERED, PostResult


# --------------------------------------------------------------------------- migration

V1_SESSIONS = """
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, message_id TEXT,
    title TEXT NOT NULL, start_at INTEGER NOT NULL, slots INTEGER NOT NULL DEFAULT 10,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','cancelled','done')),
    created_by TEXT NOT NULL, created_at INTEGER NOT NULL, lobby_url TEXT,
    alerts_enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE signups (
    id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL, display_name TEXT NOT NULL, guest_no INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL, joined_at REAL NOT NULL, checked_in INTEGER NOT NULL DEFAULT 0,
    UNIQUE (session_id, user_id, guest_no));
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
INSERT INTO schema_migrations VALUES (1, 0);
INSERT INTO sessions (id, guild_id, channel_id, title, start_at, created_by, created_at)
    VALUES (1, '1', '2', 'Friday 10-man', 9999999999, '7', 0);
INSERT INTO signups (session_id, user_id, display_name, status, joined_at) VALUES (1, 'a', 'A', 'in', 1);
"""


def test_v1_database_upgrades_in_place_and_keeps_every_signup(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(V1_SESSIONS)
    conn.close()

    q = DBQueue(path, init=db.ensure_schema)
    q.start()
    try:
        version = q.read(lambda c: c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])
        assert version == db.LATEST_SCHEMA_VERSION
        session = q.read(db.get_session, 1)
        assert session["post_state"] == db.POSTED and session["series_id"] is None
        assert [s["user_id"] for s in q.read(db.list_signups, 1)] == ["a"]
        assert q.call(db.ensure_schema) == db.LATEST_SCHEMA_VERSION  # idempotent on restart
    finally:
        q.stop()


# --------------------------------------------------------------------------- scheduled posting

def schedule(q, start, post_at, **kw):
    return q.call(db.create_session, guild_id=1, channel_id=2, title="t", start_at=start, created_by=7,
                  post_at=post_at, **kw)


def test_scheduled_session_is_hidden_until_posted(q):
    now = time.time()
    sid = schedule(q, now + 5 * 86400, now + 86400)
    assert q.read(db.get_session, sid)["post_state"] == db.SCHEDULED
    assert q.read(db.open_sessions) == []                      # no alerts, no clicks yet
    assert [s["id"] for s in q.read(db.channel_upcoming, 2)] == [sid]
    assert q.read(db.due_posts, int(now)) == []
    assert [s["id"] for s in q.read(db.due_posts, int(now + 86400))] == [sid]


def test_claim_post_is_exclusive_and_outcomes_are_recorded(q):
    now = time.time()
    sid = schedule(q, now + 5 * 86400, now + 60)
    assert q.call(db.claim_post, sid) is not None
    assert q.call(db.claim_post, sid) is None                  # second claimer loses
    assert q.call(db.finish_post, sid, message_id=555) == db.POSTED
    session = q.read(db.get_session, sid)
    assert session["message_id"] == "555" and session in q.read(db.open_sessions)


def test_definite_post_failures_retry_then_cancel(q):
    now = time.time()
    sid = schedule(q, now + 5 * 86400, now + 60)
    for _ in range(db.MAX_POST_ATTEMPTS - 1):
        q.call(db.claim_post, sid)
        assert q.call(db.finish_post, sid, definitely_failed=True) == db.SCHEDULED
    q.call(db.claim_post, sid)
    assert q.call(db.finish_post, sid, definitely_failed=True) == db.CANCELLED


def test_crash_mid_post_is_never_reposted(q):
    now = time.time()
    sid = schedule(q, now + 5 * 86400, now + 60)
    q.call(db.claim_post, sid)
    q.call(db.recover_stale_sends)
    assert q.read(db.get_session, sid)["post_state"] == db.POSTED
    assert q.call(db.claim_post, sid) is None


def test_scheduled_limit(q):
    now = time.time()
    for i in range(3):
        schedule(q, now + (5 + i) * 86400, now + 86400, max_scheduled=3)
    with pytest.raises(db.LimitReached, match="3 scheduled"):
        schedule(q, now + 9 * 86400, now + 86400, max_scheduled=3)


# --------------------------------------------------------------------------- weekly series

def make_series(q, *, weekday=4, local_time="21:00", remaining=None, post_days=3):
    return q.call(db.create_series, guild_id=1, channel_id=2, title=None, weekday=weekday, local_time=local_time,
                  slots=10, post_days=post_days, remaining=remaining, created_by=7)


def series_sessions(q, series_id):
    return q.read(lambda c: [dict(r) for r in c.execute(
        "SELECT * FROM sessions WHERE series_id = ? ORDER BY start_at", (series_id,))])


def test_series_keeps_one_game_ahead_and_respects_remaining(q):
    series_id = make_series(q, remaining=2)
    now = datetime(2026, 10, 5, 10, 0, tzinfo=ET)               # a Monday
    assert len(q.call(db.materialize_series, now, ET)) == 1
    assert q.call(db.materialize_series, now, ET) == []         # one ahead is enough
    first = series_sessions(q, series_id)[0]
    assert datetime.fromtimestamp(first["start_at"], ET) == datetime(2026, 10, 9, 21, 0, tzinfo=ET)
    assert datetime.fromtimestamp(first["post_at"], ET) == datetime(2026, 10, 6, 21, 0, tzinfo=ET)
    assert first["title"] == "Friday 10-man"

    after_first = datetime(2026, 10, 9, 22, 0, tzinfo=ET)
    assert len(q.call(db.materialize_series, after_first, ET)) == 1
    assert q.call(db.materialize_series, datetime(2026, 10, 16, 22, 0, tzinfo=ET), ET) == []  # remaining used up
    assert len(series_sessions(q, series_id)) == 2
    assert q.read(db.get_series, series_id)["active"] == 0


def test_series_stays_at_9pm_local_across_the_dst_change(q):
    series_id = make_series(q, weekday=4)                        # Fridays; DST ends Sun 2026-11-01
    clock = datetime(2026, 10, 26, 10, 0, tzinfo=ET)
    for _ in range(3):
        q.call(db.materialize_series, clock, ET)
        clock += timedelta(days=7)
    starts = [datetime.fromtimestamp(s["start_at"], ET) for s in series_sessions(q, series_id)]
    assert [(t.month, t.day, t.hour, t.minute) for t in starts] == [(10, 30, 21, 0), (11, 6, 21, 0), (11, 13, 21, 0)]
    assert starts[1].timestamp() - starts[0].timestamp() == 7 * 86400 + 3600   # the wall clock held


def test_stop_series_cancels_future_games(q):
    series_id = make_series(q)
    q.call(db.materialize_series, datetime.now(ET), ET)
    assert q.call(db.stop_series, series_id) == 1
    assert q.call(db.materialize_series, datetime.now(ET) + timedelta(days=30), ET) == []


# --------------------------------------------------------------------------- alerts

class Recorder:
    def __init__(self):
        self.kinds = []

    def __call__(self, message, session):
        self.kinds.append(message.embed["title"])
        return PostResult(DELIVERED)


def posted_session(q, start, created):
    sid = q.call(db.create_session, guild_id=1, channel_id=2, title="Game", start_at=int(start.timestamp()),
                 created_by=7, now=int(created.timestamp()))
    q.call(db.set_status, sid, "a", "A", db.IN, now=created.timestamp())
    return sid


def test_game_day_daily_and_gameday_never_fire_together(q, cfg):
    start = datetime(2026, 10, 9, 13, 0, tzinfo=ET)              # 1 pm game: gameday alert 11 am
    posted_session(q, start, datetime(2026, 10, 5, 9, 0, tzinfo=ET))
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=datetime(2026, 10, 9, 12, 5, tzinfo=ET))  # bot was down 11-12
    assert len(send.kinds) == 1 and "Game" in send.kinds[0]


def test_late_game_gets_noon_daily_then_gameday(q, cfg):
    start = datetime(2026, 10, 9, 21, 0, tzinfo=ET)
    posted_session(q, start, datetime(2026, 10, 5, 9, 0, tzinfo=ET))
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=datetime(2026, 10, 9, 12, 0, tzinfo=ET))
    alerts.run_alerts_once(q, send, cfg, now=datetime(2026, 10, 9, 19, 0, tzinfo=ET))
    assert len(send.kinds) == 2


def test_gameday_alert_is_two_real_hours_before_across_dst(q, cfg):
    start = datetime(2026, 11, 1, 3, 0, tzinfo=ET)               # 03:00 EST, the morning DST ends
    session = {"start_at": int(start.timestamp()), "created_at": 0, "post_at": None, "status": "open",
               "post_state": "posted", "alerts_enabled": 1}
    roster = build_roster([], 10)
    due_too_early = datetime.fromtimestamp(start.timestamp() - 2 * 3600 - 60, ET)
    due_on_time = datetime.fromtimestamp(start.timestamp() - 2 * 3600, ET)
    assert all(a.kind != alerts.GAMEDAY for a in alerts.due_alerts(session, roster, due_too_early, cfg))
    assert any(a.kind == alerts.GAMEDAY for a in alerts.due_alerts(session, roster, due_on_time, cfg))


def test_starting_alert_fires_at_the_real_start_during_the_repeated_hour(q, cfg):
    # 01:30 happens twice on 2026-11-01; the game is at the first (EDT) one.
    start = datetime(2026, 11, 1, 1, 30, tzinfo=ET)
    session = {"start_at": int(start.timestamp()), "created_at": 0, "post_at": None, "status": "open",
               "post_state": "posted", "alerts_enabled": 1}
    roster = build_roster([{"id": 1, "status": "in", "joined_at": 0, "user_id": "a", "display_name": "A",
                            "guest_no": 0}], 10)
    ten_after = datetime.fromtimestamp(start.timestamp() + 600, ET)
    assert [a.kind for a in alerts.due_alerts(session, roster, ten_after, cfg)] == [alerts.STARTING]


def test_finished_alerts_cost_no_writes_on_later_passes(q, cfg):
    posted_session(q, datetime(2026, 10, 9, 21, 0, tzinfo=ET), datetime(2026, 10, 5, 9, 0, tzinfo=ET))
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=datetime(2026, 10, 6, 12, 0, tzinfo=ET))
    writes = q.stats()["jobs_done"]
    for minute in range(1, 30):
        alerts.run_alerts_once(q, send, cfg, now=datetime(2026, 10, 6, 12, minute, tzinfo=ET))
    assert q.stats()["jobs_done"] == writes
    assert len(send.kinds) == 1


def test_scheduled_session_alerts_count_from_its_post_time(q, cfg):
    created = datetime(2026, 10, 1, 9, 0, tzinfo=ET)
    sid = q.call(db.create_session, guild_id=1, channel_id=2, title="Game",
                 start_at=int(datetime(2026, 10, 9, 21, 0, tzinfo=ET).timestamp()), created_by=7,
                 now=int(created.timestamp()), post_at=int(datetime(2026, 10, 6, 21, 0, tzinfo=ET).timestamp()))
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=datetime(2026, 10, 3, 12, 0, tzinfo=ET))
    assert send.kinds == []                                       # not posted yet
    q.call(db.claim_post, sid)
    q.call(db.finish_post, sid, message_id=1)
    alerts.run_alerts_once(q, send, cfg, now=datetime(2026, 10, 6, 12, 0, tzinfo=ET))
    assert send.kinds == []                                       # noon came before it went up
    alerts.run_alerts_once(q, send, cfg, now=datetime(2026, 10, 7, 12, 0, tzinfo=ET))
    assert len(send.kinds) == 1


# --------------------------------------------------------------------------- write queue

def test_failed_commit_is_rolled_back_and_the_queue_keeps_working(q, make_session):
    def orphan_signup(conn):
        # A deferred foreign-key violation only fails at COMMIT.
        conn.execute("PRAGMA defer_foreign_keys = ON")
        conn.execute("INSERT INTO signups (session_id, user_id, display_name, status, joined_at) "
                     "VALUES (999, 'x', 'x', 'in', 0)")

    with pytest.raises(sqlite3.IntegrityError):
        q.call(orphan_signup)
    sid = make_session(time.time() + 86400)
    q.call(db.set_status, sid, "a", "A", db.IN)
    assert len(q.read(db.list_signups, sid)) == 1


def test_apply_click_rejects_foreign_messages_and_closed_games(q, make_session):
    sid = make_session(time.time() + 3600)
    q.call(db.set_message_id, sid, 555)
    with pytest.raises(db.ForeignClick):
        q.call(db.apply_click, sid, action="in", user_id="a", display_name="A", guild_id=1, message_id=999,
               max_guests=1)
    with pytest.raises(db.ForeignClick):
        q.call(db.apply_click, sid, action="in", user_id="a", display_name="A", guild_id=2, message_id=555,
               max_guests=1)
    with pytest.raises(db.SessionClosed, match="already started"):
        q.call(db.apply_click, sid, action="in", user_id="a", display_name="A", guild_id=1, message_id=555,
               max_guests=1, now=time.time() + 7200)
    change = q.call(db.apply_click, sid, action="in", user_id="a", display_name="A", guild_id=1, message_id=555,
                    max_guests=1)
    assert change["before"] == [] and len(change["after"]) == 1 and change["session"]["id"] == sid


def test_weekly_game_whose_post_time_passed_is_still_posted(q):
    # post_days 3 but the game is 2 days away: it must wait as scheduled (due now), not count as posted.
    series_id = make_series(q, weekday=4, post_days=3)
    now = datetime(2026, 10, 7, 10, 0, tzinfo=ET)                # Wednesday; Friday's post time (Tue) has passed
    sid = q.call(db.materialize_series, now, ET)[0]
    session = q.read(db.get_session, sid)
    assert session["post_state"] == db.SCHEDULED
    assert q.read(db.due_posts, int(now.timestamp()) + 1)[0]["id"] == sid
    assert series_id
