"""Aggregations for one student's own dashboard.

Every query here is filtered by the authenticated user's id. No function takes a
student identifier -- the caller passes the `User` resolved from the session
cookie, so there is no parameter through which one student could address
another's data.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chat import ChatSession
from app.models.quiz import QuizAttempt
from app.models.subject import Subject
from app.models.user import User

log = logging.getLogger(__name__)

TREND_LENGTH = 5
DEFAULT_ACTIVITY_LIMIT = 10

EVENT_CHAT = "chat_session"
EVENT_QUIZ_COMPLETED = "quiz_completed"
EVENT_QUIZ_IN_PROGRESS = "quiz_in_progress"


@dataclass
class SubjectScore:
    subject_id: int
    subject_code: str
    subject_name: str
    completed_attempts: int
    in_progress_attempts: int
    # None rather than 0.0 when nothing has been completed -- 0% would read as
    # "took a quiz and scored nothing", which is a different and wrong story.
    average_score: float | None
    best_score: float | None
    last_attempt_at: datetime | None
    trend: list[float] = field(default_factory=list)

    @property
    def has_attempts(self) -> bool:
        return self.completed_attempts > 0


@dataclass
class ActivityEvent:
    type: str
    subject_id: int
    subject_code: str
    subject_name: str
    occurred_at: datetime
    reference_id: int
    score: int | None = None
    total_questions: int | None = None


@dataclass
class DashboardSummary:
    subjects: list[SubjectScore]
    overall_readiness: float | None
    subjects_with_attempts: int
    total_completed_attempts: int
    total_in_progress_attempts: int


def _percentage(score: int, total: int) -> float:
    return round(100.0 * score / total, 1) if total else 0.0


def _student_subjects(db: Session, user: User) -> list[Subject]:
    """Subjects in the student's own university and year."""
    if user.university_id is None or user.year is None:
        return []
    return list(
        db.scalars(
            select(Subject)
            .where(
                Subject.university_id == user.university_id,
                Subject.year == user.year,
            )
            .order_by(Subject.code)
        )
    )


def build_summary(db: Session, user: User) -> DashboardSummary:
    """Per-subject score cards plus overall readiness, for this student only."""
    subjects = _student_subjects(db, user)

    attempts = list(
        db.scalars(
            select(QuizAttempt)
            .where(QuizAttempt.user_id == user.id)
            .order_by(QuizAttempt.created_at, QuizAttempt.id)
        )
    )

    by_subject: dict[int, list[QuizAttempt]] = {}
    for attempt in attempts:
        by_subject.setdefault(attempt.subject_id, []).append(attempt)

    cards: list[SubjectScore] = []
    for subject in subjects:
        subject_attempts = by_subject.get(subject.id, [])
        # Only finished attempts count towards any score.
        completed = [a for a in subject_attempts if a.completed_at is not None]
        in_progress = [a for a in subject_attempts if a.completed_at is None]

        percentages = [_percentage(a.score, a.total_questions) for a in completed]
        cards.append(
            SubjectScore(
                subject_id=subject.id,
                subject_code=subject.code,
                subject_name=subject.name,
                completed_attempts=len(completed),
                in_progress_attempts=len(in_progress),
                average_score=(
                    round(sum(percentages) / len(percentages), 1)
                    if percentages
                    else None
                ),
                best_score=max(percentages) if percentages else None,
                last_attempt_at=completed[-1].completed_at if completed else None,
                trend=percentages[-TREND_LENGTH:],
            )
        )

    # Mean over subjects that actually have results. A subject never quizzed is
    # left out rather than counted as 0 -- for the same reason its card shows
    # "no attempts yet". Subjects are weighted equally rather than by attempt
    # count, so heavily drilling one subject cannot dominate the figure.
    scored = [c.average_score for c in cards if c.average_score is not None]
    overall = round(sum(scored) / len(scored), 1) if scored else None

    return DashboardSummary(
        subjects=cards,
        overall_readiness=overall,
        subjects_with_attempts=len(scored),
        total_completed_attempts=sum(c.completed_attempts for c in cards),
        total_in_progress_attempts=sum(c.in_progress_attempts for c in cards),
    )


def build_activity(
    db: Session, user: User, limit: int = DEFAULT_ACTIVITY_LIMIT
) -> list[ActivityEvent]:
    """Chat sessions and quiz attempts merged into one reverse-chronological feed.

    An unfinished quiz still appears, timestamped when it was started and marked
    in-progress, so an interrupted attempt is visible rather than vanishing. Each
    attempt shows up once -- either completed or in-progress, never both.
    """
    subjects = {s.id: s for s in db.scalars(select(Subject))}

    def describe(subject_id: int) -> tuple[str, str]:
        subject = subjects.get(subject_id)
        if subject is None:
            return ("?", "Unknown subject")
        return (subject.code, subject.name)

    events: list[ActivityEvent] = []

    for session in db.scalars(
        select(ChatSession).where(ChatSession.user_id == user.id)
    ):
        code, name = describe(session.subject_id)
        events.append(
            ActivityEvent(
                type=EVENT_CHAT,
                subject_id=session.subject_id,
                subject_code=code,
                subject_name=name,
                occurred_at=session.created_at,
                reference_id=session.id,
            )
        )

    for attempt in db.scalars(
        select(QuizAttempt).where(QuizAttempt.user_id == user.id)
    ):
        code, name = describe(attempt.subject_id)
        finished = attempt.completed_at is not None
        events.append(
            ActivityEvent(
                type=EVENT_QUIZ_COMPLETED if finished else EVENT_QUIZ_IN_PROGRESS,
                subject_id=attempt.subject_id,
                subject_code=code,
                subject_name=name,
                occurred_at=attempt.completed_at if finished else attempt.created_at,
                reference_id=attempt.id,
                score=attempt.score if finished else None,
                total_questions=attempt.total_questions,
            )
        )

    events.sort(key=lambda e: e.occurred_at, reverse=True)
    return events[:limit]
