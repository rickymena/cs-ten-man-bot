"""
Countdown alerts: "Friday 10-man in 3 days needs 4 more (6/10)".

Schedule per open, posted session, in the bot's TIMEZONE:
  daily    every day at ALERT_TIME, from the day the sign-up is posted through
           game day, while the roster is short. Skipped on game day once the
           game-day alert is due or done, so two pings never land together.
  gameday  GAMEDAY_ALERT_HOURS_BEFORE hours before start, while still short
  full     once, the first time the roster fills; daily alerts stop while full
  starting once, at start time: pings the confirmed players with the lobby link

Missed slots follow one rule: sent late if at most
ALERT_CATCHUP_HOURS overdue, otherwise not at all. Each (session, kind, slot)
is claimed in the alerts table before the POST and finished after it, so
restarts and overlapping passes can never double-post. Instants are compared
as unix timestamps: aware datetimes sharing one tzinfo compare by wall clock,
which is wrong across a DST change.

run_alerts_once() is synchronous: call it from a thread (asyncio.to_thread).
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from utils import database as db
from utils import style
from utils.roster import Roster, build_roster
from utils.sanitize import clean_text, md
from utils.webhook import AMBIGUOUS, CONFIG_ERROR, DELIVERED

logger = logging.getLogger(__name__)

DAILY = "daily"
GAMEDAY = "gameday"
FULL = "full"
STARTING = "starting"
STARTING_WINDOW_SECONDS = 30 * 60  # a "starting now" post later than this is pointless


@dataclass(frozen=True)
class AlertMessage:
    content: str                 # what notifications show; carries the pings
    embed: dict | None = None    # plain dict, so the webhook and the bot can both send it
    role_id: str | None = None   # the only role allowed to be pinged
    user_ids: tuple[str, ...] = ()  # the only users allowed to be pinged


@dataclass(frozen=True)
class DueAlert:
    kind: str
    slot_key: str
    due_at: datetime


def due_alerts(session: dict, roster: Roster, now: datetime, cfg, states: dict | None = None) -> list[DueAlert]:
    """
    Alerts due right now. `states` ({(kind, slot_key): row}) lets finished
    ones be skipped without a database write, and decides the game-day rule.
    """
    states = states or {}
    tz = cfg.tz
    now_ts = now.timestamp()
    start_ts = session["start_at"]
    # A scheduled sign-up only counts from when it went up.
    opened_ts = max(session["created_at"], session.get("post_at") or 0)
    catchup = cfg.alert_catchup_hours * 3600
    if session["status"] != db.OPEN or session.get("post_state", db.POSTED) != db.POSTED \
            or not session["alerts_enabled"]:
        return []

    def fresh(kind, key):
        return not db.alert_is_finished(states.get((kind, key)))

    if now_ts >= start_ts:
        if now_ts - start_ts <= STARTING_WINDOW_SECONDS and roster.count and fresh(STARTING, "once"):
            return [DueAlert(STARTING, "once", datetime.fromtimestamp(start_ts, tz))]
        return []
    if roster.full:
        return [DueAlert(FULL, "once", now)] if fresh(FULL, "once") else []

    due = []
    gameday_ts = start_ts - cfg.gameday_alert_hours * 3600
    gameday_due = opened_ts <= gameday_ts <= now_ts and now_ts - gameday_ts <= catchup
    gameday_done = (GAMEDAY, "once") in states
    if gameday_due and fresh(GAMEDAY, "once"):
        due.append(DueAlert(GAMEDAY, "once", datetime.fromtimestamp(gameday_ts, tz)))

    local_now = now.astimezone(tz)
    slot = datetime.combine(local_now.date(), cfg.alert_time, tz)
    slot_ts = slot.timestamp()
    on_game_day = datetime.fromtimestamp(start_ts, tz).date() == local_now.date()
    # A game-day ping already went out today (possibly for a time the game was
    # since moved from; kept as history under "once@<old start>"): no daily too.
    pinged_today = any(
        kind == GAMEDAY and row.get("status") in (db.ALERT_SENT, db.ALERT_AMBIGUOUS)
        and datetime.fromtimestamp(row.get("updated_at") or 0, tz).date() == local_now.date()
        for (kind, _key), row in states.items())
    if (opened_ts <= slot_ts <= now_ts and now_ts - slot_ts <= catchup and slot_ts < start_ts
            and not (on_game_day and (gameday_due or gameday_done)) and not pinged_today
            and fresh(DAILY, slot.date().isoformat())):
        due.append(DueAlert(DAILY, slot.date().isoformat(), slot))
    return due


def _days_phrase(start: datetime, now: datetime) -> str:
    days = (start.date() - now.date()).days
    if days <= 0:
        return "tonight" if start.hour >= 17 else "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def jump_link(session: dict) -> str | None:
    if not session.get("message_id"):
        return None
    return f"https://discord.com/channels/{session['guild_id']}/{session['channel_id']}/{session['message_id']}"


def _embed(title: str, description: str, colour: int, session: dict, cfg, footer: str | None = None) -> dict:
    embed = {
        "title": clean_text(title, 256),
        "description": description,
        "color": colour,
        "author": {"name": style.BRAND},
    }
    link = jump_link(session)
    if link:
        embed["url"] = link  # the title jumps to the sign-up post
    if cfg.avatar_url:
        embed["author"]["icon_url"] = cfg.avatar_url
        embed["thumbnail"] = {"url": cfg.avatar_url}
    if footer:
        embed["footer"] = {"text": footer}
    return embed


def render_alert(alert: DueAlert, session: dict, roster: Roster, now: datetime, cfg) -> AlertMessage:
    start_unix = int(session["start_at"])
    start = datetime.fromtimestamp(start_unix, cfg.tz)
    title = md(session["title"])
    when = f"**Starts** <t:{start_unix}:F> · <t:{start_unix}:R>"
    bar = f"{style.progress_bar(roster.count, roster.slots)} **{roster.count}/{roster.slots}**"
    lobby = session.get("lobby_url")
    lobby_line = f"\n**Lobby** [join here]({style.safe_link(lobby)})" if lobby else ""

    if alert.kind == STARTING:
        user_ids = tuple(sorted({r["user_id"] for r in roster.confirmed}))
        content = f"**{title}** is starting now. " + " ".join(f"<@{u}>" for u in user_ids)
        description = f"{bar} · time to load in" + (
            lobby_line or "\nNo lobby link yet: organizer, run `/tenman lobby`.")
        return AlertMessage(content, _embed(f"{session['title']} is starting", description, style.RED, session, cfg,
                                            "GLHF"), None, user_ids)

    if alert.kind == FULL:
        content = f"**{title}** is full: {roster.count}/{roster.slots}"
        description = (f"{bar} · roster locked\n{when}\n"
                       "Waitlist is open: click In to be next up if someone drops.")
        return AlertMessage(content, _embed(f"{session['title']} is full", description, style.GREEN, session, cfg))

    days = _days_phrase(start, now.astimezone(cfg.tz))
    ping = f"<@&{cfg.role_id}> " if cfg.role_id else ""
    content = f"{ping}**{title}** {days} needs **{roster.need} more** ({roster.count}/{roster.slots})"
    lines = [f"{bar} · need **{roster.need}** more", when]
    if roster.maybe:
        lines.append(f"**Maybe** {len(roster.maybe)}: lock it in if you can make it")
    colour = style.RED if alert.kind == GAMEDAY else style.ORANGE
    footer = "Click the title to jump to the sign-up" if jump_link(session) else None
    return AlertMessage(content, _embed(f"{session['title']} · {days}", "\n".join(lines), colour, session, cfg,
                                        footer), cfg.role_id)


def _status_for(outcome: str) -> str:
    if outcome == DELIVERED:
        return db.ALERT_SENT
    if outcome == AMBIGUOUS:
        return db.ALERT_AMBIGUOUS
    return db.ALERT_FAILED


def _load_open(conn, now_unix):
    sessions = db.open_sessions(conn, now_unix)
    ids = [s["id"] for s in sessions]
    signups = db.signups_for(conn, ids)
    states = db.alert_states_for(conn, ids)
    return [(s, signups[s["id"]], {(k, key): row for (sid, k, key), row in states.items() if sid == s["id"]})
            for s in sessions]


def run_alerts_once(q, send, cfg, now: datetime | None = None) -> list[dict]:
    """
    One pass over open sessions. `send(message: AlertMessage, session) -> PostResult`.
    A CONFIG_ERROR result means the delivery path itself is broken (bad
    webhook): the pass stops, because every other alert would fail the same
    way. Senders report a per-channel problem as FAILED instead.
    Returns a list of {session_id, kind, slot_key, outcome} for what was attempted.
    """
    now = now or datetime.now(cfg.tz)
    results = []
    for session, signups, states in q.read(_load_open, int(now.timestamp())):
        roster = build_roster(signups, session["slots"])
        for alert in due_alerts(session, roster, now, cfg, states):
            if not q.call(db.claim_alert, session["id"], alert.kind, alert.slot_key, now=now.timestamp()):
                continue
            message = render_alert(alert, session, roster, now, cfg)
            try:
                post = send(message, session)
                outcome, detail = post.outcome, post.detail or None
            except Exception as exc:  # a bug in send must not leave the claim 'sending'
                logger.exception(f"alert send crashed for session {session['id']}")
                outcome, detail = AMBIGUOUS, f"send crashed ({type(exc).__name__})"
            q.call(db.finish_alert, session["id"], alert.kind, alert.slot_key, _status_for(outcome), detail,
                   now=now.timestamp())
            logger.info(f"alert {alert.kind}/{alert.slot_key} session {session['id']}: {outcome}")
            results.append({"session_id": session["id"], "kind": alert.kind,
                            "slot_key": alert.slot_key, "outcome": outcome})
            if outcome == CONFIG_ERROR:
                return results
    return results
