from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.controllers import quiz_controller, quiz_stream
from app.config import settings
from app.db.session import get_db
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.quiz import (
    AnswerIn,
    AnswerOut,
    QuizHistoryEntry,
    QuizQuestionOut,
    QuizResultItem,
    QuizResults,
    QuizStart,
    QuizStartOut,
)

router = APIRouter(prefix="/quiz", tags=["quiz"])


@router.post("/start", response_model=QuizStartOut, status_code=status.HTTP_201_CREATED)
@limiter.shared_limit(settings.rate_limit_quiz, scope="quiz")
def start_quiz(
    request: Request,
    payload: QuizStart,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate and persist a quiz. 403 if the subject is out of scope.

    All questions come back at once so the student never waits between them,
    but `correct_answer_index` is withheld until each one is answered.
    """
    attempt = quiz_controller.start_quiz(
        db,
        user,
        payload.subject_id,
        payload.question_count,
        payload.language,
        payload.prompt,
    )
    return QuizStartOut(
        attempt_id=attempt.id,
        subject_id=attempt.subject_id,
        language=attempt.language,
        total_questions=attempt.total_questions,
        questions=[
            QuizQuestionOut(
                id=item.id,
                question_text=item.question_text,
                options=item.options_json,
                source_citation=item.source_citation,
            )
            for item in attempt.items
        ],
    )


@router.post("/start/stream")
@limiter.shared_limit(settings.rate_limit_quiz, scope="quiz")
async def start_quiz_stream(
    request: Request,
    payload: QuizStart,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Same quiz as `start_quiz`, delivered question by question as NDJSON events.

    Events: `start`, one `question` per validated question, then `done` (or `error`
    if generation broke off -- the questions already sent stay valid). See
    app/controllers/quiz_stream.py. Nothing is sent until the first question
    exists, so Ollama being down is the same HTTP 503 as the plain route; the quiz
    shares that route's rate-limit allowance.

    `async def`, with the blocking work pushed to the threadpool, for the same
    reason as the chat stream: a long-lived stream must not hold a worker thread.
    """
    started = await run_in_threadpool(
        quiz_controller.begin_streamed_quiz,
        db,
        user,
        payload.subject_id,
        payload.question_count,
        payload.language,
        payload.prompt,
    )
    # The student may have cancelled while the first question was being written.
    # Ask now, before a response exists and nothing else is reading the connection:
    # once streaming starts, uvicorn silently swallows writes to a closed socket,
    # so the stream could not tell and would keep a one-question stub.
    if await request.is_disconnected():
        await run_in_threadpool(started.close)  # nobody saw it: discards the attempt
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return quiz_stream.streaming_response(started)


@router.post("/{attempt_id}/answer", response_model=AnswerOut)
def answer_question(
    attempt_id: int,
    payload: AnswerIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record an answer and reveal whether it was right."""
    attempt = quiz_controller.get_attempt_or_404(db, attempt_id, user, lock=True)
    item, answered = quiz_controller.submit_answer(
        db, attempt, payload.item_id, payload.chosen_answer_index
    )
    return AnswerOut(
        item_id=item.id,
        is_correct=bool(item.is_correct),
        correct_answer_index=item.correct_answer_index,
        source_citation=item.source_citation,
        answered_count=answered,
        total_questions=attempt.total_questions,
        is_complete=attempt.is_complete,
    )


@router.post("/{attempt_id}/complete", response_model=QuizResults)
def complete_quiz(
    attempt_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Close an attempt early, leaving unanswered questions unanswered."""
    attempt = quiz_controller.get_attempt_or_404(db, attempt_id, user, lock=True)
    quiz_controller.complete_quiz(db, attempt)
    return _results(attempt)


@router.get("/history", response_model=list[QuizHistoryEntry])
def quiz_history(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Past attempts, most recent first. Feeds the Phase 5 dashboard."""
    return quiz_controller.list_history(db, user)


@router.get("/{attempt_id}/results", response_model=QuizResults)
def quiz_results(
    attempt_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Full review with correct answers. Only once the attempt is finished."""
    attempt = quiz_controller.get_attempt_or_404(db, attempt_id, user)
    if not attempt.is_complete:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Finish the quiz before viewing results",
        )
    return _results(attempt)


def _results(attempt) -> QuizResults:
    return QuizResults(
        attempt_id=attempt.id,
        subject_id=attempt.subject_id,
        language=attempt.language,
        score=attempt.score,
        total_questions=attempt.total_questions,
        created_at=attempt.created_at,
        completed_at=attempt.completed_at,
        items=[QuizResultItem.model_validate(i) for i in attempt.items],
    )
