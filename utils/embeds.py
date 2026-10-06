"""Discord rendering of a sign-up: the embed and its persistent buttons."""

import time
from datetime import datetime, timedelta
from datetime import time as dtime

import discord

from utils import style
from utils.roster import Roster
from utils.sanitize import clean_text, md

BUTTONS = (
    # action, label, style, emoji
    ("in", "In", discord.ButtonStyle.success, "✅"),
    ("maybe", "Maybe", discord.ButtonStyle.secondary, "\U0001F914"),
    ("out", "Out", discord.ButtonStyle.danger, "✖️"),
    ("guest", "+1 Guest", discord.ButtonStyle.primary, "➕"),
    ("unguest", "-1 Guest", discord.ButtonStyle.secondary, "➖"),
)

SLOTS_PER_COLUMN = 5
EMBED_BUDGET = 5800  # Discord's limit is 6000 characters per embed


def _bounded(items: list[str], sep: str, limit: int = 1024) -> str:
    """Join as many items as fit in one field value, then '+N more'."""
    out, used = [], 0
    for n, item in enumerate(items):
        more = f"{sep}+{len(items) - n} more"
        if used + len(sep) + len(item) + len(more) > limit:
            return sep.join(out) + more
        out.append(item)
        used += len(item) + len(sep)
    return sep.join(out) or "—"


def _slot_columns(roster: Roster) -> list[tuple[str, str]]:
    """Numbered slots split into columns of 5, open slots shown as such."""
    lines = []
    for i in range(roster.slots):
        if i < len(roster.confirmed):
            lines.append(f"`{i + 1:>2}` {md(roster.confirmed[i]['display_name'])}")
        else:
            lines.append(f"`{i + 1:>2}` *open*")
    columns = []
    for start in range(0, len(lines), SLOTS_PER_COLUMN):
        chunk = lines[start:start + SLOTS_PER_COLUMN]
        name = f"Slots {start + 1}–{start + len(chunk)}"
        columns.append((name, "\n".join(chunk)[:1024]))
    return columns


def _status(session: dict, roster: Roster, now: float) -> tuple[int, str]:
    if session["status"] == "cancelled":
        return style.GREY, "**cancelled**"
    if session["status"] != "open" or now >= session["start_at"]:
        return style.GREY, "**closed**"
    if roster.full:
        extra = f" · {len(roster.waitlist)} on waitlist" if roster.waitlist else ""
        return style.GREEN, f"**full**{extra}"
    soon = session["start_at"] - now <= style.STARTING_SOON_HOURS * 3600
    return (style.RED if soon else style.ORANGE), f"need **{roster.need}** more"


def signup_embed(session: dict, roster: Roster, icon_url: str | None = None, now: float | None = None) -> discord.Embed:
    now = now if now is not None else time.time()
    start = int(session["start_at"])
    colour, status = _status(session, roster, now)

    embed = discord.Embed(
        title=clean_text(session["title"], 256),  # titles render no mentions; links/mentions are rejected on input
        description=(f"**Starts** <t:{start}:F> · <t:{start}:R>\n"
                     f"{style.progress_bar(roster.count, roster.slots)} "
                     f"**{roster.count}/{roster.slots}** · {status}"),
        colour=colour,
    )
    embed.set_author(name=style.BRAND, icon_url=icon_url)
    if icon_url:
        embed.set_thumbnail(url=icon_url)
    for name, value in _slot_columns(roster):
        embed.add_field(name=name, value=value, inline=True)
    if roster.waitlist:
        embed.add_field(name=f"Waitlist ({len(roster.waitlist)})",
                        value=_bounded([f"`W{i}` {md(r['display_name'])}" for i, r in enumerate(roster.waitlist, 1)],
                                       "\n"), inline=False)
    if roster.maybe:
        embed.add_field(name=f"Maybe ({len(roster.maybe)})",
                        value=_bounded([md(r["display_name"]) for r in roster.maybe], ", "), inline=False)
    if session.get("lobby_url"):
        embed.add_field(name="Lobby", value=f"[Join lobby]({style.safe_link(session['lobby_url'])})", inline=False)
    footer = "First come, first served · waitlist moves up automatically"
    if session["status"] == "cancelled":
        footer = "This sign-up was cancelled"
    embed.set_footer(text=footer)
    # Discord rejects an embed over 6000 characters, and then every click on
    # the sign-up would fail. Shrink the long lists, never the playing slots.
    for index in reversed(range(len(embed.fields))):
        if len(embed) <= EMBED_BUDGET:
            break
        field = embed.fields[index]
        if field.name.startswith(("Waitlist", "Maybe")):
            embed.set_field_at(index, name=field.name, value="(too many to list)", inline=False)
    return embed


def _short_time(start_at: int, tz) -> str:
    t = datetime.fromtimestamp(start_at, tz)
    return f"{t:%a %b} {t.day} {t.hour % 12 or 12}:{t:%M} {'PM' if t.hour >= 12 else 'AM'} {t:%Z}"


def numbered_lines(rows: list[tuple[dict, Roster]]) -> str:
    """ufw-style numbered list of sign-ups: `[ 1]` Title · time · 2/10."""
    lines = []
    for n, (session, roster) in enumerate(rows, 1):
        if session.get("post_state", "posted") != "posted":
            state = f"scheduled, posts <t:{session['post_at']}:R>"
        else:
            state = f"{roster.count}/{roster.slots}, " + ("full" if roster.full else f"need {roster.need}")
        weekly = " · weekly" if session.get("series_id") else ""
        lines.append(f"`[{n:>2}]` **{md(session['title'])}** · <t:{session['start_at']}:f> · {state}{weekly}")
    return "\n".join(lines)


def choice_label(n: int, session: dict, roster: Roster, tz) -> str:
    """Autocomplete entry; Discord shows plain text here (no <t:> timestamps), max 100 chars."""
    state = "scheduled" if session.get("post_state", "posted") != "posted" else f"{roster.count}/{roster.slots}"
    label = f"{n} · {clean_text(session['title'], 60)} · {_short_time(session['start_at'], tz)} · {state}"
    return label[:100]


WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def series_lines(series: list[dict], tz=None) -> str:
    lines = []
    for s in series:
        hours, minutes = (int(x) for x in s["local_time"].split(":"))
        zone = ""
        if tz is not None:
            # The abbreviation in effect at the next game (EDT vs EST across a DST change).
            today = datetime.now(tz).date()
            next_day = today + timedelta(days=(s["weekday"] - today.weekday()) % 7)
            zone = f" {datetime.combine(next_day, dtime(hours, minutes), tz):%Z}"
        clock = f"{hours % 12 or 12}:{minutes:02d} {'PM' if hours >= 12 else 'AM'}{zone}"
        left = "until stopped" if s["remaining"] is None else f"{s['remaining']} more to come"
        name = md(s["title"]) if s["title"] else "10-man"
        lines.append(f"`[S{s['id']}]` **{name}** every {WEEKDAYS[s['weekday']]} {clock} · "
                     f"posts {s['post_days']} day(s) ahead · {left}" + (" · lobby set" if s.get("lobby_url") else ""))
    return "\n".join(lines)
