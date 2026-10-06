"""
Validation and escaping for everything a Discord user can type or control.

Two rules, applied everywhere:

  1. VALIDATE ON THE WAY IN. Untrusted input (slash-command options, display
     names, autocomplete text) passes through a clean_* function before it is
     parsed, stored or logged. Text is reduced to one line of printable
     characters with a length cap; structured values (titles, lobby links) are
     REJECTED with a reason the user sees, never silently rewritten into
     something they did not type.
  2. ESCAPE ON THE WAY OUT. Stored text is kept as the user typed it (minus
     control characters) and passed through md() whenever it is placed in a
     Discord message or embed, so it can never render as markdown, a masked
     link or a mention.

SQL needs neither: every query in utils/database.py uses bound parameters.

clean_text() is the steam tracker's _clean_text (utils/scrapesteam.py),
extended to drop invisible format characters as well.
"""

import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

ELLIPSIS = "…"

TITLE_MAX = 80
NAME_MAX = 32
WHEN_MAX = 40
URL_MAX = 300
SEARCH_MAX = 50

# Bidi embedding/override/isolate controls: they can visually reorder text
# ("evil.com/moc.elgoog") and are never needed in a title or a name.
_BIDI_CONTROLS = frozenset("\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
# Zero-width joiner stays: it builds emoji sequences. Other format chars
# (zero-width space, soft hyphen, BOM, ...) only serve to disguise text.
_KEEP_FORMAT = frozenset("\u200d")

# Characters Discord markdown gives meaning to mid-line. "\\*" renders as "*",
# so escaping costs nothing visually. "#", "-" and ">" only matter at the start
# of a line, and md() output is always placed after other text on its line.
# ":" stops "https://..." in a display name from becoming a clickable link.
_MARKDOWN_CHARS = re.compile(r"([\\*_~|`\[\]()<:])")

_URLISH = re.compile(
    r"(https?://|www\.|discord\.gg|discord(?:app)?\.com/invite"
    r"|\b[a-z0-9-]+\.(?:com|net|org|gg|io|ru|xyz|ly|me)\b)", re.I)
_MENTIONISH = re.compile(r"(@everyone|@here|<@[!&]?\d+>|<#\d+>)", re.I)
_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class InputError(ValueError):
    """Untrusted input was rejected. str(exc) is safe to show the user."""


def clean_text(value, max_len: int) -> str:
    """
    Coerce an untrusted value to a single-line, length-bounded str.

    Non-strings become "". Control characters (C0, C1, DEL, including CR/LF,
    so input cannot forge log lines), lone surrogates, bidi controls and
    invisible format characters are replaced with a space; runs of whitespace
    collapse to one. Over-length values are cut to max_len with an ellipsis.
    """
    if not isinstance(value, str):
        return ""
    out = []
    for ch in value:
        category = unicodedata.category(ch)
        if ch in _BIDI_CONTROLS or category in ("Cc", "Cs", "Co", "Cn") or (
            category == "Cf" and ch not in _KEEP_FORMAT
        ):
            out.append(" ")
        else:
            out.append(ch)
    text = " ".join(unicodedata.normalize("NFC", "".join(out)).split())
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + ELLIPSIS
    return text


def md(value) -> str:
    """Escape stored text for display: no markdown, no masked links, no mentions."""
    escaped = _MARKDOWN_CHARS.sub(r"\\\1", clean_text(value, 1000))
    # A zero-width space after "@" breaks @everyone/@here even if a caller
    # ever forgets allowed_mentions (same trick as discord.utils.escape_mentions).
    return escaped.replace("@", "@\u200b")


def clean_title(value) -> str:
    """A sign-up title. Rejects links, invites and mentions instead of mangling them."""
    if not isinstance(value, str) or len(value) > TITLE_MAX * 4:
        raise InputError(f"the title must be at most {TITLE_MAX} characters")
    text = clean_text(value, TITLE_MAX * 4)
    if not text:
        raise InputError("the title is empty")
    if len(text) > TITLE_MAX:
        raise InputError(f"the title must be at most {TITLE_MAX} characters")
    if _URLISH.search(text):
        raise InputError("links aren't allowed in the title (use /tenman lobby for the lobby link)")
    if _MENTIONISH.search(text):
        raise InputError("mentions aren't allowed in the title")
    return text


def clean_display_name(value) -> str:
    """A Discord display name for the roster. Never rejected: falls back to 'player'."""
    return clean_text(value, NAME_MAX) or "player"


def clean_when(value) -> str:
    if not isinstance(value, str) or len(value) > WHEN_MAX * 4:
        raise InputError("that start time is too long (try 'fri 9pm')")
    text = clean_text(value, WHEN_MAX * 4)
    if not text or len(text) > WHEN_MAX:
        raise InputError("give a start time like 'fri 9pm'")
    return text


def clean_search(value) -> str:
    """Autocomplete text: only used to filter a list, so just bound it."""
    return clean_text(value, SEARCH_MAX)


def clean_lobby_url(value, allowed_hosts: tuple[str, ...] = ()) -> str:
    """
    A lobby link: https only, a real public hostname, no embedded credentials,
    no whitespace or control characters, at most URL_MAX characters. When
    allowed_hosts is non-empty the host must be one of them or a subdomain.
    Rejected, never repaired: a link that needed repair is not one to post.
    """
    if not isinstance(value, str):
        raise InputError("give a full https:// link")
    raw = value.strip()
    if not raw or len(raw) > URL_MAX:
        raise InputError(f"the link must be at most {URL_MAX} characters")
    if clean_text(raw, URL_MAX) != raw or " " in raw:
        raise InputError("the link contains spaces or hidden characters")
    try:
        parts = urlsplit(raw)
        port = parts.port  # raises ValueError on a bad port
    except ValueError:
        raise InputError("that link isn't valid") from None
    if parts.scheme.lower() != "https":
        raise InputError("only https:// links are allowed")
    if "@" in parts.netloc or parts.username or parts.password:
        raise InputError("links with a username or password aren't allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise InputError("that link has no host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise InputError("use a domain name, not an IP address")
    labels = host.split(".")
    if len(labels) < 2 or not all(_HOST_LABEL.match(label) for label in labels):
        raise InputError("that link's host isn't a valid domain")
    if port not in (None, 443):
        raise InputError("links with a custom port aren't allowed")
    if allowed_hosts and not any(host == h or host.endswith("." + h) for h in allowed_hosts):
        raise InputError(f"lobby links must be on: {', '.join(allowed_hosts)}")
    return raw
