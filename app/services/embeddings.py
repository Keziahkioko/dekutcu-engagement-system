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


# Cloudflare's free allowance is 10,000 "neurons" per UTC day, shared with EVERY live companion question.
# Bulk loading used all of it twice (2026-10-12 and 2026-10-15) and live questions failed until the reset.
# So bulk embedding keeps a ledger (on the machine running the loaders) and stops at 40% of the day's allowance,
# always leaving the rest for the live bot. bge-large costs about 18,600 neurons per million tokens;
# tokens are estimated as words x 2 -- deliberately high: the first estimate (x 1.4) put 2026-10-15's
# loads at ~4,500 neurons, yet the whole 10,000 ran out (old English splits into more tokens per word).
_DAILY_NEURONS = 10_000
_BULK_SHARE = 0.4
_NEURONS_PER_TOKEN = 18_582 / 1_000_000
_LEDGER = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                       "corpus", "embedding_ledger.json")


class DailyBudgetReached(RuntimeError):
    """Bulk embedding stopped to leave Cloudflare's allowance for the live bot -- carry on after midnight UTC."""


def _estimate_neurons(texts):
    return sum(len(t.split()) for t in texts) * 2 * _NEURONS_PER_TOKEN


def _ledger_used_today():
    import json
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        ledger = json.load(open(_LEDGER, encoding="utf-8"))
    except (OSError, ValueError):
        ledger = {}
    return today, ledger, ledger.get(today, 0.0)


def _ledger_add(neurons):
    import json
    today, ledger, used = _ledger_used_today()
    ledger = {day: n for day, n in ledger.items() if day == today}   # only today matters
    ledger[today] = used + neurons
    with open(_LEDGER, "w", encoding="utf-8") as out:
        json.dump(ledger, out)


def bulk_budget_left():
    """Neurons bulk loading may still use today (UTC)."""
    _today, _ledger, used = _ledger_used_today()
    return max(0.0, _DAILY_NEURONS * _BULK_SHARE - used)


def embed_passages(texts):
    """
    For corpus chunks -- no prefix. Batched, so a whole document loads in a few calls. Refuses (DailyBudgetReached)
    BEFORE starting if the whole job would take bulk loading past half of today's allowance.
    """
    needed = _estimate_neurons(texts)
    left = bulk_budget_left()
    if needed > left:
        raise DailyBudgetReached(
            f"This needs about {needed:.0f} of Cloudflare's daily neurons, but bulk loading may only use "
            f"{left:.0f} more today (most of the allowance is kept for the live bot). Run it after midnight UTC "
            "(3am in Kenya), or split it up.")
    vectors = []
    for start in range(0, len(texts), _BATCH_SIZE):
        batch = texts[start:start + _BATCH_SIZE]
        vectors.extend(_embed(batch))
        _ledger_add(_estimate_neurons(batch))
    return vectors


def embed_query(text):
    """For a member's question -- with BGE's query prefix."""
    return _embed([_QUERY_PREFIX + text])[0]
