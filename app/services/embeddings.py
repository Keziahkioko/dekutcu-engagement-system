"""
app/services/embeddings.py

Stage 12: turns text into embeddings -- 1,024 numbers per text, placed
so that texts with similar MEANING get similar numbers -- via Cloudflare
Workers AI's hosted bge-large-en-v1.5. See PROJECT_LOG.md, Stage 12
decision 2, for why hosted (isolation from the safety-critical features,
a bigger model than the free Render server can hold) and why this model
(4/4 vs 3/4 on the demo questions, including "How do I get to heaven?").

Two details the earlier test settled, kept exact here because vectors
from different settings aren't comparable with each other:
  - mean pooling (clearer separation than 'cls' in the test);
  - BGE's own instruction prefix on QUESTIONS only, never on passages --
    the model was trained that way for short-query-vs-passage search.

Every chunk and every question MUST go through this same model with
these same settings -- switching either means re-embedding the corpus.
"""

import os
import requests

MODEL = "@cf/baai/bge-large-en-v1.5"
_POOLING = "mean"
_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
_BATCH_SIZE = 50  # well within Cloudflare's per-request limit
_TIMEOUT_SECONDS = 60


def _embed(texts):
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    if not account_id or not token:
        raise RuntimeError("CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN are not set")

    response = requests.post(
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{MODEL}",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": texts, "pooling": _POOLING},
        timeout=_TIMEOUT_SECONDS,
    )
    body = response.json()
    if response.status_code != 200 or not body.get("success"):
        raise RuntimeError(f"Cloudflare embedding failed (HTTP {response.status_code}): {body.get('errors')}")
    return body["result"]["data"]


def embed_passages(texts):
    """For corpus chunks -- no prefix. Batched, so a whole document loads in a few calls."""
    vectors = []
    for start in range(0, len(texts), _BATCH_SIZE):
        vectors.extend(_embed(texts[start:start + _BATCH_SIZE]))
    return vectors


def embed_query(text):
    """For a member's question -- with BGE's query prefix."""
    return _embed([_QUERY_PREFIX + text])[0]
