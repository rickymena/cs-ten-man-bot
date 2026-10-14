import datetime
import gzip
import os
import threading
import time
from pathlib import Path

import pytest

from utils import backup
from utils import database as db
from utils.backup import BackupError, backup_database, plan_retention, prune_backups, restore_backup, verify_backup


def only_files(directory):
    return sorted(p.name for p in Path(directory).iterdir() if p.is_file())


@pytest.fixture
def live_db(q, make_session):
    """A real database with some data, being served by the write queue."""
    sid = make_session(time.time() + 86400)
    for i in range(20):
        q.call(db.set_status, sid, f"u{i}", f"user{i}", db.IN)
    return q.path, sid


# ---------------------------------------------------------------------------
# Backup / verify / restore
# ---------------------------------------------------------------------------

def test_backup_round_trip_keeps_every_row(live_db, tmp_path):
    path, sid = live_db
    archive = backup_database(path, str(tmp_path / "b"))
    assert archive.endswith(".sqlite3.gz")
    assert only_files(tmp_path / "b") == sorted([os.path.basename(archive), os.path.basename(archive) + ".sha256"])
    assert verify_backup(archive)

    restored = str(tmp_path / "restored.db")
    restore_backup(archive, restored)
    q2 = db  # plain sqlite read of the restored copy
    import sqlite3
    conn = sqlite3.connect(restored)
    conn.row_factory = sqlite3.Row
    assert len(q2.list_signups(conn, sid)) == 20
    conn.close()


def test_backup_during_concurrent_writes_is_consistent(live_db, q, tmp_path):
    path, sid = live_db
    stop = threading.Event()

    def writer():
        n = 0
        while not stop.is_set():
            q.call(db.set_status, sid, f"w{n}", "w", db.IN)
            n += 1

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        archives = [backup_database(path, str(tmp_path / "b"), datetime.datetime(2026, 1, 1, 0, 0, i))
                    for i in range(3)]
    finally:
        stop.set()
        thread.join()
    assert all(verify_backup(a) for a in archives)


def test_bit_rot_and_truncation_are_detected(live_db, tmp_path):
    archive = backup_database(live_db[0], str(tmp_path))
    data = bytearray(Path(archive).read_bytes())
    data[len(data) // 2] ^= 0xFF
    Path(archive).write_bytes(bytes(data))
    assert not verify_backup(archive)

    archive2 = backup_database(live_db[0], str(tmp_path / "t"))
    Path(archive2).write_bytes(Path(archive2).read_bytes()[:100])
    assert not verify_backup(archive2)


def test_verify_rejects_a_gzip_that_is_not_our_database(tmp_path):
    fake = tmp_path / "backup_tenman_2026-01-01_00-00-00.sqlite3.gz"
    with gzip.open(fake, "wb") as f:
        f.write(b"not a database" * 100)
    backup._write_checksum(str(fake), backup.sha256_file(str(fake)))
    assert not verify_backup(str(fake))


def test_failed_backup_leaves_nothing_behind(live_db, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "commit_artifact", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(BackupError, match="disk full"):
        backup_database(live_db[0], str(tmp_path / "b"))
    assert only_files(tmp_path / "b") == []


def test_missing_database_is_an_error(tmp_path):
    with pytest.raises(BackupError, match="does not exist"):
        backup_database(str(tmp_path / "nope.db"), str(tmp_path))


def test_restore_never_overwrites(live_db, tmp_path):
    archive = backup_database(live_db[0], str(tmp_path / "b"))
    target = tmp_path / "exists.db"
    target.write_bytes(b"keep me")
    with pytest.raises(BackupError, match="already exists"):
        restore_backup(archive, str(target))
    assert target.read_bytes() == b"keep me"


def test_run_daily_takes_one_backup_per_day(live_db, tmp_path):
    day = datetime.datetime(2026, 10, 5, 4, 0)
    assert backup.run_daily(live_db[0], str(tmp_path), day) is not None
    assert backup.run_daily(live_db[0], str(tmp_path), day.replace(hour=5)) is None
    assert backup.run_daily(live_db[0], str(tmp_path), day + datetime.timedelta(days=1)) is not None
    assert len([f for f in only_files(tmp_path) if f.endswith(".gz")]) == 2


def test_cli_backup_and_verify(live_db, tmp_path, capsys):
    assert backup.main(["--db", live_db[0], "--dir", str(tmp_path)]) == 0
    archive = capsys.readouterr().out.strip()
    assert backup.main(["--verify", archive]) == 0


# ---------------------------------------------------------------------------
# Retention policy (pure)
# ---------------------------------------------------------------------------

NOW = datetime.datetime(2026, 9, 25, 12, 30, 0)


def daily(n, start_days_ago=0, hour=12):
    return {
        f"b{i}": (NOW - datetime.timedelta(days=start_days_ago + i)).replace(hour=hour, minute=0)
        for i in range(n)
    }


def test_retention_keeps_newest_n_even_when_all_expired():
    artifacts = daily(10, start_days_ago=100)

    doomed = plan_retention(artifacts, NOW, days_to_keep=30, min_keep=7)

    assert doomed == {"b7", "b8", "b9"}


def test_retention_deletes_expired_beyond_min_keep():
    artifacts = daily(40)

    doomed = plan_retention(artifacts, NOW, days_to_keep=30, min_keep=7)

    # b30 is 30 days and 30 minutes old -> past the cutoff.
    assert doomed == {f"b{i}" for i in range(30, 40)}


def test_retention_never_deletes_anything_when_fewer_than_min_keep():
    artifacts = daily(3, start_days_ago=365)

    assert plan_retention(artifacts, NOW, days_to_keep=30, min_keep=7) == set()


def test_retention_collapses_same_day_duplicates_on_past_days_only():
    yesterday = NOW - datetime.timedelta(days=1)
    artifacts = {
        "today_a": NOW.replace(hour=11),
        "today_b": NOW.replace(hour=12),
        "yday_early": yesterday.replace(hour=11),
        "yday_late": yesterday.replace(hour=22),
    }

    doomed = plan_retention(artifacts, NOW, days_to_keep=30, min_keep=7)

    assert doomed == {"yday_early"}


# ---------------------------------------------------------------------------
# prune_backups (filesystem)
# ---------------------------------------------------------------------------

def _stamp(days_ago):
    return (datetime.datetime.now() - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d_%H-%M-%S")


def _touch(directory, name, age_days=0.0):
    path = Path(directory) / name
    path.write_bytes(b"x")
    ts = (datetime.datetime.now() - datetime.timedelta(days=age_days)).timestamp()
    os.utime(path, (ts, ts))
    return path


def test_prune_keeps_min_keep_and_removes_sidecars_of_deleted(tmp_path):
    for d in range(40, 50):
        _touch(tmp_path, f"backup_tenman_{_stamp(d)}.sqlite3.gz", age_days=d)
        _touch(tmp_path, f"backup_tenman_{_stamp(d)}.sqlite3.gz.sha256", age_days=d)
    assert prune_backups(str(tmp_path), days_to_keep=30, min_keep=7) == 0
    remaining = only_files(tmp_path)
    assert len([f for f in remaining if f.endswith(".gz")]) == 7
    assert len([f for f in remaining if f.endswith(".sha256")]) == 7


def test_prune_removes_old_leftovers_but_not_fresh_ones(tmp_path):
    keep = f"backup_tenman_{_stamp(0)}.sqlite3.gz"
    _touch(tmp_path, keep)
    _touch(tmp_path, keep + ".sha256")
    _touch(tmp_path, f"backup_tenman_{_stamp(3)}.sqlite3.partial", age_days=3)
    fresh = _touch(tmp_path, f"backup_tenman_{_stamp(0)}.sqlite3.gz.partial").name
    unrelated = _touch(tmp_path, "README.txt", age_days=400).name
    assert prune_backups(str(tmp_path)) == 0
    assert only_files(tmp_path) == sorted([keep, keep + ".sha256", fresh, unrelated])


def test_prune_missing_directory_is_fine(tmp_path):
    assert prune_backups(str(tmp_path / "nope")) == 0


def test_backup_names_use_the_bots_timezone_not_the_hosts(live_db, tmp_path, monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Pacific/Kiritimati")  # UTC+14: always a different date from UTC at some hours
    expected = backup.local_now().strftime("%Y-%m-%d_%H")
    archive = backup_database(live_db[0], str(tmp_path / "b"))
    assert f"_{expected}-" in archive
