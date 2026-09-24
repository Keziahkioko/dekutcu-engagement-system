"""
app/services/llm_client.py

Shared LLM-calling logic for classify_intent (intent_router.py) and
answer_group_question (group_query.py): a short retry on rate-limit
errors, then a fallback to a second provider (Cerebras) if Groq is
still unavailable after that.

Groq is primary -- it's the model these prompts were actually tuned
against. Cerebras is OpenAI-compatible (same client shape, just a
different base_url/model/key), so it's a cheap second option to wire
in, and its free tier has far more per-minute headroom than Groq's.
It's used only as a genuine fallback, not load-balanced against Groq
by default -- a different underlying model can behave differently on
the same prompt, so it's worth treating with a little more caution
than "just another interchangeable worker."

Callers pass everything EXCEPT which client/model to use (messages,
and optionally tools/tool_choice/temperature/response_format) --
provider selection is entirely this module's job, so classify_intent
and group_query don't need to know Cerebras exists at all.
"""

import os
import time
from groq import Groq, RateLimitError as GroqRateLimitError
from openai import OpenAI, RateLimitError as OpenAIRateLimitError

_MAX_RETRIES = 2
_BASE_DELAY_SECONDS = 0.5

GROQ_MODEL = "openai/gpt-oss-20b"
# Same model family as GROQ_MODEL (just a larger variant) -- picked
# deliberately over Cerebras's other listed option (qwen-3.8-27b)
# since our prompts were tuned against gpt-oss's specific behavior;
# a same-family fallback is the safer bet for staying consistent.
#
# KNOWN ISSUE (2026-09-24): confirmed via a live models.list() call
# that this model NAME is correct -- Cerebras's own docs/search
# results at the time referenced llama3.3-70b, which 404'd. But a
# real chat.completions.create() call against it (and against
# qwen-3.8-27b) both returned 402 Payment Required on the account
# this was tested with. Check the Cerebras dashboard's billing tab --
# this fallback will not actually work until that's resolved, though
# it fails safely (falls through to the caller's existing "unclear"/
# apology handling) rather than crashing anything.
CEREBRAS_MODEL = "gpt-oss-120b"
CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"

_RATE_LIMIT_ERRORS = (GroqRateLimitError, OpenAIRateLimitError)


def _call_with_retry(client, **kwargs):
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except _RATE_LIMIT_ERRORS:
            if attempt == _MAX_RETRIES:
                raise
            time.sleep(_BASE_DELAY_SECONDS * (2 ** attempt))


def create_chat_completion(**kwargs):
    groq_key = os.getenv("GROQ_API_KEY")
    groq_error = None

    if groq_key:
        try:
            return _call_with_retry(Groq(api_key=groq_key), model=GROQ_MODEL, **kwargs)
        except _RATE_LIMIT_ERRORS as e:
            groq_error = e
            print(f"Groq unavailable after retries, falling back to Cerebras: {e}")

    cerebras_key = os.getenv("CEREBRAS_API_KEY")
    if cerebras_key:
        cerebras_client = OpenAI(api_key=cerebras_key, base_url=CEREBRAS_BASE_URL)
        return _call_with_retry(cerebras_client, model=CEREBRAS_MODEL, **kwargs)

    if groq_error:
        raise groq_error
    raise RuntimeError("No LLM provider available -- GROQ_API_KEY is not set")
