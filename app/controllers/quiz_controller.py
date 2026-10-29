"""Quiz lifecycle: start, answer, complete, history."""

import logging
import threading
from app import log_safe
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.controllers.chat_controller import assert_subject_in_scope
from app.db.session import session_scope
from app.models.quiz import QuizAttempt, QuizItem
from app.models.user import User
from app.services.llm import LLMUnavailableError
from app.services.quiz_service import (
    DEFAULT_QUESTION_COUNT,
    LANG_EN,
    GeneratedQuestion,
    QuizGenerationError,
    detect_language,
    generate_quiz,
    iter_quiz,
)

log = logging.getLogger(__name__)

# Generic on purpose: the cause is logged, never echoed to the client.
QUIZ_UNAVAILABLE_DETAIL = (
    "Could not generate a quiz right now. Please try again in a moment."
)


def get_attempt_or_404(
    db: Session, attempt_id: int, user: User, *, lock: bool = False
) -> QuizAttempt:
    """Fetch an attempt; 404 if it does not exist OR is someone else's (same answer for both).

    `lock` takes a row lock (SELECT ... FOR UPDATE) that is held until the request
    commits. Answering and completing use it so they cannot interleave with a quiz
    that is still being generated (see finish_streamed_quiz).
    """
    attempt = db.get(QuizAttempt, attempt_id, with_for_update=lock)
    if attempt is None or attempt.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Quiz attempt not found"
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
    except (LLMUnavailableError, QuizGenerationError) as exc:
        log_safe.failure(log, "quiz generation failed", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=QUIZ_UNAVAILABLE_DETAIL,
        ) from exc
    except Exception as exc:  # e.g. Chroma unreadable
        log_safe.failure(log, "quiz generation failed", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=QUIZ_UNAVAILABLE_DETAIL,
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
    """Close an attempt early; unanswered questions simply stay unanswered.

    A quiz that was still being written has fewer stored questions than its total
    (which then holds the number requested). Closing it settles the total to what
    exists, so a result is never reported out of questions that were never made.
    For an ordinary quiz the two are equal and nothing changes.
    """
    if attempt.completed_at is None:
        attempt.completed_at = datetime.now(UTC)
        stored = len(attempt.items)
        if 0 < stored < attempt.total_questions:
            attempt.total_questions = stored
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


# --- progressive delivery -------------------------------------------------------
#
# A streamed quiz is stored while it is being written: the attempt row is created
# as soon as the first question exists, and each later question is added the moment
# it validates, so the student can answer question 1 while question 4 is generated.
# (Answers are checked server-side and the correct index is withheld, so a question
# has to exist in the database before it can be answered.)
#
# While generation runs, total_questions holds the number *requested*. That keeps
# the "all questions answered" auto-complete from firing early, since there are
# always fewer stored items than that. finish_streamed_quiz() then sets it to the
# number actually produced.


def question_payload(item: QuizItem) -> dict:
    """A question as the student sees it -- deliberately no correct answer."""
    return {
        "id": item.id,
        "question_text": item.question_text,
        "options": item.options_json,
        "source_citation": item.source_citation,
    }


def _item_row(attempt_id: int, question: GeneratedQuestion) -> QuizItem:
    return QuizItem(
        attempt_id=attempt_id,
        question_text=question.question_text,
        options_json=question.options,
        correct_answer_index=question.correct_answer_index,
        source_citation=question.source.citation,
    )


@dataclass
class StreamedQuiz:
    """A quiz whose first question is stored and whose others are still coming."""

    attempt_id: int
    subject_id: int
    language: str
    requested: int
    first: dict  # question_payload() of the first stored question
    questions: Iterator[GeneratedQuestion]
    cancel: threading.Event
    # Set once the client has actually been sent something. Until then nobody has
    # seen this quiz, so closing it discards it instead of keeping a stub.
    delivered: bool = False
    _closed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)
    produced: int = 1

    def close(self) -> tuple[int, bool] | None:
        """Stop generating and settle the stored attempt. Safe to call repeatedly.

        Returns (questions_kept, partial) the first time, None afterwards.
        """
        with self._lock:
            if self._closed:
                return None
            self._closed = True
        self.cancel.set()
        try:
            self.questions.close()  # runs iter_quiz's own cleanup
        except ValueError:
            pass  # a worker thread is still inside it; it stops at the cancel flag
        if not self.delivered:
            discard_streamed_quiz(self.attempt_id)
            return 0, True
        return finish_streamed_quiz(self.attempt_id, self.requested)


def begin_streamed_quiz(
    db: Session,
    user: User,
    subject_id: int,
    question_count: int = DEFAULT_QUESTION_COUNT,
    language: str | None = None,
    prompt: str | None = None,
) -> StreamedQuiz:
    """Check access, generate and store the first question, and hand back the rest.

    Blocks until one question exists. Failures before that point are ordinary HTTP
    errors -- the same ones start_quiz gives -- because no response has started.
    """
    assert_subject_in_scope(db, subject_id, user)
    resolved = language or (detect_language(prompt) if prompt else LANG_EN)
    cancel = threading.Event()

    try:
        questions = iter_quiz(subject_id, question_count, resolved, cancel=cancel)
        first = next(questions, None)
    except NotImplementedError:
        raise
    except (LLMUnavailableError, QuizGenerationError) as exc:
        log_safe.failure(log, "quiz generation failed", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=QUIZ_UNAVAILABLE_DETAIL
        ) from exc
    except Exception as exc:  # e.g. Chroma unreadable
        log_safe.failure(log, "quiz generation failed", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=QUIZ_UNAVAILABLE_DETAIL
        ) from exc

    if first is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Not enough lecture material in this subject to build a quiz",
        )

    attempt = QuizAttempt(
        user_id=user.id,
        subject_id=subject_id,
        score=0,
        total_questions=question_count,  # requested; settled by finish_streamed_quiz
        language=resolved,
    )
    db.add(attempt)
    db.flush()
    item = _item_row(attempt.id, first)
    db.add(item)
    db.commit()
    db.refresh(item)

    return StreamedQuiz(
        attempt_id=attempt.id,
        subject_id=subject_id,
        language=resolved,
        requested=question_count,
        first=question_payload(item),
        questions=questions,
        cancel=cancel,
    )


def add_streamed_item(attempt_id: int, question: GeneratedQuestion) -> dict | None:
    """Store one more question. None if the quiz was closed or finished meanwhile.

    Runs in its own session: the request that started the quiz is long gone.
    """
    with session_scope() as db:
        attempt = db.get(QuizAttempt, attempt_id, with_for_update=True)
        if attempt is None or attempt.completed_at is not None:
            return None
        # While a quiz is being written total_questions holds the number REQUESTED, so it is
        # always above what is stored. Once finish_streamed_quiz has settled it, total equals
        # what is stored: a question arriving after that (a worker thread that was still
        # inside the model call when the stream was closed) would be stored but never counted.
        stored = db.scalar(
            select(func.count()).select_from(QuizItem).where(QuizItem.attempt_id == attempt_id)
        )
        if stored >= attempt.total_questions:
            return None
        item = _item_row(attempt_id, question)
        db.add(item)
        db.commit()
        db.refresh(item)
        return question_payload(item)


def finish_streamed_quiz(attempt_id: int, requested: int | None = None) -> tuple[int, bool]:
    """Settle a streamed quiz: total_questions becomes the number actually stored.

    Returns (questions_kept, partial). Holds the attempt's row lock so it cannot
    interleave with an answer being recorded: whichever commits second sees the
    other's effect, so a quiz whose every stored question was answered during
    generation is completed here rather than left open.
    """
    with session_scope() as db:
        attempt = db.get(QuizAttempt, attempt_id, with_for_update=True)
        if attempt is None:
            return 0, True
        stored = db.scalar(
            select(func.count()).select_from(QuizItem).where(QuizItem.attempt_id == attempt_id)
        )
        answered = db.scalar(
            select(func.count())
            .select_from(QuizItem)
            .where(QuizItem.attempt_id == attempt_id, QuizItem.chosen_answer_index.is_not(None))
        )
        target = requested if requested is not None else attempt.total_questions
        attempt.total_questions = stored
        if attempt.completed_at is None and stored > 0 and answered == stored:
            attempt.completed_at = datetime.now(UTC)
        db.commit()
        return stored, stored < target


def discard_streamed_quiz(attempt_id: int) -> None:
    """Delete a streamed quiz nobody ever saw (its items go with it)."""
    with session_scope() as db:
        attempt = db.get(QuizAttempt, attempt_id)
        if attempt is not None:
            db.delete(attempt)
            db.commit()
