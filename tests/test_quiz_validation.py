"""Quiz item validation: the rules that catch bad model output."""

import pytest

from app.services.quiz_service import (
    MAX_BRACKETED_LATIN,
    is_all_arabic_required,
    is_bad_placeholder,
    valid_item,
)

AR_OPTS = ["التجميع", "التصنيف", "الانحدار", "الارتباط"]
EN_OPTS = ["Clustering", "Classification", "Regression", "Association"]


def test_valid_english_item():
    assert valid_item("What groups similar objects?", EN_OPTS, "Clustering", False)


def test_valid_arabic_item():
    assert valid_item("ما هي عملية تجميع الكائنات المتشابهة؟", AR_OPTS, "التجميع", True)


# --- Latin-script leakage in Arabic items ---------------------------------


def test_arabic_question_with_latin_word_is_rejected():
    assert not valid_item("ما هو مفهوم clustering في التنقيب؟", AR_OPTS, "التجميع", True)


def test_arabic_option_with_latin_word_is_rejected():
    opts = ["التجميع", "التصنيف", "Regression الانحدار", "الارتباط"]
    assert not valid_item("ما هي عملية تجميع الكائنات المتشابهة؟", opts, "التجميع", True)


def test_arabic_answer_with_latin_is_rejected():
    opts = ["التجميع", "التصنيف", "الانحدار", "الارتباط (SQL)"]
    # The bracketed term is tolerated in an option...
    assert valid_item("ما هي لغة الاستعلام المستخدمة؟", opts, "الارتباط (SQL)", True)
    # ...but not when it is unbracketed.
    opts = ["التجميع", "التصنيف", "الانحدار", "SQL الارتباط"]
    assert not valid_item("ما هي لغة الاستعلام المستخدمة؟", opts, "SQL الارتباط", True)


def test_short_bracketed_latin_is_tolerated_but_long_is_not():
    assert is_all_arabic_required("قاعدة بيانات (SQL)", True)
    long_term = "(" + "a" * (MAX_BRACKETED_LATIN + 1) + ")"
    assert not is_all_arabic_required(f"قاعدة بيانات {long_term}", True)


def test_purely_latin_text_is_rejected_for_arabic():
    assert not is_all_arabic_required("Clustering", True)


def test_english_quiz_does_not_apply_script_rule():
    assert is_all_arabic_required("Clustering", False)


# --- placeholder options ---------------------------------------------------


@pytest.mark.parametrize(
    "text", ["Option 4", "option4", "Choice 2", "الخيار 3", "خيار 1", "", "   ", None]
)
def test_placeholder_options_are_flagged(text):
    assert is_bad_placeholder(text)


@pytest.mark.parametrize("text", ["Option pricing model", "Clustering", "الخيار الأفضل"])
def test_real_options_are_not_placeholders(text):
    assert not is_bad_placeholder(text)


def test_item_with_placeholder_option_is_rejected():
    opts = ["Clustering", "Classification", "Regression", "Option 4"]
    assert not valid_item("What groups similar objects?", opts, "Clustering", False)


# --- option count / duplicates --------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 2, 3, 5])
def test_wrong_option_count_is_rejected(count):
    opts = [f"Distinct answer {i}" for i in range(count)]
    answer = opts[0] if opts else "x"
    assert not valid_item("What groups similar objects?", opts, answer, False)


def test_duplicate_options_are_rejected():
    opts = ["Clustering", "Classification", "Regression", "Clustering"]
    assert not valid_item("What groups similar objects?", opts, "Clustering", False)


def test_duplicates_differing_only_by_whitespace_are_rejected():
    opts = ["Clustering", "Classification", "Regression", " Clustering "]
    assert not valid_item("What groups similar objects?", opts, "Clustering", False)


# --- other gates -----------------------------------------------------------


def test_answer_must_be_one_of_the_options():
    assert not valid_item("What groups similar objects?", EN_OPTS, "Sorting", False)


def test_too_short_question_is_rejected():
    assert not valid_item("?", EN_OPTS, "Clustering", False)


@pytest.mark.parametrize(
    "question",
    ["What does this diagram show?", "As shown in the figure, what is X?",
     "ما الذي يوضحه هذا الشكل؟"],
)
def test_questions_pointing_at_unseen_source_are_rejected(question):
    assert not valid_item(question, EN_OPTS, "Clustering", False)
