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
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor


def get_connection():
    """
    Opens and returns a connection to the PostgreSQL database.
    cursor_factory=RealDictCursor makes rows behave like dictionaries
    (row["column_name"]), matching how the code was already written
    against sqlite3.Row.

    DATABASE_URL is read here (not at module import time) so it
    reflects whatever load_dotenv() has already loaded by the time
    a connection is actually requested.
    """
    database_url = os.getenv("DATABASE_URL")
    conn = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    return conn
