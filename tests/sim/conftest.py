"""Shared fixtures for the simulated end-to-end runs."""

from datetime import time as dtime

import pytest

from utils import webhook
from tests.conftest import ET
from tests.sim.fakes import FakeDiscord
from tests.sim.harness import CHANNEL_ID, GUILD_ID, ROLE_ID
from utils.config import Config


@pytest.fixture
def discord_fake(monkeypatch):
    fake = FakeDiscord().start()
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", fake.webhook_url)
    monkeypatch.setattr(webhook, "_backoff", lambda attempt: 0)
    monkeypatch.setattr(webhook, "READ_TIMEOUT_SECONDS", 0.2)
    yield fake
    fake.close()


@pytest.fixture
def sim_cfg(tmp_path, discord_fake):
    return Config(
        discord_token=None, guild_id=str(GUILD_ID), channel_id=str(CHANNEL_ID), role_id=ROLE_ID,
        webhook_url=discord_fake.webhook_url, tz=ET, alert_time=dtime(12, 0), gameday_alert_hours=2,
        alert_catchup_hours=3, db_path=str(tmp_path / "sim.db"), default_slots=10, max_guests=1,
        max_open_per_channel=5, max_open_per_user=5, click_cooldown_seconds=0,
        backup_dir=str(tmp_path / "backups"),
    )
