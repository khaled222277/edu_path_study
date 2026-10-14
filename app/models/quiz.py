from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.subject import Subject
    from app.models.user import User


class QuizAttempt(Base):
    """One quiz a student sat for one subject."""

    __tablename__ = "quiz_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_questions: Mapped[int] = mapped_column(Integer, nullable=False)
    # Which language the questions were generated in; needed to review an old
    # attempt correctly and to break down results by language later.
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship()
    subject: Mapped["Subject"] = relationship()
    items: Mapped[list["QuizItem"]] = relationship(
        back_populates="attempt",
        cascade="all, delete-orphan",
        order_by="QuizItem.id",
    )

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None

    def __repr__(self) -> str:
        return f"<QuizAttempt id={self.id} subject={self.subject_id} score={self.score}>"


class QuizItem(Base):
    __tablename__ = "quiz_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("quiz_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    options_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    correct_answer_index: Mapped[int] = mapped_column(Integer, nullable=False)

    # Null until the student answers.
    chosen_answer_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # "file.pdf, page 12" -- the chunk this question was grounded in.
    source_citation: Mapped[str] = mapped_column(String(512), nullable=False)

    attempt: Mapped["QuizAttempt"] = relationship(back_populates="items")

    @property
    def options(self) -> list[Any]:
        """Alias so response schemas can read `options` off the ORM object."""
        return self.options_json

    @property
    def is_answered(self) -> bool:
        return self.chosen_answer_index is not None

    def __repr__(self) -> str:
        return f"<QuizItem id={self.id} attempt={self.attempt_id}>"
