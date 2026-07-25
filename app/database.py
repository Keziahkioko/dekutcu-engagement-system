"""
app/database.py

Handles the SQLite database connection.
Any file inside the app package that needs to talk to the database
imports get_connection from here, e.g.:

    from app.database import get_connection
"""

import sqlite3

DB_NAME = "engagement_system.db"


def get_connection():
    """
    Opens and returns a connection to the SQLite database.
    check_same_thread=False allows the connection to be used across
    Flask's request-handling threads.
    """
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # lets us access columns by name, e.g. row["name"]
    return conn
