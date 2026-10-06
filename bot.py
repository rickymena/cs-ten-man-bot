"""
10-man Discord bot: entry point.

    /tenman start when: fri 9pm          post a sign-up now
    /tenman schedule when: fri 9pm       save one; its sign-up posts automatically N days before
    /tenman schedule ... repeat: weekly  a weekly series; each week's sign-up posts by itself
    /tenman list                         everything coming up in this channel, numbered
    /tenman lobby url:<link>             share the lobby link with the confirmed players
    /tenman cancel [number:N]            cancel one (or stop a weekly series)

Every database write goes through utils/db_queue.py (single writer). Buttons
are DynamicItems keyed by session id, so they keep working after a restart.
A 60 s loop creates weekly occurrences, posts scheduled sign-ups, closes
started games, takes the daily backup and runs the alerts; a 30 s loop
writes the heartbeat. Full documentation: docs/README.md.
"""

import asyncio
import dataclasses
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from utils import alerts  # noqa: E402
from utils import backup  # noqa: E402
from utils import database as db  # noqa: E402
from utils.config import ConfigError, load_config  # noqa: E402
from utils.db_queue import DBQueue, QueueBusy  # noqa: E402
from utils.embeds import BUTTONS, choice_label, numbered_lines, series_lines, signup_embed  # noqa: E402
from utils.exit_codes import EXIT_CONFIG, EXIT_OK  # noqa: E402
from utils.roster import build_roster, promoted  # noqa: E402
from utils.sanitize import (  # noqa: E402
    InputError, clean_display_name, clean_lobby_url, clean_search, clean_title, clean_when, md,
)
from utils.scheduler_state import write_heartbeat  # noqa: E402
from utils.tracking import setup_logging  # noqa: E402
from utils.webhook import AMBIGUOUS, CONFIG_ERROR, DELIVERED, FAILED, PostResult, send_message  # noqa: E402
from utils.when import WhenError, describe, parse_when, typed_zone  # noqa: E402

logger = logging.getLogger("tenman.bot")

HEARTBEAT_PATH = os.getenv("HEARTBEAT_PATH", str(Path(__file__).parent / "logs" / "heartbeat.json"))
SEND_TIMEOUT_SECONDS = 30
# Discord drops an interaction not answered within 3 s; leave room for the reply itself.
INTERACTION_DB_TIMEOUT = 2.0
SCHEDULE_MAX_DAYS = 120
BACKUP_RETRY_SECONDS = 30 * 60
WEEKLY_MAX_POST_DAYS = 6
# View Channel, Send Messages, Embed Links, Read Message History, Mention Everyone
INVITE_PERMISSIONS = 1024 | 2048 | 16384 | 65536 | 131072
NOT_HERE_REPLY = "This bot only works in its home server."
BUSY_REPLY = "The bot is busy, try again in a few seconds."
ERROR_REPLY = "Something went wrong. It's been logged."


# --------------------------------------------------------------------------- helpers

def _roster_for(conn, session_id: int):
    session = db.get_session(conn, session_id)
    return session, build_roster(db.list_signups(conn, session_id), session["slots"])


def _with_rosters(conn, sessions: list[dict]):
    signups = db.signups_for(conn, [s["id"] for s in sessions])
    return [(s, build_roster(signups[s["id"]], s["slots"])) for s in sessions]


def _channel_live(conn, channel_id):
    """Posted, open sign-ups: what lobby acts on."""
    return _with_rosters(conn, db.channel_open_sessions(conn, channel_id))


def _channel_upcoming(conn, channel_id):
    """Posted and scheduled: what list and cancel number."""
    return _with_rosters(conn, db.channel_upcoming(conn, channel_id)), db.channel_series(conn, channel_id)


def build_view(session_id: int, closed: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if not closed:
        for action, *_ in BUTTONS:
            view.add_item(SignupButton(action, session_id))
    return view


def role_mentions(cfg) -> discord.AllowedMentions:
    return discord.AllowedMentions(roles=[discord.Object(int(cfg.role_id))] if cfg.role_id else False,
                                   users=False, everyone=False)


async def answer(interaction: discord.Interaction, content: str) -> None:
    """Ephemeral reply that works whether or not the interaction was already answered."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except discord.HTTPException as exc:
        logger.warning(f"could not answer interaction: HTTP {exc.status}")


def zone_clock(when: datetime) -> str:
    """'9:00 PM EDT': the wall-clock time with its zone spelled out."""
    return f"{when.hour % 12 or 12}:{when:%M} {'PM' if when.hour >= 12 else 'AM'} {when:%Z}"


def is_organizer_or_mod(interaction: discord.Interaction, created_by: str) -> bool:
    perms = interaction.channel.permissions_for(interaction.user)
    return str(interaction.user.id) == str(created_by) or perms.manage_messages


# --------------------------------------------------------------------------- buttons

BUTTON_ID = r"tenman:(?P<action>[a-z]{2,8}):(?P<sid>\d{1,12})"


class SignupButton(discord.ui.DynamicItem[discord.ui.Button], template=BUTTON_ID):
    def __init__(self, action: str, session_id: int):
        label, style, emoji = next((lbl, sty, emo) for a, lbl, sty, emo in BUTTONS if a == action)
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji,
                                           custom_id=f"tenman:{action}:{session_id}"))
        self.action = action
        self.session_id = session_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        if match["action"] not in db.ACTIONS:
            raise ValueError("unknown action")
        return cls(match["action"], int(match["sid"]))

    async def callback(self, interaction: discord.Interaction):
        try:
            await self._click(interaction)
        except Exception:
            logger.exception(f"click {self.action} on session {self.session_id} failed")
            await answer(interaction, ERROR_REPLY)

    async def _click(self, interaction: discord.Interaction):
        bot: TenManBot = interaction.client  # type: ignore[assignment]
        user = interaction.user
        if not bot.in_home_guild(interaction):
            return await answer(interaction, NOT_HERE_REPLY)
        if bot.too_fast(user.id):
            return await answer(interaction, "Slow down a little and try again.")
        try:
            # One job: ownership check (the custom_id is client-supplied, so it
            # is only honoured on this session's own message), the change, and
            # the before/after rosters, all in a single transaction.
            change = await bot.q.run(
                db.apply_click, self.session_id, action=self.action, user_id=user.id,
                display_name=clean_display_name(user.display_name), guild_id=interaction.guild_id,
                message_id=interaction.message.id if interaction.message else None,
                max_guests=bot.cfg.max_guests, timeout=INTERACTION_DB_TIMEOUT)
        except QueueBusy:
            return await answer(interaction, BUSY_REPLY)
        except db.ForeignClick:
            logger.warning(f"button for session {self.session_id} used on a foreign message by {user.id}; ignored")
            return await answer(interaction, "That button doesn't belong to this sign-up.")
        except db.SessionNotFound:
            return await answer(interaction, "This sign-up no longer exists.")
        except db.SessionClosed as exc:
            return await answer(interaction, f"Can't change that: {exc}.")
        except ValueError as exc:
            text = str(exc)
            return await answer(interaction, text[:1].upper() + text[1:] + ".")

        session = change["session"]
        roster = build_roster(change["after"], session["slots"])
        await interaction.response.edit_message(embed=signup_embed(session, roster, bot.icon_url),
                                                view=build_view(self.session_id))
        # before/after come from the same transaction, so two concurrent
        # clicks can never both announce the same promotion.
        before = build_roster(change["before"], session["slots"])
        moved_up = sorted({r["user_id"] for r in promoted(before, roster) if r["user_id"] != str(user.id)})
        if moved_up:
            await interaction.followup.send(
                " ".join(f"<@{i}>" for i in moved_up) + f" you're off the waitlist for **{md(session['title'])}**.",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))


# --------------------------------------------------------------------------- client

class TenManBot(discord.Client):
    def __init__(self, cfg, q: DBQueue):
        super().__init__(intents=discord.Intents.default())
        self.cfg = cfg
        self.q = q
        self._last_click: dict[int, float] = {}
        self._down_since: float | None = time.time()
        self._backup_retry_at = 0.0  # after a failed backup, wait before trying again
        self.tree = app_commands.CommandTree(self)
        self.tree.on_error = self.on_app_command_error
        self.tree.add_command(TenManCommands(self))

    def in_home_guild(self, interaction: discord.Interaction) -> bool:
        return interaction.guild_id is not None and str(interaction.guild_id) == self.cfg.guild_id

    def too_fast(self, user_id: int) -> bool:
        """Per-user click cooldown so one person can't flood the write queue."""
        now = time.monotonic()
        last = self._last_click.get(user_id)
        if last is not None and now - last < self.cfg.click_cooldown_seconds:
            return True
        if len(self._last_click) > 5000:
            self._last_click.clear()
        self._last_click[user_id] = now
        return False

    @property
    def icon_url(self) -> str | None:
        return self.user.display_avatar.with_size(256).url if self.user else None

    async def setup_hook(self):
        self.add_dynamic_items(SignupButton)
        guild = discord.Object(id=int(self.cfg.guild_id))
        self.tree.copy_global_to(guild=guild)
        try:
            await self.tree.sync(guild=guild)
        except discord.Forbidden as exc:
            # 50001: the bot is not in GUILD_ID, or was invited without the
            # applications.commands scope. Nothing works until that is fixed.
            logger.critical(
                f"cannot register slash commands in GUILD_ID={self.cfg.guild_id} ({exc.code}: {exc.text}). "
                "Check GUILD_ID is the server ID and invite the bot with: "
                f"https://discord.com/oauth2/authorize?client_id={self.application_id}"
                f"&scope=bot+applications.commands&permissions={INVITE_PERMISSIONS}")
            raise SystemExit(EXIT_CONFIG) from None
        self.heartbeat.start()
        self.background.start()

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            return await answer(interaction, NOT_HERE_REPLY)
        logger.error(f"command failed: {type(error).__name__}: {error}", exc_info=error)
        await answer(interaction, ERROR_REPLY)

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} ({self.user.id})")
        self.check_channel_access()

    def check_channel_access(self) -> bool:
        """Log exactly which channel permissions are missing. Commands still work without them."""
        if not self.cfg.channel_id:
            return True
        channel = self.get_channel(int(self.cfg.channel_id))
        if channel is None:
            logger.critical(
                f"bot cannot see CHANNEL_ID={self.cfg.channel_id}. Slash commands still work, but sign-up "
                "updates, scheduled posts and bot-posted alerts will fail. Fix: channel settings -> Permissions "
                "-> add the bot with View Channel, Send Messages, Embed Links, Read Message History"
                + (" (alerts fall back to the webhook meanwhile)" if self.cfg.webhook_url else ""))
            return False
        perms = channel.permissions_for(channel.guild.me)
        missing = [name for name in ("view_channel", "send_messages", "embed_links", "read_message_history")
                   if not getattr(perms, name)]
        if missing:
            logger.critical(f"bot is missing {', '.join(missing)} in #{channel.name}")
            return False
        role = channel.guild.get_role(int(self.cfg.role_id)) if self.cfg.role_id else None
        if self.cfg.role_id and role is None:
            logger.critical(f"TENMAN_ROLE_ID={self.cfg.role_id} is not a role in this server")
        elif role is not None and not role.mentionable and not perms.mention_everyone:
            logger.warning(f"@{role.name} is not mentionable and the bot lacks Mention Everyone in "
                           f"#{channel.name}: the mention will show but nobody will be notified")
        logger.info(f"channel access ok: #{channel.name}" + (f", pings @{role.name}" if role else ""))
        return True

    async def close(self):
        self.background.cancel()
        self.heartbeat.cancel()
        await super().close()

    # ----------------------------------------------------------------- sign-up messages

    async def refresh(self, session_id: int) -> None:
        """Re-render a sign-up message after a change made outside its buttons."""
        session, roster = await self.q.aread(_roster_for, session_id)
        if not session.get("message_id"):
            return
        channel = self.get_channel(int(session["channel_id"]))
        if channel is None:
            return
        try:
            await channel.get_partial_message(int(session["message_id"])).edit(
                embed=signup_embed(session, roster, self.icon_url),
                view=build_view(session_id, closed=session["status"] != db.OPEN))
        except discord.HTTPException as exc:
            logger.warning(f"could not refresh sign-up message for session {session_id}: {exc.status}")

    async def post_scheduled(self, session_id: int) -> None:
        """Post one scheduled sign-up: claim, send, record. Never posts twice."""
        claimed = await self.q.run(db.claim_post, session_id)
        if claimed is None:
            return
        session = claimed["session"]
        roster = build_roster(claimed["signups"], session["slots"])
        channel = self.get_channel(int(session["channel_id"]))
        if channel is None:
            state = await self.q.run(db.finish_post, session_id, definitely_failed=True)
            logger.error(f"posting scheduled session {session_id}: channel not visible to the bot; now {state}")
            return
        try:
            ping = f"<@&{self.cfg.role_id}> " if self.cfg.role_id else ""
            weekly = " (weekly)" if session.get("series_id") else ""
            message = await asyncio.wait_for(channel.send(
                f"{ping}Sign-ups are open: **{md(session['title'])}**{weekly}",
                embed=signup_embed(session, roster, self.icon_url), view=build_view(session_id),
                allowed_mentions=role_mentions(self.cfg)), SEND_TIMEOUT_SECONDS)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            # 4xx and 500/502/503: Discord rejected it, safe to retry. 504 may
            # have been posted behind the gateway timeout: treat as unknown.
            definite = getattr(exc, "status", 504) != 504
            state = await self.q.run(db.finish_post, session_id, definitely_failed=definite)
            logger.error(f"posting scheduled session {session_id} failed ({type(exc).__name__}); now {state}")
            return
        except (TimeoutError, OSError) as exc:
            # Unknown whether Discord posted it: record as posted, never resend.
            await self.q.run(db.finish_post, session_id, message_id=None)
            logger.error(f"posting scheduled session {session_id}: outcome unknown ({type(exc).__name__})")
            return
        await self.q.run(db.finish_post, session_id, message_id=message.id)
        logger.info(f"scheduled session {session_id} posted")

    # ----------------------------------------------------------------- alerts

    def _send_alert(self, message: alerts.AlertMessage, channel_id: str) -> PostResult:
        """Called from the alerts thread. Prefers the bot itself; webhook if no channel access."""
        if self.cfg.webhook_url and self.cfg.alerts_via == "webhook":
            return self._send_alert_as_webhook(message)
        result = self._send_alert_as_bot(message, channel_id)
        if result.outcome == CONFIG_ERROR:
            if self.cfg.webhook_url:
                # The bot cannot post there and Discord definitely did not
                # accept the message, so the webhook cannot double-post it.
                logger.warning(f"bot cannot post alerts ({result.detail}); using the webhook instead")
                return self._send_alert_as_webhook(message)
            # One unreachable channel must not stop alerts for every other one.
            return PostResult(FAILED, status_code=result.status_code, detail=result.detail)
        return result

    def _send_alert_as_webhook(self, message: alerts.AlertMessage) -> PostResult:
        return send_message(message.content, role_id=message.role_id, user_ids=message.user_ids,
                            embeds=[message.embed] if message.embed else None,
                            username=self.user.name if self.user else None, avatar_url=self.icon_url)

    def _send_alert_as_bot(self, message: alerts.AlertMessage, channel_id: str) -> PostResult:
        channel = self.get_channel(int(channel_id))
        if channel is None:
            return PostResult(CONFIG_ERROR, detail=f"channel {channel_id} not visible to the bot")
        mentions = discord.AllowedMentions(
            everyone=False,
            users=[discord.Object(int(u)) for u in message.user_ids] or False,
            roles=[discord.Object(int(message.role_id))] if message.role_id else False)
        embed = discord.Embed.from_dict(message.embed) if message.embed else None
        future = asyncio.run_coroutine_threadsafe(
            channel.send(message.content, embed=embed, allowed_mentions=mentions), self.loop)
        try:
            future.result(SEND_TIMEOUT_SECONDS)
        except TimeoutError:
            future.cancel()
            return PostResult(AMBIGUOUS, detail="send timed out")
        except (discord.Forbidden, discord.NotFound) as exc:
            return PostResult(CONFIG_ERROR, status_code=exc.status, detail=type(exc).__name__)
        except discord.HTTPException as exc:
            return PostResult(FAILED, status_code=exc.status, detail=f"HTTP {exc.status}")
        except Exception as exc:
            return PostResult(AMBIGUOUS, detail=type(exc).__name__)
        return PostResult(DELIVERED)

    # ----------------------------------------------------------------- loops

    async def tick(self, now: datetime | None = None) -> None:
        """One pass of the background work. Each step is isolated: one failure doesn't stop the rest."""
        now = now or datetime.now(self.cfg.tz)
        try:
            for sid in await self.q.run(db.materialize_series, now, self.cfg.tz):
                logger.info(f"weekly series created session {sid}")
        except Exception:
            logger.exception("series pass failed")
        try:
            for session in await self.q.aread(db.due_posts, int(now.timestamp())):
                await self.post_scheduled(session["id"])
        except Exception:
            logger.exception("scheduled posting pass failed")
        try:
            for sid in await self.q.run(db.close_started, int(now.timestamp())):
                await self.refresh(sid)  # strip the buttons from games that have started
        except Exception:
            logger.exception("closing started games failed")
        if (self.cfg.backup_dir and now.timetz().replace(tzinfo=None) >= self.cfg.backup_time
                and time.monotonic() >= self._backup_retry_at):
            try:
                # Off the event loop: compressing and verifying takes a moment.
                path = await asyncio.to_thread(backup.run_daily, self.cfg.db_path, self.cfg.backup_dir, now)
                if path:
                    logger.info(f"daily backup done: {path}")
            except Exception:
                self._backup_retry_at = time.monotonic() + BACKUP_RETRY_SECONDS
                logger.exception(f"daily backup failed; retrying in {BACKUP_RETRY_SECONDS // 60} min")
        try:
            def send(message, session):
                return self._send_alert(message, session["channel_id"])
            cfg = dataclasses.replace(self.cfg, avatar_url=self.icon_url)
            await asyncio.to_thread(alerts.run_alerts_once, self.q, send, cfg, now)
        except Exception:
            logger.exception("alert pass failed")

    @tasks.loop(seconds=60)
    async def background(self):
        await self.tick()

    @tasks.loop(seconds=30)
    async def heartbeat(self):
        # Its own loop: an alert pass waiting out Discord rate limits must not
        # make the healthcheck think the bot has stalled.
        try:
            ready = self.is_ready() and not self.is_closed()
            if ready:
                self._down_since = None
            elif self._down_since is None:
                self._down_since = time.time()
            write_heartbeat(HEARTBEAT_PATH, {"queue": self.q.stats(), "gateway_ready": ready,
                                             "gateway_down_since_unix": self._down_since})
        except OSError as exc:
            logger.error(f"heartbeat write failed: {exc}")

    @background.before_loop
    async def _wait_ready(self):
        await self.wait_until_ready()


# --------------------------------------------------------------------------- commands

REPEAT_CHOICES = [app_commands.Choice(name="once", value="once"), app_commands.Choice(name="weekly", value="weekly")]


class TenManCommands(app_commands.Group):
    def __init__(self, bot: TenManBot):
        super().__init__(name="tenman", description="Organize a 10-man", guild_only=True)
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return self.bot.in_home_guild(interaction)

    def _parse(self, when: str, title: str | None, max_days: int):
        cfg = self.bot.cfg
        start_at = parse_when(clean_when(when), datetime.now(cfg.tz), cfg.tz, max_days=max_days)
        title = clean_title(title) if title is not None else None
        return start_at, title

    async def _when_choices(self, interaction: discord.Interaction, current: str, max_days: int):
        """Show how the bot reads the time while it is typed, before anything is sent."""
        if not self.bot.in_home_guild(interaction):
            return []
        tz = self.bot.cfg.tz
        now = datetime.now(tz)
        typed = clean_search(current)
        if not typed.strip():
            picks = []
            for example in ("tonight 9pm", "tomorrow 9pm", "fri 9pm", "sat 9pm", "sun 9pm"):
                try:
                    picks.append(parse_when(example, now, tz, max_days))
                except WhenError:
                    continue
            return [app_commands.Choice(name=describe(t), value=f"{t:%Y-%m-%d %H:%M}")
                    for t in sorted(set(picks), key=lambda t: t.timestamp())]
        try:
            when = parse_when(typed, now, tz, max_days)
        except WhenError as exc:
            return [app_commands.Choice(name=f"Not understood yet: {exc}"[:100], value=typed[:100])]
        zone = typed_zone(typed)
        name = describe(when)
        if zone is not None and when.astimezone(zone).utcoffset() != when.utcoffset():
            name = f"{describe(when.astimezone(zone))} = {zone_clock(when)}"
        # The value is in the bot's zone, so it re-parses to the same instant.
        return [app_commands.Choice(name=name[:100], value=f"{when:%Y-%m-%d %H:%M}")]

    # ----------------------------------------------------------------- start

    @app_commands.command(name="start", description="Post a sign-up for a 10-man now")
    @app_commands.describe(when="e.g. sat 9pm, tomorrow 10:30pm, 10/10 9pm; Eastern unless you add ct/mt/pt",
                           title="Optional title, defaults to '<Weekday> 10-man'",
                           slots="Players needed (default 10)",
                           lobby="Optional lobby link (https://...), shown on the sign-up")
    async def start(self, interaction: discord.Interaction, when: str,
                    title: str | None = None, slots: app_commands.Range[int, 2, 40] | None = None,
                    lobby: str | None = None):
        cfg = self.bot.cfg
        try:
            start_at, title = self._parse(when, title, 30)
            lobby = clean_lobby_url(lobby, cfg.lobby_allowed_hosts) if lobby else None
        except (InputError, WhenError) as exc:
            return await answer(interaction, f"Couldn't use that: {exc}.")
        title = title or f"{start_at:%A} 10-man"
        try:
            # One job: create + organizer In, so a failure can't leave half a sign-up.
            created = await self.bot.q.run(
                db.start_session, guild_id=interaction.guild_id, channel_id=interaction.channel_id, title=title,
                start_at=int(start_at.timestamp()), created_by=interaction.user.id,
                slots=min(max(int(slots or cfg.default_slots), 2), 40),
                max_per_channel=cfg.max_open_per_channel, max_per_user=cfg.max_open_per_user, lobby_url=lobby,
                organizer_name=clean_display_name(interaction.user.display_name), timeout=INTERACTION_DB_TIMEOUT)
        except db.LimitReached as exc:
            return await answer(interaction, f"Can't start another one: {exc}.")
        except QueueBusy:
            return await answer(interaction, BUSY_REPLY)
        session = created["session"]
        roster = build_roster(created["signups"], session["slots"])
        ping = f"<@&{cfg.role_id}> " if cfg.role_id else ""
        try:
            await interaction.response.send_message(
                f"{ping}Sign-ups are open: **{md(title)}**",
                embed=signup_embed(session, roster, self.bot.icon_url), view=build_view(session["id"]),
                allowed_mentions=role_mentions(cfg))
            message = await interaction.original_response()
        except discord.HTTPException:
            # Never leave a session with no message: it would count against
            # the limits and still trigger alerts for a post nobody can see.
            await self.bot.q.run(db.cancel_if_open, session["id"])
            raise
        await self.bot.q.run(db.set_message_id, session["id"], message.id)
        logger.info(f"session {session['id']} created by {interaction.user.id} for {start_at.isoformat()}")

    @start.autocomplete("when")
    async def _start_when(self, interaction, current: str):
        return await self._when_choices(interaction, current, 30)

    # ----------------------------------------------------------------- schedule

    @app_commands.command(name="schedule", description="Schedule a 10-man; its sign-up posts automatically")
    @app_commands.describe(when="First game, e.g. fri 9pm or oct 30 9pm; Eastern unless you add ct/mt/pt",
                           repeat="once, or weekly on the same day and time",
                           weeks="Weekly only: how many weeks (leave empty to run until stopped)",
                           post_days="Days before the game to post the sign-up (default 3)",
                           title="Optional title, defaults to '<Weekday> 10-man'",
                           slots="Players needed (default 10)",
                           lobby="Optional lobby link; for weekly, used every week")
    @app_commands.choices(repeat=REPEAT_CHOICES)
    async def schedule(self, interaction: discord.Interaction, when: str,
                       repeat: app_commands.Choice[str] | None = None,
                       weeks: app_commands.Range[int, 1, 52] | None = None,
                       post_days: app_commands.Range[int, 0, 14] | None = None,
                       title: str | None = None, slots: app_commands.Range[int, 2, 40] | None = None,
                       lobby: str | None = None):
        cfg = self.bot.cfg
        try:
            start_at, title = self._parse(when, title, SCHEDULE_MAX_DAYS)
            lobby = clean_lobby_url(lobby, cfg.lobby_allowed_hosts) if lobby else None
        except (InputError, WhenError) as exc:
            return await answer(interaction, f"Couldn't use that: {exc}.")
        weekly = repeat is not None and repeat.value == "weekly"
        days_before = cfg.schedule_post_days if post_days is None else int(post_days)
        capped = weekly and days_before > WEEKLY_MAX_POST_DAYS
        if capped:
            # A series keeps one game ahead, created when the previous one starts,
            # so a weekly sign-up can't go up more than 6 days early.
            days_before = WEEKLY_MAX_POST_DAYS
        slots = min(max(int(slots or cfg.default_slots), 2), 40)
        now_ts = int(time.time())
        # Post N days ahead at the game's wall-clock time; if that's already
        # past, it posts on the next loop pass (within a minute).
        post_at = max(int(db.post_time(start_at, days_before).timestamp()), now_ts + 1)

        def create(conn):
            series_id = None
            if weekly:
                series_id = db.create_series(
                    conn, guild_id=interaction.guild_id, channel_id=interaction.channel_id, title=title,
                    weekday=start_at.weekday(), local_time=f"{start_at:%H:%M}", slots=slots, post_days=days_before,
                    remaining=None if weeks is None else int(weeks) - 1, created_by=interaction.user.id,
                    lobby_url=lobby, max_per_channel=cfg.max_series_per_channel)
            sid = db.create_session(
                conn, guild_id=interaction.guild_id, channel_id=interaction.channel_id,
                title=title or f"{start_at:%A} 10-man", start_at=int(start_at.timestamp()),
                created_by=interaction.user.id, slots=slots, now=now_ts, post_at=post_at, series_id=series_id,
                lobby_url=lobby, max_scheduled=cfg.max_scheduled_per_channel)
            return sid, series_id

        try:
            sid, series_id = await self.bot.q.run(create, timeout=INTERACTION_DB_TIMEOUT)
        except db.LimitReached as exc:
            return await answer(interaction, f"Can't schedule that: {exc}.")
        except QueueBusy:
            return await answer(interaction, BUSY_REPLY)

        name = md(title or f"{start_at:%A} 10-man")
        start_unix = int(start_at.timestamp())
        if weekly:
            length = f"for {weeks} weeks" if weeks else "until stopped"
            text = (f"Scheduled **{name}** every {start_at:%A} at {zone_clock(start_at)} "
                    f"(<t:{start_unix}:t> your time), {length}.\n"
                    f"First game <t:{start_unix}:F>. Each sign-up posts {days_before} day(s) before, "
                    f"the first one <t:{post_at}:R>.")
        else:
            text = (f"Scheduled **{name}** for {start_at:%A} at {zone_clock(start_at)} "
                    f"(<t:{start_unix}:F> your time). The sign-up posts <t:{post_at}:R>.")
        if lobby:
            text += "\nLobby link saved" + (" for every week." if weekly else ".")
        if capped:
            text += f"\nWeekly sign-ups post at most {WEEKLY_MAX_POST_DAYS} days ahead, so post_days was set to that."
        await interaction.response.send_message(text + "\nSee everything with `/tenman list`.",
                                                allowed_mentions=discord.AllowedMentions.none())
        logger.info(f"session {sid} scheduled by {interaction.user.id} (series {series_id}), posts at {post_at}")

    @schedule.autocomplete("when")
    async def _schedule_when(self, interaction, current: str):
        return await self._when_choices(interaction, current, SCHEDULE_MAX_DAYS)

    # ----------------------------------------------------------------- picking

    async def _pick(self, interaction: discord.Interaction, number: int | None, verb: str, *, upcoming: bool):
        """
        Resolve which sign-up a command means, ufw style: with one candidate
        it is used directly; with several and no number, reply with the
        numbered list. Returns (session, roster) or None after replying.
        """
        if upcoming:
            rows, _ = await self.bot.q.aread(_channel_upcoming, interaction.channel_id)
        else:
            rows = await self.bot.q.aread(_channel_live, interaction.channel_id)
        if not rows:
            await answer(interaction, "Nothing open or scheduled in this channel." if upcoming
                         else "No open sign-ups in this channel.")
            return None
        if number is None and len(rows) == 1:
            return rows[0]
        if number is None or not 1 <= number <= len(rows):
            head = "Pick one:" if number is None else f"There is no [{number}]."
            await answer(interaction, f"{head}\n{numbered_lines(rows)}\n\nRun `/tenman {verb} number:<N>`.")
            return None
        return rows[number - 1]

    async def _choices(self, interaction: discord.Interaction, current: str, upcoming: bool):
        if not self.bot.in_home_guild(interaction):
            return []
        current = clean_search(current).strip().lower()
        if upcoming:
            rows, _ = await self.bot.q.aread(_channel_upcoming, interaction.channel_id)
        else:
            rows = await self.bot.q.aread(_channel_live, interaction.channel_id)
        return [app_commands.Choice(name=choice_label(n, s, r, self.bot.cfg.tz), value=n)
                for n, (s, r) in enumerate(rows, 1)
                if current in (str(n), "") or current in s["title"].lower()][:25]

    # ----------------------------------------------------------------- list / lobby / cancel

    @app_commands.command(name="list", description="Everything coming up in this channel, numbered")
    async def list_(self, interaction: discord.Interaction):
        rows, series = await self.bot.q.aread(_channel_upcoming, interaction.channel_id)
        if not rows and not series:
            return await answer(interaction, "Nothing open or scheduled in this channel. "
                                             "Use `/tenman start` or `/tenman schedule`.")
        parts = []
        if rows:
            parts.append(f"**Upcoming**\n{numbered_lines(rows)}")
        if series:
            parts.append(f"**Weekly series**\n{series_lines(series, self.bot.cfg.tz)}")
        parts.append("Use the number with `/tenman cancel number:N` or `/tenman lobby number:N`.")
        text = "\n\n".join(parts)
        if len(text) > 2000:  # cut on a line boundary so no markdown is left half-open
            lines, kept = text.splitlines(), []
            for line in lines:
                if len("\n".join(kept + [line])) > 1960:
                    break
                kept.append(line)
            text = "\n".join(kept) + f"\n...and {len(lines) - len(kept)} more lines"
        await answer(interaction, text)

    @app_commands.command(name="lobby", description="Set the lobby link for a 10-man (number from /tenman list)")
    @app_commands.describe(url="Lobby link (https://...)",
                           number="Which game, from /tenman list (needed when there are several)",
                           whole_series="Weekly games: use this link for every week, not just this one")
    async def lobby(self, interaction: discord.Interaction, url: str, number: int | None = None,
                    whole_series: bool = False):
        try:
            url = clean_lobby_url(url, self.bot.cfg.lobby_allowed_hosts)
        except InputError as exc:
            return await answer(interaction, f"Couldn't use that link: {exc}.")
        picked = await self._pick(interaction, number, "lobby url:<link>", upcoming=True)
        if picked is None:
            return
        session, roster = picked
        posted = session["post_state"] == db.POSTED
        confirmed = {r["user_id"] for r in roster.confirmed}
        allowed = is_organizer_or_mod(interaction, session["created_by"]) or (
            posted and not whole_series and str(interaction.user.id) in confirmed)
        if not allowed:
            return await answer(interaction, "Only the organizer, a moderator or a confirmed player can set the lobby"
                                             " (only the organizer or a moderator for scheduled or weekly games).")
        try:
            updated = await self.bot.q.run(db.set_lobby_url, session["id"], url, whole_series=whole_series,
                                           timeout=INTERACTION_DB_TIMEOUT)
        except QueueBusy:
            return await answer(interaction, BUSY_REPLY)
        if not posted:
            # Nobody is signed up yet: save it quietly; it shows on the sign-up when it posts.
            scope = " and every later week" if whole_series and session.get("series_id") else ""
            return await answer(interaction, f"Lobby saved for **{md(session['title'])}** (<t:{session['start_at']}:f>)"
                                             f"{scope}. It'll be on the sign-up when it posts.")
        roster = build_roster(updated["signups"], session["slots"])
        ids = sorted({r["user_id"] for r in roster.confirmed})
        await interaction.response.send_message(
            f"Lobby for **{md(session['title'])}**: {url}\n" + " ".join(f"<@{i}>" for i in ids),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        await self.bot.refresh(session["id"])

    @lobby.autocomplete("number")
    async def _lobby_number(self, interaction, current: str):
        return await self._choices(interaction, current, upcoming=True)

    @app_commands.command(name="cancel", description="Cancel a 10-man, or stop a weekly series")
    @app_commands.describe(number="Which one, from the numbered list (leave empty to see it)")
    async def cancel(self, interaction: discord.Interaction, number: int | None = None):
        picked = await self._pick(interaction, number, "cancel", upcoming=True)
        if picked is None:
            return
        session, roster = picked
        if not is_organizer_or_mod(interaction, session["created_by"]):
            return await answer(interaction, "Only the organizer or a moderator can cancel.")
        status = ("scheduled, not posted yet" if session["post_state"] != db.POSTED
                  else f"{roster.count}/{roster.slots} signed up")
        await interaction.response.send_message(
            f"Cancel **{md(session['title'])}** · <t:{session['start_at']}:f> · {status}?",
            view=ConfirmCancel(self.bot, session), ephemeral=True)

    @cancel.autocomplete("number")
    async def _cancel_number(self, interaction, current: str):
        return await self._choices(interaction, current, upcoming=True)


class ConfirmCancel(discord.ui.View):
    """Ephemeral yes/no before cancelling, like ufw's "Proceed with operation (y|n)?"."""

    def __init__(self, bot: TenManBot, session: dict):
        super().__init__(timeout=60)
        self.bot = bot
        self.session = session
        if not session.get("series_id"):
            self.remove_item(self.stop_series)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        logger.exception("cancel confirmation failed", exc_info=error)
        await answer(interaction, ERROR_REPLY)

    async def _announce(self, interaction: discord.Interaction, text: str) -> None:
        await interaction.followup.send(text, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Cancel it", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = self.session
        try:
            cancelled = await self.bot.q.run(db.cancel_if_open, session["id"], timeout=INTERACTION_DB_TIMEOUT)
        except QueueBusy:
            return await interaction.response.edit_message(content=BUSY_REPLY, view=None)
        if not cancelled:
            return await interaction.response.edit_message(content="That one was already closed.", view=None)
        await interaction.response.edit_message(content="Cancelled.", view=None)
        await self._announce(interaction, f"**{md(session['title'])}** (<t:{session['start_at']}:f>) was "
                                          f"cancelled by {interaction.user.mention}.")
        await self.bot.refresh(session["id"])
        logger.info(f"session {session['id']} cancelled by {interaction.user.id}")

    @discord.ui.button(label="Stop the whole series", style=discord.ButtonStyle.danger)
    async def stop_series(self, interaction: discord.Interaction, button: discord.ui.Button):
        series_id = self.session["series_id"]

        def stop(conn):
            ids = [r[0] for r in conn.execute("SELECT id FROM sessions WHERE series_id = ? AND status = 'open'",
                                              (series_id,))]
            db.stop_series(conn, series_id)
            return ids

        try:
            ids = await self.bot.q.run(stop, timeout=INTERACTION_DB_TIMEOUT)
        except QueueBusy:
            return await interaction.response.edit_message(content=BUSY_REPLY, view=None)
        await interaction.response.edit_message(content="Series stopped.", view=None)
        await self._announce(interaction, f"The weekly **{md(self.session['title'])}** series was stopped by "
                                          f"{interaction.user.mention}.")
        for sid in ids:
            await self.bot.refresh(sid)
        logger.info(f"series {series_id} stopped by {interaction.user.id}")

    @discord.ui.button(label="Keep it", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Nothing cancelled.", view=None)


def running_as_real_root() -> bool:
    """uid 0 that is root on the host too (Docker without a user:), not rootless Podman's mapped root."""
    if os.getuid() != 0:
        return False
    try:
        with open("/proc/self/uid_map", encoding="ascii") as handle:
            inside, outside, _count = handle.readline().split()
        return inside == "0" and outside == "0"
    except (OSError, ValueError):
        return True  # can't tell: assume the worst


def main() -> int:
    setup_logging()
    if running_as_real_root() and os.getenv("TENMAN_ALLOW_ROOT") != "1":
        logger.error("refusing to run as root on the host. Run it as its own user, tenman (uid 10001): keep "
                     "`user: \"10001:10001\"` in the compose file. Override, not recommended: TENMAN_ALLOW_ROOT=1")
        return EXIT_CONFIG
    try:
        cfg = load_config()
    except ConfigError as exc:
        logger.error(f"bad settings in .env: {exc}")
        return EXIT_CONFIG
    missing = [n for n, v in (("DISCORD_TOKEN", cfg.discord_token), ("GUILD_ID", cfg.guild_id)) if not v]
    if missing:
        logger.error(f"missing settings in .env: {', '.join(missing)}")
        return EXIT_CONFIG

    q = DBQueue(cfg.db_path, init=db.ensure_schema)
    try:
        os.makedirs(os.path.dirname(cfg.db_path), exist_ok=True)
        q.start()
    except db.SchemaVersionError as exc:
        logger.error(f"{exc}. This image is older than the database: deploy the newer image (or restore a "
                     "backup taken by this version).")
        return EXIT_CONFIG
    except (OSError, sqlite3.Error) as exc:
        logger.error(f"cannot open the database at {cfg.db_path}: {exc}. The data folder must exist and be "
                     f"writable by uid {os.getuid()}. Give the folders to the bot's user (10001): "
                     "Docker `sudo chown -R 10001:10001 data logs backups secrets`, "
                     "Podman `podman unshare chown -R 10001:10001 data logs backups secrets`")
        return EXIT_CONFIG
    stale = q.call(db.recover_stale_sends)
    if stale:
        logger.warning(f"{stale} send(s) were in flight at last shutdown; marked done, not resent")

    bot = TenManBot(cfg, q)
    try:
        bot.run(cfg.discord_token, log_handler=None)
    finally:
        q.stop()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
