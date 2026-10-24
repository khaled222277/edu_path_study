"""A word in the wrong script is rejected, not silently deleted from the middle of a sentence."""

import json

import pytest

from app.services import quiz_service as qs

AR_OPTS = ["التجميع", "التصنيف", "الانحدار", "الارتباط"]
EN_OPTS = ["Clustering", "Classification", "Regression", "Association"]
CHUNK = qs.SourceChunk(text="x" * 300, source_filename="dm.pdf", page_number=3, content_type="text", lecture_id=1)


def reply(question, options, answer):
    return json.dumps([{"question": question, "options": options, "answer": answer}], ensure_ascii=False)


# --- what counts as a foreign script ------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("تمثيل مُ подроб", "подроб"),   # Cyrillic (a real generation)
        ("هو مهم ل避ANCE", "避"),          # Chinese (a real generation)
        ("한국어", "한국어"),
        ("हिन्दी", "हिन्दी"),
        ("ภาษาไทย", "ภาษาไทย"),
        ("שלום", "שלום"),
        ("ひらがな", "ひらがな"),
    ],
)
def test_other_scripts_are_detected(text, expected):
    found = qs.foreign_script_letters(text)
    assert found and set(found) <= set(expected)


@pytest.mark.parametrize(
    "text",
    [
        "ما هي عملية التجميع؟", "What is clustering?", "café déjà vu naïve",
        "learning rate (η) and (α, β, Σ)", "the weight 𝑊 in AdaBoost", "x² ≥ 3 × 10⁻⁶",
        "مُحَمَّد", "١٢٣ and 456", "y = 3x + 1", "", None, 5,
    ],
)
def test_arabic_latin_greek_digits_and_formulas_are_not(text):
    assert qs.foreign_script_letters(text) == []


# --- cleaning no longer corrupts what it keeps --------------------------------------------------


@pytest.mark.parametrize(
    "text, cleaned",
    [
        ("y = 3x - 1", "y = 3x - 1"),
        ("y = -3x + 1", "y = -3x + 1"),
        ("learning rate (η)", "learning rate (η)"),
        ("the 'Regression' component", "the 'Regression' component"),
        ("𝑊 in AdaBoost", "W in AdaBoost"),
        ("café", "café"),
        ("x ≥ 3", "x ≥ 3"),
    ],
)
def test_formulas_greek_quotes_and_accents_survive_cleaning(text, cleaned):
    assert qs.clean_text(text) == cleaned


@pytest.mark.parametrize("text, cleaned", [("ok 😀", "ok"), ("a\u0000b", "ab"), ("a ✓ b", "a b")])
def test_emoji_and_control_characters_are_still_dropped(text, cleaned):
    assert qs.clean_text(text) == cleaned


def test_an_accented_letter_in_arabic_text_counts_as_latin_leakage():
    assert qs.arabic_violation("التجميع é") == qs.REASON_LATIN


# --- items are rejected, with the right name -----------------------------------------------------------


@pytest.mark.parametrize(
    "item",
    [
        {"question": "ما هو التجميع подроб؟", "options": AR_OPTS, "answer": AR_OPTS[0]},
        {"question": "ما هو التجميع؟", "options": [*AR_OPTS[:3], "الارتباط 避"], "answer": AR_OPTS[0]},
        {"question": "ما هو التجميع؟", "options": AR_OPTS, "answer": "التجميع подроб"},
        {"question": "What groups objects 避?", "options": EN_OPTS, "answer": "Clustering"},
        {"q": "ما هو؟ подроб", "choices": AR_OPTS, "answer": AR_OPTS[0]},  # aliased keys too
    ],
)
def test_a_foreign_script_anywhere_in_the_item_rejects_it(item):
    assert qs._normalize_ex(item) == (None, qs.REASON_FOREIGN)
    assert qs._normalize(item) is None


def test_the_old_behaviour_would_have_let_the_garbled_question_through():
    """The point of the change: after deletion the question still looked valid."""
    garbled = "ما هو مفهوم التجميع في подроб التنقيب؟"
    cleaned_question = "ما هو مفهوم التجميع في التنقيب؟"
    assert qs.valid_item(cleaned_question, AR_OPTS, AR_OPTS[0], True), "what cleaning alone would have produced"
    assert qs._normalize_ex({"question": garbled, "options": AR_OPTS, "answer": AR_OPTS[0]})[0] is None


def test_a_clean_item_with_formulas_is_accepted_unchanged():
    item = {"question": "What is the line y = 3x + 1 called?", "options": ["Linear", "Convex", "Cyclic", "Sparse"], "answer": "Linear"}
    normalized, why = qs._normalize_ex(item)
    assert why is None and normalized[0] == "What is the line y = 3x + 1 called?"


def test_foreign_script_is_checked_before_the_option_count():
    item = {"question": "ما هو подроб؟", "options": AR_OPTS[:3], "answer": AR_OPTS[0]}
    assert qs._normalize_ex(item) == (None, qs.REASON_FOREIGN)


# --- in the generation loop --------------------------------------------------------------------------


@pytest.fixture()
def scripted(monkeypatch):
    def install(replies):
        queue = list(replies)
        monkeypatch.setattr(qs, "_ask_model", lambda prompt, model: queue.pop(0))

    return install


GOOD_AR = reply("ما هي عملية تجميع الكائنات المتشابهة؟", AR_OPTS, AR_OPTS[0])
BAD_AR = reply("ما هي عملية تجميع подроб الكائنات؟", AR_OPTS, AR_OPTS[0])


def test_a_rejected_reply_is_retried_and_named_in_the_trace(scripted):
    scripted([BAD_AR, GOOD_AR])
    trace = qs.ChunkTrace("c")
    question = qs._generate_one(CHUNK, True, "m", trace)
    assert question is not None and "подроб" not in question.question_text
    assert [a.outcome for a in trace.attempts] == [qs.REASON_FOREIGN, qs.OUTCOME_ACCEPTED]


def test_the_relaxed_arabic_phase_does_not_accept_it_either(scripted):
    """Relaxed drops the Latin-script rule only; a Cyrillic word is never acceptable."""
    scripted([BAD_AR] * (qs.STRICT_ATTEMPTS + qs.RELAXED_ATTEMPTS))
    trace = qs.ChunkTrace("c")
    assert qs._generate_one(CHUNK, True, "m", trace) is None
    assert {a.outcome for a in trace.attempts} == {qs.REASON_FOREIGN}


def test_english_quizzes_reject_it_too(scripted):
    scripted([reply("What groups 避 objects?", EN_OPTS, "Clustering"), reply("What groups objects?", EN_OPTS, "Clustering")])
    trace = qs.ChunkTrace("c")
    assert qs._generate_one(CHUNK, False, "m", trace) is not None
    assert trace.attempts[0].outcome == qs.REASON_FOREIGN


def test_every_arabic_block_survives_cleaning_and_is_not_foreign():
    """Letters from the Arabic Supplement (U+0750) and Extended-A (U+08A0) blocks are Arabic."""
    text = "ݐ ݑ ࢠ ࢡ مُحَمَّد"
    assert qs.clean_text(text) == text
    assert qs.foreign_script_letters(text) == []
