"""Ollama's "token repeat limit reached" aborts ONE generation; it is not an outage.

Ollama answers HTTP 500 {"error": "prediction aborted, token repeat limit reached"} when the model falls into a
loop. The quiz service used to treat every ollama.ResponseError as "Ollama is down", so one looping generation
ended the whole quiz (a 503 for the student). Now that one attempt is rejected like an unusable reply and the
next attempt or chunk goes on; real outages still abort with the clean 503.

The signal is narrow on purpose: status 500 AND the message. Any other 500 may mean the server really is in trouble.
"""

import logging

import httpx
import ollama
import pytest

from app.config import settings
from app.controllers import quiz_controller as qc
from app.services import quiz_service as qs
from app.services.llm import LLMUnavailableError, ModelRepeatLimitError, is_repeat_limit

from .conftest import login
from .test_error_handling import LEAK, _assert_clean
from .test_quiz_options import chunk, reply
from .test_quiz_stream import EN_OPTS, attempts, events, good, items, model, quiz_db, start  # noqa: F401  (fixtures)

LOOP = ollama.ResponseError("prediction aborted, token repeat limit reached", 500)
GOOD_EN = reply("What groups similar objects together?", EN_OPTS, "Clustering")
CHUNK = chunk("A")


def loop():
    return ollama.ResponseError("prediction aborted, token repeat limit reached", 500)


# --- the signal ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [
    ollama.ResponseError("prediction aborted, token repeat limit reached", 500),
    ollama.ResponseError("Prediction Aborted, Token Repeat Limit Reached", 500),
    ollama.ResponseError('{"error": "prediction aborted, token repeat limit reached"}', 500),
])
def test_ollamas_repeat_limit_abort_is_recognised(exc):
    assert is_repeat_limit(exc)


@pytest.mark.parametrize("exc", [
    ollama.ResponseError("model runner has unexpectedly stopped", 500),
    ollama.ResponseError("out of memory", 500),
    ollama.ResponseError("token repeat limit reached", 503),
    ollama.ResponseError("token repeat limit reached", 400),
    ollama.ResponseError("token repeat limit reached"),  # no status
    ollama.ResponseError("model 'x' not found", 404),
    ollama.RequestError("bad request"),
    ConnectionError("token repeat limit reached"),
    httpx.ReadTimeout("token repeat limit reached"),
    RuntimeError("token repeat limit reached"),
])
def test_anything_else_is_not(exc):
    assert not is_repeat_limit(exc)


# --- _ask_model: what becomes what ------------------------------------------------------------------------------


def test_the_repeat_limit_becomes_its_own_error_not_an_outage(fake_ollama):
    fake_ollama(default=loop())
    with pytest.raises(ModelRepeatLimitError) as caught:
        qs._ask_model("prompt", "m")
    assert not isinstance(caught.value, LLMUnavailableError)
    assert str(caught.value) == "", "carries no message; the cause is chained"
    assert isinstance(caught.value.__cause__, ollama.ResponseError)


@pytest.mark.parametrize("exc", [
    ConnectionError("refused"),
    httpx.ConnectError("refused"),
    httpx.ReadTimeout("slow"),
    ollama.ResponseError("model 'x' not found", 404),
    ollama.ResponseError("model runner has unexpectedly stopped", 500),
    ollama.ResponseError("service unavailable", 503),
])
def test_real_outages_are_still_unavailable(fake_ollama, exc):
    fake_ollama(default=exc)
    with pytest.raises(LLMUnavailableError):
        qs._ask_model("prompt", "m")


# --- one chunk ------------------------------------------------------------------------------------------------------


def test_a_repeat_limit_is_retried_and_the_next_attempt_can_succeed(fake_ollama):
    fake = fake_ollama(responses=[loop(), {"response": GOOD_EN}])
    trace = qs.ChunkTrace("A.pdf, page 1")
    result = qs._generate_one(CHUNK, False, "m", trace)
    assert result.question_text == "What groups similar objects together?"
    assert fake.calls == 2
    assert [a.outcome for a in trace.attempts] == [qs.REASON_REPEAT_LIMIT, qs.OUTCOME_ACCEPTED]
    assert trace.attempts[0].reasons == [qs.REASON_REPEAT_LIMIT]


def test_repeat_limits_count_toward_the_attempt_caps_and_then_the_chunk_gives_up(fake_ollama, monkeypatch):
    monkeypatch.setattr(qs, "STRICT_ATTEMPTS", 3)
    monkeypatch.setattr(qs, "RELAXED_ATTEMPTS", 2)
    fake = fake_ollama(default=loop())
    trace = qs.ChunkTrace("A.pdf, page 1")
    assert qs._generate_one(CHUNK, False, "m", trace) is None
    assert fake.calls == 3, "English: the strict cap only"
    assert [a.outcome for a in trace.attempts] == [qs.REASON_REPEAT_LIMIT] * 3

    fake = fake_ollama(default=loop())
    trace = qs.ChunkTrace("A.pdf, page 1")
    assert qs._generate_one(CHUNK, True, "m", trace) is None
    assert fake.calls == 5, "Arabic: strict cap then relaxed cap, never an unbounded loop"
    assert [a.phase for a in trace.attempts] == [qs.PHASE_STRICT] * 3 + [qs.PHASE_RELAXED] * 2


def test_the_log_has_the_class_and_the_code_but_never_the_prompt_or_the_message(fake_ollama, caplog):
    fake_ollama(responses=[loop(), {"response": GOOD_EN}])
    secret = chunk("Z")  # the prompt contains this text
    with caplog.at_level(logging.INFO):
        qs._generate_one(secret, False, "m", qs.ChunkTrace("Z.pdf, page 1"))
    lines = [r.getMessage() for r in caplog.records if "repeat limit" in r.getMessage()]
    assert len(lines) == 1
    assert "ModelRepeatLimitError (500)" in lines[0]
    assert "Z" * 50 not in caplog.text, "no prompt or lecture text"
    assert "prediction aborted" not in caplog.text, "not the message either"


# --- a whole quiz ---------------------------------------------------------------------------------------------------


@pytest.fixture()
def per_chunk(monkeypatch):
    """Script the model per chunk through the REAL client path: plan[tag] = list of replies (the last repeats)."""

    def install(plan):
        chunks = [chunk(t) for t in plan]
        monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: chunks)
        seen = {t: 0 for t in plan}
        calls = []

        class Client:
            def generate(self, **kw):
                tag = next(t for t in plan if t * 50 in kw["prompt"])
                replies = plan[tag]
                item = replies[min(seen[tag], len(replies) - 1)]
                seen[tag] += 1
                calls.append(tag)
                if isinstance(item, Exception):
                    raise item
                return {"response": item}

        monkeypatch.setattr(qs, "get_client", lambda *a, **k: Client())
        return calls

    return install


def test_a_chunk_that_keeps_looping_is_replaced_by_the_next_one(per_chunk):
    calls = per_chunk({"A": [loop()], "B": [good("B")], "C": [good("C")], "D": [good("D")]})
    questions = qs.generate_quiz(1, 2, "en")
    assert [q.source.source_filename for q in questions] == ["B.pdf", "C.pdf"]
    assert calls.count("A") >= 1 and "D" not in calls, "tried A, moved on, stopped once the quiz was full"


def test_when_only_some_chunks_work_the_quiz_is_short_not_failed(per_chunk):
    per_chunk({"A": [good("A")], "B": [loop()], "C": [loop()]})
    questions = qs.generate_quiz(1, 3, "en")
    assert [q.source.source_filename for q in questions] == ["A.pdf"]


def test_when_every_attempt_loops_the_quiz_fails_cleanly_as_no_valid_question(per_chunk, monkeypatch):
    monkeypatch.setattr(qs, "STRICT_ATTEMPTS", 2)
    calls = per_chunk({"A": [loop()], "B": [loop()]})
    trace = qs.QuizTrace()
    with pytest.raises(qs.QuizGenerationError):
        qs.generate_quiz(1, 2, "en", trace=trace)
    assert not issubclass(qs.QuizGenerationError, LLMUnavailableError), "not reported as an outage"
    assert trace.error == "no_valid_question"
    assert len(calls) == 4, "two attempts per chunk, then it stops"


def test_a_real_outage_still_aborts_at_once(per_chunk):
    calls = per_chunk({"A": [ConnectionError("refused")], "B": [good("B")]})
    with pytest.raises(LLMUnavailableError):
        qs.generate_quiz(1, 2, "en")
    assert calls == ["A"], "no retries, no second chunk"


def test_a_real_outage_after_a_repeat_limit_still_aborts(per_chunk):
    per_chunk({"A": [loop(), httpx.ConnectError("refused")], "B": [good("B")]})
    with pytest.raises(LLMUnavailableError):
        qs.generate_quiz(1, 2, "en")


def test_an_outage_after_some_questions_still_raises_with_the_questions_already_yielded(per_chunk):
    per_chunk({"A": [good("A")], "B": [loop(), ConnectionError("refused")]})
    gen = qs.iter_quiz(1, 2, "en")
    assert next(gen).source.source_filename == "A.pdf"
    with pytest.raises(LLMUnavailableError):
        next(gen)


# --- the routes ------------------------------------------------------------------------------------------------------

_CHUNK = qs.SourceChunk(text="x" * 300, source_filename="dm.pdf", page_number=1, content_type="text", lecture_id=1)


def _start(client, world):
    return login(client, world["alice"]).post("/quiz/start", json={"subject_id": world["subject"].id, "question_count": 1})


def test_plain_route_one_looping_generation_no_longer_gives_a_503(client, world, fake_ollama, monkeypatch):
    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: [_CHUNK] * 2)
    fake = fake_ollama(responses=[loop(), {"response": GOOD_EN}])
    r = _start(client, world)
    assert r.status_code == 201 and r.json()["total_questions"] == 1
    assert fake.calls == 2


def test_plain_route_every_generation_looping_is_the_clean_503(client, world, fake_ollama, monkeypatch):
    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: [_CHUNK])
    fake_ollama(default=loop())
    r = _start(client, world)
    _assert_clean(r, 503)
    assert r.json()["detail"] == qc.QUIZ_UNAVAILABLE_DETAIL


def test_plain_route_a_real_outage_is_the_clean_503_after_one_call(client, world, fake_ollama, monkeypatch):
    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: [_CHUNK] * 4)
    fake = fake_ollama(default=ConnectionError(LEAK))
    _assert_clean(_start(client, world), 503)
    assert fake.calls == 1


def test_stream_route_a_looping_chunk_keeps_the_questions_and_goes_on(client, world, quiz_db, model):
    model({"A": [good("A")], "B": [ModelRepeatLimitError()], "C": [good("C")]})
    evs = events(start(client, world, count=2))
    assert [e["type"] for e in evs] == ["start", "question", "question", "done"]
    assert evs[-1]["partial"] is False
    assert [i.question_text[-2] for i in items(quiz_db)] == ["A", "C"]


def test_stream_route_a_real_outage_after_a_question_keeps_it_and_ends_with_an_error_event(client, world, quiz_db, model):
    model({"A": [good("A")], "B": [LLMUnavailableError(LEAK)]})
    r = start(client, world, count=2)
    evs = events(r)
    assert [e["type"] for e in evs] == ["start", "question", "error"]
    assert evs[-1]["partial"] is True and LEAK not in r.text
    assert len(items(quiz_db)) == 1 and attempts(quiz_db)[0].total_questions == 1


def test_stream_route_every_chunk_looping_before_the_first_question_is_the_clean_503(client, world, quiz_db, model):
    model({"A": [ModelRepeatLimitError()], "B": [ModelRepeatLimitError()]})
    r = start(client, world)
    assert r.status_code == 503 and r.json()["detail"] == qc.QUIZ_UNAVAILABLE_DETAIL
    assert attempts(quiz_db) == []


# --- the optional model check ------------------------------------------------------------------------------------------


def test_a_repeat_limit_during_the_model_check_lets_the_question_through(fake_ollama, monkeypatch):
    monkeypatch.setattr(settings, "quiz_model_check", True)
    fake = fake_ollama(responses=[{"response": GOOD_EN}, loop()])
    result = qs._generate_one(CHUNK, False, "m", qs.ChunkTrace("A.pdf, page 1"))
    assert result is not None and fake.calls == 2


# --- chat: what it does today (not changed here) --------------------------------------------------------------------------


def test_chat_a_repeat_limit_is_the_generic_503_and_nothing_is_stored(client, db, world, fake_ollama, monkeypatch):
    """Chat makes one model call per turn and has no multi-step job to lose; the student just asks again."""
    from app.models import ChatMessage, ChatSession

    session = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(session)
    db.commit()
    monkeypatch.setattr("app.controllers.chat_controller.retrieve", lambda q, s: [])
    fake_ollama(default=loop())
    r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": "hello"})
    _assert_clean(r, 503)
    assert db.query(ChatMessage).count() == 0
