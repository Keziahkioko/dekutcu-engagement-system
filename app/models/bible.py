"""
app/models/bible.py

The Berean Standard Bible (public domain since 30 April 2023 -- berean.bible/terms.htm), one row per verse,
for LOOKING UP exact passages when a member names a reference ("Explain Romans 12:2"). Meaning-search over
the Bible uses rag_chunks (tier 'bible') instead -- see scripts/ingest_bible.py. Added 2026-10-12 (Keziah).
"""

from psycopg2.extras import execute_values

from app.database import get_connection


def init_bible_table():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bible_verses (
            book TEXT NOT NULL,
            chapter INTEGER NOT NULL,
            verse INTEGER NOT NULL,
            text TEXT NOT NULL,
            PRIMARY KEY (book, chapter, verse)
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def replace_all_verses(rows):
    """rows: [(book, chapter, verse, text), ...] -- replaces the whole Bible in one transaction."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM bible_verses")
        execute_values(cursor, "INSERT INTO bible_verses (book, chapter, verse, text) VALUES %s", rows, page_size=2000)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def get_verses(book, chapter, first=None, last=None):
    """Verses of one chapter, in order -- all of it, or first..last."""
    conn = get_connection()
    cursor = conn.cursor()
    if first is None:
        cursor.execute("SELECT verse, text FROM bible_verses WHERE book = %s AND chapter = %s ORDER BY verse",
                       (book, chapter))
    else:
        cursor.execute("""SELECT verse, text FROM bible_verses WHERE book = %s AND chapter = %s
                          AND verse BETWEEN %s AND %s ORDER BY verse""", (book, chapter, first, last))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows
