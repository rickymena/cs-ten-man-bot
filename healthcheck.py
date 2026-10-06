"""
Container healthcheck for the bot. Stdlib only; exits 0 healthy, 1 not.
Ported from the steam tracker's healthcheck.py.

Reads the heartbeat bot.py writes every 30 s and fails when any of these hold:

  1. the heartbeat is missing, unreadable, or older than HEALTH_MAX_TICK_AGE
     seconds (default 120): the event loop has stalled or the process died;
  2. the bot has not been connected to Discord for HEALTH_GATEWAY_GRACE
     seconds (default 600): a short reconnect is normal, ten minutes is not;
  3. one database job has been running longer than HEALTH_DB_JOB_MAX seconds
     (default 60): the write queue is stuck, so every click would time out.

The reason is printed on stdout; `podman inspect` keeps it in the health log.
"Unhealthy" alone neither restarts the container nor alerts anyone under
Podman or Docker Compose: see guides/guide-deploy.txt.
"""

import json
import os
import sys
import time


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def check(now: float | None = None) -> tuple[bool, str]:
    now = time.time() if now is None else now
    # Same default as bot.py: logs/ next to this file (/app/logs in the container).
    path = os.getenv("HEARTBEAT_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs",
                                                    "heartbeat.json"))
    try:
        with open(path, encoding="utf-8") as handle:
            beat = json.load(handle)
        last_tick = float(beat["last_tick_unix"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return False, f"heartbeat unreadable at {path}: {type(exc).__name__}"

    age = now - last_tick
    if age > _float_env("HEALTH_MAX_TICK_AGE", 120):
        return False, f"bot stalled: last heartbeat {age:.0f}s ago"

    if beat.get("gateway_ready") is False:
        since = beat.get("gateway_down_since_unix")
        grace = _float_env("HEALTH_GATEWAY_GRACE", 600)
        if isinstance(since, (int, float)) and now - since > grace:
            return False, f"disconnected from Discord for {now - since:.0f}s"

    queue = beat.get("queue") if isinstance(beat.get("queue"), dict) else {}
    started = queue.get("current_job_started_unix")
    if isinstance(started, (int, float)) and now - started > _float_env("HEALTH_DB_JOB_MAX", 60):
        return False, f"database queue stuck: one job running for {now - started:.0f}s"

    return True, f"ok: heartbeat {age:.0f}s old, backlog {queue.get('backlog', '?')}"


def main() -> int:
    healthy, reason = check()
    print(reason)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
