"""
Parse the start time people type into /tenman start and /tenman schedule.

Forgiving on purpose: people type this on phones, in a hurry, sometimes with
quotes copied from an example. Case, extra words and word order don't matter.

  day      today, tonight, tomorrow (tmrw, tmr), a weekday (mon .. sunday,
           tues, weds, thur, thurs; "this"/"next" are ignored), a date
           (2026-10-09, 10/9, 10/9/2026, oct 9, october 9, 9 oct)
  time     9pm, 9 pm, 9p, 9:30pm, 21:00, 2100, noon, midnight, or a bare
           hour "9" (1-11 read as PM: 10-mans are evening games)
  zone     optional: et/est/edt/eastern, ct/cst/cdt/central, mt/mst/mountain,
           pt/pst/pacific, utc. Without one, the bot's TIMEZONE applies.
  evening  without am/pm, an hour 1-11 means PM ("9", "9:30"); "21:30",
           "09:30", "0930" and anything with "am" are taken literally.
  filler   quotes, "at", "@", "on", commas are ignored

  "sat 9pm", '"Saturday at 9 PM"', "9pm sat", "oct 10 9:30pm", "10/10 21:00", "fri 9", "sat 9pm ct"

No day means today, or tomorrow if that time has already passed. A weekday
means the next one (today if still ahead). Returns an aware datetime in the
bot's TIMEZONE (a typed zone is converted), so series and alerts stay in one zone.
"""

import re
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

_WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
_MONTHS = {name: i + 1 for i, names in enumerate([
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
    ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
    ("nov", "november"), ("dec", "december")]) for name in names}
_ZONES = {
    **dict.fromkeys(("et", "est", "edt", "eastern"), "America/New_York"),
    **dict.fromkeys(("ct", "cst", "cdt", "central"), "America/Chicago"),
    **dict.fromkeys(("mt", "mst", "mdt", "mountain"), "America/Denver"),
    **dict.fromkeys(("pt", "pst", "pdt", "pacific"), "America/Los_Angeles"),
    **dict.fromkeys(("utc", "gmt", "z"), "UTC"),
}
_FILLER = {"at", "@", "on", "this", "next", "the", "around", "about", "starting", "start"}
_QUOTES = "\"'`“”‘’«»"

_TIME_PATTERNS = [
    # 9pm, 9 pm, 9:30pm, 9.30 pm, 9p, 9a.m.
    re.compile(r"\b(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?|a|p)(?![a-z])"),
    # 21:00, 9:30
    re.compile(r"\b(?P<h>\d{1,2}):(?P<m>\d{2})\b"),
    # 2100 (four digits, military)
    re.compile(r"(?<![/\-\d])\b(?P<h>[01]\d|2[0-3])(?P<m>[0-5]\d)\b(?![/\-])"),
]
_DATE_ISO = re.compile(r"\b(?P<y>\d{4})-(?P<mo>\d{1,2})-(?P<d>\d{1,2})\b")
_DATE_US = re.compile(r"\b(?P<mo>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?\b")


class WhenError(ValueError):
    pass


HINT = "try 'sat 9pm', 'tomorrow 10:30pm' or '10/10 9pm'"


def _normalize(text: str) -> str:
    text = text.strip().strip(_QUOTES).lower()
    for q in _QUOTES:
        text = text.replace(q, " ")
    return " ".join(text.replace(",", " ").split())


def _take_time(text: str) -> tuple[dtime | None, str]:
    """Find the time anywhere in the text; return it and the text without it."""
    if re.search(r"\bnoon\b", text):
        return dtime(12, 0), re.sub(r"\bnoon\b", " ", text)
    if re.search(r"\bmidnight\b", text):
        return dtime(23, 59), re.sub(r"\bmidnight\b", " ", text)
    for pattern in _TIME_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        hour, minute = int(m.group("h")), int(m.group("m") or 0)
        ampm = (m.groupdict().get("ap") or "").replace(".", "")
        if not ampm and pattern is _TIME_PATTERNS[1] and 1 <= hour <= 11 and not m.group("h").startswith("0"):
            # "9:30" with no am/pm means 9:30 PM, like a bare "9": 10-mans are evening games.
            # "21:30", "09:30" (leading zero) and explicit "am" keep their literal meaning.
            hour += 12
        if ampm:
            if not 1 <= hour <= 12:
                raise WhenError(f"{hour}{ampm} isn't a time ({HINT})")
            hour = hour % 12 + (12 if ampm.startswith("p") else 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise WhenError(f"that time isn't valid ({HINT})")
        return dtime(hour, minute), text[:m.start()] + " " + text[m.end():]
    return None, text


def _take_date(text: str, now: datetime) -> tuple[date | None, int | None, str]:
    """Return (exact date, weekday, leftover text); at most one of the first two is set."""
    m = _DATE_ISO.search(text)
    if m:
        return _mkdate(int(m["y"]), int(m["mo"]), int(m["d"])), None, text[:m.start()] + text[m.end():]
    m = _DATE_US.search(text)
    if m:
        year = int(m["y"]) if m["y"] else None
        if year is not None and year < 100:
            year += 2000
        return _upcoming(now, int(m["mo"]), int(m["d"]), year), None, text[:m.start()] + text[m.end():]
    words = text.split()
    for i, word in enumerate(words):
        if word in _MONTHS:
            # "oct 9" or "9 oct"
            for j in (i + 1, i - 1):
                if 0 <= j < len(words) and re.fullmatch(r"\d{1,2}(st|nd|rd|th)?", words[j]):
                    day = int(re.match(r"\d+", words[j]).group())
                    rest = [w for k, w in enumerate(words) if k not in (i, j)]
                    return _upcoming(now, _MONTHS[word], day, None), None, " ".join(rest)
            raise WhenError(f"which day of {word}? ({HINT})")
    for i, word in enumerate(words):
        if word in ("today", "tonight", "tn"):
            return now.date(), None, " ".join(words[:i] + words[i + 1:])
        if word in ("tomorrow", "tmrw", "tmr", "tomorow", "tommorow", "tmw"):
            return now.date() + timedelta(days=1), None, " ".join(words[:i] + words[i + 1:])
        if word in _WEEKDAYS:
            return None, _WEEKDAYS[word], " ".join(words[:i] + words[i + 1:])
    return None, None, text


def _mkdate(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        raise WhenError(f"{month}/{day} isn't a real date") from None


def _upcoming(now: datetime, month: int, day: int, year: int | None) -> date:
    """A month/day without a year means its next occurrence."""
    if year is not None:
        return _mkdate(year, month, day)
    candidate = _mkdate(now.year, month, day)
    return candidate if candidate >= now.date() else _mkdate(now.year + 1, month, day)


def typed_zone(text: str):
    """The time zone named in the text (e.g. 'ct'), or None."""
    for word in _normalize(text or "").split():
        if word in _ZONES:
            return ZoneInfo(_ZONES[word])
    return None


def parse_when(text: str, now: datetime, tz, max_days: int = 30) -> datetime:
    bot_tz = tz
    raw = _normalize(text or "")
    if not raw:
        raise WhenError(f"give a start time ({HINT})")
    zone = typed_zone(raw)
    if zone is not None:
        tz = zone
        raw = " ".join(w for w in raw.split() if w not in _ZONES)
    now = now.astimezone(tz)

    on_date, weekday, rest = _take_date(raw, now)
    at, rest = _take_time(rest)
    leftover = [w for w in rest.split() if w not in _FILLER]
    if at is None and len(leftover) == 1 and re.fullmatch(r"\d{1,2}", leftover[0]):
        hour = int(leftover[0])
        if not 1 <= hour <= 23:
            raise WhenError(f"that time isn't valid ({HINT})")
        at = dtime(hour + 12 if hour < 12 else hour, 0)  # a bare "9" is 9 PM: 10-mans are evening games
        leftover = []
    if at is None:
        raise WhenError(f"add a time ({HINT})")
    if leftover:
        raise WhenError(f"didn't understand \"{' '.join(leftover)[:30]}\" ({HINT})")

    if on_date is not None:
        result = datetime.combine(on_date, at, tz)
    elif weekday is not None:
        result = datetime.combine(now.date() + timedelta(days=(weekday - now.weekday()) % 7), at, tz)
        if result.timestamp() <= now.timestamp():
            result = datetime.combine(result.date() + timedelta(days=7), at, tz)
    else:
        result = datetime.combine(now.date(), at, tz)
        if result.timestamp() <= now.timestamp():
            result = datetime.combine(result.date() + timedelta(days=1), at, tz)

    # Compare instants, not wall clocks: aware datetimes sharing a tzinfo
    # compare by wall time, which is off by an hour across a DST change.
    if result.timestamp() <= now.timestamp():
        raise WhenError("that time has already passed")
    if result.timestamp() - now.timestamp() > max_days * 86400:
        raise WhenError(f"pick a time within the next {max_days} days")
    return result.astimezone(bot_tz)


def describe(when: datetime) -> str:
    """'Sat, Oct 10 at 9:00 PM EDT': how the bot read it, for autocomplete."""
    hour = when.hour % 12 or 12
    return f"{when:%a, %b} {when.day} at {hour}:{when:%M} {'PM' if when.hour >= 12 else 'AM'} {when:%Z}"
