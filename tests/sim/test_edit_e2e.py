"""
Simulated end-to-end runs of /tenman edit and /tenman update: the real
command callbacks, queue and SQLite against fake Discord (harness.py).
"""

import asyncio
import dataclasses
from datetime import datetime, timedelta
from datetime import time as dtime

import pytest

from tests.conftest import ET
from tests.sim.harness import CHANNEL_ID, World
from tests.sim.test_e2e import MOD, ORGANIZER, open_queue, players
from utils import database as db

pytestmark = pytest.mark.sim


@pytest.fixture
def cfg(sim_cfg):
    return dataclasses.replace(sim_cfg, alerts_via="webhook")


def day_at(days_ahead: int, hour: int = 21) -> datetime:
    return datetime.combine((datetime.now(ET) + timedelta(days=days_ahead)).date(), dtime(hour, 0), ET)


def stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M")


def no_role_ping(reply) -> bool:
    am = reply.allowed_mentions
    return am is not None and am.roles is False and am.everyone is False


def pinged_users(reply) -> set[int]:
    users = reply.allowed_mentions.users
    return set() if users is False else {u.id for u in users}


async def started_game(world, q, crowd, slots=10, days=3):
    await world.start(ORGANIZER, stamp(day_at(days)), slots=slots)
    sid = q.read(db.channel_open_sessions, CHANNEL_ID)[0]["id"]
    for user in crowd:
        await world.click(user, "in", sid)
    return sid


def test_only_the_organizer_or_a_moderator_can_edit_or_update(cfg, discord_fake):
    q = open_queue(cfg)
    stranger = players(1, start=5000)[0]

    async def main():
        world = World(cfg, q, moderators={MOD.id})
        sid = await started_game(world, q, [stranger])
        refused = await world.edit(stranger, slots=12)
        assert "Only the organizer or a moderator" in refused.replies[0].content and refused.replies[0].ephemeral
        assert q.read(db.get_session, sid)["slots"] == 10
        for user, slots in ((ORGANIZER, 12), (MOD, 14)):
            ok = await world.edit(user, slots=slots)
            assert not ok.replies[0].ephemeral and "now" in ok.replies[0].content
        assert q.read(db.get_session, sid)["slots"] == 14

        await world.schedule(ORGANIZER, stamp(day_at(5)), repeat="weekly")
        refused = await world.update(stranger, slots=12)
        assert "Only the series organizer or a moderator" in refused.replies[0].content
        ok = await world.update(MOD, slots=12)
        assert ok.replies[0].content.startswith("Updated weekly") and ok.replies[0].ephemeral
        world.check()

    asyncio.run(main())
    q.stop()


def test_editing_a_posted_game_redraws_it_and_posts_a_note_without_a_role_ping(cfg, discord_fake):
    q = open_queue(cfg)
    crowd = players(9)                                            # organizer + 9 = 10/10

    async def main():
        world = World(cfg, q)
        sid = await started_game(world, q, crowd)
        message_id = world.message_id(sid)
        before_edits = len(world.text_channel.edits.get(message_id, []))
        new_time = day_at(4, 20)
        it = await world.edit(ORGANIZER, when=stamp(new_time), slots=8, title="Scrim night")
        note = it.replies[0]
        assert note.kind == "send" and not note.ephemeral
        assert f"<t:{int(new_time.timestamp())}:F>" in note.content and "now 8 slots (was 10)" in note.content
        assert "renamed from" in note.content and "**Scrim night**" in note.content
        assert no_role_ping(note) and "<@&" not in note.content
        # The two players pushed out by the smaller roster are pinged, and only they.
        demoted = {crowd[7].id, crowd[8].id}
        assert pinged_users(note) == demoted and "you're now on the waitlist" in note.content
        edits = world.text_channel.edits[message_id]
        assert len(edits) == before_edits + 1
        embed, view = edits[-1]
        assert embed.title == "Scrim night" and "8/8" in embed.description and len(view.children) == 5
        assert any(f.name.startswith("Waitlist (2)") for f in embed.fields)
        # More slots again: the same two come back, pinged like a promotion.
        it = await world.edit(ORGANIZER, slots=10)
        assert pinged_users(it.replies[0]) == demoted and "off the waitlist" in it.replies[0].content
        world.check()

    asyncio.run(main())
    q.stop()


def test_editing_a_scheduled_game_is_quiet_and_it_posts_at_the_new_time(cfg, discord_fake):
    q = open_queue(cfg)

    async def main():
        world = World(cfg, q)
        await world.schedule(ORGANIZER, stamp(day_at(10)), post_days=3)
        new_time = day_at(12)
        it = await world.edit(ORGANIZER, when=stamp(new_time), post_days=2, lobby="https://popflash.site/-/z")
        reply = it.replies[0]
        assert reply.ephemeral and reply.content.startswith("Updated **")
        assert "scheduled, not posted yet" in reply.content and "lobby link updated" in reply.content
        assert world.text_channel.sent == []
        session = q.read(db.channel_upcoming, CHANNEL_ID)[0]
        post_at = datetime.combine(new_time.date() - timedelta(days=2), dtime(21, 0), ET)
        assert session["post_at"] == int(post_at.timestamp()) and session["lobby_url"] == "https://popflash.site/-/z"
        await world.bot.tick(post_at - timedelta(minutes=1))
        assert world.text_channel.sent == []
        await world.bot.tick(post_at)
        assert len(world.text_channel.sent) == 1
        world.check()

    asyncio.run(main())
    q.stop()


def test_hostile_and_empty_edits_are_refused_and_change_nothing(cfg, discord_fake):
    q = open_queue(cfg)

    async def main():
        world = World(cfg, q)
        sid = await started_game(world, q, [])
        before = q.read(db.get_session, sid)
        cases = [
            ({}, "at least one thing"),
            ({"title": "@everyone get in"}, "mentions aren't allowed"),
            ({"title": "[free](https://evil.example)"}, "links aren't allowed"),
            ({"title": "x" * 500}, "at most 80"),
            ({"lobby": "http://popflash.site/x"}, "only https"),
            ({"lobby": "https://user:pw@popflash.site/x"}, "username or password"),
            ({"when": "whenever"}, "Couldn't use that"),
            ({"when": "x" * 1000}, "too long"),
            ({"when": stamp(day_at(60))}, "30 days"),           # a posted game stays within 30 days
            ({"slots": 99}, "slots must be 2-40"),
            ({"post_days": -1}, "post_days must be 0-14"),
        ]
        for options, expected in cases:
            it = await world.edit(ORGANIZER, **options)
            assert expected in it.replies[0].content and it.replies[0].ephemeral, (options, it.replies[0].content)
        assert q.read(db.get_session, sid) == before
        it = await world.update(ORGANIZER, slots=12)
        assert "No weekly series" in it.replies[0].content
        await world.schedule(ORGANIZER, stamp(day_at(5)), repeat="weekly")
        for series in ("S99", "<@&1>", "' OR 1=1 --", "S" + "9" * 40, "@everyone"):
            it = await world.update(ORGANIZER, series=series, slots=12)
            reply = it.replies[0]
            assert "There is no such series" in reply.content and "[S" in reply.content, series
            assert "@everyone" not in reply.content
        it = await world.update(ORGANIZER, title="<@&323456789012345678>")
        assert "mentions aren't allowed" in it.replies[0].content
        assert q.read(db.channel_series, CHANNEL_ID)[0]["slots"] == 10
        world.check()

    asyncio.run(main())
    q.stop()


def test_markdown_in_a_new_title_is_escaped_in_the_note(cfg, discord_fake):
    q = open_queue(cfg)

    async def main():
        world = World(cfg, q)
        await started_game(world, q, [])
        it = await world.edit(ORGANIZER, title="**loud** __game__")
        assert "\\*\\*loud\\*\\* \\_\\_game\\_\\_" in it.replies[0].content
        world.check()

    asyncio.run(main())
    q.stop()


def test_edit_racing_clicks_keeps_the_roster_consistent(cfg, discord_fake):
    q = open_queue(cfg)
    crowd = players(14)

    async def main():
        world = World(cfg, q)
        sid = await started_game(world, q, crowd[:9])
        late = crowd[9:]
        jobs = [world.click(u, "in", sid) for u in late] + [world.edit(ORGANIZER, slots=6)] \
            + [world.click(crowd[0], "out", sid), world.edit(MOD, slots=12)]
        await asyncio.gather(*jobs)
        world.check()                                             # every interaction answered once
        session = q.read(db.get_session, sid)
        roster = world.roster(sid)
        assert session["slots"] == 6                              # the moderator was refused
        assert roster.count == 6 and len(roster.confirmed) + len(roster.waitlist) == 14  # organizer + 14 - one Out
        # The note pings only players who are In, and only the ones the edit moved.
        edit_it = next(i for i in world.interactions if i.user is ORGANIZER and i.replies
                       and "was updated" in (i.replies[0].content or ""))
        note = edit_it.replies[0]
        signed_in = {int(r["user_id"]) for r in roster.confirmed + roster.waitlist}
        assert pinged_users(note) <= signed_in and no_role_ping(note)
        assert len(pinged_users(note)) == note.content.count("<@") - note.content.count("<@" + str(ORGANIZER.id))

    asyncio.run(main())
    q.stop()


def test_edit_rechecks_inside_the_job_when_the_game_was_cancelled_meanwhile(cfg, discord_fake):
    q = open_queue(cfg)

    async def main():
        world = World(cfg, q)
        sid = await started_game(world, q, [])
        stale = await world.bot.q.aread(lambda c: (db.get_session(c, sid), world.roster(sid)))
        q.call(db.cancel_if_open, sid)

        async def stale_pick(*args, **kwargs):
            return stale
        world.cmds._pick = stale_pick
        it = await world.edit(ORGANIZER, slots=12)
        assert it.replies[0].content == "Can't edit that: that game is cancelled."
        assert q.read(db.get_session, sid)["slots"] == 10
        world.check()

    asyncio.run(main())
    q.stop()


def test_update_a_series_and_its_posted_next_game(cfg, discord_fake):
    q = open_queue(cfg)
    crowd = players(3)

    async def main():
        world = World(cfg, q, moderators={MOD.id})
        await world.schedule(ORGANIZER, stamp(day_at(2)), repeat="weekly", post_days=3)
        await world.bot.tick(datetime.now(ET) + timedelta(seconds=5))   # 3 days ahead > 2: posts now
        sid = q.read(db.channel_open_sessions, CHANNEL_ID)[0]["id"]
        for user in crowd:
            await world.click(user, "in", sid)
        series_id = q.read(db.channel_series, CHANNEL_ID)[0]["id"]

        it = world._it(ORGANIZER)
        choices = await world.cmds._update_series(it, "")
        assert [c.value for c in choices] == [f"S{series_id}"] and choices[0].name.startswith(f"S{series_id} · ")
        world.interactions.remove(it)

        it = await world.update(ORGANIZER, series=f"[S{series_id}]", slots=2, title="Weekly scrim",
                                post_days=9, apply_to_next=True)
        summary, note = it.replies
        assert summary.ephemeral and "Updated weekly **Weekly scrim**" in summary.content
        assert "at most 6 days ahead" in summary.content and "upcoming game" in summary.content
        assert note.kind == "followup" and not note.ephemeral and no_role_ping(note)
        assert pinged_users(note) == {crowd[2].id}                # the first two keep the 2 slots
        edits = world.text_channel.edits[world.message_id(sid)]
        assert edits[-1][0].title == "Weekly scrim"

        listing = await world.list(ORGANIZER)
        assert "**Weekly scrim**" in listing.replies[0].content and "posts 6 day(s) ahead" in listing.replies[0].content
        assert "/tenman update" in listing.replies[0].content
        series = q.read(db.get_series, series_id)
        assert (series["slots"], series["post_days"], series["title"]) == (2, 6, "Weekly scrim")
        world.check()

    asyncio.run(main())
    q.stop()


def test_update_without_apply_to_next_leaves_the_upcoming_game(cfg, discord_fake):
    q = open_queue(cfg)

    async def main():
        world = World(cfg, q)
        first = day_at(9)
        await world.schedule(ORGANIZER, stamp(first), repeat="weekly")
        new = first + timedelta(days=1, hours=-1)
        it = await world.update(ORGANIZER, when=stamp(new))
        reply = it.replies[0]
        assert "keeps its current settings" in reply.content and len(it.replies) == 1
        series = q.read(db.channel_series, CHANNEL_ID)[0]
        assert (series["weekday"], series["local_time"]) == (new.weekday(), "20:00")
        assert q.read(db.channel_upcoming, CHANNEL_ID)[0]["start_at"] == int(first.timestamp())
        world.check()

    asyncio.run(main())
    q.stop()
