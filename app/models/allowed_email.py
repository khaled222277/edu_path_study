from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AllowedEmail(Base):
    """An email address allowed to sign in while ALLOWLIST_ENABLED is on (stored lower-case)."""

    __tablename__ = "allowed_emails"

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
