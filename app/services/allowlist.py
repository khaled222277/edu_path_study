"""Who may sign in during the pilot (ALLOWLIST_ENABLED)."""

import hashlib

from sqlalchemy.orm import Session

from app.config import settings
from app.models.allowed_email import AllowedEmail


def normalize(email: str | None) -> str:
    return (email or "").strip().lower()


def looks_like_email(email: str) -> bool:
    local, _, domain = email.partition("@")
    return bool(local) and "." in domain and " " not in email and email.count("@") == 1


def is_allowed(db: Session, email: str | None) -> bool:
    """True if the allowlist is off, or `email` (case-insensitively) is on it."""
    if not settings.allowlist_enabled:
        return True
    address = normalize(email)
    return bool(address) and db.get(AllowedEmail, address) is not None


def log_ref(email: str | None) -> str:
    """A short hash of the address, for logs: lets the same account be recognised without
    writing the address itself. Not reversible by reading the log."""
    return hashlib.sha256(normalize(email).encode("utf-8")).hexdigest()[:10]
