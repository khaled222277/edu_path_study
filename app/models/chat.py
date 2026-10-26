from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.feedback import MessageFeedback
    from app.models.subject import Subject
    from app.models.user import User

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


class ChatSession(Base):
    """One student's conversation about one subject."""

    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship()
    subject: Mapped["Subject"] = relationship()
    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.id",
    )

    def __repr__(self) -> str:
        return f"<ChatSession id={self.id} user={self.user_id} subject={self.subject_id}>"


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # The sources shown under an assistant reply, exactly as the "done" event returned them
    # (validated against what was retrieved; empty for a refusal). NULL on user messages
    # and on replies stored before this column existed.
    sources: Mapped[list | None] = mapped_column(JSON, nullable=True)

    session: Mapped["ChatSession"] = relationship(back_populates="messages")
    feedback_rows: Mapped[list["MessageFeedback"]] = relationship(
        cascade="all, delete-orphan"
    )

    @property
    def feedback(self) -> str | None:
        """"up" / "down" if the student rated this reply, else None."""
        if not self.feedback_rows:
            return None
        return "up" if self.feedback_rows[0].rating > 0 else "down"

    @property
    def citations(self) -> list:
        return self.sources or []

    def __repr__(self) -> str:
        return f"<ChatMessage id={self.id} role={self.role!r}>"
