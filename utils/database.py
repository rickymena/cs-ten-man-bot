"""
SQLite data layer: schema, migrations and the queries the bot runs.

Adapted from the steam tracker's GameDatabase (utils/database_entry_comparision.py):
the ordered MIGRATIONS list, the schema_migrations table and SchemaVersionError
carry over. Postgres advisory locks are gone: every write goes through
utils/db_queue.py, whose single writer thread already serializes them.

Every function that writes takes the writer's connection first and is meant
to be submitted to the queue (`await q.run(db.apply_click, ...)`). They never
commit or roll back themselves: the queue wraps each job in one BEGIN
IMMEDIATE transaction, so read-then-write logic inside one function is atomic.

Session lifecycle
-----------------
  status      open -> cancelled | done          (the game itself)
  post_state  scheduled -> posting -> posted    (its sign-up message)

A session made with /tenman start is posted at once. One made with
/tenman schedule, or by a weekly series, waits in `scheduled` until post_at,
when the bot posts it. Only open + posted sessions take clicks and alerts.
"""

import logging
import sqlite3
import time
from datetime import datetime, timedelta, time as dtime

logger = logging.getLogger(__name__)

# Signup statuses
IN = "in"
MAYBE = "maybe"
OUT = "out"
STATUSES = (IN, MAYBE, OUT)

# Session statuses
OPEN = "open"
CANCELLED = "cancelled"
DONE = "done"

# Post states
SCHEDULED = "scheduled"
POSTING = "posting"
POSTED = "posted"
MAX_POST_ATTEMPTS = 5

# Alert delivery states (same contract as the steam tracker's discord_sent):
#   sending   claimed, POST in flight. Left behind by a crash mid-send, it is
#             treated as ambiguous: Discord may already have posted it.
#   sent      confirmed 2xx
#   failed    definitely not posted; re-eligible while its slot is still due
#   ambiguous may have been posted; never retried, to avoid a double alert
ALERT_SENDING = "sending"
ALERT_SENT = "sent"
ALERT_FAILED = "failed"
ALERT_AMBIGUOUS = "ambiguous"
MAX_ALERT_ATTEMPTS = 3

# Click actions (button custom_id verbs)
GUEST = "guest"
UNGUEST = "unguest"
ACTIONS = (IN, MAYBE, OUT, GUEST, UNGUEST)

DDL = """
CREATE TABLE IF NOT EXISTS series (
    id              INTEGER PRIMARY KEY,
    guild_id        TEXT    NOT NULL,
    channel_id      TEXT    NOT NULL,
    title           TEXT,                          -- NULL: '<Weekday> 10-man'
    weekday         INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    local_time      TEXT    NOT NULL,              -- HH:MM in the bot's TIMEZONE
    slots           INTEGER NOT NULL DEFAULT 10 CHECK (slots BETWEEN 2 AND 40),
    post_days       INTEGER NOT NULL CHECK (post_days BETWEEN 0 AND 14),
    remaining       INTEGER,                       -- occurrences left to create; NULL = until cancelled
    lobby_url       TEXT,                          -- copied onto each week's game
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_by      TEXT    NOT NULL,
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY,
    guild_id        TEXT    NOT NULL,
    channel_id      TEXT    NOT NULL,
    message_id      TEXT,
    title           TEXT    NOT NULL,
    start_at        INTEGER NOT NULL,              -- unix seconds, UTC
    slots           INTEGER NOT NULL DEFAULT 10 CHECK (slots BETWEEN 2 AND 40),
    status          TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open','cancelled','done')),
    created_by      TEXT    NOT NULL,
    created_at      INTEGER NOT NULL,
    lobby_url       TEXT,
    alerts_enabled  INTEGER NOT NULL DEFAULT 1 CHECK (alerts_enabled IN (0,1)),
    post_state      TEXT    NOT NULL DEFAULT 'posted' CHECK (post_state IN ('scheduled','posting','posted')),
    post_at         INTEGER,                       -- when a scheduled sign-up goes up
    post_attempts   INTEGER NOT NULL DEFAULT 0,
    series_id       INTEGER REFERENCES series(id)
);

-- One row per roster spot. A guest is a row whose user_id is the sponsor's and
-- guest_no > 0; the sponsor's own row has guest_no = 0. Roster order is
-- (joined_at, id): position is computed, never stored, so a dropout promotes
-- the waitlist head without any write.
CREATE TABLE IF NOT EXISTS signups (
    id              INTEGER PRIMARY KEY,
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL,
    display_name    TEXT    NOT NULL,
    guest_no        INTEGER NOT NULL DEFAULT 0 CHECK (guest_no >= 0),
    status          TEXT    NOT NULL CHECK (status IN ('in','maybe','out')),
    joined_at       REAL    NOT NULL,
    checked_in      INTEGER NOT NULL DEFAULT 0 CHECK (checked_in IN (0,1)),  -- reserved for check-in
    UNIQUE (session_id, user_id, guest_no)
);
CREATE INDEX IF NOT EXISTS signups_by_session ON signups (session_id, status, joined_at, id);

CREATE TABLE IF NOT EXISTS alerts (
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind            TEXT    NOT NULL,              -- daily | gameday | full | starting
    slot_key        TEXT    NOT NULL,              -- local date, or 'once'
    status          TEXT    NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (session_id, kind, slot_key)
);
"""

# Indexes on columns that a migration adds: created after migrations run, so
# an old database gets its columns before an index refers to them.
POST_DDL = """
CREATE INDEX IF NOT EXISTS sessions_by_state ON sessions (status, post_state, start_at);
CREATE INDEX IF NOT EXISTS sessions_by_series ON sessions (series_id, start_at);
"""

MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  INTEGER NOT NULL
);
"""


def _add_column(conn, table: str, column: str, ddl: str) -> None:
    """ADD COLUMN has no IF NOT EXISTS in SQLite; check first so it is idempotent."""
    if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migration_2(conn) -> None:
    # Scheduling. Added as columns, not a table rebuild: rebuilding sessions
    # would fire ON DELETE CASCADE and wipe every signup.
    _add_column(conn, "sessions", "post_state",
                "TEXT NOT NULL DEFAULT 'posted' CHECK (post_state IN ('scheduled','posting','posted'))")
    _add_column(conn, "sessions", "post_at", "INTEGER")
    _add_column(conn, "sessions", "post_attempts", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "sessions", "series_id", "INTEGER REFERENCES series(id)")


def _migration_3(conn) -> None:
    # Default lobby link for a weekly series.
    _add_column(conn, "series", "lobby_url", "TEXT")


# Version 1 is the original baseline. Every later schema change is appended
# here as (version, callable) -- strictly increasing, never edited once
# released, idempotent because DDL already creates fresh databases at the
# latest shape.
BASELINE_VERSION = 1
MIGRATIONS = [
    (2, _migration_2),
    (3, _migration_3),
]
LATEST_SCHEMA_VERSION = max([BASELINE_VERSION] + [v for v, _ in MIGRATIONS])


class SchemaVersionError(RuntimeError):
    """The database schema is newer than this build understands."""


class SessionNotFound(LookupError):
    pass


class SessionClosed(RuntimeError):
    """str(exc) is safe to show the user."""


class ForeignClick(PermissionError):
    """A button whose custom_id names this session, used somewhere else."""


class LimitReached(RuntimeError):
    """Too many sign-ups. str(exc) is safe to show the user."""


class EditRefused(RuntimeError):
    """An edit or series update that can't be applied. str(exc) is safe to show the user."""


def ensure_schema(conn: sqlite3.Connection) -> int:
    """Create or migrate the schema. Runs as a queue job, inside its transaction."""
    for statement in _split(MIGRATIONS_DDL + DDL):
        conn.execute(statement)
    current = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    if current is None:
        conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                     (LATEST_SCHEMA_VERSION, int(time.time())))
        logger.info(f"Created schema at version {LATEST_SCHEMA_VERSION}")
        current = LATEST_SCHEMA_VERSION
    elif current > LATEST_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"database schema is version {current}, this build understands up to {LATEST_SCHEMA_VERSION}")
    else:
        for version, migrate in MIGRATIONS:
            if version > current:
                migrate(conn)
                conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                             (version, int(time.time())))
                logger.info(f"Applied schema migration {version}")
        current = LATEST_SCHEMA_VERSION
    for statement in _split(POST_DDL):
        conn.execute(statement)
    return current


def _split(script: str) -> list[str]:
    # executescript() issues its own COMMIT, which would break the queue's
    # one-transaction-per-job contract; run statements one by one instead.
    # Comments are stripped first (whole-line and trailing): a ';' inside one
    # would otherwise split a statement. The DDL has no '--' inside literals.
    lines = [ln.split("--", 1)[0] for ln in script.splitlines()]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def _rows(cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]


# --------------------------------------------------------------------------- sessions

def create_session(conn, *, guild_id, channel_id, title, start_at, created_by, slots=10, now=None,
                   post_at=None, series_id=None, lobby_url: str | None = None, max_per_channel: int | None = None,
                   max_per_user: int | None = None, max_scheduled: int | None = None) -> int:
    """
    Insert a session. Limits are checked inside the same queue transaction as
    the insert, so simultaneous /tenman start calls cannot slip past the cap.
    post_at in the future makes it a scheduled sign-up.
    """
    if not isinstance(title, str) or not 1 <= len(title) <= 80:
        raise ValueError("title must be 1-80 characters")  # callers validate first; this is the backstop
    if not 2 <= int(slots) <= 40:
        raise ValueError("slots must be 2-40")
    now = int(now if now is not None else time.time())
    scheduled = post_at is not None and int(post_at) > now
    if scheduled:
        if max_scheduled is not None:
            count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE status = 'open' AND post_state != 'posted' AND channel_id = ?",
                (str(channel_id),)).fetchone()[0]
            if count >= max_scheduled:
                raise LimitReached(f"this channel already has {max_scheduled} scheduled 10-mans")
    else:
        live = open_sessions(conn, now)
        if max_per_channel is not None and sum(s["channel_id"] == str(channel_id) for s in live) >= max_per_channel:
            raise LimitReached(f"this channel already has {max_per_channel} open sign-ups; cancel one first")
        if max_per_user is not None and sum(s["created_by"] == str(created_by) for s in live) >= max_per_user:
            raise LimitReached(f"you already have {max_per_user} open sign-ups; cancel one first")
    cur = conn.execute(
        "INSERT INTO sessions (guild_id, channel_id, title, start_at, slots, created_by, created_at, "
        "post_state, post_at, series_id, lobby_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(guild_id), str(channel_id), title, int(start_at), int(slots), str(created_by), now,
         SCHEDULED if scheduled else POSTED, int(post_at) if post_at is not None else None, series_id, lobby_url),
    )
    return cur.lastrowid


def start_session(conn, *, organizer_name: str, **kwargs) -> dict:
    """
    /tenman start in ONE job: create the session and put the organizer in.
    Returns {"session", "signups"} so the caller can post without another read.
    """
    sid = create_session(conn, **kwargs)
    session = get_session(conn, sid)
    if session["post_state"] == POSTED:
        _upsert_status(conn, sid, kwargs["created_by"], organizer_name, IN, time.time())
    return {"session": session, "signups": list_signups(conn, sid)}


def get_session(conn, session_id: int) -> dict:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise SessionNotFound(f"session {session_id} not found")
    return dict(row)


def set_message_id(conn, session_id: int, message_id) -> None:
    conn.execute("UPDATE sessions SET message_id = ? WHERE id = ?", (str(message_id), session_id))


def set_lobby_url(conn, session_id: int, url: str, *, whole_series: bool = False) -> dict:
    """Set one game's lobby link; with whole_series, also the series default and its other open games."""
    conn.execute("UPDATE sessions SET lobby_url = ? WHERE id = ?", (url, session_id))
    series_id = get_session(conn, session_id)["series_id"]
    if whole_series and series_id is not None:
        conn.execute("UPDATE series SET lobby_url = ? WHERE id = ?", (url, series_id))
        conn.execute("UPDATE sessions SET lobby_url = ? WHERE series_id = ? AND status = 'open'", (url, series_id))
    return {"session": get_session(conn, session_id), "signups": list_signups(conn, session_id)}


def set_session_status(conn, session_id: int, status: str) -> None:
    conn.execute("UPDATE sessions SET status = ? WHERE id = ?", (status, session_id))


def open_sessions(conn, now=None) -> list[dict]:
    """Open, posted sessions that have not started more than 6 h ago: what alerts and clicks act on."""
    now = int(now if now is not None else time.time())
    return _rows(conn.execute(
        "SELECT * FROM sessions WHERE status = 'open' AND post_state = 'posted' AND start_at > ? "
        "ORDER BY start_at, id", (now - 6 * 3600,)))


def channel_open_sessions(conn, channel_id, now=None) -> list[dict]:
    """A channel's posted open sessions, soonest first."""
    return [s for s in open_sessions(conn, now) if s["channel_id"] == str(channel_id)]


def channel_upcoming(conn, channel_id, now=None) -> list[dict]:
    """Everything still ahead in a channel, posted or scheduled, soonest first: what /tenman list numbers."""
    now = int(now if now is not None else time.time())
    return _rows(conn.execute(
        "SELECT * FROM sessions WHERE status = 'open' AND channel_id = ? AND start_at > ? "
        "ORDER BY start_at, id", (str(channel_id), now - 6 * 3600)))


def cancel_if_open(conn, session_id: int) -> bool:
    """Cancel only if still open; False means someone else already closed it."""
    cur = conn.execute("UPDATE sessions SET status = 'cancelled' WHERE id = ? AND status = 'open'", (session_id,))
    return cur.rowcount == 1


def close_started(conn, now=None, grace_seconds: int = 30 * 60) -> list[int]:
    """Mark sessions done once their start + grace has passed. Returns their ids (to strip buttons)."""
    now = int(now if now is not None else time.time())
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM sessions WHERE status = 'open' AND start_at <= ?", (now - grace_seconds,))]
    if ids:
        conn.executemany("UPDATE sessions SET status = 'done' WHERE id = ?", [(i,) for i in ids])
    return ids


# --------------------------------------------------------------------------- scheduled posting

def due_posts(conn, now=None) -> list[dict]:
    now = int(now if now is not None else time.time())
    return _rows(conn.execute(
        "SELECT * FROM sessions WHERE status = 'open' AND post_state = 'scheduled' AND post_at <= ? "
        "ORDER BY post_at, id", (now,)))


def claim_post(conn, session_id: int) -> dict | None:
    """Take a scheduled sign-up for posting. None if someone else has it or it is gone."""
    cur = conn.execute(
        "UPDATE sessions SET post_state = 'posting', post_attempts = post_attempts + 1 "
        "WHERE id = ? AND status = 'open' AND post_state = 'scheduled'", (session_id,))
    if cur.rowcount != 1:
        return None
    return {"session": get_session(conn, session_id), "signups": list_signups(conn, session_id)}


def finish_post(conn, session_id: int, *, message_id=None, definitely_failed: bool = False) -> str:
    """
    Record the outcome of posting. Posted: store the message. Definitely not
    posted: back to scheduled for the next pass, or cancelled after
    MAX_POST_ATTEMPTS. Unknown (timeout): posted without a message id, so it
    is never posted twice.
    """
    if definitely_failed:
        attempts = get_session(conn, session_id)["post_attempts"]
        if attempts >= MAX_POST_ATTEMPTS:
            conn.execute("UPDATE sessions SET status = 'cancelled', post_state = 'scheduled' WHERE id = ?",
                         (session_id,))
            return CANCELLED
        conn.execute("UPDATE sessions SET post_state = 'scheduled' WHERE id = ?", (session_id,))
        return SCHEDULED
    conn.execute("UPDATE sessions SET post_state = 'posted', message_id = ?, post_at = COALESCE(post_at, ?) "
                 "WHERE id = ?", (str(message_id) if message_id else None, int(time.time()), session_id))
    return POSTED


# --------------------------------------------------------------------------- weekly series

def create_series(conn, *, guild_id, channel_id, title, weekday, local_time, slots, post_days,
                  remaining, created_by, now=None, lobby_url: str | None = None,
                  max_per_channel: int | None = None) -> int:
    if max_per_channel is not None:
        count = conn.execute("SELECT COUNT(*) FROM series WHERE active = 1 AND channel_id = ?",
                             (str(channel_id),)).fetchone()[0]
        if count >= max_per_channel:
            raise LimitReached(f"this channel already has {max_per_channel} weekly series")
    cur = conn.execute(
        "INSERT INTO series (guild_id, channel_id, title, weekday, local_time, slots, post_days, remaining, "
        "created_by, created_at, lobby_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(guild_id), str(channel_id), title, int(weekday), local_time, int(slots), int(post_days),
         remaining, str(created_by), int(now if now is not None else time.time()), lobby_url))
    return cur.lastrowid


def get_series(conn, series_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM series WHERE id = ?", (series_id,)).fetchone()
    return dict(row) if row else None


def channel_series(conn, channel_id) -> list[dict]:
    return _rows(conn.execute("SELECT * FROM series WHERE active = 1 AND channel_id = ? ORDER BY weekday, local_time",
                              (str(channel_id),)))


def stop_series(conn, series_id: int, *, cancel_upcoming: bool = True) -> int:
    """End a series. Also cancels its not-yet-started sessions. Returns how many were cancelled."""
    conn.execute("UPDATE series SET active = 0 WHERE id = ?", (series_id,))
    if not cancel_upcoming:
        return 0
    cur = conn.execute("UPDATE sessions SET status = 'cancelled' WHERE series_id = ? AND status = 'open' "
                       "AND start_at > ?", (series_id, int(time.time())))
    return cur.rowcount


def occurrence(day, local_time: str, tz) -> datetime:
    hours, minutes = local_time.split(":")
    return datetime.combine(day, dtime(int(hours), int(minutes)), tz)


def post_time(start: datetime, post_days: int) -> datetime:
    """Post N days before, at the same wall-clock time (correct across DST)."""
    return datetime.combine(start.date() - timedelta(days=post_days), start.timetz().replace(tzinfo=None),
                            start.tzinfo)


def materialize_series(conn, now: datetime, tz) -> list[int]:
    """
    Keep exactly one upcoming session per active series: when a series has no
    open session still ahead, create the next weekly occurrence as a
    scheduled sign-up (due immediately if its post time already passed).
    Returns new ids.
    """
    created = []
    for series in _rows(conn.execute("SELECT * FROM series WHERE active = 1")):
        ahead = conn.execute(
            "SELECT MAX(start_at) FROM sessions WHERE series_id = ? AND status = 'open'", (series["id"],)
        ).fetchone()[0]
        if ahead is not None and ahead > now.timestamp():
            continue
        if series["remaining"] is not None and series["remaining"] <= 0:
            conn.execute("UPDATE series SET active = 0 WHERE id = ?", (series["id"],))
            continue
        last = conn.execute("SELECT MAX(start_at) FROM sessions WHERE series_id = ?", (series["id"],)).fetchone()[0]
        local_now = now.astimezone(tz)
        day = local_now.date() + timedelta(days=(series["weekday"] - local_now.weekday()) % 7)
        start = occurrence(day, series["local_time"], tz)
        while start.timestamp() <= now.timestamp() or (last is not None and start.timestamp() <= last):
            day += timedelta(days=7)
            start = occurrence(day, series["local_time"], tz)
        title = series["title"] or f"{start:%A} 10-man"
        # If the post time has already passed (the game is closer than post_days),
        # still create it as scheduled, due now: the posting step puts it up on
        # this or the next pass. Without this it would count as posted with no message.
        post_at = max(int(post_time(start, series["post_days"]).timestamp()), int(now.timestamp()) + 1)
        sid = create_session(conn, guild_id=series["guild_id"], channel_id=series["channel_id"], title=title,
                             start_at=int(start.timestamp()), created_by=series["created_by"],
                             slots=series["slots"], now=int(now.timestamp()), post_at=post_at,
                             series_id=series["id"], lobby_url=series["lobby_url"])
        if series["remaining"] is not None:
            conn.execute("UPDATE series SET remaining = remaining - 1 WHERE id = ?", (series["id"],))
        created.append(sid)
    return created


# --------------------------------------------------------------------------- editing

# Alert rows tied to the start time (utils/alerts.py GAMEDAY / STARTING). Daily
# rows are keyed by calendar date and "full" by the roster, not the start time.
_START_TIED_ALERTS = ("gameday", "starting")
POSTED_EDIT_MAX_DAYS = 30  # same horizon as /tenman start


def default_title(start: datetime) -> str:
    return f"{start:%A} 10-man"


def _scheduled_post_days(session: dict, series: dict | None, tz, default: int) -> int:
    """
    How many days ahead a scheduled game posts, when the edit doesn't say.
    One-off games don't store post_days: it is read back from post_at when
    post_at still sits at the game's wall-clock time (the /tenman schedule
    rule); a post_at that was pulled forward to "now" falls back to default.
    """
    if series is not None:
        return int(series["post_days"])
    if session.get("post_at"):
        start = datetime.fromtimestamp(session["start_at"], tz)
        post = datetime.fromtimestamp(session["post_at"], tz)
        if post.timetz().replace(tzinfo=None) == start.timetz().replace(tzinfo=None):
            return max(0, min((start.date() - post.date()).days, 14))
    return default


def edit_session(conn, session_id: int, *, tz, now=None, start_at: int | None = None, title: str | None = None,
                 slots: int | None = None, post_days: int | None = None, lobby_url: str | None = None,
                 default_post_days: int = 3, weekly_max_post_days: int = 6, gameday_hours: int = 2,
                 posted_max_days: int = POSTED_EDIT_MAX_DAYS) -> dict:
    """
    /tenman edit in ONE job: re-check the game is still open, apply only the
    given fields, and for a posted game return the before/after signups so the
    caller can name who moved onto or off the waitlist. Every check runs
    before the first write, so a refusal changes nothing.

    Scheduled game: post_at is recomputed from the new time / post_days with
    the /tenman schedule rule. Posted game: post_days is ignored; when the
    time moves, start-tied alert rows (gameday, starting) whose new moment is
    still ahead are cleared so they follow the new time. A row whose new
    moment has already passed is kept, so an alert already sent is never sent
    again; daily rows are never cleared (one daily per calendar day). When
    slots change and the roster is no longer full, the "full" row is cleared
    so it fires again when the bigger roster fills.

    Returns {"session", "before_session", "posted", "changes", "before",
    "after", "demoted", "promoted", "alerts_reset", "notes"}. "changes" maps
    field -> (old, new) for fields that actually changed.
    """
    from utils.roster import build_roster, promoted as promoted_rows  # roster imports this module

    now = int(now if now is not None else time.time())
    session = get_session(conn, session_id)
    if session["status"] != OPEN:
        raise EditRefused(f"that game is {session['status']}")
    if now >= session["start_at"]:
        raise EditRefused("that game has already started")
    if session["post_state"] == POSTING:
        raise EditRefused("its sign-up is being posted right now; try again in a minute")
    posted = session["post_state"] == POSTED
    if slots is not None and not 2 <= int(slots) <= 40:
        raise ValueError("slots must be 2-40")
    if post_days is not None and not 0 <= int(post_days) <= 14:
        raise ValueError("post_days must be 0-14")
    if title is not None and (not isinstance(title, str) or not 1 <= len(title) <= 80):
        raise ValueError("title must be 1-80 characters")
    if start_at is not None:
        start_at = int(start_at)
        if start_at <= now:
            raise EditRefused("the new time has already passed")
        if posted and start_at - now > posted_max_days * 86400:
            raise EditRefused(f"a posted game can be moved at most {posted_max_days} days ahead")

    series = get_series(conn, session["series_id"]) if session.get("series_id") else None
    notes = []
    new = {"title": session["title"], "start_at": session["start_at"], "slots": session["slots"],
           "lobby_url": session["lobby_url"], "post_at": session["post_at"]}
    old_start = datetime.fromtimestamp(session["start_at"], tz)
    if start_at is not None:
        new["start_at"] = start_at
        if title is None and session["title"] == default_title(old_start):
            # The default '<Weekday> 10-man' follows the new day.
            new["title"] = default_title(datetime.fromtimestamp(start_at, tz))
    if title is not None:
        new["title"] = title
    if slots is not None:
        new["slots"] = int(slots)
    if lobby_url is not None:
        new["lobby_url"] = lobby_url
    if posted:
        if post_days is not None:
            notes.append("post_days ignored: the sign-up is already up")
    elif start_at is not None or post_days is not None:
        days = int(post_days) if post_days is not None else _scheduled_post_days(session, series, tz,
                                                                                   default_post_days)
        if series is not None and days > weekly_max_post_days:
            days = weekly_max_post_days
            notes.append(f"weekly sign-ups post at most {weekly_max_post_days} days ahead, so post_days was "
                         "set to that")
        start = datetime.fromtimestamp(new["start_at"], tz)
        new["post_at"] = max(int(post_time(start, days).timestamp()), now + 1)
        if new["post_at"] <= now + 60:
            notes.append("the sign-up posts within a minute")

    changes = {k: (session[k], v) for k, v in new.items() if session[k] != v}
    before = list_signups(conn, session_id)
    result = {"before_session": session, "posted": posted, "changes": changes, "before": before,
              "after": before, "demoted": [], "promoted": [], "alerts_reset": [], "notes": notes}
    if changes:
        conn.execute(
            "UPDATE sessions SET title = ?, start_at = ?, slots = ?, lobby_url = ?, post_at = ? "
            "WHERE id = ? AND status = 'open'",
            (new["title"], new["start_at"], new["slots"], new["lobby_url"], new["post_at"], session_id))
    if posted and changes:
        old_roster = build_roster(before, session["slots"])
        new_roster = build_roster(before, new["slots"])
        confirmed_now = {r["id"] for r in new_roster.confirmed}
        result["demoted"] = [r for r in old_roster.confirmed if r["id"] not in confirmed_now]
        result["promoted"] = promoted_rows(old_roster, new_roster)
        result["alerts_reset"] = _reset_alerts(conn, session_id, changes, new_roster.full, now, gameday_hours)
    result["session"] = get_session(conn, session_id)
    return result


def _reset_alerts(conn, session_id: int, changes: dict, full: bool, now: int, gameday_hours: int) -> list:
    """Clear the alert rows an edit made stale; never one in flight ('sending')."""
    moments = {}
    if "start_at" in changes:
        new_start = changes["start_at"][1]
        moments = {"gameday": new_start - gameday_hours * 3600, "starting": new_start}
    reset = []
    for row in conn.execute("SELECT kind, slot_key, status FROM alerts WHERE session_id = ?", (session_id,)):
        if row["status"] == ALERT_SENDING:
            continue
        stale = (row["kind"] in _START_TIED_ALERTS and row["kind"] in moments and moments[row["kind"]] > now) \
            or (row["kind"] == "full" and "slots" in changes and not full)
        if stale:
            reset.append((row["kind"], row["slot_key"]))
    for kind, slot_key in reset:
        conn.execute("DELETE FROM alerts WHERE session_id = ? AND kind = ? AND slot_key = ?",
                     (session_id, kind, slot_key))
    return reset


def next_series_session(conn, series_id: int, now=None) -> dict | None:
    """The series' upcoming open game (the one materialize_series keeps ahead), if any."""
    now = int(now if now is not None else time.time())
    row = conn.execute("SELECT * FROM sessions WHERE series_id = ? AND status = 'open' AND start_at > ? "
                       "ORDER BY start_at, id LIMIT 1", (series_id, now)).fetchone()
    return dict(row) if row else None


def update_series(conn, series_id: int, *, tz, now=None, weekday: int | None = None, local_time: str | None = None,
                  title: str | None = None, slots: int | None = None, post_days: int | None = None,
                  lobby_url: str | None = None, apply_to_next: bool = False, weekly_max_post_days: int = 6,
                  gameday_hours: int = 2) -> dict:
    """
    /tenman update in ONE job: change a weekly series' defaults for the games
    it creates from now on. With apply_to_next, the upcoming game gets the
    same change through edit_session (moved to the new weekday/time on or
    after its current day); if that game can't be edited, the series change
    still applies and "next_error" says why.

    Returns {"series", "before", "changes", "capped", "upcoming", "next",
    "next_error"}: "upcoming" is the series' next open game as it was before
    any edit, "next" edit_session's result (or None), "next_error" a
    user-safe reason the upcoming game was left as it was.
    """
    now = int(now if now is not None else time.time())
    series = get_series(conn, series_id)
    if series is None:
        raise EditRefused("that series doesn't exist")
    if not series["active"]:
        raise EditRefused("that series has stopped")
    if slots is not None and not 2 <= int(slots) <= 40:
        raise ValueError("slots must be 2-40")
    if post_days is not None and not 0 <= int(post_days) <= 14:
        raise ValueError("post_days must be 0-14")
    if weekday is not None and not 0 <= int(weekday) <= 6:
        raise ValueError("weekday must be 0-6")
    if local_time is not None:
        hours, minutes = local_time.split(":")
        if not (0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59):
            raise ValueError("time must be HH:MM")
        local_time = f"{int(hours):02d}:{int(minutes):02d}"
    capped = post_days is not None and int(post_days) > weekly_max_post_days
    new = {"weekday": series["weekday"] if weekday is None else int(weekday),
           "local_time": local_time or series["local_time"],
           "title": series["title"] if title is None else title,
           "slots": series["slots"] if slots is None else int(slots),
           "post_days": series["post_days"] if post_days is None else min(int(post_days), weekly_max_post_days),
           "lobby_url": series["lobby_url"] if lobby_url is None else lobby_url}
    changes = {k: (series[k], v) for k, v in new.items() if series[k] != v}
    if changes:
        conn.execute("UPDATE series SET weekday = ?, local_time = ?, title = ?, slots = ?, post_days = ?, "
                     "lobby_url = ? WHERE id = ?",
                     (new["weekday"], new["local_time"], new["title"], new["slots"], new["post_days"],
                      new["lobby_url"], series_id))
    upcoming = next_series_session(conn, series_id, now)
    result = {"series": get_series(conn, series_id), "before": series, "changes": changes, "capped": capped,
              "upcoming": upcoming, "next": None, "next_error": None}
    if not apply_to_next:
        return result
    if upcoming is None:
        result["next_error"] = "there is no upcoming game yet"
        return result
    start_at = None
    if "weekday" in changes or "local_time" in changes:
        current = datetime.fromtimestamp(upcoming["start_at"], tz)
        day = current.date() + timedelta(days=(new["weekday"] - current.weekday()) % 7)
        start_at = int(occurrence(day, new["local_time"], tz).timestamp())
    try:
        result["next"] = edit_session(
            conn, upcoming["id"], tz=tz, now=now, start_at=start_at, title=title,
            slots=slots, post_days=post_days, lobby_url=lobby_url,
            weekly_max_post_days=weekly_max_post_days, gameday_hours=gameday_hours)
    except EditRefused as exc:
        result["next_error"] = str(exc)
    return result


# --------------------------------------------------------------------------- signups

def list_signups(conn, session_id: int) -> list[dict]:
    return _rows(conn.execute("SELECT * FROM signups WHERE session_id = ? ORDER BY joined_at, id", (session_id,)))


def signups_for(conn, session_ids: list[int]) -> dict[int, list[dict]]:
    """Signups for many sessions in one query (no N+1)."""
    out = {sid: [] for sid in session_ids}
    if not session_ids:
        return out
    marks = ",".join("?" * len(session_ids))
    for row in conn.execute(f"SELECT * FROM signups WHERE session_id IN ({marks}) ORDER BY joined_at, id",
                            list(session_ids)):
        out[row["session_id"]].append(dict(row))
    return out


def _require_open(conn, session_id: int, now: float) -> dict:
    session = get_session(conn, session_id)
    if session["status"] != OPEN:
        raise SessionClosed(f"this sign-up is {session['status']}")
    if session["post_state"] != POSTED:
        raise SessionClosed("this sign-up isn't open yet")
    if now >= session["start_at"]:
        raise SessionClosed("this game has already started")
    return session


def _upsert_status(conn, session_id, user_id, display_name, status, now) -> None:
    user_id = str(user_id)
    existing = conn.execute(
        "SELECT status FROM signups WHERE session_id = ? AND user_id = ? AND guest_no = 0",
        (session_id, user_id)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO signups (session_id, user_id, display_name, guest_no, status, joined_at) "
            "VALUES (?, ?, ?, 0, ?, ?)", (session_id, user_id, display_name, status, now))
    elif existing["status"] != status:
        conn.execute(
            "UPDATE signups SET status = ?, display_name = ?, joined_at = ?, checked_in = 0 "
            "WHERE session_id = ? AND user_id = ? AND guest_no = 0",
            (status, display_name, now, session_id, user_id))
    if status != IN:
        # A guest only holds a spot while their sponsor is In: Maybe or Out takes them along.
        conn.execute("DELETE FROM signups WHERE session_id = ? AND user_id = ? AND guest_no > 0",
                     (session_id, user_id))


def set_status(conn, session_id: int, user_id, display_name: str, status: str, now=None) -> dict:
    """
    Set a player's own status. Re-clicking the status they already have keeps
    their place; switching to IN from anything else puts them at the back
    (first come, first served). Going Maybe or Out also drops their guests.
    Returns {"session", "before", "after"}.
    """
    if status not in STATUSES:
        raise ValueError(f"bad status {status!r}")
    now = now if now is not None else time.time()
    session = _require_open(conn, session_id, now)
    before = list_signups(conn, session_id)
    _upsert_status(conn, session_id, user_id, display_name, status, now)
    return {"session": session, "before": before, "after": list_signups(conn, session_id)}


def add_guest(conn, session_id: int, sponsor_id, sponsor_name: str, max_guests: int = 1, now=None) -> dict:
    """Add one guest under a sponsor who is IN. Raises ValueError when not allowed."""
    now = now if now is not None else time.time()
    session = _require_open(conn, session_id, now)
    before = list_signups(conn, session_id)
    sponsor_id = str(sponsor_id)
    sponsor = next((s for s in before if s["user_id"] == sponsor_id and s["guest_no"] == 0), None)
    if sponsor is None or sponsor["status"] != IN:
        raise ValueError("click In before adding a guest")
    guests = [s["guest_no"] for s in before if s["user_id"] == sponsor_id and s["guest_no"] > 0]
    if len(guests) >= max_guests:
        raise ValueError(f"you can bring at most {max_guests} guest(s)")
    conn.execute(
        "INSERT INTO signups (session_id, user_id, display_name, guest_no, status, joined_at) "
        "VALUES (?, ?, ?, ?, 'in', ?)",
        (session_id, sponsor_id, f"{sponsor_name}'s +1", max(guests, default=0) + 1, now))
    return {"session": session, "before": before, "after": list_signups(conn, session_id)}


def remove_guest(conn, session_id: int, sponsor_id, now=None) -> dict:
    """Remove the sponsor's most recently added guest."""
    now = now if now is not None else time.time()
    session = _require_open(conn, session_id, now)
    before = list_signups(conn, session_id)
    conn.execute(
        "DELETE FROM signups WHERE id = (SELECT id FROM signups WHERE session_id = ? AND user_id = ? "
        "AND guest_no > 0 ORDER BY guest_no DESC LIMIT 1)", (session_id, str(sponsor_id)))
    return {"session": session, "before": before, "after": list_signups(conn, session_id)}


def apply_click(conn, session_id: int, *, action: str, user_id, display_name: str, guild_id, message_id,
                max_guests: int, now=None) -> dict:
    """
    One button click in ONE job: check the button belongs to this session's
    message in this guild, apply it, and return {"session", "before", "after"}
    so the caller can answer Discord without another round trip.
    """
    if action not in ACTIONS:
        raise ValueError("unknown button")
    session = get_session(conn, session_id)
    if session["guild_id"] != str(guild_id) or (
            session["message_id"] and message_id is not None and session["message_id"] != str(message_id)):
        raise ForeignClick(f"button for session {session_id} used on another message")
    if action in STATUSES:
        return set_status(conn, session_id, user_id, display_name, action, now)
    if action == GUEST:
        return add_guest(conn, session_id, user_id, display_name, max_guests, now)
    return remove_guest(conn, session_id, user_id, now)


# --------------------------------------------------------------------------- alerts

def alert_state(conn, session_id: int, kind: str, slot_key: str) -> dict | None:
    row = conn.execute("SELECT * FROM alerts WHERE session_id = ? AND kind = ? AND slot_key = ?",
                       (session_id, kind, slot_key)).fetchone()
    return dict(row) if row else None


def alert_states_for(conn, session_ids: list[int]) -> dict[tuple, dict]:
    """{(session_id, kind, slot_key): row} for many sessions in one query."""
    if not session_ids:
        return {}
    marks = ",".join("?" * len(session_ids))
    return {(r["session_id"], r["kind"], r["slot_key"]): dict(r)
            for r in conn.execute(f"SELECT * FROM alerts WHERE session_id IN ({marks})", list(session_ids))}


def alert_is_finished(row: dict | None) -> bool:
    """True when claiming again could not succeed: no write needed to find out."""
    if row is None:
        return False
    return not (row["status"] == ALERT_FAILED and row["attempts"] < MAX_ALERT_ATTEMPTS)


def claim_alert(conn, session_id: int, kind: str, slot_key: str, now=None) -> bool:
    """
    Claim an alert slot for sending. True means the caller owns the send and
    must report back with finish_alert(). False: already sent, in flight,
    ambiguous or out of attempts -- do not send.
    """
    now = int(now if now is not None else time.time())
    state = alert_state(conn, session_id, kind, slot_key)
    if state is None:
        conn.execute("INSERT INTO alerts (session_id, kind, slot_key, status, attempts, updated_at) "
                     "VALUES (?, ?, ?, ?, 1, ?)", (session_id, kind, slot_key, ALERT_SENDING, now))
        return True
    if state["status"] == ALERT_FAILED and state["attempts"] < MAX_ALERT_ATTEMPTS:
        conn.execute("UPDATE alerts SET status = ?, attempts = attempts + 1, updated_at = ? "
                     "WHERE session_id = ? AND kind = ? AND slot_key = ?",
                     (ALERT_SENDING, now, session_id, kind, slot_key))
        return True
    return False


def finish_alert(conn, session_id: int, kind: str, slot_key: str, status: str, error: str | None = None,
                 now=None) -> None:
    now = int(now if now is not None else time.time())
    conn.execute(
        "INSERT INTO alerts (session_id, kind, slot_key, status, attempts, last_error, updated_at) "
        "VALUES (?, ?, ?, ?, 0, ?, ?) ON CONFLICT (session_id, kind, slot_key) DO UPDATE SET "
        "status = excluded.status, last_error = excluded.last_error, updated_at = excluded.updated_at",
        (session_id, kind, slot_key, status, error, now))


def recover_stale_sends(conn) -> int:
    """At startup: anything left mid-send by a crash is ambiguous and never resent."""
    alerts = conn.execute("UPDATE alerts SET status = ?, last_error = 'process stopped mid-send' WHERE status = ?",
                          (ALERT_AMBIGUOUS, ALERT_SENDING)).rowcount
    posts = conn.execute("UPDATE sessions SET post_state = 'posted' WHERE post_state = 'posting'").rowcount
    if posts:
        logger.warning(f"{posts} scheduled sign-up(s) were mid-post at shutdown; marked posted, not reposted")
    return alerts + posts
