"""
/tenman edit and /tenman update at the database layer: utils/database.py
edit_session / update_series, on a fixed clock (Monday 2026-10-05, ET).
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from tests.conftest import ET
from utils import alerts
from utils import database as db
from utils.db_queue import DBQueue
from utils.roster import build_roster
from utils.webhook import DELIVERED, PostResult

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=ET)                 # Monday
FRIDAY = datetime(2026, 10, 9, 21, 0, tzinfo=ET)
SATURDAY = datetime(2026, 10, 10, 21, 0, tzinfo=ET)


def ts(when: datetime) -> int:
    return int(when.timestamp())


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=ET)


class Recorder:
    def __init__(self):
        self.kinds = []

    def __call__(self, message, session):
        self.kinds.append(message.embed["title"])
        return PostResult(DELIVERED)


def edit(q, sid, now=NOW, **changes):
    return q.call(db.edit_session, sid, tz=ET, now=ts(now), **changes)


def scheduled(q, start=FRIDAY, post_days=3, series_id=None, title="Friday 10-man"):
    return q.call(db.create_session, guild_id=1, channel_id=2, title=title, start_at=ts(start), created_by=7,
                  now=ts(NOW), post_at=ts(db.post_time(start, post_days)), series_id=series_id)


def posted(q, start=FRIDAY, players=0, slots=10, title="Friday 10-man"):
    sid = q.call(db.create_session, guild_id=1, channel_id=2, title=title, start_at=ts(start), created_by=7,
                 slots=slots, now=ts(NOW) - 3600)
    for i in range(players):
        q.call(db.set_status, sid, f"u{i}", f"user{i}", db.IN, now=ts(NOW) - 3000 + i)
    return sid


def alert_rows(q, sid):
    return {(r["kind"], r["slot_key"]): r["status"] for r in
            q.read(lambda c: [dict(x) for x in c.execute("SELECT * FROM alerts WHERE session_id = ?", (sid,))])}


# --------------------------------------------------------------------------- scheduled games

def test_scheduled_time_change_recomputes_post_at_and_default_title(q):
    sid = scheduled(q, post_days=3)
    result = edit(q, sid, start_at=ts(SATURDAY))
    session = result["session"]
    assert not result["posted"]
    assert session["start_at"] == ts(SATURDAY)
    assert session["post_at"] == ts(at(7, 21))                  # still 3 days before, at the game's time
    assert session["title"] == "Saturday 10-man"                 # the default title follows the day
    assert set(result["changes"]) == {"start_at", "post_at", "title"}
    assert result["demoted"] == [] and result["alerts_reset"] == []


def test_scheduled_custom_title_is_kept_and_post_days_alone_moves_post_at(q):
    sid = scheduled(q, post_days=3, title="Scrim night")
    result = edit(q, sid, post_days=1)
    assert result["session"]["title"] == "Scrim night"
    assert result["session"]["post_at"] == ts(at(8, 21))
    assert set(result["changes"]) == {"post_at"}


def test_scheduled_post_at_never_in_the_past(q):
    sid = scheduled(q, post_days=3)
    result = edit(q, sid, start_at=ts(at(6, 21)))               # tomorrow: 3 days before is already past
    assert result["session"]["post_at"] == ts(NOW) + 1
    assert any("within a minute" in n for n in result["notes"])


def test_scheduled_post_at_pulled_forward_falls_back_to_the_default(q):
    # Scheduled close to the game: post_at was clamped to "now", so its post_days can't be read back.
    sid = q.call(db.create_session, guild_id=1, channel_id=2, title="t", start_at=ts(at(6, 21)), created_by=7,
                 now=ts(NOW), post_at=ts(NOW) + 1)
    result = edit(q, sid, start_at=ts(at(12, 21)), default_post_days=2)
    assert result["session"]["post_at"] == ts(at(10, 21))


def test_weekly_game_edit_changes_only_that_game_and_caps_post_days(q):
    series_id = q.call(db.create_series, guild_id=1, channel_id=2, title=None, weekday=4, local_time="21:00",
                       slots=10, post_days=3, remaining=None, created_by=7)
    q.call(db.materialize_series, NOW, ET)
    sid = q.read(db.next_series_session, series_id, ts(NOW))["id"]
    result = edit(q, sid, post_days=10, slots=12)
    assert result["session"]["post_at"] == ts(NOW) + 1    # capped to 6 days before (Sat 3rd): already past
    assert any("at most 6 days" in n for n in result["notes"])
    series = q.read(db.get_series, series_id)
    assert series["slots"] == 10 and series["post_days"] == 3   # the series itself is untouched


# --------------------------------------------------------------------------- posted games

def test_posted_slots_down_moves_players_to_the_waitlist(q):
    sid = posted(q, players=10)
    result = edit(q, sid, slots=8)
    assert [r["user_id"] for r in result["demoted"]] == ["u8", "u9"]
    assert result["promoted"] == []
    roster = build_roster(q.read(db.list_signups, sid), 8)
    assert roster.count == 8 and [r["user_id"] for r in roster.waitlist] == ["u8", "u9"]


def test_posted_slots_up_promotes_the_waitlist(q):
    sid = posted(q, players=12)
    result = edit(q, sid, slots=11)
    assert [r["user_id"] for r in result["promoted"]] == ["u10"]
    assert result["demoted"] == []
    assert len(q.read(db.list_signups, sid)) == 12                # roster kept, nobody removed


def test_posted_post_days_is_ignored_with_a_note(q):
    sid = posted(q)
    result = edit(q, sid, post_days=2)
    assert result["changes"] == {} and "post_days ignored" in result["notes"][0]


def test_posted_game_cannot_move_into_the_past_or_too_far(q):
    sid = posted(q)
    with pytest.raises(db.EditRefused, match="already passed"):
        edit(q, sid, start_at=ts(NOW) - 60)
    with pytest.raises(db.EditRefused, match="30 days"):
        edit(q, sid, start_at=ts(NOW + timedelta(days=31)))
    assert q.read(db.get_session, sid)["start_at"] == ts(FRIDAY)


def test_closed_started_and_posting_games_are_refused_without_writing(q):
    cancelled = posted(q)
    q.call(db.cancel_if_open, cancelled)
    with pytest.raises(db.EditRefused, match="cancelled"):
        edit(q, cancelled, slots=12)
    started = posted(q)
    with pytest.raises(db.EditRefused, match="already started"):
        edit(q, started, now=FRIDAY + timedelta(minutes=5), slots=12)
    posting = scheduled(q)
    q.call(db.claim_post, posting)
    with pytest.raises(db.EditRefused, match="being posted"):
        edit(q, posting, title="New")
    for sid in (cancelled, started, posting):
        session = q.read(db.get_session, sid)
        assert session["slots"] == 10 and session["title"] == "Friday 10-man"


def test_bad_values_are_refused_by_the_backstop(q):
    sid = posted(q)
    with pytest.raises(ValueError):
        edit(q, sid, slots=41)
    with pytest.raises(ValueError):
        edit(q, sid, title="x" * 81)


# --------------------------------------------------------------------------- alerts after a time change

def test_moving_a_day_later_re_arms_gameday_but_never_repeats_todays_daily(q, cfg):
    sid = posted(q, players=6)
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=at(9, 12, 0))      # Friday noon daily
    alerts.run_alerts_once(q, send, cfg, now=at(9, 19, 0))      # Friday gameday (2 h before 9 pm)
    assert len(send.kinds) == 2
    result = edit(q, sid, now=at(9, 19, 30), start_at=ts(SATURDAY))
    assert ("gameday", "once") in result["alerts_reset"]
    assert ("daily", "2026-10-09") in alert_rows(q, sid)         # today's daily is kept
    alerts.run_alerts_once(q, send, cfg, now=at(9, 19, 31))
    assert len(send.kinds) == 2, "nothing new fires on the day of the move"
    alerts.run_alerts_once(q, send, cfg, now=at(10, 12, 0))     # Saturday daily
    alerts.run_alerts_once(q, send, cfg, now=at(10, 19, 0))     # Saturday gameday, for the new time
    alerts.run_alerts_once(q, send, cfg, now=at(10, 21, 0))     # starting
    assert len(send.kinds) == 5 and send.kinds[-1].endswith("is starting")


def test_moving_slightly_never_resends_a_gameday_alert_already_sent(q, cfg):
    sid = posted(q, players=6)
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=at(9, 19, 0))
    assert len(send.kinds) == 1
    # 30 min later: the new gameday moment (6:30 pm) has already passed, so the row stays.
    result = edit(q, sid, now=at(9, 19, 10), start_at=ts(at(9, 20, 30)))
    assert result["alerts_reset"] == []
    for minute in (11, 30, 59):
        alerts.run_alerts_once(q, send, cfg, now=at(9, 19, minute))
    assert len(send.kinds) == 1, "gameday alert sent twice for one game day"
    alerts.run_alerts_once(q, send, cfg, now=at(9, 20, 30))
    assert len(send.kinds) == 2 and send.kinds[-1].endswith("is starting")


def test_an_alert_in_flight_is_never_cleared(q):
    sid = posted(q, players=6)
    q.call(db.claim_alert, sid, "gameday", "once", now=ts(at(9, 19)))
    result = edit(q, sid, now=at(9, 19), start_at=ts(SATURDAY))
    assert result["alerts_reset"] == []
    assert alert_rows(q, sid)[("gameday", "once")] == db.ALERT_SENDING


def test_more_slots_re_arms_the_full_alert_only_when_no_longer_full(q, cfg):
    sid = posted(q, players=10)
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=at(6, 12, 0))
    assert send.kinds == ["Friday 10-man is full"]
    assert edit(q, sid, now=at(6, 13), slots=9)["alerts_reset"] == []        # still full: no second "full"
    alerts.run_alerts_once(q, send, cfg, now=at(6, 13, 1))
    assert len(send.kinds) == 1
    assert ("full", "once") in edit(q, sid, now=at(6, 14), slots=12)["alerts_reset"]
    q.call(db.set_status, sid, "x", "X", db.IN, now=ts(at(6, 15)))
    q.call(db.set_status, sid, "y", "Y", db.IN, now=ts(at(6, 15)) + 1)
    q.call(db.set_status, sid, "z", "Z", db.IN, now=ts(at(6, 15)) + 2)
    alerts.run_alerts_once(q, send, cfg, now=at(6, 15, 5))
    assert send.kinds[-1] == "Friday 10-man is full" and len(send.kinds) == 2


# --------------------------------------------------------------------------- weekly series

def make_series(q, **kw):
    args = {"guild_id": 1, "channel_id": 2, "title": None, "weekday": 4, "local_time": "21:00", "slots": 10,
            "post_days": 3, "remaining": None, "created_by": 7, **kw}
    return q.call(db.create_series, **args)


def test_series_update_changes_the_next_materialized_game(q):
    series_id = make_series(q)
    q.call(db.materialize_series, NOW, ET)                       # Fri 9th created as before
    result = q.call(db.update_series, series_id, tz=ET, now=ts(NOW), weekday=5, local_time="20:00",
                    slots=12, post_days=9, title="Sat scrims")
    assert result["capped"] and result["series"]["post_days"] == 6
    assert set(result["changes"]) == {"weekday", "local_time", "slots", "post_days", "title"}
    first = result["upcoming"]
    assert first["start_at"] == ts(FRIDAY) and first["slots"] == 10   # not applied to the upcoming game
    created = q.call(db.materialize_series, FRIDAY + timedelta(hours=1), ET)
    nxt = q.read(db.get_session, created[0])
    assert datetime.fromtimestamp(nxt["start_at"], ET) == at(10, 20)   # Saturday 8 pm, the day after
    assert nxt["slots"] == 12 and nxt["title"] == "Sat scrims"
    assert nxt["post_at"] == ts(FRIDAY) + 3600 + 1                     # 6 days before is past: due now


def test_series_update_apply_to_next_moves_the_upcoming_game(q):
    series_id = make_series(q)
    q.call(db.materialize_series, NOW, ET)
    result = q.call(db.update_series, series_id, tz=ET, now=ts(NOW), weekday=5, apply_to_next=True)
    game = result["next"]["session"]
    assert datetime.fromtimestamp(game["start_at"], ET) == SATURDAY
    assert game["title"] == "Saturday 10-man" and game["post_at"] == ts(at(7, 21))
    # The series keeps one game ahead: nothing extra is created, and the week after follows.
    assert q.call(db.materialize_series, NOW + timedelta(days=1), ET) == []
    later = q.call(db.materialize_series, SATURDAY + timedelta(hours=1), ET)
    assert datetime.fromtimestamp(q.read(db.get_session, later[0])["start_at"], ET) == at(17, 21)


def test_series_update_apply_to_next_on_a_posted_game_reports_the_roster_change(q):
    series_id = make_series(q)
    q.call(db.materialize_series, NOW, ET)
    sid = q.read(db.next_series_session, series_id, ts(NOW))["id"]
    q.call(db.claim_post, sid)
    q.call(db.finish_post, sid, message_id=1)
    for i in range(10):
        q.call(db.set_status, sid, f"u{i}", f"user{i}", db.IN, now=ts(NOW) + i)
    result = q.call(db.update_series, series_id, tz=ET, now=ts(NOW) + 60, slots=8, apply_to_next=True)
    assert result["next"]["posted"]
    assert [r["user_id"] for r in result["next"]["demoted"]] == ["u8", "u9"]


def test_series_update_keeps_the_series_change_when_the_next_game_is_refused(q):
    series_id = make_series(q)
    q.call(db.materialize_series, NOW, ET)
    sid = q.read(db.next_series_session, series_id, ts(NOW))["id"]
    q.call(db.claim_post, sid)                                    # mid-post: can't be edited
    result = q.call(db.update_series, series_id, tz=ET, now=ts(NOW), slots=12, apply_to_next=True)
    assert "being posted" in result["next_error"]
    assert q.read(db.get_series, series_id)["slots"] == 12
    assert q.read(db.get_session, sid)["slots"] == 10


def test_stopped_series_is_refused(q):
    series_id = make_series(q)
    q.call(db.stop_series, series_id)
    with pytest.raises(db.EditRefused, match="stopped"):
        q.call(db.update_series, series_id, tz=ET, slots=12)


# --------------------------------------------------------------------------- production database

def test_a_v3_database_works_unchanged(tmp_path):
    """v0.1.0 ships schema version 3; edit/update need no migration and keep every row."""
    assert db.LATEST_SCHEMA_VERSION == 3, "edit/update must not need a schema change"
    path = str(tmp_path / "v3.db")
    conn = sqlite3.connect(path)
    for statement in db._split(db.MIGRATIONS_DDL + db.DDL):
        conn.execute(statement)
    conn.executescript(f"""
        INSERT INTO schema_migrations VALUES (1, 0), (2, 0), (3, 0);
        INSERT INTO series (id, guild_id, channel_id, weekday, local_time, slots, post_days, created_by, created_at)
            VALUES (1, '1', '2', 4, '21:00', 10, 3, '7', 0);
        INSERT INTO sessions (id, guild_id, channel_id, message_id, title, start_at, created_by, created_at)
            VALUES (1, '1', '2', '55', 'Friday 10-man', {ts(FRIDAY)}, '7', 0);
        INSERT INTO signups (session_id, user_id, display_name, status, joined_at) VALUES
            (1, 'a', 'A', 'in', 1), (1, 'b', 'B', 'in', 2), (1, 'c', 'C', 'in', 3);
        INSERT INTO alerts VALUES (1, 'gameday', 'once', 'sent', 1, NULL, 0);
    """)
    conn.close()
    q = DBQueue(path, init=db.ensure_schema)
    q.start()
    try:
        assert q.read(lambda c: c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]) == 3
        result = q.call(db.edit_session, 1, tz=ET, now=ts(NOW), slots=2, start_at=ts(SATURDAY))
        assert [r["user_id"] for r in result["demoted"]] == ["c"]
        assert [s["user_id"] for s in q.read(db.list_signups, 1)] == ["a", "b", "c"]
        assert q.call(db.update_series, 1, tz=ET, now=ts(NOW), slots=12)["series"]["slots"] == 12
    finally:
        q.stop()
