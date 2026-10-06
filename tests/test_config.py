import pytest

from utils.config import ConfigError, load_config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("DISCORD_TOKEN", "GUILD_ID", "CHANNEL_ID", "TENMAN_ROLE_ID", "TIMEZONE", "ALERT_TIME",
                 "TENMAN_SLOTS", "TENMAN_MAX_GUESTS", "LOBBY_ALLOWED_HOSTS", "GAMEDAY_ALERT_HOURS_BEFORE"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_load_and_placeholders_are_unset(monkeypatch):
    monkeypatch.setenv("GUILD_ID", "PASTE_SERVER_ID_HERE")
    cfg = load_config()
    assert cfg.guild_id is None
    assert cfg.alert_time.hour == 12


@pytest.mark.parametrize("name, value, message", [
    ("GUILD_ID", "my server", "GUILD_ID must be a Discord ID"),
    ("TENMAN_ROLE_ID", "<@&123456789012345678>", "TENMAN_ROLE_ID must be a Discord ID"),
    ("TIMEZONE", "Mars/Olympus", "not a known timezone"),
    ("TIMEZONE", "../../etc/passwd", "not a known timezone"),
    ("ALERT_TIME", "25:00", "HH:MM"),
    ("ALERT_TIME", "noon", "HH:MM"),
    ("TENMAN_SLOTS", "1000", "outside 2..40"),
    ("TENMAN_MAX_GUESTS", "-1", "outside 0..5"),
    ("GAMEDAY_ALERT_HOURS_BEFORE", "soon", "not a number"),
    ("LOBBY_ALLOWED_HOSTS", "popflash.site, bad host", "not a domain"),
    ("DISCORD_TOKEN", "not-a-token", "does not look like a bot token"),
])
def test_bad_settings_are_rejected_with_a_reason(monkeypatch, name, value, message):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=message):
        load_config()


def test_token_value_never_appears_in_the_error(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "SECRETSECRET")
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert "SECRETSECRET" not in str(exc.value)


def test_all_errors_reported_at_once(monkeypatch):
    monkeypatch.setenv("GUILD_ID", "x")
    monkeypatch.setenv("ALERT_TIME", "x")
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert "GUILD_ID" in str(exc.value) and "ALERT_TIME" in str(exc.value)


def test_token_file_wins_over_env_and_stays_out_of_errors(monkeypatch, tmp_path):
    secret = tmp_path / "discord_token"
    secret.write_text("a" * 24 + "." + "b" * 6 + "." + "c" * 27 + "\n")
    monkeypatch.setenv("DISCORD_TOKEN", "PASTE_BOT_TOKEN_HERE")
    monkeypatch.setenv("DISCORD_TOKEN_FILE", str(secret))
    assert load_config().discord_token == "a" * 24 + "." + "b" * 6 + "." + "c" * 27


def test_unreadable_secret_file_is_a_config_error(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_TOKEN_FILE", str(tmp_path / "missing"))
    with pytest.raises(ConfigError, match="DISCORD_TOKEN_FILE=.* can't be read"):
        load_config()


def test_empty_webhook_secret_means_no_webhook(monkeypatch, tmp_path):
    empty = tmp_path / "hook"
    empty.write_text("")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_FILE", str(empty))
    assert load_config().webhook_url is None
