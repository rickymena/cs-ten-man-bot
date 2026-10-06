import logging
from unittest.mock import MagicMock, patch

import pytest
import requests
import urllib3.exceptions

from utils import webhook
from utils.webhook import (
    AMBIGUOUS,
    CONFIG_ERROR,
    DELIVERED,
    DISCORD_CHAR_LIMIT,
    FAILED,
    PostResult,
    _post_with_retry,
    send_message,
)

URL = "https://discord.test/api/webhooks/1/SECRET-TOKEN"


@pytest.fixture(autouse=True)
def _no_real_sleep():
    with patch("time.sleep") as sleep_mock:
        yield sleep_mock


def make_response(status_code, headers=None, json_body=None):
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    if json_body is None:
        response.json.side_effect = ValueError("no json")
    else:
        response.json.return_value = json_body
    return response


def connect_refused_error():
    """A ConnectionError shaped like the one requests raises for ECONNREFUSED."""
    reason = urllib3.exceptions.NewConnectionError(None, "Failed to establish a new connection")
    return requests.ConnectionError(urllib3.exceptions.MaxRetryError(None, URL, reason))


def connection_aborted_error():
    """A ConnectionError for a socket dropped after the request was sent."""
    return requests.ConnectionError(
        urllib3.exceptions.ProtocolError("Connection aborted.", ConnectionResetError(104, "reset"))
    )


def delivered(*_, **__):
    return PostResult(DELIVERED, status_code=204)


# ── _post_with_retry: retry policy ───────────────────────────────────────────

def test_post_with_retry_success_on_first_attempt(_no_real_sleep):
    session = MagicMock()
    session.post.return_value = make_response(204)

    result = _post_with_retry(session, URL, json_payload={})

    assert result.outcome == DELIVERED
    session.post.assert_called_once()
    _no_real_sleep.assert_not_called()


def test_post_with_retry_uses_split_connect_read_timeout():
    session = MagicMock()
    session.post.return_value = make_response(204)

    _post_with_retry(session, URL, json_payload={})

    timeout = session.post.call_args.kwargs["timeout"]
    assert isinstance(timeout, tuple) and len(timeout) == 2


def test_post_with_retry_retries_on_5xx(_no_real_sleep):
    session = MagicMock()
    session.post.side_effect = [make_response(500), make_response(200)]

    result = _post_with_retry(session, URL, json_payload={})

    assert result.outcome == DELIVERED
    assert session.post.call_count == 2
    assert _no_real_sleep.call_count == 1


def test_post_with_retry_fails_after_exhausting_5xx(_no_real_sleep):
    session = MagicMock()
    session.post.side_effect = [make_response(500)] * 3

    result = _post_with_retry(session, URL, json_payload={})

    assert result.outcome == FAILED
    assert session.post.call_count == 3
    assert _no_real_sleep.call_count == 2  # no sleep after the last attempt


def test_post_with_retry_max_attempts_one(_no_real_sleep):
    session = MagicMock()
    session.post.return_value = make_response(500)

    result = _post_with_retry(session, URL, json_payload={}, max_attempts=1)

    assert result.outcome == FAILED
    assert session.post.call_count == 1
    _no_real_sleep.assert_not_called()


def test_post_with_retry_retries_on_connection_refused():
    session = MagicMock()
    session.post.side_effect = [connect_refused_error(), make_response(204)]

    result = _post_with_retry(session, URL, json_payload={})

    assert result.outcome == DELIVERED
    assert session.post.call_count == 2


def test_post_with_retry_retries_on_connect_timeout():
    session = MagicMock()
    session.post.side_effect = [requests.ConnectTimeout("slow"), make_response(204)]

    result = _post_with_retry(session, URL, json_payload={})

    assert result.outcome == DELIVERED
    assert session.post.call_count == 2


def test_post_with_retry_read_timeout_not_retried():
    """H2c: Discord may already have accepted the message — retrying duplicates it."""
    session = MagicMock()
    session.post.side_effect = [requests.ReadTimeout("slow body"), make_response(204)]

    result = _post_with_retry(session, URL, json_payload={})

    assert result.outcome == AMBIGUOUS
    session.post.assert_called_once()


def test_post_with_retry_connection_aborted_mid_response_not_retried():
    session = MagicMock()
    session.post.side_effect = [connection_aborted_error(), make_response(204)]

    result = _post_with_retry(session, URL, json_payload={})

    assert result.outcome == AMBIGUOUS
    session.post.assert_called_once()


def test_post_with_retry_429_honours_retry_after_header(_no_real_sleep):
    session = MagicMock()
    session.post.side_effect = [make_response(429, headers={"Retry-After": "2.5"}), make_response(204)]

    result = _post_with_retry(session, URL, json_payload={})

    assert result.outcome == DELIVERED
    _no_real_sleep.assert_called_once_with(2.5)


def test_post_with_retry_429_uses_json_retry_after(_no_real_sleep):
    session = MagicMock()
    session.post.side_effect = [make_response(429, json_body={"retry_after": 0.75}), make_response(204)]

    _post_with_retry(session, URL, json_payload={})

    _no_real_sleep.assert_called_once_with(0.75)


def test_post_with_retry_429_retry_after_capped_at_60(_no_real_sleep):
    session = MagicMock()
    session.post.side_effect = [make_response(429, headers={"Retry-After": "3600"}), make_response(204)]

    _post_with_retry(session, URL, json_payload={})

    _no_real_sleep.assert_called_once_with(60.0)


def test_post_with_retry_429_exhausted_is_failed():
    session = MagicMock()
    session.post.side_effect = [make_response(429, headers={"Retry-After": "1"})] * 3

    result = _post_with_retry(session, URL, json_payload={})

    assert result.outcome == FAILED
    assert result.status_code == 429
    assert session.post.call_count == 3


@pytest.mark.parametrize("code", [401, 403, 404])
def test_post_with_retry_revoked_webhook_is_config_error_not_retried(code, _no_real_sleep):
    session = MagicMock()
    session.post.return_value = make_response(code)

    result = _post_with_retry(session, URL, json_payload={})

    assert result.outcome == CONFIG_ERROR
    session.post.assert_called_once()
    _no_real_sleep.assert_not_called()


def test_post_with_retry_400_not_retried():
    session = MagicMock()
    session.post.return_value = make_response(400)

    result = _post_with_retry(session, URL, json_payload={})

    assert result.outcome == FAILED
    assert result.status_code == 400
    session.post.assert_called_once()


def test_post_with_retry_408_is_retried():
    session = MagicMock()
    session.post.side_effect = [make_response(408), make_response(204)]

    assert _post_with_retry(session, URL, json_payload={}).outcome == DELIVERED


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.MissingSchema(f"Invalid URL '{URL}': No scheme supplied"),
        requests.exceptions.InvalidURL(f"Failed to parse: {URL}"),
    ],
)
def test_post_with_retry_invalid_url_is_config_error_and_not_logged(exc, caplog):
    """ISSUE-007: the webhook URL (secret) must never reach the log."""
    session = MagicMock()
    session.post.side_effect = exc

    with caplog.at_level(logging.DEBUG):
        result = _post_with_retry(session, URL, json_payload={})

    assert result.outcome == CONFIG_ERROR
    assert "SECRET-TOKEN" not in caplog.text
    assert "SECRET-TOKEN" not in result.detail


def test_post_with_retry_connection_error_text_not_logged(caplog):
    session = MagicMock()
    err = requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {URL}")
    session.post.side_effect = err

    with caplog.at_level(logging.DEBUG):
        _post_with_retry(session, URL, json_payload={})

    assert "SECRET-TOKEN" not in caplog.text


# ── send_discord_notification ────────────────────────────────────────────────

def test_send_unset_webhook_is_config_error(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert send_message("hi").outcome == CONFIG_ERROR


def test_send_placeholder_webhook_is_config_error(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "PASTE_WEBHOOK_URL_HERE")
    result = send_message("hi")
    assert result.outcome == CONFIG_ERROR
    assert "placeholder" in result.detail


def test_send_malformed_webhook_is_config_error(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "discord.com/api/webhooks/1/x")
    assert send_message("hi").outcome == CONFIG_ERROR


def test_send_too_long_is_refused_without_posting(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", URL)
    with patch.object(webhook, "_post_with_retry") as post:
        result = send_message("x" * (DISCORD_CHAR_LIMIT + 1))
    assert result.outcome == FAILED
    post.assert_not_called()


def test_send_only_allows_the_given_role_to_be_pinged(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", URL)
    with patch.object(webhook, "_post_with_retry", side_effect=delivered) as post:
        assert send_message("<@&42> @everyone go", role_id="42").ok
    payload = post.call_args.kwargs["json_payload"]
    assert payload["allowed_mentions"] == {"parse": [], "roles": ["42"], "users": []}


def test_send_without_role_blocks_all_mentions(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", URL)
    with patch.object(webhook, "_post_with_retry", side_effect=delivered) as post:
        send_message("@everyone")
    assert post.call_args.kwargs["json_payload"]["allowed_mentions"] == {"parse": [], "roles": [], "users": []}


def test_send_never_logs_the_url(monkeypatch, caplog):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", URL)
    with patch.object(webhook, "_post_with_retry", return_value=PostResult(FAILED, detail="HTTP 500")):
        with caplog.at_level(logging.DEBUG):
            send_message("hi")
    assert "SECRET-TOKEN" not in caplog.text


def test_webhook_url_can_come_from_a_secret_file(monkeypatch, tmp_path):
    secret = tmp_path / "hook"
    secret.write_text(URL + "\n")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_FILE", str(secret))
    assert webhook.get_webhook_url() == URL
