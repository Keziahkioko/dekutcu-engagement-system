"""
app/models/rag.py

Stage 12 (RAG Companion): storage for the corpus the bot answers from.

  - rag_documents: one row per source document, with its TIER --
    'constitution' (DeKUTCU's own voice, the sole authority) or
    'supporting' (a vetted outside source, always labelled as such in
    answers). See PROJECT_LOG.md, Stage 12 decision 1.
  - rag_chunks: the passages retrieval actually searches. Each keeps a
    human-readable citation ("DeKUTCU Constitution, Art. 11(F)") and
    its embedding -- 1,024 numbers from Cloudflare's bge-large-en-v1.5
    (decision 2), stored with pgvector so "find the chunks nearest in
    meaning to this question" runs inside the existing Neon database.

No vector index (HNSW/IVFFlat): with ~100 chunks an exact scan of every
row is instant and exactly correct, while an approximate index would
trade exactness for speed this corpus size doesn't need. Worth adding
only if the corpus grows into the thousands.
"""

from app.database import get_connection

EMBEDDING_DIMENSIONS = 1024  # bge-large-en-v1.5


def init_rag_tables():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rag_documents (
            id SERIAL PRIMARY KEY,
            title TEXT UNIQUE NOT NULL,
            tier TEXT NOT NULL CHECK (tier IN ('constitution', 'supporting')),
            source TEXT,
            added_at TIMESTAMP
        )
    """)
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS rag_chunks (
            id SERIAL PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES rag_documents(id) ON DELETE CASCADE,
            citation TEXT NOT NULL,
            content TEXT NOT NULL,
            embedding vector({EMBEDDING_DIMENSIONS}) NOT NULL
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def _as_vector(values):
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def replace_document(title, tier, source, added_at, chunks):
    """
    Loads (or re-loads) one document atomically: deletes any previous
    version of it -- its chunks go with it via ON DELETE CASCADE -- and
    inserts the new chunks, all in one transaction, so a re-run of the
    ingestion script never leaves a half-old, half-new document behind.
    `chunks` is a list of (citation, content, embedding) tuples.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM rag_documents WHERE title = %s", (title,))
        cursor.execute("""
            INSERT INTO rag_documents (title, tier, source, added_at)
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (title, tier, source, added_at))
        document_id = cursor.fetchone()["id"]
        for citation, content, embedding in chunks:
            cursor.execute("""
                INSERT INTO rag_chunks (document_id, citation, content, embedding)
                VALUES (%s, %s, %s, %s::vector)
            """, (document_id, citation, content, _as_vector(embedding)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
    return document_id


def nearest_chunks(query_embedding, limit):
    """
    The chunks closest in meaning to the question, closest first.
    similarity is cosine similarity (1.0 = identical meaning) -- pgvector's
    <=> operator gives cosine DISTANCE, so similarity = 1 - distance.
    """
    vector = _as_vector(query_embedding)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.id, c.citation, c.content, d.title, d.tier,
               1 - (c.embedding <=> %s::vector) AS similarity
        FROM rag_chunks c
        JOIN rag_documents d ON d.id = c.document_id
        ORDER BY c.embedding <=> %s::vector
        LIMIT %s
    """, (vector, vector, limit))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows
