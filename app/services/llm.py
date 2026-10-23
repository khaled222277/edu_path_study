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


def keep_alive() -> float | str:
    """OLLAMA_KEEP_ALIVE in the form Ollama's API accepts.

    Plain numbers must be sent as numbers: the server parses the *string* "-1"
    as a duration and rejects it for lacking a unit, while the number -1 means
    "forever". Anything else ("30m") is a duration string and goes through as is.
    """
    value = settings.ollama_keep_alive
    try:
        return float(value)
    except ValueError:
        return value


def warm_chat_model(model: str | None = None) -> None:
    """Make the chat model resident now, so the first student does not wait for it.

    A one-token request: it exists only to load the model and start the
    keep-alive clock. It deliberately sets no context-size option, because a
    different num_ctx from the real requests would make Ollama reload the model.
    """
    get_client().chat(
        model=model or settings.chat_model,
        messages=[{"role": "user", "content": "ok"}],
        options={"num_predict": 1},
        keep_alive=keep_alive(),
    )


class LLMUnavailableError(Exception):
    """Ollama could not serve the request. Never carries user-facing detail."""


@lru_cache(maxsize=4)
def get_client(timeout: float | None = None) -> ollama.Client:
    """Ollama client with a bounded timeout (seconds)."""
    return ollama.Client(
        host=settings.ollama_base_url or None,
        timeout=timeout if timeout is not None else settings.ollama_timeout_seconds,
    )
