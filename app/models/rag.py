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

  - rag_queries: one row per question the companion handled -- the
    evaluation record for the proposal's three RAG metrics. `retrieved`
    (every chunk passed to the model, with its similarity) supports
    retrieval relevance; `answer` + `cited` support groundedness and
    citation accuracy; `invalid_citations` counts passage numbers the
    model gave that didn't exist (caught in code, never shown).
    `outcome` is one of answered / not_covered / secondary_issue /
    escalated / error. `tokens_used` backs the budget claims with real
    numbers rather than estimates.
"""

import json

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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rag_queries (
            id SERIAL PRIMARY KEY,
            reg_number TEXT,
            question TEXT NOT NULL,
            kind TEXT NOT NULL,
            severity TEXT,
            retrieved JSONB,
            outcome TEXT NOT NULL,
            answer TEXT,
            cited JSONB,
            invalid_citations INTEGER DEFAULT 0,
            tokens_used INTEGER,
            created_at TIMESTAMP
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


def asked_companion_recently(reg_number, minutes):
    """Has this member asked the companion something (general or pastoral) in the last `minutes`?"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""SELECT 1 FROM rag_queries WHERE reg_number = %s AND kind IN ('general', 'pastoral')
                      AND created_at >= (NOW() AT TIME ZONE 'UTC') - (%s * INTERVAL '1 minute') LIMIT 1""",
                   (reg_number, minutes))
    found = cursor.fetchone() is not None
    cursor.close()
    conn.close()
    return found


def log_query(reg_number, question, kind, severity, retrieved, outcome, answer, cited,
              invalid_citations, tokens_used, created_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO rag_queries (reg_number, question, kind, severity, retrieved, outcome,
                                 answer, cited, invalid_citations, tokens_used, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (reg_number, question, kind, severity, json.dumps(retrieved), outcome,
          answer, json.dumps(cited), invalid_citations, tokens_used, created_at))
    conn.commit()
    cursor.close()
    conn.close()


def chunks_with_citation_prefix(prefix):
    """Every chunk whose citation starts with `prefix`, in document order -- e.g. all of Art. 11 for small-to-big retrieval."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.id, c.citation, c.content, d.title, d.tier, NULL::float AS similarity
        FROM rag_chunks c
        JOIN rag_documents d ON d.id = c.document_id
        WHERE c.citation LIKE %s
        ORDER BY c.id
    """, (prefix.replace("%", r"\%").replace("_", r"\_") + "%",))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


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
