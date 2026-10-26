"""The quiz row lock (SELECT ... FOR UPDATE) on a real Postgres.

Scenario: a streamed quiz is still being written (total_questions holds the number requested,
4) with 3 questions stored, 2 answered. The student answers the last stored question at the
moment the stream settles (finish_streamed_quiz). On SQLite FOR UPDATE does nothing, so this
was never exercised. Whatever the order, the quiz must end completed, with total = 3 and the
right score. Opt-in (see tests/pg_helpers.py).
"""

import threading
import time
import pytest
from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .pg_helpers import alembic_config, base_url, drop_database, needs_postgres, new_database

pytestmark = needs_postgres


@pytest.fixture()
def pg(monkeypatch):
    """A migrated scratch database; the app's SessionLocal is pointed at it for the test."""
    from app.db import session as dbs

    name = new_database()
    url = base_url(name)
    command.upgrade(alembic_config(monkeypatch, url), "head")
    engine = create_engine(url, pool_size=10)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(dbs, "SessionLocal", Session)
    try:
        yield Session
    finally:
        engine.dispose()
        drop_database(name)


def make_quiz(Session):
    from app.models import QuizAttempt, QuizItem, Subject, University, User

    with Session() as db:
        uni = University(name=f"U{time.time_ns()}", country="E")
        db.add(uni)
        db.flush()
        subject = Subject(university_id=uni.id, year=5, name="S", code="S1")
        user = User(google_id=f"g{time.time_ns()}", email="a@example.com", name="A", university_id=uni.id, year=5)
        db.add_all([subject, user])
        db.flush()
        attempt = QuizAttempt(user_id=user.id, subject_id=subject.id, score=0, total_questions=4, language="en")
        db.add(attempt)
        db.flush()
        items = []
        for i in range(3):
            item = QuizItem(attempt_id=attempt.id, question_text=f"q{i}", options_json=["a", "b"], correct_answer_index=0, source_citation="a.pdf, page 1")
            db.add(item)
            items.append(item)
        db.flush()
        for item in items[:2]:  # two answered correctly while the stream was still writing
            item.chosen_answer_index, item.is_correct = 0, True
        attempt.score = 2
        db.commit()
        return user.id, attempt.id, items[2].id


def final_state(Session, attempt_id):
    from app.models import QuizAttempt

    with Session() as db:
        a = db.get(QuizAttempt, attempt_id)
        return a.completed_at is not None, a.total_questions, a.score


def answer_last(Session, user_id, attempt_id, item_id, hold=0.0, started=None):
    """What the /answer route does: lock the attempt, then record the answer."""
    from app.controllers import quiz_controller as qc
    from app.models import User

    with Session() as db:
        attempt = qc.get_attempt_or_404(db, attempt_id, db.get(User, user_id), lock=True)
        if started:
            started.set()
        time.sleep(hold)  # keep the lock a while, as a slow request would
        qc.submit_answer(db, attempt, item_id, 0)


def test_settling_waits_for_an_answer_in_flight_and_the_quiz_ends_complete(pg):
    from app.controllers import quiz_controller as qc

    user_id, attempt_id, last_item = make_quiz(pg)
    holding = threading.Event()
    t = threading.Thread(target=answer_last, args=(pg, user_id, attempt_id, last_item, 1.5, holding))
    t.start()
    assert holding.wait(10)
    t0 = time.monotonic()
    kept, partial = qc.finish_streamed_quiz(attempt_id, 4)  # must block on the row lock
    waited = time.monotonic() - t0
    t.join()
    assert waited > 1.0, f"finish did not wait for the lock (waited {waited:.2f}s)"
    assert (kept, partial) == (3, True)
    assert final_state(pg, attempt_id) == (True, 3, 3)


def test_an_answer_waits_for_settling_and_the_quiz_ends_complete(pg, monkeypatch):
    """The other order: settling holds the lock first; the answer then completes the quiz itself."""
    from app.controllers import quiz_controller as qc

    user_id, attempt_id, last_item = make_quiz(pg)
    inside = threading.Event()
    # hold finish_streamed_quiz's lock for a while by slowing its first query after the lock
    original = qc.finish_streamed_quiz

    def slow_finish(attempt_id, requested=None):
        from app.models import QuizAttempt

        with pg() as db:
            db.get(QuizAttempt, attempt_id, with_for_update=True)
            inside.set()
            time.sleep(1.5)
            db.rollback()  # releases the lock without changing anything; the real call follows
        return original(attempt_id, requested)

    result = {}
    t = threading.Thread(target=lambda: result.setdefault("r", slow_finish(attempt_id, 4)))
    t.start()
    assert inside.wait(10)
    t0 = time.monotonic()
    answer_last(pg, user_id, attempt_id, last_item)  # blocks until the lock is released
    assert time.monotonic() - t0 > 1.0, "the answer did not wait for the lock"
    t.join()
    assert final_state(pg, attempt_id) == (True, 3, 3)


def test_many_random_interleavings_all_end_complete_with_the_right_total_and_score(pg):
    from app.controllers import quiz_controller as qc

    for round_ in range(25):
        user_id, attempt_id, last_item = make_quiz(pg)
        barrier = threading.Barrier(2)
        errors = []

        def a():
            try:
                barrier.wait()
                time.sleep((round_ % 5) * 0.004)
                answer_last(pg, user_id, attempt_id, last_item)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def b():
            try:
                barrier.wait()
                time.sleep(((round_ // 5) % 5) * 0.004)
                qc.finish_streamed_quiz(attempt_id, 4)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=a), threading.Thread(target=b)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errors, errors
        assert final_state(pg, attempt_id) == (True, 3, 3), f"round {round_}"


def generated():
    from app.services.quiz_service import GeneratedQuestion, SourceChunk

    return GeneratedQuestion("q3", ["a", "b"], 0, SourceChunk("t", "a.pdf", 2, "text", 1))


def test_a_question_arriving_while_the_quiz_settles_is_either_counted_or_refused_never_orphaned(pg, monkeypatch):
    """The writer thread may add a question just as the stream settles. Afterwards the stored
    questions and total_questions must agree. A pause inside the settle (between its count and
    its commit) makes the bad ordering certain without the lock."""
    from sqlalchemy import select as sa_select

    from app.controllers import quiz_controller as qc
    from app.models import QuizItem

    user_id, attempt_id, last_item = make_quiz(pg)
    real_scalar = qc.session_scope  # the settle uses session_scope(); slow its first count a little
    paused, release = threading.Event(), threading.Event()

    import sqlalchemy.orm.session as sa_session

    original_scalar = sa_session.Session.scalar
    state = {"n": 0}

    def slow_scalar(self, statement, *a, **k):
        out = original_scalar(self, statement, *a, **k)
        if threading.current_thread().name == "settler" and state["n"] == 0:
            state["n"] = 1
            paused.set()
            release.wait(5)
        return out

    monkeypatch.setattr(sa_session.Session, "scalar", slow_scalar)
    t = threading.Thread(target=lambda: qc.finish_streamed_quiz(attempt_id, 4), name="settler")
    t.start()
    assert paused.wait(10)  # the settle has counted 3 questions and not committed yet
    out = {}
    adder = threading.Thread(target=lambda: out.setdefault("r", qc.add_streamed_item(attempt_id, generated())))
    adder.start()
    time.sleep(0.8)
    release.set()
    t.join()
    adder.join()
    with pg() as db:
        stored = len(db.scalars(sa_select(QuizItem).where(QuizItem.attempt_id == attempt_id)).all())
    _, total, _ = final_state(pg, attempt_id)
    assert total == stored, f"total_questions={total} but {stored} questions are stored"
    assert real_scalar is qc.session_scope


def test_a_quiz_the_student_already_closed_gets_no_more_questions(pg):
    from app.controllers import quiz_controller as qc
    from app.models import QuizAttempt

    user_id, attempt_id, last_item = make_quiz(pg)
    with pg() as db:
        qc.complete_quiz(db, db.get(QuizAttempt, attempt_id))
    assert qc.add_streamed_item(attempt_id, generated()) is None


def test_a_fourth_question_arriving_while_the_third_is_answered_never_completes_the_quiz_early(pg):
    """total_questions holds the number REQUESTED (4) while writing: answering 3 of 3 stored must not
    complete the quiz while a 4th may still arrive; finishing then settles it either way."""
    from app.controllers import quiz_controller as qc

    for round_ in range(20):
        user_id, attempt_id, last_item = make_quiz(pg)
        barrier, errors, added = threading.Barrier(2), [], []

        def answer():
            try:
                barrier.wait()
                time.sleep((round_ % 4) * 0.005)
                answer_last(pg, user_id, attempt_id, last_item)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def add():
            try:
                barrier.wait()
                time.sleep(((round_ // 4) % 4) * 0.005)
                added.append(qc.add_streamed_item(attempt_id, generated()))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=answer), threading.Thread(target=add)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errors, errors
        assert added[0] is not None, "the quiz was not complete, so the 4th question must be stored"
        done_before_settle, total_before, _ = final_state(pg, attempt_id)
        assert done_before_settle is False and total_before == 4, f"round {round_}: completed early"
        kept, partial = qc.finish_streamed_quiz(attempt_id, 4)
        assert (kept, partial) == (4, False)
        assert final_state(pg, attempt_id) == (False, 4, 3), "the 4th question is still unanswered"
