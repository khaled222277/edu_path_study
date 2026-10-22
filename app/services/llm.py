"""One place that builds Ollama clients, so every call has an explicit timeout.

The module-level `ollama.chat` / `ollama.generate` helpers use a client with no
timeout at all: a hung Ollama would pin a request thread forever. Everything
goes through get_client() instead.
"""

from functools import lru_cache

import httpx
import ollama

from app.config import settings

# What "Ollama is down, hung or misbehaving" looks like from the client:
# ConnectionError (refused), httpx timeouts / transport errors, and
# ResponseError / RequestError (model missing, 5xx, bad request).
LLM_ERRORS = (
    ConnectionError,
    httpx.TransportError,
    ollama.ResponseError,
    ollama.RequestError,
)


class LLMUnavailableError(Exception):
    """Ollama could not serve the request. Never carries user-facing detail."""


@lru_cache(maxsize=4)
def get_client(timeout: float | None = None) -> ollama.Client:
    """Ollama client with a bounded timeout (seconds)."""
    return ollama.Client(
        timeout=timeout if timeout is not None else settings.ollama_timeout_seconds
    )
