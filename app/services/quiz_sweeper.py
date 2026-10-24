"""Repair quizzes that a crash left half-finished.

A streamed quiz (see app/controllers/quiz_stream.py) is stored while it is written. Until
it ends, its `total_questions` holds the number *requested*, and finish_streamed_quiz()
then sets it to the number actually stored. If the server dies in between -- a crash, a
deploy, the machine going to sleep -- nothing runs that last step, and the attempt keeps a
total larger than the questions it has: it can never be completed by answering, and it
sits on the dashboard as "in progress" forever.

The sweeper finds exactly those attempts and does what the missing step would have done.
It deliberately leaves two kinds of unfinished quiz alone:

  * young ones -- a quiz may genuinely still be being written;
  * ordinary abandoned ones (every question stored, the student just walked away) -- the
    dashboard shows those as "in progress" on purpose, so an interrupted attempt does not
    silently vanish.
"""

import logging
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.quiz import QuizAttempt, QuizItem

log = logging.getLogger(__name__)


@dataclass
class SweepResult:
    settled: int = 0  # total_questions corrected to the stored count
    completed: int = 0  # ...of which were also closed, because every stored question was answered
    deleted: int = 0  # attempts with no questions at all

    def __bool__(self) -> bool:
        return bool(self.settled or self.deleted)


def sweep_stale_attempts(
    older_than: timedelta,
    *,
    scope: Callable[[], AbstractContextManager[Session]] | None = None,
    now: datetime | None = None,
) -> SweepResult:
    """Settle every unfinished attempt older than `older_than` whose total is wrong.

    Idempotent: a settled attempt no longer matches, so running it again does nothing.
    `scope` yields a database session (default: app.db.session.session_scope).
    """
    if scope is None:
        from app.db.session import session_scope as scope  # imported late: needs the engine

    now = now or datetime.now(UTC)
    cutoff = now - older_than
    result = SweepResult()

    with scope() as db:
        candidates = db.scalars(
            select(QuizAttempt.id).where(
                QuizAttempt.completed_at.is_(None), QuizAttempt.created_at < cutoff
            )
        ).all()

        for attempt_id in candidates:
            # Row lock, like answering and finishing take: whoever commits second sees the
            # other's effect instead of overwriting it.
            attempt = db.get(QuizAttempt, attempt_id, with_for_update=True)
            if attempt is None or attempt.completed_at is not None:
                continue
            stored = db.scalar(
                select(func.count()).select_from(QuizItem).where(QuizItem.attempt_id == attempt_id)
            )
            if stored == 0:
                db.delete(attempt)  # a quiz with no questions is not a quiz
                result.deleted += 1
                continue
            if stored >= attempt.total_questions:
                continue  # consistent: an ordinary quiz the student has not finished
            answered = db.scalar(
                select(func.count())
                .select_from(QuizItem)
                .where(QuizItem.attempt_id == attempt_id, QuizItem.chosen_answer_index.is_not(None))
            )
            attempt.total_questions = stored
            result.settled += 1
            if answered == stored:
                attempt.completed_at = now
                result.completed += 1
        db.commit()

    if result:
        log.info(
            "quiz sweep: settled %s stale attempt(s) (%s completed), deleted %s empty",
            result.settled, result.completed, result.deleted,
        )
    return result


class QuizSweeper(threading.Thread):
    """Sweeps once at startup, then every `interval_minutes` (0 = only once).

    A daemon thread: it never keeps the process alive, and a failure (the database not up
    yet, say) is logged and retried at the next interval rather than raised.
    """

    def __init__(
        self,
        older_than: timedelta,
        interval_minutes: int,
        sweep: Callable[[timedelta], SweepResult] = sweep_stale_attempts,
    ) -> None:
        super().__init__(name="quiz-sweeper", daemon=True)
        self.older_than = older_than
        self.interval = interval_minutes * 60
        self._sweep = sweep
        self._stop_event = threading.Event()
        self.runs = 0

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._sweep(self.older_than)
            except Exception:
                log.warning("quiz sweep failed; will retry", exc_info=True)
            self.runs += 1
            if self.interval <= 0:
                return
            self._stop_event.wait(self.interval)

    def stop(self) -> None:
        self._stop_event.set()
