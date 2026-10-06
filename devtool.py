"""
Developer tool: exercise the alert pipeline against the real webhook, no bot needed.

Uses its own database (data/dev.db) so demo sessions never reach the real bot.

    python devtool.py ping                          post a test line through the webhook
    python devtool.py demo --when "fri 9pm" --players 6
                                                    create a demo sign-up and post its alert now
    python devtool.py alerts                        run one scheduled alert pass (respects ALERT_TIME)
    python devtool.py list                          show demo sessions and rosters
    python devtool.py set-avatar [--image assets/avatar.png]
                                                    upload the bot's profile picture (needs DISCORD_TOKEN)
    python devtool.py preview                       post every message style through the webhook, no pings
"""

import argparse
import tempfile
import asyncio
import dataclasses
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from utils import alerts  # noqa: E402
from utils import database as db  # noqa: E402
from utils.config import load_config  # noqa: E402
from utils.db_queue import DBQueue  # noqa: E402
from utils.exit_codes import EXIT_CONFIG, EXIT_NOTIFY_FAILED, EXIT_OK  # noqa: E402
from utils.roster import build_roster  # noqa: E402
from utils.tracking import setup_logging  # noqa: E402
from utils.webhook import send_message, webhook_config_error  # noqa: E402
from utils.when import WhenError, parse_when  # noqa: E402

DEV_DB = str(Path(__file__).parent / "data" / "dev.db")


def _queue(path: str) -> DBQueue:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    q = DBQueue(path, init=db.ensure_schema)
    q.start()
    return q


def cmd_ping(args, cfg) -> int:
    result = send_message("10-man bot webhook test: delivered")
    print(f"{result.outcome} {result.detail}".strip())
    return EXIT_OK if result.ok else EXIT_NOTIFY_FAILED


def cmd_demo(args, cfg) -> int:
    if not args.ping:
        cfg = dataclasses.replace(cfg, role_id=None)  # demos never ping the real role unless asked
    now = datetime.now(cfg.tz)
    try:
        start = parse_when(args.when, now, cfg.tz)
    except WhenError as exc:
        print(f"bad --when: {exc}")
        return EXIT_CONFIG
    q = _queue(args.db)
    try:
        title = f"{start:%A} 10-man"
        sid = q.call(db.create_session, guild_id=cfg.guild_id or 0, channel_id=cfg.channel_id or 0,
                     title=title, start_at=int(start.timestamp()), created_by="devtool",
                     slots=cfg.default_slots)
        for i in range(args.players):
            q.call(db.set_status, sid, f"demo{i}", f"Player {i + 1}", db.IN, now=time.time() + i / 1000)
        session = q.read(db.get_session, sid)
        roster = build_roster(q.read(db.list_signups, sid), session["slots"])
        kind = alerts.FULL if roster.full else alerts.DAILY
        message = alerts.render_alert(alerts.DueAlert(kind, "manual", now), session, roster, now, cfg)
        print(f"session {sid}: {roster.count}/{roster.slots}, start {start:%a %Y-%m-%d %H:%M %Z}")
        print(f"---\n{message.content}\n---")
        if args.dry_run:
            return EXIT_OK
        result = send_message(message.content, role_id=message.role_id, embeds=[message.embed])
        print(f"{result.outcome} {result.detail}".strip())
        return EXIT_OK if result.ok else EXIT_NOTIFY_FAILED
    finally:
        q.stop()


def cmd_alerts(args, cfg) -> int:
    if not args.ping:
        cfg = dataclasses.replace(cfg, role_id=None)  # like demo: no real pings unless asked
    q = _queue(args.db)
    try:
        results = alerts.run_alerts_once(q, lambda m, _s: send_message(
            m.content, role_id=m.role_id, user_ids=m.user_ids, embeds=[m.embed]), cfg)
    finally:
        q.stop()
    if not results:
        print(f"nothing due (alerts go out at {cfg.alert_time:%H:%M} {cfg.tz.key})")
    for r in results:
        print(r)
    return EXIT_NOTIFY_FAILED if any(r["outcome"] != "delivered" for r in results) else EXIT_OK


async def _bot_user(cfg, avatar: bytes | None = None):
    """Log in over REST only (no gateway), so this works while the bot is running."""
    import discord

    async with discord.Client(intents=discord.Intents(guilds=True)) as client:
        await client.login(cfg.discord_token)
        if avatar is not None:
            await client.user.edit(avatar=avatar)
        # Fetch fresh: the logged-in user object still holds the old avatar after edit().
        me = await client.fetch_user(client.user.id)
        return me.name, me.display_avatar.with_size(256).url


def cmd_set_avatar(args, cfg) -> int:
    if not cfg.discord_token:
        print("DISCORD_TOKEN is not set")
        return EXIT_CONFIG
    path = Path(args.image)
    data = path.read_bytes()
    if len(data) > 8 * 1024 * 1024 or path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        print("use a png/jpg/gif/webp under 8 MB")
        return EXIT_CONFIG
    import discord
    try:
        name, url = asyncio.run(_bot_user(cfg, data))
    except discord.HTTPException as exc:
        # Discord allows only a couple of avatar changes per hour.
        print(f"Discord refused the avatar: HTTP {exc.status} {exc.text}")
        return EXIT_NOTIFY_FAILED
    print(f"avatar set for {name}: {url}")
    return EXIT_OK


def _fake_roster(q, sid, players, *, maybe=0):
    for i in range(players):
        q.call(db.set_status, sid, f"p{i}", f"Player {i + 1}", db.IN, now=time.time() + i / 1000)
    for i in range(maybe):
        q.call(db.set_status, sid, f"m{i}", f"Maybe {i + 1}", db.MAYBE, now=time.time() + 1 + i / 1000)


def cmd_preview(args, cfg) -> int:
    from utils.embeds import signup_embed

    username, avatar = (None, None)
    if cfg.discord_token:
        username, avatar = asyncio.run(_bot_user(cfg))
    cfg = dataclasses.replace(cfg, avatar_url=avatar, role_id=None)  # previews never ping
    now = datetime.now(cfg.tz)
    workdir = tempfile.TemporaryDirectory(prefix="tenman-preview-")  # never grows, never touches real data
    q = _queue(str(Path(workdir.name) / "preview.db"))
    samples = []
    try:
        def make(when, players, *, maybe=0, slots=10, lobby=None, title=None):
            start = parse_when(when, now, cfg.tz)
            sid = q.call(db.create_session, guild_id=cfg.guild_id or 0, channel_id=cfg.channel_id or 0,
                         title=title or f"{start:%A} 10-man", start_at=int(start.timestamp()),
                         created_by="preview", slots=slots, now=time.time() - 86400)
            _fake_roster(q, sid, players, maybe=maybe)
            if lobby:
                q.call(db.set_lobby_url, sid, lobby)
            session = q.read(db.get_session, sid)
            return session, build_roster(q.read(db.list_signups, sid), slots)

        s, r = make("fri 9pm", 6, maybe=2)
        samples.append(("Sign-up post, filling", signup_embed(s, r, avatar).to_dict()))
        s, r = make("sat 9pm", 12, lobby="https://popflash.site/-/wiggz/scrim")
        samples.append(("Sign-up post, full with waitlist and lobby", signup_embed(s, r, avatar).to_dict()))

        s, r = make("fri 9pm", 6, maybe=1)
        m = alerts.render_alert(alerts.DueAlert(alerts.DAILY, "p", now), s, r, now, cfg)
        samples.append(("Daily countdown alert", m.embed, m.content))
        s, r = make("tomorrow 9pm", 8)
        m = alerts.render_alert(alerts.DueAlert(alerts.GAMEDAY, "p", now), s, r, now, cfg)
        samples.append(("Game-day alert (2 h before, still short)", m.embed, m.content))
        s, r = make("sat 9pm", 10)
        m = alerts.render_alert(alerts.DueAlert(alerts.FULL, "p", now), s, r, now, cfg)
        samples.append(("Roster full alert", m.embed, m.content))
        s, r = make("tomorrow 9pm", 10, lobby="https://popflash.site/-/wiggz/scrim")
        m = alerts.render_alert(alerts.DueAlert(alerts.STARTING, "p", now), s, r, now, cfg)
        names = " ".join(f"@{row['display_name'].replace(' ', '')}" for row in r.confirmed)
        samples.append(("Starting now alert (pings the 10 players)", m.embed,
                        m.content.split("now!")[0] + "now! " + names))
    finally:
        q.stop()
        workdir.cleanup()

    failed = 0
    for sample in samples:
        label, embed = sample[0], sample[1]
        content = f"-# preview: {label}" + (f"\n{sample[2]}" if len(sample) > 2 else "")
        result = send_message(content, embeds=[embed], username=username, avatar_url=avatar)
        print(f"{label}: {result.outcome}")
        failed += not result.ok
        time.sleep(1)  # stay well under the webhook rate limit
    return EXIT_NOTIFY_FAILED if failed else EXIT_OK


def cmd_list(args, cfg) -> int:
    q = _queue(args.db)
    try:
        for s in q.read(db.open_sessions):
            roster = build_roster(q.read(db.list_signups, s["id"]), s["slots"])
            start = datetime.fromtimestamp(s["start_at"], cfg.tz)
            print(f"#{s['id']} {s['title']} {start:%a %m-%d %H:%M} {roster.count}/{roster.slots} "
                  f"waitlist {len(roster.waitlist)}")
    finally:
        q.stop()
    return EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEV_DB, help="SQLite file (default data/dev.db)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping")
    demo = sub.add_parser("demo")
    demo.add_argument("--when", default="fri 9pm")
    demo.add_argument("--players", type=int, default=6)
    demo.add_argument("--dry-run", action="store_true", help="print the alert, don't post it")
    demo.add_argument("--ping", action="store_true", help="really ping TENMAN_ROLE_ID")
    alerts_cmd = sub.add_parser("alerts")
    alerts_cmd.add_argument("--ping", action="store_true", help="really ping TENMAN_ROLE_ID")
    sub.add_parser("list")
    avatar = sub.add_parser("set-avatar")
    avatar.add_argument("--image", default=str(Path(__file__).parent / "assets" / "avatar.png"))
    sub.add_parser("preview")
    args = parser.parse_args(argv)

    setup_logging("devtool.log")
    cfg = load_config()
    needs_webhook = args.cmd in ("ping", "alerts", "preview") or (args.cmd == "demo" and not args.dry_run)
    if needs_webhook and webhook_config_error():
        print(webhook_config_error())
        return EXIT_CONFIG
    commands = {"ping": cmd_ping, "demo": cmd_demo, "alerts": cmd_alerts, "list": cmd_list,
                "set-avatar": cmd_set_avatar, "preview": cmd_preview}
    return commands[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
