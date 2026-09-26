"""
app/services/llm_client.py

Shared LLM-calling logic for classify_intent (intent_router.py) and
answer_group_question (group_query.py): a short retry on rate-limit
errors, then a fallback to a SECOND provider if Groq is still
unavailable after that.

Groq is primary -- it's the model these prompts were actually tuned
against. The fallback slot is deliberately GENERIC rather than tied to
one named provider: as of 2026-09-24, eight free-tier alternatives
were tried (Cerebras, Gemini, OpenRouter, GitHub Models, Cohere,
DeepSeek, Mistral) and every single one required billing, hit an
account-level restriction, or had a real quality/latency problem --
see PROJECT_LOG.md for the full account. Rather than hardcode whichever
one *might* eventually work, this fallback is configured entirely
through three environment variables (see below) -- so if a working
free-tier provider ever turns up, it can be wired in by setting env
vars alone, no code changes needed, as long as it exposes an
OpenAI-compatible chat completions endpoint (most providers do; that's
what let Cerebras/OpenRouter/DeepSeek/Mistral all slot in identically
during testing, just by pointing the same `openai` client at a
different base_url).

To activate the fallback, set ALL THREE of:
    FALLBACK_API_KEY       -- the provider's API key
    FALLBACK_BASE_URL      -- e.g. "https://api.cerebras.ai/v1"
    FALLBACK_MODEL         -- e.g. "gpt-oss-120b"
Leave any of them unset and the fallback is skipped entirely -- Groq's
own error propagates to the caller's existing safe fallback handling,
exactly as if no second provider were configured at all.

Callers pass everything EXCEPT which client/model to use (messages,
and optionally tools/tool_choice/temperature/response_format) --
provider selection is entirely this module's job.
"""

import os
import time
from groq import Groq, RateLimitError as GroqRateLimitError
from openai import OpenAI, RateLimitError as OpenAIRateLimitError

_MAX_RETRIES = 2
_BASE_DELAY_SECONDS = 0.5

GROQ_MODEL = "openai/gpt-oss-20b"

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
            print(f"Groq unavailable after retries, checking fallback provider: {e}")

    fallback_key = os.getenv("FALLBACK_API_KEY")
    fallback_base_url = os.getenv("FALLBACK_BASE_URL")
    fallback_model = os.getenv("FALLBACK_MODEL")
    if fallback_key and fallback_base_url and fallback_model:
        fallback_client = OpenAI(api_key=fallback_key, base_url=fallback_base_url)
        return _call_with_retry(fallback_client, model=fallback_model, **kwargs)

    if groq_error:
        raise groq_error
    raise RuntimeError("No LLM provider available -- GROQ_API_KEY is not set")
