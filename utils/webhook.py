"""
Discord webhook delivery.

Delivery contract:

* A message is POSTed once and retried ONLY when the failure proves Discord
  never received the request (connect error / connect timeout / 5xx / 408 /
  429). A read timeout or a connection dropped mid-response is *ambiguous* --
  Discord may already have posted it -- so it is never retried. For the bot
  that means a flaky network can cost one alert, but never ping the role
  twice.
* The webhook URL (which embeds a secret token) is never written to the log;
  request exceptions are reported by class name only.

Bot messages are short; send_message() refuses anything over Discord's limit.
"""

import logging
import random
import time
from dataclasses import dataclass

import requests
import urllib3.exceptions

logger = logging.getLogger(__name__)

DISCORD_CHAR_LIMIT = 2000
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 15
MAX_RETRY_AFTER_SECONDS = 60.0

# Outcomes of a single POST.
DELIVERED = "delivered"      # 2xx -- Discord accepted it
FAILED = "failed"            # definitely not posted (retries exhausted / rejected)
AMBIGUOUS = "ambiguous"      # may or may not have been posted -- never retried
CONFIG_ERROR = "config_error"  # webhook missing/invalid/revoked -- will not self-heal

_CONFIG_STATUS_CODES = {401, 403, 404}
_RETRYABLE_4XX = {408, 429}

PLACEHOLDER_MARKER = "PASTE_"


@dataclass
class PostResult:
    outcome: str
    status_code: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == DELIVERED


def get_webhook_url() -> str | None:
    from utils.config import ConfigError, read_secret  # DISCORD_WEBHOOK_URL_FILE (Docker secret) or the env var
    try:
        url = read_secret("DISCORD_WEBHOOK_URL") or None
    except ConfigError:
        return None
    if url and PLACEHOLDER_MARKER in url:
        return None  # still the .env placeholder
    return url


def webhook_config_error() -> str | None:
    """Return a sanitized reason if the webhook is unusable, else None."""
    url = get_webhook_url()
    if not url:
        return "DISCORD_WEBHOOK_URL is not set (fill in the placeholder in .env)"
    if not url.startswith(("https://", "http://")):
        return "DISCORD_WEBHOOK_URL is malformed (must start with https://)"
    return None


def _is_safe_to_retry(exc: requests.RequestException) -> bool:
    """True only when the failure proves the request never reached Discord."""
    if isinstance(exc, requests.ConnectTimeout):
        return True
    if isinstance(exc, requests.Timeout):  # ReadTimeout: request was sent
        return False
    if isinstance(exc, requests.exceptions.SSLError):
        return True  # handshake failed before any request bytes were sent
    if isinstance(exc, requests.ConnectionError):
        # requests wraps urllib3 errors; walk the chain looking for a
        # pre-connection failure (DNS, refused, unreachable).
        seen = set()
        stack = [exc]
        while stack:
            cur = stack.pop()
            if cur is None or id(cur) in seen:
                continue
            seen.add(id(cur))
            if isinstance(cur, (urllib3.exceptions.NewConnectionError,
                                urllib3.exceptions.ConnectTimeoutError)):
                return True
            stack.extend(a for a in getattr(cur, "args", ()) if isinstance(a, BaseException))
            stack.append(getattr(cur, "reason", None))
            stack.append(cur.__cause__)
            stack.append(cur.__context__)
        # e.g. ProtocolError("Connection aborted", RemoteDisconnected) — the
        # request may have been processed before the socket dropped.
        return False
    # MissingSchema / InvalidURL etc. are handled by the caller as config errors
    return False


def _retry_after_seconds(resp) -> float | None:
    raw = None
    try:
        raw = resp.headers.get("Retry-After")
    except Exception:
        raw = None
    if raw is None:
        try:
            body = resp.json()
            raw = body.get("retry_after") if isinstance(body, dict) else None
        except Exception:
            raw = None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return min(value, MAX_RETRY_AFTER_SECONDS)


def _backoff(attempt: int) -> float:
    return 2 ** attempt + random.uniform(0, 1)  # noqa: S311 - retry jitter, not crypto


def _post_with_retry(session, url, *, json_payload, max_attempts=3) -> PostResult:
    last = PostResult(FAILED, detail="no attempt made")
    for attempt in range(1, max_attempts + 1):
        delay = None
        try:
            resp = session.post(
                url,
                json=json_payload,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            )
        except (requests.exceptions.MissingSchema,
                requests.exceptions.InvalidSchema,
                requests.exceptions.InvalidURL) as exc:
            # ISSUE-007: requests embeds the URL in these messages — log the
            # class name only.
            logger.error(f"Discord webhook URL rejected by requests ({type(exc).__name__})")
            return PostResult(CONFIG_ERROR, detail=f"invalid webhook URL ({type(exc).__name__})")
        except requests.RequestException as exc:
            name = type(exc).__name__
            if not _is_safe_to_retry(exc):
                logger.error(
                    f"Discord request outcome unknown ({name}) on attempt {attempt}/{max_attempts}; "
                    "not retrying to avoid a duplicate post"
                )
                return PostResult(AMBIGUOUS, detail=f"ambiguous delivery ({name})")
            logger.warning(f"Discord connection error ({name}) attempt {attempt}/{max_attempts}")
            last = PostResult(FAILED, detail=f"connection error ({name})")
        else:
            code = resp.status_code
            if 200 <= code < 300:
                return PostResult(DELIVERED, status_code=code)
            if code in _CONFIG_STATUS_CODES:
                logger.error(
                    f"Discord webhook HTTP {code} — webhook deleted, revoked or misconfigured; "
                    "not retrying"
                )
                return PostResult(CONFIG_ERROR, status_code=code, detail=f"webhook HTTP {code}")
            if code == 429:
                delay = _retry_after_seconds(resp)
                logger.warning(
                    f"Discord rate limited (HTTP 429), attempt {attempt}/{max_attempts}"
                    + (f"; Retry-After {delay:.1f}s" if delay is not None else "")
                )
                last = PostResult(FAILED, status_code=code, detail="rate limited (HTTP 429)")
            elif 400 <= code < 500 and code not in _RETRYABLE_4XX:
                logger.error(f"Discord rejected payload with HTTP {code}; not retrying")
                return PostResult(FAILED, status_code=code, detail=f"rejected HTTP {code}")
            else:
                logger.warning(f"Discord webhook HTTP {code}, attempt {attempt}/{max_attempts}")
                last = PostResult(FAILED, status_code=code, detail=f"HTTP {code}")
        if attempt < max_attempts:
            time.sleep(delay if delay is not None else _backoff(attempt))
    return last


def send_message(content: str, *, role_id: str | None = None, user_ids=(), embeds: list[dict] | None = None,
                 username: str | None = None, avatar_url: str | None = None, session=None) -> PostResult:
    """
    Post one message. Only the given role and users may be pinged; every other
    mention in the text (titles and names are user-controlled) is suppressed.
    """
    problem = webhook_config_error()
    if problem:
        logger.error(problem)
        return PostResult(CONFIG_ERROR, detail=problem)
    if len(content) > DISCORD_CHAR_LIMIT:
        return PostResult(FAILED, detail=f"message is {len(content)} chars, limit {DISCORD_CHAR_LIMIT}")

    payload = {
        "content": content,
        "allowed_mentions": {
            "parse": [],
            "roles": [str(role_id)] if role_id else [],
            "users": [str(u) for u in user_ids][:100],
        },
    }
    if embeds:
        payload["embeds"] = embeds[:10]
    if username:
        payload["username"] = username[:80]
    if avatar_url:
        payload["avatar_url"] = avatar_url
    own_session = session is None
    session = session or requests.Session()
    try:
        result = _post_with_retry(session, get_webhook_url(), json_payload=payload)
    finally:
        if own_session:
            session.close()
    if result.ok:
        logger.info("Discord webhook message delivered")
    else:
        logger.error(f"Discord webhook message not delivered: {result.outcome} ({result.detail})")
    return result
