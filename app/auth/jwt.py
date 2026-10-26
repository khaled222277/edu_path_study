"""Issue and verify the session JWT."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.config import settings


def create_access_token(user_id: int) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "jti": uuid.uuid4().hex,  # lets logout revoke this one token
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def token_claims(token: str) -> dict | None:
    """The verified claims of `token`, or None if it is invalid/expired."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
        int(payload["sub"])
        return payload
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        return None


def decode_access_token(token: str) -> int | None:
    """Return the user id encoded in `token`, or None if it is invalid/expired."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            # A token without an expiry would otherwise be valid forever.
            options={"require": ["exp", "sub"]},
        )
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        return None
