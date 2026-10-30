"""scripts/quiz_checks.py: the rules that score generated quiz items for the answerability evaluation."""

import pytest

from scripts.quiz_checks import chunk_features, classify

SELF_CONTAINED = [
    ("What is the primary goal of regression in a prediction task?", "en"),
    ("Which of the following is a type of attribute?", "en"),
    ("What is the y-intercept in the context of linear regression?", "en"),
    ("ما هي خطوات عملية اكتشاف المعرفة من قواعد البيانات؟", "ar"),
    ("كيف تعمل خوارزمية k-means؟", "ar"),
]
DEICTIC = [
    ("What is being demonstrated in this code snippet?", "en"),
    ("What is the concept being demonstrated?", "en"),
    ("What is the purpose of the table in the given data?", "en"),
    ("What is this?", "en"),
    ("ما يرمز إليه كل عمود في هذا التمثيل البياني؟", "ar"),
    ("ما هي العلاقة بين المجموعات المذكورة في الجدول؟", "ar"),
    ("هذا الفكرة هي", "ar"),
]


@pytest.mark.parametrize("q,lang", SELF_CONTAINED)
def test_a_self_contained_question_is_not_flagged(q, lang):
    assert classify(q, lang)["self_contained"], q


@pytest.mark.parametrize("q,lang", DEICTIC)
def test_a_question_that_points_at_something_missing_is_flagged(q, lang):
    assert classify(q, lang)["deictic"], q


def test_the_observed_arabic_question_is_deictic_fragment_and_grammar_suspect():
    flags = classify("هذا الفكرة هي", "ar")
    assert flags["deictic"] and flags["fragment"] and flags["grammar"]


def test_a_statement_without_question_form_is_a_fragment():
    assert classify("تظهر العلاقة بين حجم المنزل ومميزاته", "ar")["fragment"]
    assert not classify("ما هو الإنتروبيا وكيف تحسب؟", "ar")["fragment"]


def test_chunk_features():
    f = chunk_features("Agenda\nSome words here", "image_description")
    assert f["figure"] and f["title_like"] and f["short"] and f["low_info"]
    g = chunk_features("word " * 120, "text")
    assert not g["figure"] and not g["low_info"] and g["alpha_share"] == 1.0
