"""POST /quiz/start/stream: questions arrive one by one, are stored as they do, and survive failure."""

import asyncio
import json
import time
from contextlib import contextmanager

import pytest
from fastapi.concurrency import run_in_threadpool

from app.controllers import chat_stream
from app.controllers import quiz_controller as qc
from app.controllers import quiz_stream
from app.models import QuizAttempt, QuizItem
from app.services import quiz_service as qs
from app.services.llm import LLMUnavailableError

from .conftest import login
from .test_chat_stream import _cookie, _drive
from .test_error_handling import LEAK, rate_limited  # noqa: F401  (fixture)
from .test_quiz_options import DUP_EN, chunk, reply

TAGS = "ABCDEFGHIJKLMNOPQRST"
EN_OPTS = ["Clustering", "Classification", "Regression", "Association"]


def good(tag):
    """A valid English question that says which chunk it came from."""
    return reply(f"What groups similar objects in part {tag}?", EN_OPTS, "Clustering")


@pytest.fixture()
def quiz_db(db, monkeypatch):
    """Point the stream's own-session writes at the shared in-memory test database."""

    @contextmanager
    def scope():
        yield db

    monkeypatch.setattr("app.controllers.quiz_controller.session_scope", scope)
    return db


@pytest.fixture()
def model(monkeypatch):
    """Script the model per chunk: plan[tag] lists replies (the last repeats); an
    Exception instance is raised. Returns the log of calls, in order."""

    def install(plan, delay=0.0):
        chunks = [chunk(t) for t in plan]
        monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: chunks)
        seen = {t: 0 for t in plan}
        calls = []

        def fake(prompt, _model):
            tag = next(t for t in plan if t * 50 in prompt)
            if delay:
                time.sleep(delay)
            replies = plan[tag]
            item = replies[min(seen[tag], len(replies) - 1)]
            seen[tag] += 1
            calls.append(tag)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(qs, "_ask_model", fake)
        return calls

    return install


def start(client, world, count=3, **extra):
    body = {"subject_id": world["subject"].id, "question_count": count, "language": "en", **extra}
    return login(client, world["alice"]).post("/quiz/start/stream", json=body)


def events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def attempts(db):
    db.expire_all()
    return db.query(QuizAttempt).all()


def items(db):
    db.expire_all()
    return db.query(QuizItem).order_by(QuizItem.id).all()


# --- the happy path -------------------------------------------------------------


def test_events_arrive_in_order_one_question_at_a_time(client, world, quiz_db, model):
    model({"A": [good("A")], "B": [good("B")], "C": [good("C")]})
    r = start(client, world, count=3)

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    evs = events(r)
    assert [e["type"] for e in evs] == ["start", "question", "question", "question", "done"]
    assert [e["index"] for e in evs[1:4]] == [0, 1, 2]
    assert [e["question"]["question_text"][-2] for e in evs[1:4]] == ["A", "B", "C"]
    assert evs[0]["requested"] == 3 and evs[0]["language"] == "en"
    assert evs[-1] == {
        "type": "done", "attempt_id": evs[0]["attempt_id"],
        "total_questions": 3, "requested": 3, "partial": False,
    }


def test_each_question_is_stored_and_matches_what_was_sent(client, world, quiz_db, model):
    model({"A": [good("A")], "B": [good("B")]})
    evs = events(start(client, world, count=2))
    (attempt,) = attempts(quiz_db)
    rows = items(quiz_db)

    assert attempt.id == evs[0]["attempt_id"] and attempt.user_id == world["alice"].id
    assert attempt.total_questions == 2 and attempt.completed_at is None
    assert [r.id for r in rows] == [e["question"]["id"] for e in evs[1:3]]
    assert all(r.correct_answer_index == 0 for r in rows)  # "Clustering" is option 0


def test_the_correct_answer_is_never_sent(client, world, quiz_db, model):
    model({"A": [good("A")], "B": [good("B")]})
    r = start(client, world, count=2)
    assert "correct" not in r.text.lower()
    for e in events(r):
        assert "correct_answer_index" not in json.dumps(e)
    assert set(events(r)[1]["question"]) == {"id", "question_text", "options", "source_citation"}


def test_arabic_survives_the_wire_unescaped(client, world, quiz_db, model):
    ar = reply("ما هي عملية تجميع الكائنات المتشابهة؟", ["التجميع", "التصنيف", "الانحدار", "الارتباط"], "التجميع")
    model({"A": [ar]})
    r = start(client, world, count=1, language="ar")
    assert "التجميع" in r.content.decode("utf-8")
    assert events(r)[1]["question"]["question_text"].startswith("ما هي")


# --- errors before anything is sent: the same HTTP errors as the plain route --------


def _clean_503(r):
    assert r.status_code == 503
    assert list(r.json()) == ["detail"]
    assert r.json()["detail"] == qc.QUIZ_UNAVAILABLE_DETAIL
    assert LEAK not in r.text and "Traceback" not in r.text


def test_ollama_down_before_the_first_question_is_a_clean_503(client, world, quiz_db, model):
    model({"A": [LLMUnavailableError(LEAK)]})
    _clean_503(start(client, world))
    assert attempts(quiz_db) == [] and items(quiz_db) == []


def test_no_valid_question_at_all_is_a_clean_503_and_stores_nothing(client, world, quiz_db, model):
    model({"A": [DUP_EN], "B": [DUP_EN]})
    _clean_503(start(client, world))
    assert attempts(quiz_db) == []


def test_chroma_failure_is_a_clean_503(client, world, quiz_db, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError(LEAK)

    monkeypatch.setattr(qs, "sample_chunks", broken)
    _clean_503(start(client, world))


def test_a_subject_without_material_is_a_422(client, world, quiz_db, monkeypatch):
    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: [])
    r = start(client, world)
    assert r.status_code == 422 and "lecture material" in r.json()["detail"]


# --- access ------------------------------------------------------------------------


def test_missing_cookie_is_401(client, world, quiz_db, model):
    calls = model({"A": [good("A")]})
    client.cookies.clear()
    r = client.post("/quiz/start/stream", json={"subject_id": world["subject"].id})
    assert r.status_code == 401 and calls == []


def test_a_subject_outside_the_students_year_is_403_before_any_model_call(client, world, quiz_db, model):
    calls = model({"A": [good("A")]})
    r = login(client, world["alice"]).post("/quiz/start/stream", json={"subject_id": world["other_year"].id})
    assert r.status_code == 403 and calls == [] and attempts(quiz_db) == []


@pytest.mark.parametrize("body", [{"subject_id": 0}, {"subject_id": 1, "question_count": 0},
                                  {"subject_id": 1, "question_count": 21}, {"subject_id": 1, "language": "fr"}])
def test_bad_requests_are_422(client, world, quiz_db, model, body):
    model({"A": [good("A")]})
    assert login(client, world["alice"]).post("/quiz/start/stream", json=body).status_code == 422


def test_rate_limit_is_shared_with_the_plain_route(client, world, quiz_db, model, rate_limited):  # noqa: F811
    model({"A": [good("A")]})
    c = login(client, world["alice"])
    body = {"subject_id": world["subject"].id, "question_count": 1, "language": "en"}
    codes = [c.post("/quiz/start/stream" if i % 2 else "/quiz/start", json=body).status_code for i in range(5)]
    assert codes[0] == 201 and all(code in (200, 201) for code in codes)
    over = [c.post("/quiz/start/stream", json=body).status_code, c.post("/quiz/start", json=body).status_code]
    assert over == [429, 429]


# --- partial results ------------------------------------------------------------------


def test_a_failure_part_way_keeps_the_questions_already_sent(client, world, quiz_db, model):
    model({"A": [good("A")], "B": [good("B")], "C": [LLMUnavailableError(LEAK)], "D": [good("D")]})
    r = start(client, world, count=4)

    assert r.status_code == 200
    evs = events(r)
    assert [e["type"] for e in evs] == ["start", "question", "question", "error"]
    assert evs[-1] == {
        "type": "error", "detail": quiz_stream.QUIZ_STREAM_ERROR_DETAIL,
        "attempt_id": evs[0]["attempt_id"], "total_questions": 2, "partial": True,
    }
    assert LEAK not in r.text and "Traceback" not in r.text and "Unavailable" not in r.text
    (attempt,) = attempts(quiz_db)
    assert attempt.total_questions == 2 and len(items(quiz_db)) == 2  # settled to what exists


def test_running_out_of_usable_chunks_is_a_partial_done(client, world, quiz_db, model):
    model({"A": [good("A")], "B": [good("B")], "C": [DUP_EN], "D": [DUP_EN]})
    evs = events(start(client, world, count=4))
    assert [e["type"] for e in evs] == ["start", "question", "question", "done"]
    assert evs[-1]["partial"] is True and evs[-1]["total_questions"] == 2 and evs[-1]["requested"] == 4
    (attempt,) = attempts(quiz_db)
    assert attempt.total_questions == 2


def test_a_complete_quiz_is_not_marked_partial(client, world, quiz_db, model):
    model({"A": [good("A")], "B": [good("B")]})
    assert events(start(client, world, count=2))[-1]["partial"] is False


# --- answering while it is still being written ---------------------------------------------


def test_the_total_is_the_requested_count_while_generation_runs(world, quiz_db, model):
    model({"A": [good("A")], "B": [good("B")], "C": [good("C")]})
    started = qc.begin_streamed_quiz(quiz_db, world["alice"], world["subject"].id, 3, "en")
    (attempt,) = attempts(quiz_db)
    assert attempt.total_questions == 3 and len(items(quiz_db)) == 1
    assert started.first["id"] == items(quiz_db)[0].id and "correct" not in json.dumps(started.first)


def test_answering_the_first_question_does_not_finish_a_quiz_still_being_written(world, quiz_db, model):
    model({"A": [good("A")], "B": [good("B")]})
    started = qc.begin_streamed_quiz(quiz_db, world["alice"], world["subject"].id, 2, "en")
    (attempt,) = attempts(quiz_db)
    qc.submit_answer(quiz_db, attempt, started.first["id"], 0)
    quiz_db.expire_all()
    assert attempt.completed_at is None, "one of two answered: not done, the second is still coming"


def test_everything_answered_before_the_stream_ends_is_completed_on_settling(world, quiz_db, model):
    """The student answers both stored questions, then generation gives up on the rest."""
    model({"A": [good("A")], "B": [good("B")], "C": [DUP_EN]})
    started = qc.begin_streamed_quiz(quiz_db, world["alice"], world["subject"].id, 3, "en")
    second = qc.add_streamed_item(started.attempt_id, next(started.questions))
    (attempt,) = attempts(quiz_db)
    qc.submit_answer(quiz_db, attempt, started.first["id"], 0)
    qc.submit_answer(quiz_db, attempt, second["id"], 1)
    quiz_db.expire_all()
    assert attempt.completed_at is None and attempt.total_questions == 3

    assert qc.finish_streamed_quiz(started.attempt_id, 3) == (2, True)
    quiz_db.expire_all()
    assert attempt.total_questions == 2 and attempt.completed_at is not None
    assert attempt.score == 1


def test_unanswered_questions_leave_the_quiz_open_after_settling(world, quiz_db, model):
    model({"A": [good("A")], "B": [good("B")]})
    started = qc.begin_streamed_quiz(quiz_db, world["alice"], world["subject"].id, 3, "en")
    qc.add_streamed_item(started.attempt_id, next(started.questions))
    assert qc.finish_streamed_quiz(started.attempt_id, 3) == (2, True)
    (attempt,) = attempts(quiz_db)
    assert attempt.completed_at is None and attempt.total_questions == 2


def test_a_quiz_closed_early_by_the_student_stops_accepting_questions(world, quiz_db, model):
    model({"A": [good("A")], "B": [good("B")]})
    started = qc.begin_streamed_quiz(quiz_db, world["alice"], world["subject"].id, 3, "en")
    (attempt,) = attempts(quiz_db)
    qc.complete_quiz(quiz_db, attempt)
    assert qc.add_streamed_item(started.attempt_id, next(started.questions)) is None
    assert len(items(quiz_db)) == 1


def test_answering_over_http_while_generation_is_pending(client, world, quiz_db, model):
    model({"A": [good("A")], "B": [good("B")]})
    started = qc.begin_streamed_quiz(quiz_db, world["alice"], world["subject"].id, 2, "en")
    r = login(client, world["alice"]).post(
        f"/quiz/{started.attempt_id}/answer", json={"item_id": started.first["id"], "chosen_answer_index": 0}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["is_correct"] is True and body["is_complete"] is False
    assert body["total_questions"] == 2 and body["answered_count"] == 1


def test_another_student_cannot_answer_a_quiz_that_is_streaming(client, world, quiz_db, model):
    model({"A": [good("A")]})
    started = qc.begin_streamed_quiz(quiz_db, world["alice"], world["subject"].id, 2, "en")
    r = login(client, world["bob"]).post(
        f"/quiz/{started.attempt_id}/answer", json={"item_id": started.first["id"], "chosen_answer_index": 0}
    )
    assert r.status_code == 404


def test_answering_and_completing_take_the_row_lock(client, world, quiz_db, model, monkeypatch):
    """What keeps an answer and the settling step from interleaving on Postgres."""
    model({"A": [good("A")]})
    started = qc.begin_streamed_quiz(quiz_db, world["alice"], world["subject"].id, 1, "en")
    seen = []
    real = qc.get_attempt_or_404
    monkeypatch.setattr(qc, "get_attempt_or_404", lambda *a, **k: seen.append(k) or real(*a, **k))
    c = login(client, world["alice"])
    c.post(f"/quiz/{started.attempt_id}/answer", json={"item_id": started.first["id"], "chosen_answer_index": 0})
    c.post(f"/quiz/{started.attempt_id}/complete")
    c.get(f"/quiz/{started.attempt_id}/results")
    assert seen[0] == {"lock": True} and seen[1] == {"lock": True}
    assert seen[2] == {}  # a read-only view takes no lock


def test_get_attempt_passes_the_lock_to_the_database(world, quiz_db, model, monkeypatch):
    model({"A": [good("A")]})
    started = qc.begin_streamed_quiz(quiz_db, world["alice"], world["subject"].id, 1, "en")
    quiz_db.expire_all()
    calls = []
    real_get = quiz_db.get
    monkeypatch.setattr(quiz_db, "get", lambda *a, **k: calls.append(k) or real_get(*a, **k))
    qc.get_attempt_or_404(quiz_db, started.attempt_id, world["alice"], lock=True)
    qc.get_attempt_or_404(quiz_db, started.attempt_id, world["alice"])
    assert calls[0].get("with_for_update") is True and not calls[1].get("with_for_update")


# --- the client goes away ---------------------------------------------------------------------


def test_stopping_mid_generation_keeps_what_was_sent_and_stops_the_model(client, world, quiz_db, model):
    calls = model({t: [good(t)] for t in TAGS}, delay=0.04)

    sent, _ = asyncio.run(_drive(
        "/quiz/start/stream", _cookie(world["alice"]),
        json.dumps({"subject_id": world["subject"].id, "question_count": 20, "language": "en"}).encode(),
        disconnect_after=3,  # start, question 0, question 1
    ))
    after_return = len(calls)
    time.sleep(0.4)  # an in-flight model call may still finish; nothing new may start

    assert len(calls) <= after_return + 1, "the model must stop being asked once the client is gone"
    assert len(calls) < 20
    (attempt,) = attempts(quiz_db)
    stored = items(quiz_db)
    assert 2 <= len(stored) < 20, "questions already generated are kept"
    assert attempt.total_questions == len(stored), "and the total is settled to what exists"
    assert not any(b'"done"' in m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def test_a_quiz_nobody_received_is_discarded(world, quiz_db, model):
    """The client vanished before the first chunk: no stub attempt is left behind."""
    model({"A": [good("A")]})
    started = qc.begin_streamed_quiz(quiz_db, world["alice"], world["subject"].id, 3, "en")
    assert len(attempts(quiz_db)) == 1

    assert started.close() == (0, True)
    assert attempts(quiz_db) == [] and items(quiz_db) == []
    assert started.close() is None  # idempotent


def test_an_unstarted_body_is_still_cleaned_up_by_the_response(world, quiz_db, model):
    """send() fails before the generator ever runs, so only the response's hook can settle it."""
    model({"A": [good("A")]})
    started = qc.begin_streamed_quiz(quiz_db, world["alice"], world["subject"].id, 3, "en")

    async def go():
        response = quiz_stream.streaming_response(started)

        async def send(message):
            raise OSError("connection reset")

        async def receive():
            await asyncio.sleep(10)

        with pytest.raises(ExceptionGroup):
            await response({"type": "http"}, receive, send)

    asyncio.run(go())
    assert attempts(quiz_db) == [], "nobody saw this quiz, so it must not linger"


def test_settling_twice_does_not_double_count(world, quiz_db, model):
    model({"A": [good("A")], "B": [good("B")]})
    started = qc.begin_streamed_quiz(quiz_db, world["alice"], world["subject"].id, 2, "en")
    started.delivered = True
    first = started.close()
    assert first == (1, True) and started.close() is None


# --- the generator underneath -------------------------------------------------------------------


def test_iter_quiz_yields_each_question_before_generating_the_next(model):
    calls = model({"A": [good("A")], "B": [good("B")], "C": [good("C")]})
    gen = qs.iter_quiz(1, 3, "en")
    assert calls == []  # nothing happens until asked
    first = next(gen)
    assert calls == ["A"] and first.source.source_filename == "A.pdf"
    second = next(gen)
    assert calls == ["A", "B"] and second.source.source_filename == "B.pdf"


def test_generate_quiz_is_iter_quiz_collected(model):
    model({"A": [good("A")], "B": [good("B")]})
    assert [q.question_text for q in qs.generate_quiz(1, 2, "en")] == [q.question_text for q in qs.iter_quiz(1, 2, "en")]


def test_cancelling_stops_before_the_next_model_call(model):
    calls = model({t: [good(t)] for t in "ABCDE"})
    import threading

    cancel = threading.Event()
    gen = qs.iter_quiz(1, 5, "en", cancel=cancel)
    next(gen)
    cancel.set()
    assert list(gen) == []
    assert calls == ["A"]


def test_cancelling_is_not_reported_as_a_failure(model):
    import threading

    model({t: [good(t)] for t in "AB"})
    cancel, trace = threading.Event(), qs.QuizTrace()
    gen = qs.iter_quiz(1, 2, "en", trace=trace, cancel=cancel)
    next(gen)
    cancel.set()
    list(gen)  # must end quietly, not raise QuizGenerationError
    assert trace.error == "cancelled" and trace.produced == 1


def test_an_outage_after_some_questions_still_raises_to_the_caller(model):
    model({"A": [good("A")], "B": [LLMUnavailableError("down")]})
    gen = qs.iter_quiz(1, 2, "en")
    assert next(gen).source.source_filename == "A.pdf"
    with pytest.raises(LLMUnavailableError):
        next(gen)


# --- it must not starve other requests -------------------------------------------------------------


def test_many_concurrent_quiz_streams_do_not_starve_other_requests(world, quiz_db, monkeypatch):
    """The model calls run in the threadpool one at a time; the stream itself holds no thread."""
    import httpx
    from fastapi import FastAPI

    # A model that takes 30 ms per call, 60 students at once.
    chunks = [chunk(t) for t in TAGS[:4]]
    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: chunks)

    def slow(prompt, _model):
        time.sleep(0.03)
        tag = next(t for t in TAGS if t * 50 in prompt)
        return good(tag)

    monkeypatch.setattr(qs, "_ask_model", slow)
    monkeypatch.setattr(qc, "add_streamed_item", lambda attempt_id, q: {
        "id": 1, "question_text": q.question_text, "options": q.options, "source_citation": "x"})

    mini = FastAPI()

    @mini.get("/health")
    def health():
        return {"status": "ok"}

    @mini.post("/stream")
    async def stream_route():
        gen = qs.iter_quiz(1, 4, "en")
        first = await run_in_threadpool(next, gen)
        started = qc.StreamedQuiz(
            attempt_id=1, subject_id=1, language="en", requested=4,
            first={"id": 1, "question_text": first.question_text, "options": first.options, "source_citation": "x"},
            questions=gen, cancel=__import__("threading").Event(),
        )
        monkeypatch.setattr(qc.StreamedQuiz, "close", lambda self: (4, False))
        return quiz_stream.streaming_response(started)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mini), base_url="http://t") as c:
            streams = [asyncio.create_task(c.post("/stream")) for _ in range(60)]
            await asyncio.sleep(0.2)
            t0 = time.perf_counter()
            health = await c.get("/health")
            latency = time.perf_counter() - t0
            return health, latency, await asyncio.gather(*streams)

    health, latency, results = asyncio.run(go())
    assert health.status_code == 200 and latency < 0.5, f"health took {latency:.2f}s under load"
    assert all(r.status_code == 200 for r in results)


def test_cancelling_during_a_chunks_retries_stops_that_chunk(monkeypatch):
    """Only the check before each model call can stop a chunk that is already being retried."""
    import threading

    cancel = threading.Event()
    calls = []

    def fake(prompt, _model):
        calls.append(1)
        cancel.set()  # the student leaves while the first call is in flight
        return DUP_EN

    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: [chunk("A"), chunk("B")])
    monkeypatch.setattr(qs, "_ask_model", fake)
    trace = qs.QuizTrace()
    assert list(qs.iter_quiz(1, 2, "en", trace=trace, cancel=cancel)) == []
    assert len(calls) == 1, "no second attempt once cancelled"
    assert trace.error == "cancelled"


def test_cancelling_between_chunks_does_not_even_start_the_next_one(model):
    """Only the check between chunks avoids opening a trace for work that never happens."""
    import threading

    model({t: [good(t)] for t in "ABC"})
    cancel, trace = threading.Event(), qs.QuizTrace()
    gen = qs.iter_quiz(1, 3, "en", trace=trace, cancel=cancel)
    next(gen)
    cancel.set()
    assert list(gen) == []
    assert [c.citation for c in trace.chunks] == ["A.pdf, page 1"]


# --- cancelling while the first question is still being written --------------------------------


async def _drive_gone(path, cookie, body):
    """Raw ASGI where the client has already hung up by the time the handler asks."""
    from .test_chat_stream import _scope

    sent, delivered = [], False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    from app.main import app

    await app(_scope(path, cookie, body), receive, send)
    return sent


def test_cancelling_before_the_first_question_leaves_nothing_behind(client, world, quiz_db, model):
    calls = model({"A": [good("A")], "B": [good("B")]})
    sent = asyncio.run(_drive_gone(
        "/quiz/start/stream", _cookie(world["alice"]),
        json.dumps({"subject_id": world["subject"].id, "question_count": 2, "language": "en"}).encode(),
    ))
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 204, "no stream is started for a client that is gone"
    assert not any(m.get("body") for m in sent if m["type"] == "http.response.body")
    assert attempts(quiz_db) == [] and items(quiz_db) == [], "no stub quiz in the student's history"
    assert calls == ["A"], "and the model is not asked for more"


# --- closing a quiz that was still being written --------------------------------------------------


def test_closing_a_half_written_quiz_settles_its_total_to_what_exists(world, quiz_db, model):
    """So 'See results' right after Stop can never report '1 of 5' for a quiz of 1."""
    model({"A": [good("A")], "B": [good("B")]})
    started = qc.begin_streamed_quiz(quiz_db, world["alice"], world["subject"].id, 5, "en")
    (attempt,) = attempts(quiz_db)
    assert attempt.total_questions == 5

    qc.complete_quiz(quiz_db, attempt)
    quiz_db.expire_all()
    assert attempt.total_questions == 1 and attempt.completed_at is not None
    assert qc.add_streamed_item(started.attempt_id, next(started.questions)) is None


def test_closing_an_ordinary_quiz_leaves_its_total_alone(client, world, quiz_db, model):
    model({"A": [good("A")], "B": [good("B")], "C": [good("C")]})
    c = login(client, world["alice"])
    body = {"subject_id": world["subject"].id, "question_count": 3, "language": "en"}
    attempt_id = c.post("/quiz/start", json=body).json()["attempt_id"]
    r = c.post(f"/quiz/{attempt_id}/complete")
    assert r.status_code == 200 and r.json()["total_questions"] == 3


def test_settling_first_or_closing_first_gives_the_same_total(world, quiz_db, model):
    model({"A": [good("A")], "B": [good("B")]})
    started = qc.begin_streamed_quiz(quiz_db, world["alice"], world["subject"].id, 5, "en")
    (attempt,) = attempts(quiz_db)
    qc.complete_quiz(quiz_db, attempt)              # the student gets there first...
    assert qc.finish_streamed_quiz(started.attempt_id, 5) == (1, True)   # ...then the stream settles
    quiz_db.expire_all()
    assert attempt.total_questions == 1
