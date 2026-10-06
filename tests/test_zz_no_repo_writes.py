"""Runs last (zz): the suite must leave the project's real backups folder untouched."""

import time
from pathlib import Path

BACKUPS = Path(__file__).resolve().parent.parent / "backups"


def test_suite_never_writes_into_the_projects_backups():
    # The running bot writes at most one backup a day; several fresh ones means a test wrote here.
    fresh = [p for p in BACKUPS.glob("backup_tenman_*") if time.time() - p.stat().st_mtime < 600] \
        if BACKUPS.exists() else []
    assert len([p for p in fresh if p.name.endswith(".gz")]) <= 1, f"tests wrote backups into {BACKUPS}"
    assert not [p for p in fresh if p.name.endswith(".partial")]
