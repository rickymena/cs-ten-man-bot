import pytest

from utils.sanitize import (
    InputError,
    clean_display_name,
    clean_lobby_url,
    clean_search,
    clean_text,
    clean_title,
    clean_when,
    md,
)


# ----------------------------------------------------------------- clean_text

@pytest.mark.parametrize("raw, expected", [
    ("Friday\n10-man", "Friday 10-man"),                 # newline: no forged log lines / multi-line titles
    ("a\r\nFAKE LOG LINE", "a FAKE LOG LINE"),
    ("tab\there", "tab here"),
    ("zero\u200bwidth", "zero width"),                     # zero-width space
    ("evil\u202egnp.exe", "evil gnp.exe"),                 # right-to-left override
    ("nul\x00byte", "nul byte"),
    ("bell\x07", "bell"),
    ("  lots   of   space  ", "lots of space"),
    ("\U0001F468\u200d\U0001F469\u200d\U0001F467", "\U0001F468\u200d\U0001F469\u200d\U0001F467"),  # ZWJ emoji kept
    (None, ""),
    (12345, ""),
])
def test_clean_text_strips_control_and_invisible_characters(raw, expected):
    assert clean_text(raw, 100) == expected


def test_clean_text_caps_length_with_ellipsis():
    assert clean_text("x" * 50, 10) == "x" * 9 + "…"


# ----------------------------------------------------------------- md (output escaping)

@pytest.mark.parametrize("raw, must_not_contain", [
    ("@everyone", "@everyone"),
    ("@here", "@here"),
    ("<@123456789012345678>", "<@1"),
    ("<@&123456789012345678>", "<@&"),
    ("[free skins](https://evil.example)", "[free skins](https"),
    ("**bold** __u__ ~~s~~ ||spoiler|| `code`", "**bold**"),
    ("https://evil.example", "https://"),
])
def test_md_neutralises_markdown_mentions_and_links(raw, must_not_contain):
    assert must_not_contain not in md(raw)


def test_md_leaves_ordinary_names_readable():
    assert md("On Ji [RMNT],") == "On Ji \\[RMNT\\],"
    assert md("dadgeL") == "dadgeL"
    assert md("10-man") == "10-man"


# ----------------------------------------------------------------- titles

@pytest.mark.parametrize("title", ["Friday 10-man", "Mollen's birthday 5v5", "Scrim night \U0001F525"])
def test_clean_title_accepts_normal_titles(title):
    assert clean_title(title) == title


@pytest.mark.parametrize("title, reason", [
    ("", "empty"),
    ("   \u200b  ", "empty"),
    ("x" * 81, "at most 80"),
    ("x" * 5000, "at most 80"),
    ("free skins https://evil.example", "links"),
    ("join www.evil.example", "links"),
    ("discord.gg/abcdef", "links"),
    ("steamcommunity.com trade", "links"),
    ("@everyone get in here", "mentions"),
    ("ping <@&123456789012345678>", "mentions"),
    ("hi <@123456789012345678>", "mentions"),
])
def test_clean_title_rejects_bad_titles(title, reason):
    with pytest.raises(InputError, match=reason):
        clean_title(title)


def test_clean_title_strips_control_characters_before_checking():
    assert clean_title("Friday\n\u202e10-man") == "Friday 10-man"


# ----------------------------------------------------------------- names, when, search

def test_display_name_never_empty_and_capped():
    assert clean_display_name("\u200b\u200b") == "player"
    assert clean_display_name(None) == "player"
    assert len(clean_display_name("y" * 200)) == 32


def test_clean_when_bounds_length():
    assert clean_when("  fri   9pm ") == "fri 9pm"
    with pytest.raises(InputError):
        clean_when("x" * 41)
    with pytest.raises(InputError):
        clean_when("\n\n")


def test_clean_search_is_bounded():
    assert len(clean_search("a" * 500)) == 50


# ----------------------------------------------------------------- lobby links

@pytest.mark.parametrize("url", [
    "https://popflash.site/-/wiggz/scrim",
    "https://popflash.site/scrim/abc?x=1#frag",
    "https://sub.example.com:443/path",
])
def test_lobby_url_accepts_plain_https_links(url):
    assert clean_lobby_url(url) == url


@pytest.mark.parametrize("url, reason", [
    ("http://popflash.site/x", "only https"),
    ("javascript:alert(1)", "only https"),
    ("steam://connect/1.2.3.4", "only https"),
    ("ftp://example.com", "only https"),
    ("https://user:pass@popflash.site/x", "username or password"),
    ("https://popflash.site@evil.example/x", "username or password"),
    ("https://192.168.1.10/x", "IP address"),
    ("https://[::1]/x", "IP address"),
    ("https://localhost/x", "valid domain"),
    ("https://evil_host.com/x", "valid domain"),
    ("https://example.com:8080/x", "custom port"),
    ("https://example.com:99999/x", "isn't valid"),
    ("https://popflash.site/x y", "spaces or hidden"),
    ("https://popflash.site/\u202ex", "spaces or hidden"),
    ("https://popflash.site/x\nhttps://evil.example", "spaces or hidden"),
    ("https://example.com/" + "a" * 300, "at most 300"),
    ("", "at most 300"),
    (None, "https://"),
])
def test_lobby_url_rejects_unsafe_links(url, reason):
    with pytest.raises(InputError, match=reason):
        clean_lobby_url(url)


def test_lobby_url_allowlist():
    hosts = ("popflash.site",)
    assert clean_lobby_url("https://popflash.site/x", hosts)
    assert clean_lobby_url("https://www.popflash.site/x", hosts)
    with pytest.raises(InputError, match="must be on"):
        clean_lobby_url("https://popflash.site.evil.example/x", hosts)
    with pytest.raises(InputError, match="must be on"):
        clean_lobby_url("https://notpopflash.site/x", hosts)
