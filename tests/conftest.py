import time
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pytest

from utils import database as db
from utils.config import Config
from utils.db_queue import DBQueue

ET = ZoneInfo("America/New_York")


@pytest.fixture
def q(tmp_path):
    queue = DBQueue(str(tmp_path / "tenman.db"), init=db.ensure_schema)
    queue.start()
    yield queue
    queue.stop()


@pytest.fixture
def cfg(tmp_path):
    return Config(
        discord_token=None, guild_id="1", channel_id="2", role_id="99", webhook_url=None,
        tz=ET, alert_time=dtime(12, 0), gameday_alert_hours=2, alert_catchup_hours=3,
        db_path=str(tmp_path / "tenman.db"), default_slots=10, max_guests=1,
        backup_dir=str(tmp_path / "backups"),
    )


@pytest.fixture
def make_session(q):
    def _make(start_at, *, created_at=None, slots=10, title="Friday 10-man"):
        return q.call(db.create_session, guild_id=1, channel_id=2, title=title,
                      start_at=int(start_at), created_by=7, slots=slots,
                      now=created_at if created_at is not None else time.time())
    return _make
