"""
Daily SQLite backup: a consistent online copy, gzipped, with a
sha256sum-compatible `.sha256` sidecar. The durability contract:

  * Nothing is ever written under a final name. Every artifact is built as
    `<name>.partial`, verified by reading it back (gzip CRC + SQLite
    `PRAGMA integrity_check`), checksummed, fsynced, and only then renamed
    into place with os.replace(), followed by an fsync of the directory.
  * A final-named `backup_*.sqlite3.gz` therefore always holds a complete,
    verified database. Any failure removes the run's partial files.
  * Retention: past days keep only their newest backup; anything older than
    BACKUP_RETENTION_DAYS is pruned, but the newest BACKUP_MIN_KEEP always
    survive, so if backups silently stop the last good ones are never aged out.

The copy uses SQLite's online backup API, which is safe while the bot is
writing: it never sees a half-applied transaction.

    python -m utils.backup                      take a backup now, then prune
    python -m utils.backup --verify FILE        check a backup (checksum + integrity)
    python -m utils.backup --restore FILE DEST  verify, then unpack to DEST (never over an existing file)
"""

import argparse
import datetime
import gzip
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.exit_codes import EXIT_CONFIG, EXIT_DB_FAILED, EXIT_OK  # noqa: E402

logger = logging.getLogger(__name__)

BACKUP_RETENTION_DAYS = 30
BACKUP_MIN_KEEP = 7
# Leftovers of an interrupted run are only removed once older than this, so
# pruning can never race a backup that is still being written.
ORPHAN_GRACE_SECONDS = 24 * 60 * 60

PREFIX = "backup_"
SUFFIX = ".sqlite3.gz"
PARTIAL_SUFFIX = ".partial"
CHECKSUM_SUFFIX = ".sha256"
_TIMESTAMP_RE = re.compile(r"_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.")
_TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"


class BackupError(Exception):
    pass


def local_now() -> datetime.datetime:
    """Backups are named and grouped by day in the bot's TIMEZONE, never the host's
    clock: a container runs in UTC, and mixing the two would make 'today' and
    retention disagree with the bot's own daily run."""
    return datetime.datetime.now(ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))).replace(tzinfo=None)


# --------------------------------------------------------------------------- file helpers

def remove_quietly(*paths) -> None:
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.error(f"Could not remove {path}: {e}")


def _fsync_file(path: str) -> None:
    with open(path, "rb") as f:
        os.fsync(f.fileno())


def _fsync_dir(path: str) -> None:
    """Persist a rename. Best effort: some filesystems refuse fsync on a directory."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checksum_path(path: str) -> str:
    return path + CHECKSUM_SUFFIX


def _write_checksum(artifact_path: str, digest: str) -> None:
    """Write `<hex>  <basename>`: verifiable with `sha256sum -c` from the backup dir."""
    sidecar = checksum_path(artifact_path)
    tmp = sidecar + PARTIAL_SUFFIX
    try:
        with open(tmp, "w", encoding="ascii") as f:
            f.write(f"{digest}  {os.path.basename(artifact_path)}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, sidecar)
    finally:
        remove_quietly(tmp)


def read_checksum(artifact_path: str) -> str | None:
    try:
        with open(checksum_path(artifact_path), encoding="ascii") as f:
            return f.read().split()[0].lower()
    except (FileNotFoundError, IndexError):
        return None


def commit_artifact(partial_path: str, final_path: str) -> str:
    """fsync -> checksum sidecar -> atomic rename -> fsync directory. Returns the sha256."""
    _fsync_file(partial_path)
    digest = sha256_file(partial_path)
    _write_checksum(final_path, digest)
    os.replace(partial_path, final_path)
    _fsync_dir(os.path.dirname(final_path) or ".")
    return digest


# --------------------------------------------------------------------------- verify

def _integrity_check_gz(path: str) -> None:
    """Unpack to a temp file (gzip CRC checked at EOF) and run PRAGMA integrity_check."""
    with tempfile.TemporaryDirectory(prefix="tenman-verify-") as tmp:
        plain = os.path.join(tmp, "check.db")
        try:
            with gzip.open(path, "rb") as src, open(plain, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
        except (OSError, EOFError) as e:
            raise BackupError(f"{path}: unreadable archive ({e})") from e
        conn = sqlite3.connect(f"file:{plain}?mode=ro", uri=True)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        except sqlite3.DatabaseError as e:
            raise BackupError(f"{path}: not a usable database ({e})") from e
        finally:
            conn.close()
    if result != "ok":
        raise BackupError(f"{path}: integrity_check failed: {result}")
    if version is None:
        raise BackupError(f"{path}: no schema version recorded")


def verify_backup(path: str) -> bool:
    """True if the sidecar checksum matches (no bit rot) and the database inside is intact."""
    expected = read_checksum(path)
    if expected is None:
        logger.error(f"{path}: no {CHECKSUM_SUFFIX} sidecar")
        return False
    if sha256_file(path) != expected:
        logger.error(f"CHECKSUM MISMATCH: {path} does not match {checksum_path(path)}")
        return False
    try:
        _integrity_check_gz(path)
    except BackupError as e:
        logger.error(str(e))
        return False
    return True


# --------------------------------------------------------------------------- backup

def backup_database(db_path: str, backup_dir: str, now: datetime.datetime | None = None) -> str:
    """
    Copy the live database to a verified, checksummed archive. Returns its
    path; raises BackupError (leaving nothing behind) on any failure.
    """
    if not os.path.exists(db_path):
        raise BackupError(f"database {db_path} does not exist")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = (now or local_now()).strftime(_TIMESTAMP_FORMAT)
    final = os.path.join(backup_dir, f"{PREFIX}tenman_{stamp}{SUFFIX}")
    raw_partial = os.path.join(backup_dir, f"{PREFIX}tenman_{stamp}.sqlite3{PARTIAL_SUFFIX}")
    gz_partial = final + PARTIAL_SUFFIX
    started = time.monotonic()
    try:
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        dst = sqlite3.connect(raw_partial)
        try:
            with dst:
                src.backup(dst)  # consistent online snapshot, safe during writes
        finally:
            dst.close()
            src.close()
        with open(raw_partial, "rb") as f_in, gzip.open(gz_partial, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out, 1024 * 1024)
        remove_quietly(raw_partial)
        _integrity_check_gz(gz_partial)  # read it back before it may carry a final name
        digest = commit_artifact(gz_partial, final)
    except (OSError, sqlite3.Error, BackupError) as e:
        remove_quietly(raw_partial, gz_partial, checksum_path(final))
        raise BackupError(f"backup failed: {e}") from e
    size = os.path.getsize(final)
    logger.info(f"Backup written: {os.path.basename(final)} ({size / 1024:.0f} KB, sha256 {digest[:12]}, "
                f"{time.monotonic() - started:.2f}s)")
    return final


def todays_backup(backup_dir: str, today: datetime.date) -> str | None:
    """The newest verified-on-write backup taken today, if any (final names are only ever complete)."""
    try:
        names = sorted(os.listdir(backup_dir), reverse=True)
    except FileNotFoundError:
        return None
    stamp = today.strftime("%Y-%m-%d")
    for name in names:
        if name.startswith(PREFIX) and name.endswith(SUFFIX) and f"_{stamp}_" in name:
            return os.path.join(backup_dir, name)
    return None


def restore_backup(path: str, dest: str) -> None:
    """Verify, then unpack to dest. Refuses to overwrite: stop the bot and swap files by hand."""
    if os.path.exists(dest):
        raise BackupError(f"{dest} already exists; restore to a new path, stop the bot, then swap it in")
    if not verify_backup(path):
        raise BackupError(f"{path} failed verification; not restoring it")
    partial = dest + PARTIAL_SUFFIX
    try:
        with gzip.open(path, "rb") as src, open(partial, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        _fsync_file(partial)
        os.replace(partial, dest)
    finally:
        remove_quietly(partial)


# --------------------------------------------------------------------------- retention

def _artifact_time(path: str) -> datetime.datetime:
    """The timestamp embedded in the file name (survives copies); mtime as fallback."""
    match = _TIMESTAMP_RE.search(os.path.basename(path))
    if match:
        try:
            return datetime.datetime.strptime(match.group(1), _TIMESTAMP_FORMAT)
        except ValueError:
            pass
    return datetime.datetime.fromtimestamp(os.path.getmtime(path))


def plan_retention(artifacts: dict[str, datetime.datetime], now: datetime.datetime, days_to_keep: int,
                   min_keep: int) -> set[str]:
    """
    Decide which artifacts to delete. Pure function, so the policy is testable.

      1. For every day before today, only the newest artifact is kept.
      2. The newest `min_keep` remaining artifacts are kept regardless of age.
      3. Everything else older than `days_to_keep` is deleted.
    """
    today = now.date()
    newest_first = sorted(artifacts, key=lambda p: artifacts[p], reverse=True)
    delete, survivors, seen_days = set(), [], set()
    for path in newest_first:
        day = artifacts[path].date()
        if day < today and day in seen_days:
            delete.add(path)
            continue
        seen_days.add(day)
        survivors.append(path)
    cutoff = now - datetime.timedelta(days=days_to_keep)
    for path in survivors[min_keep:]:
        if artifacts[path] < cutoff:
            delete.add(path)
    return delete


def prune_backups(backup_dir: str, days_to_keep: int = BACKUP_RETENTION_DAYS, min_keep: int = BACKUP_MIN_KEEP,
                  now: datetime.datetime | None = None) -> int:
    """Apply the retention policy, then remove old leftovers of interrupted runs. Returns the error count."""
    if not os.path.exists(backup_dir):
        return 0
    now = now or local_now()
    errors = 0
    try:
        names = sorted(os.listdir(backup_dir))
    except OSError as e:
        logger.error(f"Cannot list {backup_dir}: {e}")
        return 1
    finals = {n for n in names if n.startswith(PREFIX) and n.endswith(SUFFIX)}
    artifacts = {os.path.join(backup_dir, n): _artifact_time(os.path.join(backup_dir, n)) for n in finals}
    surviving = set(finals)
    for path in plan_retention(artifacts, now, days_to_keep, min_keep):
        try:
            os.remove(path)
        except OSError as e:
            logger.error(f"Error deleting {path}: {e}")
            errors += 1
            continue
        surviving.discard(os.path.basename(path))
        remove_quietly(checksum_path(path))
        logger.info(f"Deleted old backup: {os.path.basename(path)}")
    grace_cutoff = time.time() - ORPHAN_GRACE_SECONDS
    for name in names:
        if not name.startswith(PREFIX) or name in finals:
            continue
        if name.endswith(CHECKSUM_SUFFIX) and name[: -len(CHECKSUM_SUFFIX)] in surviving:
            continue
        path = os.path.join(backup_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) <= grace_cutoff:
                os.remove(path)
                logger.info(f"Deleted leftover: {name}")
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.error(f"Error deleting leftover {path}: {e}")
            errors += 1
    return errors


def run_daily(db_path: str, backup_dir: str, now: datetime.datetime) -> str | None:
    """Take today's backup if there isn't one yet, then prune. Returns the new path or None."""
    if todays_backup(backup_dir, now.date()):
        return None
    path = backup_database(db_path, backup_dir, now.replace(tzinfo=None))
    if prune_backups(backup_dir, now=now.replace(tzinfo=None)):
        logger.warning("backup pruning reported errors")
    return path


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(root, ".env"))  # same TIMEZONE / paths as the bot when run by hand
    except ImportError:
        pass
    parser = argparse.ArgumentParser(description="Back up, verify or restore the 10-man database.")
    parser.add_argument("--db", default=os.path.join(root, os.getenv("DB_PATH", os.path.join("data", "tenman.db"))))
    parser.add_argument("--dir", default=os.path.join(root, os.getenv("BACKUP_DIR", "backups")))
    parser.add_argument("--verify", metavar="FILE")
    parser.add_argument("--restore", nargs=2, metavar=("FILE", "DEST"))
    args = parser.parse_args(argv)
    try:
        if args.verify:
            ok = verify_backup(args.verify)
            print("ok" if ok else "FAILED")
            return EXIT_OK if ok else EXIT_DB_FAILED
        if args.restore:
            restore_backup(*args.restore)
            print(f"restored to {args.restore[1]}")
            return EXIT_OK
        print(backup_database(args.db, args.dir))
        prune_backups(args.dir)
        return EXIT_OK
    except BackupError as e:
        logger.error(str(e))
        return EXIT_CONFIG if "already exists" in str(e) else EXIT_DB_FAILED


if __name__ == "__main__":
    sys.exit(main())
