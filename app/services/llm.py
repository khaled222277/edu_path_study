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


@lru_cache(maxsize=2)
def get_async_client(timeout: float | None = None) -> ollama.AsyncClient:
    """Async twin of get_client(), for streaming without holding a worker thread.

    The timeout is httpx's per-read timeout, so for a stream it bounds a stall
    between tokens rather than the length of the whole answer.
    """
    return ollama.AsyncClient(
        host=settings.ollama_base_url or None,
        timeout=timeout if timeout is not None else settings.ollama_timeout_seconds,
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


# Ollama stops a generation that has fallen into a loop and answers HTTP 500 with
# {"error": "prediction aborted, token repeat limit reached"}. The server is fine, only that one
# generation is lost, so it is NOT an outage: asking again (the sampling is not deterministic)
# normally works. The signal is deliberately narrow -- status AND message -- because any other 500
# ("model runner crashed", out of memory...) may mean the server really is in trouble.
REPEAT_LIMIT_STATUS = 500
REPEAT_LIMIT_MESSAGE = "token repeat limit"


def is_repeat_limit(exc: BaseException) -> bool:
    """True for Ollama's "token repeat limit reached" abort of a single generation."""
    return (
        isinstance(exc, ollama.ResponseError)
        and exc.status_code == REPEAT_LIMIT_STATUS
        and REPEAT_LIMIT_MESSAGE in str(exc.error).lower()
    )


class ModelRepeatLimitError(Exception):
    """One generation was aborted by Ollama's repeat limit; the next attempt may succeed.

    Carries no message (the cause is chained). `status_code` is there so log_safe prints it as the code.
    """

    status_code = REPEAT_LIMIT_STATUS


@lru_cache(maxsize=4)
def get_client(timeout: float | None = None) -> ollama.Client:
    """Ollama client with a bounded timeout (seconds)."""
    return ollama.Client(
        host=settings.ollama_base_url or None,
        timeout=timeout if timeout is not None else settings.ollama_timeout_seconds,
    )
