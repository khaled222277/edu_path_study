"""QUIZ_MODEL_CHECK: an optional second model call that asks whether a question stands on its own.

Off by default (see eval/README.md for the measurement). It can only remove questions: an unreadable
reply or an outage of the check never empties a quiz.
"""

import json
import threading

import pytest

from app.config import settings
from app.services import quiz_service as qs
from app.services.llm import LLMUnavailableError

CHUNK = qs.SourceChunk(text="Clustering groups similar objects. " * 10, source_filename="dm.pdf", page_number=3,
                       content_type="text", lecture_id=1)
OPTIONS = ["Clustering", "Classification", "Regression", "Association"]
GOOD = json.dumps([{"question": "What groups similar objects together?", "options": OPTIONS, "answer": "Clustering"}])
OTHER = json.dumps([{"question": "Which task groups objects by similarity?", "options": OPTIONS, "answer": "Clustering"}])
PASS = '{"standalone": true, "supported": true}'
FAIL_STANDALONE = '{"standalone": false, "supported": true}'
FAIL_SUPPORT = '{"standalone": true, "supported": false}'


@pytest.fixture()
def check_on(monkeypatch):
    monkeypatch.setattr(settings, "quiz_model_check", True)


@pytest.fixture()
def model(monkeypatch):
    """Scripted model; records (is_check, options) for every call. The check is recognised by its prompt."""

    def install(responses):
        queue, calls = list(responses), []

        def fake(prompt, model_name, options=None):
            calls.append(("checking a multiple-choice quiz question" in prompt, options))
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(qs, "_ask_model", fake)
        return calls

    return install


def run(**kw):
    trace = qs.ChunkTrace("dm.pdf, page 3")
    return qs._generate_one(CHUNK, False, "m", trace, **kw), trace


# --- reading the reply ------------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ('{"standalone": true, "supported": true}', (True, True)),
    ('```json\n{"standalone": false, "supported": true}\n```', (False, True)),
    ('Sure! {"standalone": true, "supported": false} done', (True, False)),
    ('{"standalone": "yes", "supported": true}', None),
    ('{"standalone": true}', None),
    ("not json", None),
    ("", None),
])
def test_the_reply_is_read_or_declared_unreadable(raw, expected):
    assert qs.parse_model_check(raw) == expected


def test_the_check_prompt_carries_the_slide_the_question_the_options_and_the_marked_answer(monkeypatch):
    seen = []
    monkeypatch.setattr(qs, "_ask_model", lambda prompt, m, options=None: seen.append((prompt, options)) or PASS)
    reason, seconds = qs.model_check("SLIDE TEXT", "THE QUESTION?", OPTIONS, "Regression", "m")
    prompt, options = seen[0]
    assert "SLIDE TEXT" in prompt and "THE QUESTION?" in prompt and "Marked correct: C. Regression" in prompt and "A. Clustering" in prompt
    assert options == qs.CHECK_OPTIONS and options["temperature"] == 0, "deterministic and short"
    assert reason is None and seconds >= 0


# --- inside generation --------------------------------------------------------------------------------


def test_it_is_off_by_default_and_then_makes_no_extra_call(model):
    assert settings.quiz_model_check is False
    calls = model([GOOD])
    result, _ = run()
    assert result is not None and len(calls) == 1


def test_a_question_the_model_calls_not_standalone_is_rejected_and_retried(check_on, model):
    calls = model([GOOD, FAIL_STANDALONE, OTHER, PASS])
    result, trace = run()
    assert result.question_text == "Which task groups objects by similarity?"
    assert [a.outcome for a in trace.attempts] == [qs.REASON_MODEL_CHECK, qs.OUTCOME_ACCEPTED]
    assert [is_check for is_check, _ in calls] == [False, True, False, True]


def test_a_question_whose_answer_the_slide_does_not_support_is_rejected(check_on, model):
    model([GOOD, FAIL_SUPPORT, OTHER, PASS])
    result, trace = run()
    assert trace.attempts[0].outcome == qs.REASON_MODEL_CHECK and result is not None


def test_an_unreadable_check_lets_the_question_through(check_on, model):
    model([GOOD, "I cannot say"])
    result, trace = run()
    assert result is not None and [a.outcome for a in trace.attempts] == [qs.OUTCOME_ACCEPTED]


def test_the_check_is_only_asked_about_questions_the_rules_accepted(check_on, model):
    bad = json.dumps([{"question": "What is the concept being demonstrated?", "options": OPTIONS, "answer": "Clustering"}])
    calls = model([bad, GOOD, PASS])
    result, trace = run()
    assert [a.outcome for a in trace.attempts] == [qs.REASON_MISSING_CONTEXT, qs.OUTCOME_ACCEPTED]
    assert [c for c, _ in calls] == [False, False, True], "no check call for the item the rules already rejected"


def test_an_outage_during_the_check_surfaces_like_an_outage_of_generation(check_on, model):
    model([GOOD, LLMUnavailableError("down")])
    with pytest.raises(LLMUnavailableError):
        run()


def test_a_cancelled_quiz_makes_no_check_call(check_on, model, monkeypatch):
    cancel = threading.Event()
    calls = model([GOOD])
    original = qs._ask_model

    def set_then_return(prompt, m, options=None):
        out = original(prompt, m, options)
        cancel.set()  # the student pressed Stop while the question was being written
        return out

    monkeypatch.setattr(qs, "_ask_model", set_then_return)
    with pytest.raises(qs.GenerationCancelled):
        run(cancel=cancel)
    assert [c for c, _ in calls] == [False]


def test_the_check_time_is_counted_as_model_time_in_the_trace(check_on, model, monkeypatch):
    model([GOOD, PASS])
    monkeypatch.setattr(qs, "model_check", lambda *a: (None, 5.0))
    _, trace = run()
    assert trace.attempts[0].model_s >= 5.0
