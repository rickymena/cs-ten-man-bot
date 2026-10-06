"""
Simulated end-to-end runs. Real bot code from the button callback down to a
real HTTP socket; only Discord itself is faked (tests/sim/harness.py,
tests/sim/fakes.py). Alert passes run on a simulated clock.

Run alone:  .venv/bin/python -m pytest tests/sim -v
"""

import asyncio
import time
from datetime import datetime, timedelta
from datetime import time as dtime

import pytest

from tests.conftest import ET
from tests.sim.harness import CHANNEL_ID, ROLE_ID, FakeUser, World, check_embed
from utils import alerts
from utils import database as db
from utils import style
from utils.db_queue import DBQueue

pytestmark = pytest.mark.sim

ORGANIZER = FakeUser(1001, "dadgeL")
MOD = FakeUser(1002, "Mollenkoffee")
HOSTILE_NAMES = [
    "@everyone", "@here get in", "**bold** __u__", "[free skins](https://evil.example)",
    "evil\u202egnp.exe", "zero\u200bwidth", "x" * 100, "<@&323456789012345678>", "`code` ||spoiler||",
]


def players(n, start=2000):
    names = HOSTILE_NAMES + [f"player{i}" for i in range(n)]
    return [FakeUser(start + i, names[i]) for i in range(n)]


def open_queue(cfg):
    q = DBQueue(cfg.db_path, init=db.ensure_schema)
    q.start()
    q.call(db.recover_stale_sends)
    return q


def kind_of(post) -> str:
    embed = post.embeds[0]
    if embed["title"].endswith("is starting"):
        return "starting"
    if embed["title"].endswith("is full"):
        return "full"
    return "gameday" if embed["color"] == style.RED else "daily"


async def alert_pass(world, cfg, now):
    def send(message, session):
        return world.bot._send_alert_as_webhook(message)
    return await asyncio.to_thread(alerts.run_alerts_once, world.q, send, cfg, now)


# --------------------------------------------------------------------------- the week

def test_a_full_week_from_signup_to_game_start(sim_cfg, discord_fake):
    """
    Mon..Fri on a simulated clock: join, guest, maybe, fill, waitlist, a dropout
    promoting the waitlist, a collapse back below 10, lobby link, game start.
    Checks every alert that should fire fires exactly once, and nothing else.
    """
    q = open_queue(sim_cfg)
    real_now = datetime.now(ET)
    game = (real_now + timedelta(days=4)).replace(hour=21, minute=0, second=0, microsecond=0)
    day = [(real_now + timedelta(days=i)).date() for i in range(5)]
    crowd = players(14)

    def at(d, hour, minute=0):
        return datetime.combine(day[d], dtime(hour, minute), ET)

    async def main():
        world = World(sim_cfg, q, moderators={MOD.id})
        it = await world.start(ORGANIZER, game.strftime("%Y-%m-%d %H:%M"))
        assert it.replies[0].allowed_mentions.roles[0].id == int(ROLE_ID)
        sid = q.read(db.channel_open_sessions, CHANNEL_ID)[0]["id"]

        # Events happen at 09:00 each day, before that day's 12:00 alert.
        script = {
            0: [("in", p) for p in crowd[:2]],                         # 3/10 with the organizer
            1: [("in", p) for p in crowd[2:5]],                        # 6/10
            2: [("in", p) for p in crowd[5:7]] + [("guest", crowd[0]), ("maybe", crowd[13])],  # 9/10
            3: [("in", p) for p in crowd[7:10]],                       # 12 in: 10 + 2 waitlisted
            4: [("out", crowd[1])],                                    # waitlist head promoted
        }
        expected_daily = {}
        clock = real_now
        end = game + timedelta(minutes=40)
        while clock <= end:
            d = (clock.date() - day[0]).days
            if d in script and ((clock.hour == 9 and clock.minute == 0) or d == 0):  # day 0: right after posting
                for action, user in script.pop(d):
                    reply = await world.click(user, action, sid)
                    if d == 4:
                        promoted = [r for r in reply.replies if r.kind == "followup"]
                        assert promoted and "off the waitlist" in promoted[0].content
            if clock == at(4, 18):                                    # four drop out on game day
                for user in crowd[2:6]:
                    await world.click(user, "out", sid)
            if clock == at(4, 20, 30):
                await world.lobby(ORGANIZER, "https://popflash.site/-/wiggz/scrim")
            if clock.hour == 12 and clock.minute == 0 and clock < game:
                roster = world.roster(sid)
                if not roster.full and clock.timestamp() >= q.read(db.get_session, sid)["created_at"]:
                    expected_daily[clock.date()] = roster.need
            await alert_pass(world, sim_cfg, clock)
            clock = (clock + timedelta(minutes=15)).replace(second=0, microsecond=0)
            if clock.minute % 15:
                clock = clock.replace(minute=clock.minute - clock.minute % 15)
            world.check()
        return world, sid, expected_daily

    world, sid, expected_daily = asyncio.run(main())
    posts = discord_fake.channel()
    kinds = [kind_of(p) for p in posts]
    for p in posts:
        check_embed(p.embeds[0])
        assert p.payload["allowed_mentions"]["parse"] == []

    assert kinds.count("daily") == len(expected_daily)
    assert kinds.count("full") == 1
    assert kinds.count("gameday") == 1
    assert kinds.count("starting") == 1
    assert kinds[-1] == "starting"

    dailies = [p for p in posts if kind_of(p) == "daily"]
    for post, need in zip(dailies, expected_daily.values(), strict=True):
        assert f"needs **{need} more**" in post.content
        assert post.payload["allowed_mentions"]["roles"] == [ROLE_ID]
    gameday = next(p for p in posts if kind_of(p) == "gameday")
    assert "needs **3 more** (7/10)" in gameday.content
    starting = posts[-1]
    confirmed = {r["user_id"] for r in world.roster(sid).confirmed}
    assert set(starting.payload["allowed_mentions"]["users"]) == confirmed
    assert "[join here](" in starting.embeds[0]["description"]
    assert starting.payload["allowed_mentions"]["roles"] == []
    q.stop()


# --------------------------------------------------------------------------- click storm

def test_sixty_players_click_in_at_once(sim_cfg, discord_fake):
    q = open_queue(sim_cfg)
    crowd = players(60)

    async def main():
        world = World(sim_cfg, q)
        await world.start(ORGANIZER, "tomorrow 9pm", slots=10)
        sid = q.read(db.channel_open_sessions, CHANNEL_ID)[0]["id"]
        started = time.perf_counter()
        await asyncio.gather(*(world.click(p, "in", sid) for p in crowd))
        elapsed = time.perf_counter() - started
        # Second wave: everyone flips at once, half out, half maybe, then back in.
        await asyncio.gather(*(world.click(p, "out" if i % 2 else "maybe", sid) for i, p in enumerate(crowd)))
        await asyncio.gather(*(world.click(p, "in", sid) for p in crowd))
        world.check()
        return world, sid, elapsed

    world, sid, elapsed = asyncio.run(main())
    roster = world.roster(sid)
    assert roster.count == 10 and len(roster.waitlist) == 51
    print(f"\n60 concurrent clicks in {elapsed * 1000:.0f} ms ({60 / elapsed:.0f} clicks/s)")
    q.stop()


# --------------------------------------------------------------------------- chaos

@pytest.mark.parametrize("seed", [1, 2, 3])
def test_alerts_survive_a_flaky_discord_and_restarts_without_double_posting(sim_cfg, discord_fake, seed):
    """
    Three sign-ups over five simulated days while Discord returns 500s, 429s,
    drops connections and replies too late, and the bot restarts every day.
    Safety: no alert ever reaches the channel twice. Liveness: everything the
    database records as sent really reached the channel.
    """
    discord_fake.faults.set(mix=[("500", 0.15), ("429:0", 0.1), ("drop", 0.1), ("hang:0.4", 0.1)])
    discord_fake.faults._rng.seed(seed)
    real_now = datetime.now(ET)
    q = open_queue(sim_cfg)

    async def setup():
        world = World(sim_cfg, q)
        for n, when in enumerate(["2", "3", "4"]):
            start = (real_now + timedelta(days=int(when))).replace(hour=21, minute=0, second=0, microsecond=0)
            await world.start(FakeUser(5000 + n, f"org{n}"), start.strftime("%Y-%m-%d %H:%M"))
        return [s["id"] for s in q.read(db.channel_open_sessions, CHANNEL_ID)]

    sids = asyncio.run(setup())
    crowd = players(30, start=7000)
    clock = real_now
    end = real_now + timedelta(days=5)
    day_seen = clock.date()
    while clock <= end:
        if clock.date() != day_seen:                       # restart once a simulated day
            q.stop()
            q = open_queue(sim_cfg)
            day_seen = clock.date()

        async def step(q=q, clock=clock):
            world = World(sim_cfg, q)
            if clock.hour == 10 and clock.minute == 0:
                for i, sid in enumerate(sids):
                    for p in crowd[i * 4:(i + 1) * 4 + (clock.day % 3)]:
                        session = q.read(db.get_session, sid)
                        if session["status"] == db.OPEN and clock.timestamp() < session["start_at"]:
                            await world.click(p, "in", sid)
            await alert_pass(world, sim_cfg, clock)
            world.check()

        asyncio.run(step())
        clock += timedelta(minutes=10)

    posts = discord_fake.channel()
    keys = [(p.embeds[0].get("url"), p.embeds[0]["title"], p.embeds[0]["color"]) for p in posts]
    assert len(keys) == len(set(keys)), "an alert reached the channel twice"

    rows = q.read(lambda c: [dict(r) for r in c.execute("SELECT * FROM alerts")])
    sent = [r for r in rows if r["status"] == db.ALERT_SENT]
    assert len(posts) >= len(sent), "an alert recorded as sent never reached the channel"
    assert all(r["status"] != db.ALERT_SENDING for r in rows), "a claim was left in flight"
    assert sent, "chaos run delivered nothing at all"
    print(f"\nseed {seed}: {len(discord_fake.posts)} POSTs, {len(posts)} reached the channel, "
          f"{len(sent)} sent, {sum(r['status'] == db.ALERT_AMBIGUOUS for r in rows)} ambiguous, "
          f"{sum(r['status'] == db.ALERT_FAILED for r in rows)} failed")
    q.stop()


# --------------------------------------------------------------------------- commands and abuse

def test_cancel_numbering_permissions_and_double_confirm(sim_cfg, discord_fake):
    q = open_queue(sim_cfg)
    stranger = FakeUser(9001, "stranger")

    async def main():
        world = World(sim_cfg, q, moderators={MOD.id})
        await world.start(ORGANIZER, "sat 9pm")
        await world.start(ORGANIZER, "tomorrow 9pm")

        listing = await world.cancel(ORGANIZER)
        lines = listing.replies[0].content.splitlines()
        assert listing.replies[0].ephemeral and lines[1].startswith("`[ 1]`") and lines[2].startswith("`[ 2]`")

        refused = await world.cancel(stranger, 2)
        assert "Only the organizer" in refused.replies[0].content

        ask = await world.cancel(MOD, 2)
        first = await world.confirm_cancel(ask, MOD)
        second = await world.confirm_cancel(ask, MOD)
        assert first.replies[0].content == "Cancelled."
        assert "already closed" in second.replies[0].content
        assert len(q.read(db.channel_open_sessions, CHANNEL_ID)) == 1
        world.check()

    asyncio.run(main())
    q.stop()


def test_hostile_input_is_refused_or_neutralised(sim_cfg, discord_fake):
    q = open_queue(sim_cfg)

    async def main():
        world = World(sim_cfg, q)
        bad_title = await world.start(ORGANIZER, "sat 9pm", title="@everyone free skins https://evil.example")
        assert bad_title.replies[0].ephemeral and "Couldn't use that" in bad_title.replies[0].content
        bad_time = await world.start(ORGANIZER, "x" * 500)
        assert bad_time.replies[0].ephemeral

        await world.start(ORGANIZER, "sat 9pm")
        sid = q.read(db.channel_open_sessions, CHANNEL_ID)[0]["id"]
        for url in ("javascript:alert(1)", "https://user:pw@popflash.site/x", "https://10.0.0.1/x"):
            r = await world.lobby(ORGANIZER, url)
            assert r.replies[0].ephemeral and "Couldn't use that link" in r.replies[0].content

        # A crafted button id pointing at this session, clicked on some other message.
        it = world._it(players(1)[0], message=type("M", (), {"id": 42})())
        from bot import SignupButton
        await SignupButton("in", sid).callback(it)
        assert "doesn't belong" in it.replies[0].content

        # Same button from another server.
        it = world._it(players(1)[0], message=type("M", (), {"id": world.message_id(sid)})())
        it.guild_id = 999
        await SignupButton("in", sid).callback(it)
        assert "home server" in it.replies[0].content

        for p in players(9):
            await world.click(p, "in", sid)
        world.check()
        assert world.roster(sid).count == 10

    asyncio.run(main())
    q.stop()


def test_spam_limits_hold_under_concurrent_starts(sim_cfg, discord_fake):
    q = open_queue(sim_cfg)

    async def main():
        world = World(sim_cfg, q)
        organisers = [FakeUser(8000 + i, f"o{i}") for i in range(12)]
        await asyncio.gather(*(world.start(o, f"tomorrow {9 + i % 3}pm") for i, o in enumerate(organisers)))
        world.check()

    asyncio.run(main())
    assert len(q.read(db.channel_open_sessions, CHANNEL_ID)) == sim_cfg.max_open_per_channel
    q.stop()
