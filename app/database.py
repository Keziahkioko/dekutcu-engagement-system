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
"""

import os
import threading
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

_pool = None
_pool_lock = threading.Lock()


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
        self._pool.putconn(self._conn)


def get_connection():
    """
    Borrows a connection from the pool. cursor_factory=RealDictCursor
    makes rows behave like dictionaries (row["column_name"]), matching
    how the code was already written against sqlite3.Row.
    """
    p = _get_pool()
    real_conn = p.getconn()
    return _PooledConnection(real_conn, p)
