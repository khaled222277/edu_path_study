"""The sweeper repairs the broken invariant a crash leaves, and nothing else."""

import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings, settings
from app.models import QuizAttempt, QuizItem
from app.services import dashboard_service
from app.services.quiz_sweeper import QuizSweeper, SweepResult, sweep_stale_attempts

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
AGE = timedelta(minutes=60)
OLD = NOW - timedelta(hours=3)
YOUNG = NOW - timedelta(minutes=5)


@pytest.fixture()
def scope(db):
    @contextmanager
    def use():
        yield db

    return use


def make(db, world, *, created, total, stored, answered=0, completed=False, correct_first=True):
    """An attempt with `stored` questions, the first `answered` of them answered."""
    attempt = QuizAttempt(
        user_id=world["alice"].id, subject_id=world["subject"].id, score=0,
        total_questions=total, language="en",
        created_at=created, completed_at=NOW if completed else None,
    )
    db.add(attempt)
    db.flush()
    for i in range(stored):
        item = QuizItem(
            attempt_id=attempt.id, question_text=f"Q{i}?", options_json=["a", "b", "c", "d"],
            correct_answer_index=0, source_citation="f.pdf, page 1",
        )
        if i < answered:
            item.chosen_answer_index = 0 if correct_first else 1
            item.is_correct = correct_first
            attempt.score += int(correct_first)
        db.add(item)
    db.commit()
    return attempt


def sweep(scope):
    return sweep_stale_attempts(AGE, scope=scope, now=NOW)


def reload(db, attempt):
    db.expire_all()
    return db.get(QuizAttempt, attempt.id)


# --- what it repairs ---------------------------------------------------------------------------


def test_a_crashed_quiz_gets_the_total_it_really_has(db, world, scope):
    a = make(db, world, created=OLD, total=10, stored=3)
    assert sweep(scope) == SweepResult(settled=1, completed=0, deleted=0)
    a = reload(db, a)
    assert a.total_questions == 3 and a.completed_at is None, "still open: the student may carry on"


def test_when_every_stored_question_was_answered_the_quiz_is_completed(db, world, scope):
    a = make(db, world, created=OLD, total=10, stored=3, answered=3)
    assert sweep(scope) == SweepResult(settled=1, completed=1, deleted=0)
    a = reload(db, a)
    assert a.total_questions == 3 and a.completed_at is not None and a.score == 3


def test_a_partly_answered_quiz_stays_open(db, world, scope):
    a = make(db, world, created=OLD, total=10, stored=3, answered=2)
    sweep(scope)
    a = reload(db, a)
    assert a.total_questions == 3 and a.completed_at is None and a.score == 2


def test_an_attempt_with_no_questions_is_deleted(db, world, scope):
    a = make(db, world, created=OLD, total=5, stored=0)
    assert sweep(scope) == SweepResult(settled=0, completed=0, deleted=1)
    db.expire_all()
    assert db.get(QuizAttempt, a.id) is None


def test_it_is_idempotent(db, world, scope):
    make(db, world, created=OLD, total=10, stored=3, answered=3)
    make(db, world, created=OLD, total=10, stored=2)
    assert sweep(scope)
    assert sweep(scope) == SweepResult(), "a settled attempt no longer matches"


def test_every_stale_attempt_in_one_pass_across_students(db, world, scope):
    mine = make(db, world, created=OLD, total=10, stored=2)
    other = QuizAttempt(user_id=world["bob"].id, subject_id=world["subject"].id, score=0,
                        total_questions=8, language="ar", created_at=OLD)
    db.add(other)
    db.flush()
    db.add(QuizItem(attempt_id=other.id, question_text="س؟", options_json=list("abcd"),
                    correct_answer_index=1, source_citation="x"))
    db.commit()
    assert sweep(scope).settled == 2
    assert reload(db, mine).total_questions == 2 and reload(db, other).total_questions == 1


# --- what it must leave alone ---------------------------------------------------------------------


def test_a_young_attempt_may_still_be_being_written(db, world, scope):
    a = make(db, world, created=YOUNG, total=10, stored=3)
    assert sweep(scope) == SweepResult()
    assert reload(db, a).total_questions == 10


def test_the_age_boundary(db, world, scope):
    just_over = make(db, world, created=NOW - AGE - timedelta(seconds=1), total=10, stored=2)
    just_under = make(db, world, created=NOW - AGE + timedelta(seconds=1), total=10, stored=2)
    sweep(scope)
    assert reload(db, just_over).total_questions == 2
    assert reload(db, just_under).total_questions == 10


def test_an_abandoned_ordinary_quiz_stays_in_progress(db, world, scope):
    """Every question stored, the student walked away: the dashboard shows it on purpose."""
    a = make(db, world, created=OLD, total=5, stored=5, answered=2)
    assert sweep(scope) == SweepResult()
    a = reload(db, a)
    assert a.total_questions == 5 and a.completed_at is None


def test_a_finished_quiz_is_untouched(db, world, scope):
    a = make(db, world, created=OLD, total=10, stored=3, answered=3, completed=True)
    assert sweep(scope) == SweepResult()
    assert reload(db, a).total_questions == 10, "completed attempts are never rewritten"


def test_a_quiz_that_was_never_answered_but_is_complete_in_size_is_left_alone(db, world, scope):
    a = make(db, world, created=OLD, total=4, stored=4, answered=0)
    assert sweep(scope) == SweepResult() and reload(db, a).completed_at is None


# --- the effect the student sees ---------------------------------------------------------------------------


def test_the_dashboard_stops_showing_a_dead_quiz_as_in_progress(db, world, scope):
    make(db, world, created=OLD, total=10, stored=3, answered=3)
    before = dashboard_service.build_summary(db, world["alice"])
    assert before.total_in_progress_attempts == 1 and before.total_completed_attempts == 0
    sweep(scope)
    db.expire_all()
    after = dashboard_service.build_summary(db, world["alice"])
    assert after.total_in_progress_attempts == 0 and after.total_completed_attempts == 1
    assert after.overall_readiness == 100.0, "3 of 3, not 3 of 10"


def test_a_settled_quiz_can_now_be_finished_by_answering(client, db, world, scope):
    from .conftest import login

    a = make(db, world, created=OLD, total=10, stored=3, answered=2)
    sweep(scope)
    last = db.query(QuizItem).filter_by(attempt_id=a.id, chosen_answer_index=None).one()
    r = login(client, world["alice"]).post(f"/quiz/{a.id}/answer", json={"item_id": last.id, "chosen_answer_index": 0})
    assert r.status_code == 200 and r.json()["is_complete"] is True and r.json()["total_questions"] == 3


# --- the background thread ---------------------------------------------------------------------------------------


def test_the_thread_sweeps_at_startup_and_then_on_the_interval():
    calls = []
    sweeper = QuizSweeper(AGE, 1, sweep=lambda age: calls.append(age) or SweepResult())
    sweeper.interval = 0.05  # seconds, for the test
    sweeper.start()
    time.sleep(0.3)
    sweeper.stop()
    sweeper.join(timeout=2)
    assert len(calls) >= 3 and calls[0] == AGE and not sweeper.is_alive()


def test_interval_zero_means_only_at_startup():
    calls = []
    sweeper = QuizSweeper(AGE, 0, sweep=lambda age: calls.append(1) or SweepResult())
    sweeper.start()
    sweeper.join(timeout=2)
    assert calls == [1] and not sweeper.is_alive()


def test_a_failing_sweep_is_logged_and_retried_not_fatal(caplog):
    attempts = []

    def flaky(age):
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("database not up yet")
        return SweepResult()

    sweeper = QuizSweeper(AGE, 1, sweep=flaky)
    sweeper.interval = 0.05
    with caplog.at_level("WARNING"):
        sweeper.start()
        time.sleep(0.3)
        sweeper.stop()
        sweeper.join(timeout=2)
    assert len(attempts) >= 2 and "quiz sweep failed" in caplog.text


def test_stop_ends_a_long_wait_immediately():
    sweeper = QuizSweeper(AGE, 60, sweep=lambda age: SweepResult())
    sweeper.start()
    time.sleep(0.1)
    t0 = time.perf_counter()
    sweeper.stop()
    sweeper.join(timeout=2)
    assert not sweeper.is_alive() and time.perf_counter() - t0 < 1


# --- startup wiring ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("enabled, expected", [(True, 1), (False, 0)])
def test_the_app_starts_the_sweeper_only_when_enabled(monkeypatch, enabled, expected):
    started, stopped = [], []

    class Fake(QuizSweeper):
        def start(self):
            started.append(self)

        def stop(self):
            stopped.append(self)

    monkeypatch.setattr(settings, "quiz_sweep_enabled", enabled)
    monkeypatch.setattr(main, "QuizSweeper", Fake)
    with TestClient(main.app) as c:
        assert c.get("/health").status_code == 200
        assert len(started) == expected
    assert len(stopped) == expected, "and it is stopped on shutdown"


def test_the_app_passes_the_configured_age_and_interval(monkeypatch):
    seen = {}

    class Fake(QuizSweeper):
        def __init__(self, older_than, interval_minutes, **kw):
            seen.update(age=older_than, interval=interval_minutes)
            super().__init__(older_than, interval_minutes, **kw)

        def start(self):
            pass

    monkeypatch.setattr(settings, "quiz_sweep_enabled", True)
    monkeypatch.setattr(settings, "quiz_stale_after_minutes", 90)
    monkeypatch.setattr(settings, "quiz_sweep_interval_minutes", 7)
    monkeypatch.setattr(main, "QuizSweeper", Fake)
    with TestClient(main.app):
        pass
    assert seen == {"age": timedelta(minutes=90), "interval": 7}


# --- settings ------------------------------------------------------------------------------------------------------


def test_defaults(monkeypatch):
    monkeypatch.delenv("QUIZ_SWEEP_ENABLED", raising=False)  # the test environment switches it off
    s = Settings(_env_file=None)
    assert (s.quiz_sweep_enabled, s.quiz_stale_after_minutes, s.quiz_sweep_interval_minutes) == (True, 60, 30)


@pytest.mark.parametrize("overrides", [
    {"quiz_stale_after_minutes": 4}, {"quiz_stale_after_minutes": 10081},
    {"quiz_sweep_interval_minutes": -1}, {"quiz_sweep_interval_minutes": 10081},
])
def test_out_of_range_values_are_rejected(overrides):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **overrides)


def test_a_tiny_stale_age_is_refused_so_a_quiz_being_written_cannot_be_swept():
    with pytest.raises(ValueError):
        Settings(_env_file=None, quiz_stale_after_minutes=1)


def test_each_attempt_is_read_under_a_row_lock(db, world, scope, monkeypatch):
    """So an answer being recorded at that moment cannot be overwritten (Postgres; SQLite ignores it)."""
    make(db, world, created=OLD, total=10, stored=3)
    calls = []
    real_get = db.get
    monkeypatch.setattr(db, "get", lambda *a, **k: calls.append(k) or real_get(*a, **k))
    sweep(scope)
    assert calls and all(k.get("with_for_update") is True for k in calls)


def test_a_finished_attempt_is_skipped_even_if_it_finished_after_the_candidate_list_was_read(db, world, scope, monkeypatch):
    """The re-check after taking the lock: a quiz completed in the meantime is not rewritten."""
    a = make(db, world, created=OLD, total=10, stored=3, answered=1)
    real_get = db.get

    def finish_then_get(model, ident, **kw):
        obj = real_get(model, ident, **kw)
        obj.completed_at = NOW  # the student finished it between the SELECT and the lock
        return obj

    monkeypatch.setattr(db, "get", finish_then_get)
    assert sweep(scope) == SweepResult()
    monkeypatch.undo()
    assert reload(db, a).total_questions == 10
