import asyncio
import sqlite3
import threading
import time

import pytest

from utils import database as db
from utils.db_queue import DBQueue, QueueBusy
from utils.roster import build_roster


def test_twenty_simultaneous_signups_at_nine_of_ten_fill_exactly_one_spot(q, make_session):
    sid = make_session(time.time() + 86400)
    for i in range(9):
        q.call(db.set_status, sid, f"u{i}", f"user{i}", db.IN, now=1000 + i)

    barrier = threading.Barrier(20)
    errors = []

    def click(n):
        try:
            barrier.wait()
            q.call(db.set_status, sid, f"late{n}", f"late{n}", db.IN)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=click, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    roster = build_roster(q.read(db.list_signups, sid), 10)
    assert roster.count == 10
    assert len(roster.waitlist) == 19
    assert len({s["user_id"] for s in roster.confirmed + roster.waitlist}) == 29


def test_async_clicks_from_event_loop_are_serialized(q, make_session):
    sid = make_session(time.time() + 86400)

    async def main():
        await asyncio.gather(*(q.run(db.set_status, sid, f"u{i}", "x", db.IN) for i in range(50)))

    asyncio.run(main())
    assert len(q.read(db.list_signups, sid)) == 50


def test_failed_job_rolls_back_and_queue_keeps_working(q, make_session):
    sid = make_session(time.time() + 86400)

    def half_then_fail(conn):
        db.set_status(conn, sid, "a", "a", db.IN)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        q.call(half_then_fail)
    assert q.read(db.list_signups, sid) == []
    q.call(db.set_status, sid, "b", "b", db.IN)
    assert len(q.read(db.list_signups, sid)) == 1
    assert q.stats()["jobs_failed"] == 1


def test_full_queue_raises_busy_instead_of_blocking(tmp_path):
    q = DBQueue(str(tmp_path / "x.db"), maxsize=1, init=db.ensure_schema)
    q.start()
    gate = threading.Event()
    try:
        q.submit(lambda conn: gate.wait(5))   # occupies the writer
        time.sleep(0.05)
        q.submit(lambda conn: None)           # fills the one queue slot
        with pytest.raises(QueueBusy):
            q.submit(lambda conn: None)
    finally:
        gate.set()
        q.stop()


def test_timed_out_job_is_cancelled_and_never_runs(tmp_path):
    q = DBQueue(str(tmp_path / "x.db"), init=db.ensure_schema)
    q.start()
    gate = threading.Event()
    ran = []
    try:
        q.submit(lambda conn: gate.wait(5))
        with pytest.raises(QueueBusy):
            q.call(lambda conn: ran.append(1), timeout=0.1)
        gate.set()
        q.call(lambda conn: None)  # drain
        assert ran == []
    finally:
        gate.set()
        q.stop()


def test_reads_do_not_wait_for_a_running_write(q, make_session):
    sid = make_session(time.time() + 86400)
    gate = threading.Event()
    q.submit(lambda conn: (db.set_status(conn, sid, "a", "a", db.IN), gate.wait(5)))
    time.sleep(0.05)
    started = time.monotonic()
    assert q.read(db.list_signups, sid) == []  # uncommitted write invisible, no blocking
    assert time.monotonic() - started < 1
    gate.set()


def test_submit_after_stop_is_busy(tmp_path):
    q = DBQueue(str(tmp_path / "x.db"), init=db.ensure_schema)
    q.start()
    q.stop()
    with pytest.raises(QueueBusy):
        q.submit(lambda conn: None)


def test_bad_database_path_fails_at_start(tmp_path):
    q = DBQueue(str(tmp_path / "missing-dir" / "x.db"), init=db.ensure_schema)
    with pytest.raises(sqlite3.OperationalError):
        q.start()
