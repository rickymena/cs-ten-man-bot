"""
Settings read from the environment (.env). One place, typed, validated.

load_config() raises ConfigError listing every bad value at once, so a
mistyped .env fails at startup with a readable message instead of surfacing
later as a crash inside a button click or the noon alert.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import time as dtime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_SNOWFLAKE = re.compile(r"^\d{17,20}$")
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_HOST = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")


class ConfigError(ValueError):
    pass


def read_secret(name: str) -> str | None:
    """
    A secret from NAME_FILE (a Docker secret mounted under /run/secrets/) or,
    failing that, from NAME in the environment. The file form keeps the value
    out of `docker inspect` and /proc/<pid>/environ. Raises ConfigError if
    NAME_FILE is set but unreadable, without echoing the value.
    """
    path = os.getenv(f"{name}_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read().strip() or None
        except OSError as exc:
            raise ConfigError(f"{name}_FILE={path} can't be read ({exc.strerror})") from None
    return os.getenv(name)


def _placeholder(value: str | None) -> str | None:
    # .env.sample values start with PASTE_; treat them as unset.
    if value is None:
        return None
    value = value.strip()
    if not value or value.startswith("PASTE_"):
        return None
    return value


@dataclass(frozen=True)
class Config:
    discord_token: str | None
    guild_id: str | None
    channel_id: str | None
    role_id: str | None
    webhook_url: str | None
    tz: ZoneInfo
    alert_time: dtime
    gameday_alert_hours: float
    alert_catchup_hours: float
    db_path: str
    default_slots: int
    max_guests: int
    lobby_allowed_hosts: tuple[str, ...] = field(default=())
    max_open_per_channel: int = 5
    max_open_per_user: int = 2
    click_cooldown_seconds: float = 1.0
    alerts_via: str = "bot"
    avatar_url: str | None = None  # set at runtime from the bot user; used in alert embeds
    schedule_post_days: int = 3
    max_scheduled_per_channel: int = 20
    max_series_per_channel: int = 3
    backup_dir: str | None = None  # None: no backups (load_config always sets one)
    backup_time: dtime = dtime(4, 0)


def load_config() -> Config:
    errors: list[str] = []

    def snowflake(name):
        value = _placeholder(os.getenv(name))
        if value is not None and not _SNOWFLAKE.match(value):
            errors.append(f"{name} must be a Discord ID (17-20 digits)")
            return None
        return value

    def number(name, default, low, high, kind=int):
        raw = os.getenv(name, str(default)).strip()
        try:
            value = kind(raw)
        except ValueError:
            errors.append(f"{name}={raw!r} is not a number")
            return default
        if not low <= value <= high:
            errors.append(f"{name}={value} is outside {low}..{high}")
            return default
        return value

    tz_name = os.getenv("TIMEZONE", "America/New_York").strip()
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"TIMEZONE={tz_name!r} is not a known timezone (e.g. America/New_York)")
        tz = ZoneInfo("America/New_York")

    def hhmm(name, default):
        raw = os.getenv(name, default).strip()
        match = _HHMM.match(raw)
        if match:
            return dtime(int(match.group(1)), int(match.group(2)))
        errors.append(f"{name}={raw!r} must be HH:MM, 24-hour")
        return dtime(*(int(x) for x in default.split(":")))

    alert_time = hhmm("ALERT_TIME", "12:00")
    backup_time = hhmm("BACKUP_TIME", "04:00")
    root = os.path.dirname(os.path.dirname(__file__))

    hosts = tuple(h.strip().lower() for h in os.getenv("LOBBY_ALLOWED_HOSTS", "").split(",") if h.strip())
    for host in hosts:
        if not _HOST.match(host):
            errors.append(f"LOBBY_ALLOWED_HOSTS entry {host!r} is not a domain")

    alerts_via = os.getenv("ALERTS_VIA", "bot").strip().lower()
    if alerts_via not in ("bot", "webhook"):
        errors.append(f"ALERTS_VIA={alerts_via!r} must be 'bot' or 'webhook'")
        alerts_via = "bot"

    try:
        token = _placeholder(read_secret("DISCORD_TOKEN"))
        webhook = _placeholder(read_secret("DISCORD_WEBHOOK_URL"))
    except ConfigError as exc:
        errors.append(str(exc))
        token = webhook = None
    if token is not None and (len(token) > 100 or token.count(".") != 2):
        errors.append("DISCORD_TOKEN does not look like a bot token (value not logged)")

    cfg = Config(
        discord_token=token,
        guild_id=snowflake("GUILD_ID"),
        channel_id=snowflake("CHANNEL_ID"),
        role_id=snowflake("TENMAN_ROLE_ID"),
        webhook_url=webhook,
        tz=tz,
        alert_time=alert_time,
        gameday_alert_hours=number("GAMEDAY_ALERT_HOURS_BEFORE", 2, 0, 24, float),
        alert_catchup_hours=number("ALERT_CATCHUP_HOURS", 3, 0.25, 24, float),  # below the 60 s loop, alerts never fire
        # Relative paths are taken from the project folder, not wherever the bot was started.
        db_path=os.path.join(root, os.getenv("DB_PATH", os.path.join("data", "tenman.db"))),
        backup_dir=os.path.join(root, os.getenv("BACKUP_DIR", "backups")),
        backup_time=backup_time,
        default_slots=number("TENMAN_SLOTS", 10, 2, 40),
        max_guests=number("TENMAN_MAX_GUESTS", 1, 0, 5),
        lobby_allowed_hosts=hosts,
        max_open_per_channel=number("TENMAN_MAX_OPEN_PER_CHANNEL", 5, 1, 25),
        max_open_per_user=number("TENMAN_MAX_OPEN_PER_USER", 2, 1, 25),
        click_cooldown_seconds=number("TENMAN_CLICK_COOLDOWN_SECONDS", 1.0, 0, 30, float),
        alerts_via=alerts_via,
        schedule_post_days=number("SCHEDULE_POST_DAYS_BEFORE", 3, 0, 14),
        max_scheduled_per_channel=number("TENMAN_MAX_SCHEDULED_PER_CHANNEL", 20, 1, 100),
        max_series_per_channel=number("TENMAN_MAX_SERIES_PER_CHANNEL", 3, 1, 20),
    )
    if errors:
        raise ConfigError("; ".join(errors))
    return cfg
