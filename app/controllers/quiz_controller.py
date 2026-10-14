"""Quiz lifecycle: start, answer, complete, history."""

import logging
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.controllers.chat_controller import assert_subject_in_scope
from app.models.quiz import QuizAttempt, QuizItem
from app.models.user import User
from app.services.quiz_service import (
    DEFAULT_QUESTION_COUNT,
    LANG_EN,
    detect_language,
    generate_quiz,
)

log = logging.getLogger(__name__)


def get_attempt_or_404(db: Session, attempt_id: int, user: User) -> QuizAttempt:
    attempt = db.get(QuizAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Quiz attempt not found"
        )
    if attempt.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This quiz belongs to another student",
        )
    return attempt


def start_quiz(
    db: Session,
    user: User,
    subject_id: int,
    question_count: int = DEFAULT_QUESTION_COUNT,
    language: str | None = None,
    prompt: str | None = None,
) -> QuizAttempt:
    """Generate a quiz and persist it. Access is scoped like chat."""
    assert_subject_in_scope(db, subject_id, user)

    resolved = language or (detect_language(prompt) if prompt else LANG_EN)

    try:
        questions = generate_quiz(subject_id, question_count, resolved)
    except NotImplementedError:
        raise
    except Exception as exc:
        log.exception("quiz generation failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not generate a quiz right now: {exc}",
        ) from exc

    if not questions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Not enough lecture material in this subject to build a quiz",
        )

    attempt = QuizAttempt(
        user_id=user.id,
        subject_id=subject_id,
        score=0,
        total_questions=len(questions),
        language=resolved,
    )
    db.add(attempt)
    db.flush()

    for question in questions:
        db.add(
            QuizItem(
                attempt_id=attempt.id,
                question_text=question.question_text,
                options_json=question.options,
                correct_answer_index=question.correct_answer_index,
                source_citation=question.source.citation,
            )
        )

    db.commit()
    db.refresh(attempt)
    return attempt


def submit_answer(
    db: Session, attempt: QuizAttempt, item_id: int, chosen_index: int
) -> tuple[QuizItem, int]:
    """Record an answer. Returns (item, answered_count)."""
    if attempt.is_complete:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This quiz is already complete",
        )

    item = db.get(QuizItem, item_id)
    if item is None or item.attempt_id != attempt.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Question not found in this quiz",
        )
    if item.is_answered:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This question has already been answered",
        )
    if not 0 <= chosen_index < len(item.options_json):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Answer index is outside this question's options",
        )

    item.chosen_answer_index = chosen_index
    item.is_correct = chosen_index == item.correct_answer_index
    if item.is_correct:
        attempt.score += 1

    db.flush()
    answered = sum(1 for i in attempt.items if i.chosen_answer_index is not None)

    # Auto-complete on the last answer so the student never has to call complete.
    if answered == attempt.total_questions and attempt.completed_at is None:
        attempt.completed_at = datetime.now(UTC)

    db.commit()
    db.refresh(item)
    db.refresh(attempt)
    return item, answered


def complete_quiz(db: Session, attempt: QuizAttempt) -> QuizAttempt:
    """Close an attempt early; unanswered questions simply stay unanswered."""
    if attempt.completed_at is None:
        attempt.completed_at = datetime.now(UTC)
        db.commit()
        db.refresh(attempt)
    return attempt


def list_history(db: Session, user: User) -> list[QuizAttempt]:
    return list(
        db.scalars(
            select(QuizAttempt)
            .where(QuizAttempt.user_id == user.id)
            .order_by(QuizAttempt.created_at.desc(), QuizAttempt.id.desc())
        )
    )
