import asyncio
import time

from utils import database as db
from utils.embeds import signup_embed
from utils.roster import build_roster


def test_embed_and_buttons_render_for_open_full_and_cancelled(q, make_session):
    import bot

    sid = make_session(time.time() + 86400, slots=2)
    for i, u in enumerate("abc"):
        q.call(db.set_status, sid, u, f"user-{u}", db.IN, now=i)
    session = q.read(db.get_session, sid)
    roster = build_roster(q.read(db.list_signups, sid), 2)

    embed = signup_embed(session, roster, "https://cdn.example/a.png").to_dict()
    assert "**full** \u00b7 1 on waitlist" in embed["description"]
    assert embed["color"] == 0x5E8C31
    assert embed["thumbnail"]["url"] == "https://cdn.example/a.png"
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Slots 1\u20132"] == "` 1` user-a\n` 2` user-b"
    assert fields["Waitlist (1)"] == "`W1` user-c"

    async def views():
        open_ids = [i.custom_id for i in bot.build_view(sid).children]
        closed = bot.build_view(sid, closed=True).children
        return open_ids, closed

    open_ids, closed = asyncio.run(views())
    assert open_ids == [f"tenman:{a}:{sid}" for a in ("in", "maybe", "out", "guest", "unguest")]
    assert closed == []


def test_numbered_list_and_autocomplete_labels(q, make_session):
    from datetime import datetime

    from tests.conftest import ET
    from utils.embeds import choice_label, numbered_lines

    start = datetime(2026, 10, 9, 21, 0, tzinfo=ET).timestamp()
    sid = make_session(start, created_at=start - 86400, title="Friday 10-man")
    q.call(db.set_status, sid, "a", "A", db.IN, now=start - 3600)
    session = q.read(db.get_session, sid)
    roster = build_roster(q.read(db.list_signups, sid), 10)

    assert numbered_lines([(session, roster), (session, roster)]).splitlines() == [
        f"`[ 1]` **Friday 10-man** · <t:{int(start)}:f> · 1/10, need 9",
        f"`[ 2]` **Friday 10-man** · <t:{int(start)}:f> · 1/10, need 9",
    ]
    assert choice_label(1, session, roster, ET) == "1 · Friday 10-man · Fri Oct 9 9:00 PM EDT · 1/10"


def test_worst_case_signup_stays_under_discords_embed_limit():
    from utils.embeds import signup_embed
    from utils.roster import build_roster

    nasty = "*_`[]()<:" * 4  # 36 chars, every one escaped by md()
    rows = [{"id": i, "user_id": str(i), "display_name": nasty[:32], "guest_no": 0, "joined_at": i,
             "status": "in" if i < 140 else "maybe"} for i in range(200)]
    session = {"id": 1, "title": "x" * 80, "start_at": 2_000_000_000, "status": "open", "slots": 40,
               "lobby_url": "https://popflash.site/" + "()" * 130}
    embed = signup_embed(session, build_roster(rows, 40), "https://cdn.example/a.png", now=0)
    assert len(embed) <= 6000
    assert all(len(f.value) <= 1024 for f in embed.fields)
    assert len(embed.fields) <= 25
