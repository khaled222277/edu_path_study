"""Log a failure without logging what the failure was about.

An exception's message can carry the thing being processed: a pydantic ValidationError prints the
offending input, a JSON error quotes the model's reply, an HTTP error from Ollama may echo part
of a request. In this app that can be a student's question, the prompt, or lecture text, and a
traceback prints all of it. So by default a failure is logged as

    <what was being done> [ExceptionClass (code)] at file.py:123 in func <- file.py:45 in caller

: the class, a short code where one exists (HTTP status, timeout), and where it happened. That is
enough to find the bug from the source; it never contains the message. For a developer who needs
the full traceback on their own machine, LOG_TRACEBACKS=true switches it back (do not leave it on
during the pilot).
"""

import logging
import traceback
from pathlib import Path

from app.config import settings

MAX_FRAMES = 3


def describe(exc: BaseException) -> str:
    """ExceptionClass, plus a short code (HTTP status, timeout) when the exception has one."""
    name = type(exc).__name__
    code = getattr(exc, "status_code", None)
    if code is None:
        response = getattr(exc, "response", None)
        code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return f"{name} ({code})"
    if "timeout" in name.lower() or isinstance(exc, TimeoutError):
        return f"{name} (timeout)"
    return name


def where(exc: BaseException) -> str:
    """The innermost few frames as 'file.py:LINE in function', without source lines or values."""
    frames = traceback.extract_tb(exc.__traceback__)[-MAX_FRAMES:]
    if not frames:
        return "unknown location"
    return " <- ".join(f"{Path(f.filename).name}:{f.lineno} in {f.name}" for f in reversed(frames))


def failure(logger: logging.Logger, message: str, exc: BaseException, level: int = logging.ERROR) -> None:
    """Log `message` (a fixed string, never built from user data) and what went wrong, safely."""
    if settings.log_tracebacks:
        logger.log(level, "%s", message, exc_info=exc)
        return
    logger.log(level, "%s [%s] at %s", message, describe(exc), where(exc))
