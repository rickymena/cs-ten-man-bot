"""
Simulated end-to-end runs of scheduling and the review fixes: /tenman schedule
(once and weekly) driven through bot.tick() on a simulated clock, sign-ups
closing at start, lobby permissions, and no orphan sessions.
"""

import asyncio
from datetime import datetime, timedelta
from datetime import time as dtime

import discord
import pytest

from tests.conftest import ET
from tests.sim.harness import CHANNEL_ID, FakeTextChannel, World
from tests.sim.test_e2e import MOD, ORGANIZER, open_queue, players
from utils import database as db

pytestmark = pytest.mark.sim


@pytest.fixture
def cfg(sim_cfg):
    import dataclasses
    return dataclasses.replace(sim_cfg, alerts_via="webhook")


def run_clock(world, start: datetime, end: datetime, step=timedelta(minutes=30), on_step=None):
    async def main():
        clock = start
        while clock <= end:
            if on_step:
                await on_step(clock)
            await world.bot.tick(clock)
            world.check()
            clock += step
    asyncio.run(main())


def test_one_off_schedule_posts_on_time_and_lists(cfg, discord_fake):
    q = open_queue(cfg)
    now = datetime.now(ET)
    game = datetime.combine((now + timedelta(days=10)).date(), dtime(21, 0), ET)

    async def setup():
        world = World(cfg, q)
        reply = await world.schedule(ORGANIZER, game.strftime("%Y-%m-%d %H:%M"), post_days=3)
        assert "Scheduled" in reply.replies[0].content and not reply.replies[0].ephemeral
        listing = await world.list(ORGANIZER)
        assert "scheduled, posts" in listing.replies[0].content
        # Nobody can click on a sign-up that isn't up yet.
        sid = q.read(db.channel_upcoming, CHANNEL_ID)[0]["id"]
        with pytest.raises(db.SessionClosed):
            q.call(db.set_status, sid, "x", "x", db.IN)
        return world, sid

    world, sid = asyncio.run(setup())
    post_at = datetime.combine(game.date() - timedelta(days=3), dtime(21, 0), ET)
    run_clock(world, now, post_at - timedelta(minutes=30))
    assert world.text_channel.sent == [], "posted early"
    run_clock(world, post_at, post_at + timedelta(hours=2))
    assert len(world.text_channel.sent) == 1, "not posted exactly once"
    post = world.text_channel.sent[0]
    assert post.content.startswith("<@&") and "Sign-ups are open" in post.content
    session = q.read(db.get_session, sid)
    assert session["post_state"] == db.POSTED and session["message_id"] == str(post.message_id)

    async def click():
        r = await world.click(players(1)[0], "in", sid)
        assert r.replies[0].kind == "edit"
    asyncio.run(click())
    q.stop()


def test_weekly_series_creates_one_game_at_a_time_for_n_weeks(cfg, discord_fake):
    q = open_queue(cfg)
    now = datetime.now(ET)
    first = datetime.combine((now + timedelta(days=2)).date(), dtime(21, 0), ET)

    async def setup():
        world = World(cfg, q)
        r = await world.schedule(ORGANIZER, first.strftime("%Y-%m-%d %H:%M"), repeat="weekly", weeks=3,
                                 post_days=2)
        assert "for 3 weeks" in r.replies[0].content
        return world

    world = asyncio.run(setup())
    seen_upcoming = []

    async def watch(clock):
        upcoming = q.read(db.channel_upcoming, CHANNEL_ID, int(clock.timestamp()))
        seen_upcoming.append(len([s for s in upcoming if s["start_at"] > clock.timestamp()]))

    run_clock(world, now, first + timedelta(weeks=4), step=timedelta(hours=1), on_step=watch)
    sessions = q.read(lambda c: [dict(r) for r in c.execute("SELECT * FROM sessions ORDER BY start_at")])
    assert len(sessions) == 3, "weeks=3 must create exactly three games"
    starts = [datetime.fromtimestamp(s["start_at"], ET) for s in sessions]
    assert all(t.hour == 21 and t.minute == 0 and t.weekday() == first.weekday() for t in starts), \
        "every game at the same local time, across any DST change"
    assert [b - a for a, b in zip(starts, starts[1:], strict=False)] in ([timedelta(days=7)] * 2,
                                                          [timedelta(days=7, hours=1)] * 1 + [timedelta(days=7)],
                                                          [timedelta(days=7)] + [timedelta(days=7, hours=1)])
    assert max(seen_upcoming) == 1, "a series keeps exactly one game ahead"
    assert len(world.text_channel.sent) == 3, "each week's sign-up posted once"
    assert q.read(lambda c: c.execute("SELECT active FROM series").fetchone()[0]) == 0
    q.stop()


def test_stopping_a_series_cancels_what_is_ahead(cfg, discord_fake):
    q = open_queue(cfg)
    now = datetime.now(ET)

    async def main():
        world = World(cfg, q, moderators={MOD.id})
        await world.schedule(ORGANIZER, (now + timedelta(days=5)).strftime("%Y-%m-%d 21:00"), repeat="weekly")
        ask = await world.cancel(MOD)
        assert "scheduled, not posted yet" in ask.replies[0].content
        it = world._it(MOD)
        await ask.replies[0].view.stop_series.callback(it)
        assert it.replies[0].content == "Series stopped."
        await world.bot.tick(now + timedelta(days=20))
        world.check()

    asyncio.run(main())
    assert q.read(db.channel_upcoming, CHANNEL_ID) == []
    q.stop()


def test_signups_close_at_start_and_buttons_are_removed(cfg, discord_fake):
    q = open_queue(cfg)

    async def main():
        world = World(cfg, q)
        start = datetime.now(ET) + timedelta(hours=2)
        await world.start(ORGANIZER, start.strftime("%Y-%m-%d %H:%M"))
        sid = q.read(db.channel_open_sessions, CHANNEL_ID)[0]["id"]
        # Past the start, a click is refused with a reason instead of changing the roster.
        with pytest.raises(db.SessionClosed, match="already started"):
            q.call(db.set_status, sid, "z", "z", db.IN, now=start.timestamp() + 60)
        await world.bot.tick(start + timedelta(minutes=31))
        message_id = world.message_id(sid)
        embed, view = world.text_channel.edits[message_id][-1]
        assert view.children == [], "buttons must be removed once the game has started"
        assert q.read(db.get_session, sid)["status"] == db.DONE

    asyncio.run(main())
    q.stop()


def test_lobby_is_limited_to_organizer_mods_and_players(cfg, discord_fake):
    q = open_queue(cfg)
    crowd = players(3)

    async def main():
        world = World(cfg, q, moderators={MOD.id})
        await world.start(ORGANIZER, "sat 9pm")
        sid = q.read(db.channel_open_sessions, CHANNEL_ID)[0]["id"]
        await world.click(crowd[0], "in", sid)
        stranger = await world.lobby(crowd[1], "https://popflash.site/x")
        assert "Only the organizer" in stranger.replies[0].content
        for user in (ORGANIZER, MOD, crowd[0]):
            ok = await world.lobby(user, "https://popflash.site/x")
            assert ok.replies[0].content.startswith("Lobby for")
        world.check()

    asyncio.run(main())
    q.stop()


def test_failed_post_never_leaves_an_orphan_sign_up(cfg, discord_fake):
    q = open_queue(cfg)

    async def main():
        world = World(cfg, q)

        async def broken(*args, **kwargs):
            raise discord.HTTPException(type("R", (), {"status": 500, "reason": "x"})(), "boom")

        it = world._it(ORGANIZER)
        it.response.send_message = broken
        with pytest.raises(discord.HTTPException):
            await world.cmds.get_command("start").callback(world.cmds, it, "sat 9pm")
        world.interactions.remove(it)

    asyncio.run(main())
    assert q.read(db.channel_open_sessions, CHANNEL_ID) == []
    q.stop()


def test_scheduled_post_retries_then_gives_up_without_double_posting(cfg, discord_fake):
    q = open_queue(cfg)
    broken = FakeTextChannel(fail_with=discord.Forbidden(type("R", (), {"status": 403, "reason": "x"})(), "no"))
    now = datetime.now(ET)

    async def main():
        world = World(cfg, q, text_channel=broken)
        await world.schedule(ORGANIZER, (now + timedelta(days=1)).strftime("%Y-%m-%d 21:00"), post_days=3)
        for minute in range(0, 8):
            await world.bot.tick(now + timedelta(minutes=minute))
        world.check()

    asyncio.run(main())
    session = q.read(db.channel_upcoming, CHANNEL_ID)
    assert session == [], "gives up (cancelled) after MAX_POST_ATTEMPTS definite failures"
    q.stop()


def test_start_accepts_quoted_and_casual_times_and_autocomplete_previews(cfg, discord_fake):
    q = open_queue(cfg)

    async def main():
        world = World(cfg, q)
        for text in ('"sat 9pm"', "“Saturday at 9 PM”"):
            reply = await world.start(ORGANIZER, text)
            assert reply.replies[0].ephemeral is False, reply.replies[0].content
        cmd = world.cmds.get_command("start")
        it = world._it(ORGANIZER)
        choices = await world.cmds._when_choices(it, '"sat 9pm"', 30)
        assert len(choices) == 1 and choices[0].name.startswith("Sat, ") and "at 9:00 PM" in choices[0].name
        assert choices[0].value.endswith(" 21:00")
        suggestions = await world.cmds._when_choices(it, "", 30)
        assert 1 <= len(suggestions) <= 5
        bad = await world.cmds._when_choices(it, "whenever", 30)
        assert bad[0].name.startswith("Not understood yet")
        assert cmd is not None
        world.interactions.remove(it)
        world.check()

    asyncio.run(main())
    assert len(q.read(db.channel_open_sessions, CHANNEL_ID)) == 2
    q.stop()


def test_maybe_with_a_guest_takes_both_off_the_roster(cfg, discord_fake):
    q = open_queue(cfg)
    me = players(1)[0]

    async def main():
        world = World(cfg, q)
        await world.start(ORGANIZER, "sat 9pm")
        sid = q.read(db.channel_open_sessions, CHANNEL_ID)[0]["id"]
        await world.click(me, "in", sid)
        await world.click(me, "guest", sid)
        assert world.roster(sid).count == 3
        reply = await world.click(me, "maybe", sid)
        fields = {f.name: f.value for f in reply.replies[0].embed.fields}
        assert world.roster(sid).count == 1
        assert "Maybe (1)" in fields and "+1" not in "".join(fields.values())
        world.check()

    asyncio.run(main())
    q.stop()


def test_lobby_links_per_game_scheduled_and_weekly(cfg, discord_fake):
    q = open_queue(cfg)
    now = datetime.now(ET)

    async def main():
        world = World(cfg, q, moderators={MOD.id})
        # 1. start with a lobby: it's on the sign-up from the first post
        r = await world.start(ORGANIZER, "sat 9pm", lobby="https://popflash.site/-/a")
        assert any(f.name == "Lobby" for f in r.replies[0].embed.fields)
        # 2. weekly with a lobby: every week's game gets it
        first = (now + timedelta(days=2)).strftime("%Y-%m-%d 21:00")
        r = await world.schedule(ORGANIZER, first, repeat="weekly", weeks=3, lobby="https://popflash.site/-/b")
        assert "Lobby link saved for every week" in r.replies[0].content
        assert "9:00 PM E" in r.replies[0].content                  # the zone is spelled out
        listing = await world.list(ORGANIZER)
        assert "lobby set" in listing.replies[0].content
        for day in range(0, 22, 7):
            await world.bot.tick(now + timedelta(days=day, hours=1))
        weekly = q.read(lambda c: [dict(x) for x in c.execute("SELECT * FROM sessions WHERE series_id IS NOT NULL")])
        assert len(weekly) >= 2 and all(s["lobby_url"] == "https://popflash.site/-/b" for s in weekly)

    asyncio.run(main())
    q.stop()


def test_lobby_on_a_scheduled_game_is_saved_quietly_and_can_cover_the_series(cfg, discord_fake):
    q = open_queue(cfg)
    now = datetime.now(ET)
    stranger = players(1)[0]

    async def main():
        world = World(cfg, q, moderators={MOD.id})
        await world.schedule(ORGANIZER, (now + timedelta(days=10)).strftime("%Y-%m-%d 21:00"), repeat="weekly")
        refused = await world.lobby(stranger, "https://popflash.site/x")
        assert "Only the organizer" in refused.replies[0].content
        it = world._it(MOD)
        await world.cmds.get_command("lobby").callback(world.cmds, it, "https://popflash.site/-/c", None, True)
        reply = it.replies[0]
        assert reply.ephemeral and "every later week" in reply.content and "<@" not in reply.content
        series = q.read(lambda c: dict(c.execute("SELECT * FROM series").fetchone()))
        assert series["lobby_url"] == "https://popflash.site/-/c"
        world.check()

    asyncio.run(main())
    q.stop()


def test_when_preview_shows_a_typed_zone_and_the_bots_zone(cfg, discord_fake):
    q = open_queue(cfg)

    async def main():
        world = World(cfg, q)
        it = world._it(ORGANIZER)
        choice = (await world.cmds._when_choices(it, "sat 9pm ct", 30))[0]
        assert "9:00 PM C" in choice.name and "= 10:00 PM E" in choice.name
        assert choice.value.endswith(" 22:00")                       # stored in the bot's zone
        world.interactions.remove(it)

    asyncio.run(main())
    q.stop()
