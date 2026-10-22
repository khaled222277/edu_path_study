"""quiz_service retry flow: strict attempts, then relaxed attempts for Arabic only."""

import json

import pytest

from app.services import quiz_service as qs
from app.services.llm import LLMUnavailableError

CHUNK = qs.SourceChunk(
    text="x" * 300, source_filename="dm.pdf", page_number=3,
    content_type="text", lecture_id=1,
)

# Passes strict + relaxed (English).
GOOD_EN = json.dumps([{
    "question": "What groups similar objects together?",
    "options": ["Clustering", "Classification", "Regression", "Association"],
    "answer": "Clustering",
}])
# Valid Arabic.
GOOD_AR = json.dumps([{
    "question": "ما هي عملية تجميع الكائنات المتشابهة؟",
    "options": ["التجميع", "التصنيف", "الانحدار", "الارتباط"],
    "answer": "التجميع",
}], ensure_ascii=False)
# Arabic question with a bare Latin word: fails strict, passes relaxed.
LEAKY_AR = json.dumps([{
    "question": "ما هو مفهوم clustering في التنقيب؟",
    "options": ["التجميع", "التصنيف", "الانحدار", "الارتباط"],
    "answer": "التجميع",
}], ensure_ascii=False)
# Duplicate options: fails both strict and relaxed.
BAD = json.dumps([{
    "question": "What groups similar objects together?",
    "options": ["Clustering", "Clustering", "Regression", "Association"],
    "answer": "Clustering",
}])


@pytest.fixture()
def scripted(monkeypatch):
    """Replace the model call with a script; returns the list of prompts seen."""

    def install(responses, default=BAD):
        queue = list(responses)
        calls = []

        def fake_ask(prompt, model):
            calls.append(prompt)
            item = queue.pop(0) if queue else default
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(qs, "_ask_model", fake_ask)
        return calls

    return install


def test_accepts_first_valid_response_and_stops(scripted):
    calls = scripted([GOOD_EN])
    q = qs._generate_one(CHUNK, in_arabic=False, model="m")
    assert q is not None and q.options[q.correct_answer_index] == "Clustering"
    assert len(calls) == 1


def test_retries_strictly_until_a_valid_response(scripted):
    calls = scripted([BAD, "not json at all", "[]", GOOD_EN])
    q = qs._generate_one(CHUNK, in_arabic=False, model="m")
    assert q is not None
    assert len(calls) == 4


def test_english_gives_up_after_strict_attempts_only(scripted):
    calls = scripted([], default=BAD)
    assert qs._generate_one(CHUNK, in_arabic=False, model="m") is None
    assert len(calls) == qs.STRICT_ATTEMPTS  # no relaxed pass for English


def test_arabic_falls_back_to_relaxed_after_strict_exhausted(scripted):
    # 8 strict attempts all leak Latin, then the first relaxed attempt is accepted.
    calls = scripted([LEAKY_AR] * (qs.STRICT_ATTEMPTS + 1))
    q = qs._generate_one(CHUNK, in_arabic=True, model="m")
    assert q is not None
    assert "clustering" in q.question_text
    assert len(calls) == qs.STRICT_ATTEMPTS + 1


def test_arabic_strict_success_never_reaches_relaxed(scripted):
    calls = scripted([LEAKY_AR, LEAKY_AR, GOOD_AR])
    q = qs._generate_one(CHUNK, in_arabic=True, model="m")
    assert q is not None and q.question_text.startswith("ما هي")
    assert len(calls) == 3


def test_arabic_total_failure_uses_strict_plus_relaxed_attempts(scripted):
    calls = scripted([], default=BAD)
    assert qs._generate_one(CHUNK, in_arabic=True, model="m") is None
    assert len(calls) == qs.STRICT_ATTEMPTS + qs.RELAXED_ATTEMPTS


def test_relaxed_pass_still_rejects_non_arabic_questions(scripted):
    # English output for an Arabic quiz is never acceptable, even relaxed.
    calls = scripted([], default=GOOD_EN)
    assert qs._generate_one(CHUNK, in_arabic=True, model="m") is None
    assert len(calls) == qs.STRICT_ATTEMPTS + qs.RELAXED_ATTEMPTS


def test_letter_answers_are_mapped_to_option_text(scripted):
    letter = json.dumps([{
        "question": "What groups similar objects together?",
        "options": ["Clustering", "Classification", "Regression", "Association"],
        "answer": "B",
    }])
    scripted([letter])
    q = qs._generate_one(CHUNK, in_arabic=False, model="m")
    assert q is not None and q.options[q.correct_answer_index] == "Classification"


def test_ollama_outage_aborts_instead_of_burning_retries(scripted):
    calls = scripted([LLMUnavailableError("down")])
    with pytest.raises(LLMUnavailableError):
        qs._generate_one(CHUNK, in_arabic=True, model="m")
    assert len(calls) == 1


# --- generate_quiz ----------------------------------------------------------


def test_generate_quiz_returns_empty_when_subject_has_no_chunks(monkeypatch):
    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: [])
    assert qs.generate_quiz(1, 3, "en") == []


def test_generate_quiz_raises_when_every_chunk_fails(monkeypatch, scripted):
    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: [CHUNK, CHUNK])
    scripted([], default=BAD)
    with pytest.raises(qs.QuizGenerationError):
        qs.generate_quiz(1, 2, "en")


def test_generate_quiz_returns_partial_results(monkeypatch, scripted):
    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: [CHUNK, CHUNK])
    # First chunk: valid on attempt 1. Second chunk: never valid.
    scripted([GOOD_EN], default=BAD)
    questions = qs.generate_quiz(1, 2, "en")
    assert len(questions) == 1
