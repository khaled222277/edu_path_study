"""Keep credentials out of the access log.

uvicorn logs every request line, query string included. The Google sign-in callback is
`/auth/google/callback?code=...&state=...`: a one-time authorization code that should not
sit in a log file, even though it is short-lived. Cookies and headers are never logged.
"""

import logging
import re

_SECRET_PARAM = re.compile(r"([?&](?:code|state|token|access_token|id_token|refresh_token|key)=)[^&\s]*", re.IGNORECASE)


def redact(text: str) -> str:
    return _SECRET_PARAM.sub(r"\1[redacted]", text)


class RedactQueryFilter(logging.Filter):
    """On uvicorn.access records, blank the value of credential-looking query parameters."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        return True


def install() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactQueryFilter) for f in logger.filters):
        logger.addFilter(RedactQueryFilter())
