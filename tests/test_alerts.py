from datetime import datetime, timedelta

from tests.conftest import ET
from utils import alerts
from utils import database as db
from utils.webhook import AMBIGUOUS, CONFIG_ERROR, DELIVERED, FAILED, PostResult

# Game Friday 2026-10-09 21:00 ET; sign-up created Monday 2026-10-05 09:00 ET.
START = datetime(2026, 10, 9, 21, 0, tzinfo=ET)
CREATED = datetime(2026, 10, 5, 9, 0, tzinfo=ET)


class Recorder:
    def __init__(self, outcome=DELIVERED):
        self.sent = []
        self.messages = []
        self.outcome = outcome

    def __call__(self, message, session):
        self.sent.append((message.content, message.role_id))
        self.messages.append(message)
        return PostResult(self.outcome)


def setup(q, make_session, players=6, slots=10):
    sid = make_session(START.timestamp(), created_at=CREATED.timestamp(), slots=slots)
    for i in range(players):
        q.call(db.set_status, sid, f"u{i}", f"user{i}", db.IN, now=CREATED.timestamp() + i)
    return sid


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=ET)


def test_daily_alert_says_how_many_are_needed(q, cfg, make_session):
    setup(q, make_session)
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=at(6, 12, 1))
    content, role = send.sent[0]
    assert "needs **4 more** (6/10)" in content
    assert "in 3 days" in content
    assert content.startswith("<@&99> ")
    assert role == "99"
    embed = send.messages[0].embed
    assert embed["color"] == 0xE48215
    assert "need **4** more" in embed["description"]


def test_one_alert_per_day_even_across_repeated_passes_and_restarts(q, cfg, make_session):
    setup(q, make_session)
    send = Recorder()
    for minute in (0, 5, 30):
        alerts.run_alerts_once(q, send, cfg, now=at(6, 12, minute))
    assert len(send.sent) == 1
    alerts.run_alerts_once(q, send, cfg, now=at(7, 12, 0))
    assert len(send.sent) == 2


def test_nothing_before_alert_time_and_nothing_after_catchup_window(q, cfg, make_session):
    setup(q, make_session)
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=at(6, 11, 59))
    alerts.run_alerts_once(q, send, cfg, now=at(7, 15, 1))  # 3h01m late: skipped
    assert send.sent == []


def test_missed_slot_within_window_is_sent_late(q, cfg, make_session):
    setup(q, make_session)
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=at(6, 14, 30))
    assert len(send.sent) == 1


def test_gameday_has_daily_plus_two_hours_before(q, cfg, make_session):
    setup(q, make_session)
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=at(9, 12, 0))
    alerts.run_alerts_once(q, send, cfg, now=at(9, 19, 0))
    assert len(send.sent) == 2
    assert "tonight" in send.sent[0][0]


def test_full_roster_posts_once_and_stops_daily(q, cfg, make_session):
    setup(q, make_session, players=10)
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=at(6, 12, 0))
    alerts.run_alerts_once(q, send, cfg, now=at(7, 12, 0))
    assert len(send.sent) == 1
    content, role = send.sent[0]
    assert "is full: 10/10" in content
    assert send.messages[0].embed["color"] == 0x5E8C31
    assert role is None and "<@&" not in content


def test_dropping_below_full_resumes_daily_alerts(q, cfg, make_session):
    sid = setup(q, make_session, players=10)
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=at(6, 12, 0))
    q.call(db.set_status, sid, "u0", "user0", db.OUT)
    alerts.run_alerts_once(q, send, cfg, now=at(7, 12, 0))
    assert "needs **1 more**" in send.sent[-1][0]


def test_failed_send_is_retried_but_ambiguous_is_not(q, cfg, make_session):
    setup(q, make_session)
    failing = Recorder(FAILED)
    alerts.run_alerts_once(q, failing, cfg, now=at(6, 12, 0))
    alerts.run_alerts_once(q, failing, cfg, now=at(6, 12, 1))
    assert len(failing.sent) == 2

    ambiguous = Recorder(AMBIGUOUS)
    alerts.run_alerts_once(q, ambiguous, cfg, now=at(7, 12, 0))
    alerts.run_alerts_once(q, ambiguous, cfg, now=at(7, 12, 1))
    assert len(ambiguous.sent) == 1


def test_send_crash_is_recorded_as_ambiguous(q, cfg, make_session):
    sid = setup(q, make_session)

    def boom(message, session):
        raise RuntimeError("bug")

    alerts.run_alerts_once(q, boom, cfg, now=at(6, 12, 0))
    assert q.read(db.alert_state, sid, "daily", "2026-10-06")["status"] == db.ALERT_AMBIGUOUS


def test_config_error_stops_the_pass(q, cfg, make_session):
    setup(q, make_session)
    setup(q, make_session)
    send = Recorder(CONFIG_ERROR)
    results = alerts.run_alerts_once(q, send, cfg, now=at(6, 12, 0))
    assert len(results) == 1


def test_no_alerts_for_cancelled_sessions(q, cfg, make_session):
    sid = setup(q, make_session)
    q.call(db.set_session_status, sid, db.CANCELLED)
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=at(6, 12, 0))
    alerts.run_alerts_once(q, send, cfg, now=START + timedelta(minutes=1))
    assert send.sent == []


def test_starting_alert_pings_confirmed_players_once_with_lobby(q, cfg, make_session):
    sid = setup(q, make_session, players=3)
    q.call(db.set_lobby_url, sid, "https://popflash.site/x")
    send = Recorder()
    for minutes in (1, 5, 20):
        alerts.run_alerts_once(q, send, cfg, now=START + timedelta(minutes=minutes))
    assert len(send.messages) == 1
    message = send.messages[0]
    assert "is starting now" in message.content
    assert message.user_ids == ("u0", "u1", "u2")
    assert message.role_id is None
    assert "**Lobby** [join here](https://popflash.site/x)" in message.embed["description"]


def test_no_starting_alert_after_the_window(q, cfg, make_session):
    setup(q, make_session, players=3)
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=START + timedelta(minutes=31))
    assert send.messages == []


def test_no_starting_alert_with_empty_roster(q, cfg, make_session):
    make_session(START.timestamp(), created_at=CREATED.timestamp())
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=START + timedelta(minutes=1))
    assert send.messages == []


def test_alert_embeds_carry_avatar_when_known(q, cfg, make_session):
    import dataclasses
    setup(q, make_session)
    send = Recorder()
    alerts.run_alerts_once(q, send, dataclasses.replace(cfg, avatar_url="https://cdn.example/a.png"),
                           now=at(6, 12, 0))
    embed = send.messages[0].embed
    assert embed["thumbnail"]["url"] == "https://cdn.example/a.png"
    assert embed["author"] == {"name": "CS2 10-Man", "icon_url": "https://cdn.example/a.png"}


def test_hostile_title_is_escaped_in_alerts(q, cfg, make_session):
    make_session(START.timestamp(), created_at=CREATED.timestamp(), title="**x** [y](z) @everyone")
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=at(6, 12, 0))
    content = send.messages[0].content
    assert "@everyone" not in content and "[y](z)" not in content


def test_session_created_after_todays_slot_waits_for_tomorrow(q, cfg, make_session):
    make_session(START.timestamp(), created_at=at(6, 13, 0).timestamp())
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=at(6, 13, 30))
    assert send.sent == []
    alerts.run_alerts_once(q, send, cfg, now=at(7, 12, 0))
    assert "needs **10 more** (0/10)" in send.sent[0][0]


def test_jump_link_included_once_message_is_posted(q, cfg, make_session):
    sid = setup(q, make_session)
    q.call(db.set_message_id, sid, 555)
    send = Recorder()
    alerts.run_alerts_once(q, send, cfg, now=at(6, 12, 0))
    assert send.messages[0].embed["url"] == "https://discord.com/channels/1/2/555"
