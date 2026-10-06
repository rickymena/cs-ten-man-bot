"""
Heartbeat persistence, kept from the steam tracker's scheduler_state.py.

The file is written atomically (tmp -> os.replace) so healthcheck.py never
reads a torn heartbeat. The steam scheduler's per-job state machine is
retired (deadcode/): the bot's loop in bot.py replaced it.
"""

import json
import os
import time


def atomic_write_json(path: str, payload: dict, *, durable: bool) -> None:
    """tmp -> (fsync) -> os.replace -> (fsync dir). Raises OSError on failure."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
        if durable:
            handle.flush()
            os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    if durable:
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)


def write_heartbeat(path: str, extra: dict | None = None) -> None:
    """Write the heartbeat. Raises OSError; the bot's heartbeat loop logs it."""
    payload = {"last_tick_unix": time.time()}
    if extra:
        payload.update(extra)
    atomic_write_json(path, payload, durable=False)
