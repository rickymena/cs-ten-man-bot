"""
Single-writer queue in front of SQLite.

Why: discord.py handles every button click concurrently. Two players clicking
In at 9/10 would each read "9 signed up" and both take the last spot, and
concurrent SQLite writers fail with "database is locked". Here every write is a
job on one queue, drained by one thread that owns the only write connection:

  * one job = one BEGIN IMMEDIATE transaction, so read-then-write logic inside
    a job (count the roster, then insert) is atomic;
  * jobs run strictly one at a time, so writers never contend for the lock;
  * the queue is bounded and callers wait with a timeout, so a stuck database
    surfaces as QueueBusy ("try again") instead of a hung bot;
  * a job whose caller already gave up is cancelled before it runs, so a
    "try again" reply never coincides with the write happening anyway.

Reads that do not need to be atomic with a write (rendering a roster, the
alerts pass) use read(), which opens a short-lived read-only connection; WAL
mode lets those run while the writer holds its transaction.
"""

import asyncio
import concurrent.futures
import logging
import queue
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

DEFAULT_MAXSIZE = 500
DEFAULT_TIMEOUT = 10.0
BUSY_TIMEOUT_MS = 5000

_STOP = object()


class QueueBusy(RuntimeError):
    """The queue is full, stopped, or the job did not finish within its timeout."""


def _connect(path: str, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000,
                               check_same_thread=False)
    else:
        conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
                               check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


class DBQueue:
    def __init__(self, path: str, *, maxsize: int = DEFAULT_MAXSIZE, init=None):
        self.path = path
        self._jobs: queue.Queue = queue.Queue(maxsize=maxsize)
        self._init = init
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._start_error: BaseException | None = None
        self._running = False
        # Health counters, read by the heartbeat.
        self.jobs_done = 0
        self.jobs_failed = 0
        self.current_job_started: float | None = None

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="db-writer", daemon=True)
        self._thread.start()
        self._started.wait()
        if self._start_error is not None:
            raise self._start_error
        self._running = True

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        self._running = False
        try:
            self._jobs.put(_STOP, timeout=timeout)
        except queue.Full:
            logger.error("database queue still full at shutdown; abandoning queued jobs")
        self._thread.join(timeout)
        self._thread = None

    def _loop(self) -> None:
        try:
            conn = _connect(self.path)
            if self._init is not None:
                self._execute(conn, self._init, (), {})
        except BaseException as exc:
            self._start_error = exc
            self._started.set()
            return
        self._started.set()
        try:
            while True:
                item = self._jobs.get()
                if item is _STOP:
                    break
                future, fn, args, kwargs = item
                if not future.set_running_or_notify_cancel():
                    continue  # caller gave up before it ran
                self.current_job_started = time.time()
                try:
                    result = self._execute(conn, fn, args, kwargs)
                except BaseException as exc:
                    self.jobs_failed += 1
                    future.set_exception(exc)
                else:
                    self.jobs_done += 1
                    future.set_result(result)
                finally:
                    self.current_job_started = None
        finally:
            # Fail anything still queued so no caller waits forever.
            while True:
                try:
                    item = self._jobs.get_nowait()
                except queue.Empty:
                    break
                if item is not _STOP and item[0].set_running_or_notify_cancel():
                    item[0].set_exception(QueueBusy("database queue stopped"))
            conn.close()

    @staticmethod
    def _execute(conn, fn, args, kwargs):
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn, *args, **kwargs)
            conn.execute("COMMIT")
        except BaseException:
            # Also covers a failed COMMIT (disk full): without the rollback the
            # transaction stays open and every later BEGIN fails until restart.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return result

    # ----------------------------------------------------------------- submitting

    def submit(self, fn, *args, **kwargs) -> concurrent.futures.Future:
        """Queue fn(conn, *args, **kwargs) to run in its own transaction. Thread-safe."""
        if not self._running:
            raise QueueBusy("database queue is not running")
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            self._jobs.put_nowait((future, fn, args, kwargs))
        except queue.Full:
            raise QueueBusy("database queue is full") from None
        return future

    def call(self, fn, *args, timeout: float = DEFAULT_TIMEOUT, **kwargs):
        """Blocking submit-and-wait, for threads and CLI tools."""
        future = self.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            if future.cancel():
                raise QueueBusy("database queue timed out") from None
            return future.result()  # already running: let it finish

    async def run(self, fn, *args, timeout: float = DEFAULT_TIMEOUT, **kwargs):
        """Await a write job from the event loop without blocking it."""
        future = self.submit(fn, *args, **kwargs)
        wrapped = asyncio.wrap_future(future)
        try:
            return await asyncio.wait_for(asyncio.shield(wrapped), timeout)
        except TimeoutError:
            if future.cancel():
                raise QueueBusy("database queue timed out") from None
            return await wrapped  # already running: let it finish

    # ----------------------------------------------------------------- reads

    def read(self, fn, *args, **kwargs):
        """Run fn(conn, ...) on a fresh read-only connection (blocking; use from a thread)."""
        conn = _connect(self.path, readonly=True)
        try:
            return fn(conn, *args, **kwargs)
        finally:
            conn.close()

    async def aread(self, fn, *args, **kwargs):
        return await asyncio.to_thread(self.read, fn, *args, **kwargs)

    # ----------------------------------------------------------------- health

    def stats(self) -> dict:
        return {
            "backlog": self._jobs.qsize(),
            "jobs_done": self.jobs_done,
            "jobs_failed": self.jobs_failed,
            "current_job_started_unix": self.current_job_started,
        }
