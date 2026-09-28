"""
app/database.py

Handles the PostgreSQL database connection.
Any file inside the app package that needs to talk to the database
imports get_connection from here, e.g.:

    from app.database import get_connection

Switched from SQLite to PostgreSQL because Render's free-tier web
service has no persistent disk -- a SQLite file there gets wiped on
every redeploy/restart. Render's free PostgreSQL is a separate
managed service that persists independently of the web service's
disk.

DATABASE_URL is provided by Render as an environment variable when
you create a PostgreSQL instance there. For local development, set
DATABASE_URL in your .env file to point at a local Postgres instance
(or a free Render Postgres instance you also use for dev).

Connections are drawn from a small pool rather than opened fresh on
every call. This was invisible for a long time because every other
synchronous operation in the app was already slow anyway (an LLM
call), so nobody noticed a plain connection open costing real time
too. It became visible once a database write (webhook.py's message
queue) ended up directly on the webhook's must-be-fast critical path
-- opening a brand-new connection measured multiple seconds in
testing, which defeated the entire point of that change. Pooling
keeps a handful of connections open and reuses them instead.

get_connection() still returns something every existing caller can
use exactly as before (cursor(), commit(), and importantly close())
-- close() here returns the connection to the pool rather than really
closing it, so none of the ~10 files already calling get_connection()
needed to change.

Stale connections (fixed 2026-09-28): Neon's free tier suspends the
database after a few minutes idle and drops every open connection. The
pool had no way to know -- it would hand out a dead connection, and the
caller's next query failed with "server closed the connection
unexpectedly" (seen three times in local testing). On Render that could
mean a lost message, or worse a skipped scheduled job after hours of
quiet, which nothing retries. So get_connection() now checks a
connection with a tiny SELECT 1 before handing it out -- but ONLY if it
has sat idle longer than _IDLE_CHECK_SECONDS (or has never been used),
which is exactly when it could have gone stale. Connections in constant
use (the message workers poll every half-second) never pay for the
check. Dead ones are discarded and the pool opens fresh ones in their
place. close() likewise discards a connection that broke mid-use instead
of crashing while trying to return it.
"""

import os
import time
import threading
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

_pool = None
_pool_lock = threading.Lock()

_IDLE_CHECK_SECONDS = 60

# id(connection) -> time.monotonic() it was last returned to the pool.
# A connection missing from here has never been handed out yet (e.g.
# pre-warmed at boot and sat unused since), so it gets checked too.
_last_used = {}


def _get_pool():
    """
    minconn is deliberately NOT left at a small default. Opening a
    fresh connection is the slow part (several seconds in testing),
    and the pool only pre-warms `minconn` connections at creation --
    the rest are opened on demand as they're needed. With 3 background
    worker threads (see webhook.py) constantly polling for queued
    messages, a small minconn meant they'd immediately compete for the
    few pre-warmed connections and force the pool to open new ones
    live during normal operation -- paying that same multi-second cost
    unpredictably instead of once at boot. Pre-warming enough for every
    known concurrent consumer (3 workers + headroom for the webhook
    itself) up front keeps that cost where it belongs: app startup,
    not live traffic.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:  # re-check -- another thread may have created it while we waited for the lock
                database_url = os.getenv("DATABASE_URL")
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=6, maxconn=10, dsn=database_url, cursor_factory=RealDictCursor
                )
    return _pool


class _PooledConnection:
    """
    Thin wrapper around a real psycopg2 connection borrowed from the
    pool. Every method existing code already calls (cursor, commit,
    rollback) passes straight through -- only close() behaves
    differently, returning the connection to the pool instead of
    actually closing it.
    """

    def __init__(self, real_conn, owning_pool):
        self._conn = real_conn
        self._pool = owning_pool

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        """
        Returns the connection to the pool -- or, if it broke while in
        use, discards it instead. Returning a dead connection normally
        makes the pool attempt a rollback on it, which itself raises;
        that used to crash inside close() (seen in testing).
        """
        if self._conn.closed:
            _discard(self._pool, self._conn)
            return
        try:
            self._pool.putconn(self._conn)
            _last_used[id(self._conn)] = time.monotonic()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            _discard(self._pool, self._conn)


def _discard(p, real_conn):
    _last_used.pop(id(real_conn), None)
    try:
        p.putconn(real_conn, close=True)
    except Exception:
        pass  # already unusable -- nothing more to clean up


def _is_alive(real_conn):
    """
    Cheap unless the connection has been idle long enough to have
    possibly gone stale -- see module docstring.
    """
    if real_conn.closed:
        return False
    last = _last_used.get(id(real_conn))
    if last is not None and time.monotonic() - last < _IDLE_CHECK_SECONDS:
        return True
    try:
        cur = real_conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        real_conn.rollback()  # the check opened a transaction -- leave it clean
        return True
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        return False


def get_connection():
    """
    Borrows a connection from the pool, replacing any that have gone
    stale (see module docstring). cursor_factory=RealDictCursor makes
    rows behave like dictionaries (row["column_name"]), matching how
    the code was already written against sqlite3.Row.

    Tries a bounded number of times -- after Neon wakes from sleep,
    every pooled connection can be dead at once, and each is discarded
    in turn until a fresh one is opened. If the database is genuinely
    unreachable, opening that fresh connection raises, which is the
    correct, loud failure.
    """
    p = _get_pool()
    for _ in range(p.maxconn + 1):
        real_conn = p.getconn()
        if _is_alive(real_conn):
            return _PooledConnection(real_conn, p)
        _discard(p, real_conn)
    real_conn = p.getconn()
    return _PooledConnection(real_conn, p)
