"""Quiz questions must stand on their own: no pointer at a slide, a figure or "this", and a real question form.

Observed in an Arabic KDD quiz: "هذا الفكرة هي" ("this idea is") with four unrelated options. These rules
(no model call) reject such items; the generator then retries like for any other rejection.
"""

import json

import pytest

from app.config import settings
from app.services import quiz_service as qs

REJECTED_EN = [
    ("What is being demonstrated in this code snippet?", qs.REASON_MISSING_CONTEXT),
    ("What is the concept being demonstrated?", qs.REASON_MISSING_CONTEXT),
    ("What concept is visually demonstrated by dividing space into two areas?", qs.REASON_MISSING_CONTEXT),
    ("What type of relationship is shown between X and Y?", qs.REASON_MISSING_CONTEXT),
    ("What is the goal of clustering in this concept?", qs.REASON_MISSING_CONTEXT),
    ("What is the purpose of the table in the given data?", qs.REASON_MISSING_CONTEXT),
    ("How are conditional probabilities calculated in this example?", qs.REASON_MISSING_CONTEXT),
    ("What is this?", qs.REASON_MISSING_CONTEXT),
    ("What does it mean?", qs.REASON_MISSING_CONTEXT),
    ("What is the term used here?", qs.REASON_MISSING_CONTEXT),
    ("What does the word mean in this context?", qs.REASON_MISSING_CONTEXT),
    ("A decision tree is a model that predicts", qs.REASON_NO_QUESTION_FORM),
    ("Entropy", qs.REASON_TOO_FEW_WORDS),
    ("What is?", qs.REASON_TOO_FEW_WORDS),
]
ACCEPTED_EN = [
    "What is the primary goal of regression in a prediction task?",
    "Which of the following is a type of attribute?",
    "Which of the following formulas gives the entropy of a set?",
    "What is the y-intercept in the context of linear regression?",
    "What does it mean when the entropy of a set is zero?",
    "How is a graph represented in memory?",
    "What is the purpose of organizing data in a table?",
    "Define entropy in a decision tree",
    "A decision tree is ______ .",
    "Is a decision tree a classifier",
    "In which situation is z-score normalization useful?",
]
REJECTED_AR = [
    ("هذا الفكرة هي", qs.REASON_MISSING_CONTEXT),
    ("ما هذا؟", qs.REASON_TOO_FEW_WORDS),
    ("ما هو هذا المفهوم الرئيسي؟", qs.REASON_MISSING_CONTEXT),
    ("ما يرمز إليه كل عمود في هذا التمثيل البياني؟", qs.REASON_MISSING_CONTEXT),
    ("ما هي العلاقة بين المجموعات المذكورة في الجدول؟", qs.REASON_MISSING_CONTEXT),
    ("ما هي العملية التي يتم تنفيذها في هذا الكود؟", qs.REASON_MISSING_CONTEXT),
    ("ما هو مصطلح السبك المستخدم هنا؟", qs.REASON_MISSING_CONTEXT),
    ("ما هو معنى كلمة مشكلة في هذا السياق؟", qs.REASON_MISSING_CONTEXT),
    ("ما هي فكرة هذا الخوارزمية؟", qs.REASON_DEMONSTRATIVE_AGREEMENT),
    ("تظهر العلاقة بين حجم المنزل ومميزاته", qs.REASON_NO_QUESTION_FORM),
    ("المفارقة التي تعرف الوظيفة المكتسبة هي", qs.REASON_NO_QUESTION_FORM),
]
ACCEPTED_AR = [
    "ما هو التصنيف؟",
    "ما هي خطوات عملية اكتشاف المعرفة من قواعد البيانات؟",
    "كيف تعمل خوارزمية k-means؟",
    "أي مما يلي يعد نوعا من الخصائص؟",
    "في أي مرحلة يتم تنظيف البيانات؟",
    "من هو مخترع الشجرة",
    "اشرح لي تقنيات التصنيف المذكورة في المحاضرات",
    "لماذا يجمع الكثير من البيانات حاليًا؟",
    "عرّف الإنتروبيا في شجرة القرار",
    "ما هو الفرق بين هذه الخوارزميات؟",
]


@pytest.fixture(autouse=True)
def rules_on(monkeypatch):
    monkeypatch.setattr(settings, "quiz_answerability_rules", True)


@pytest.mark.parametrize("question,reason", REJECTED_EN)
def test_english_questions_that_cannot_stand_alone_are_rejected(question, reason):
    assert qs.answerability_reason(question, False) == reason, question


@pytest.mark.parametrize("question", ACCEPTED_EN)
def test_ordinary_english_questions_are_accepted(question):
    assert qs.answerability_reason(question, False) is None, question


@pytest.mark.parametrize("question,reason", REJECTED_AR)
def test_arabic_questions_that_cannot_stand_alone_are_rejected(question, reason):
    assert qs.answerability_reason(question, True) == reason, question


@pytest.mark.parametrize("question", ACCEPTED_AR)
def test_ordinary_arabic_questions_are_accepted(question):
    assert qs.answerability_reason(question, True) is None, question


def test_diacritics_do_not_hide_a_demonstrative():
    assert qs.answerability_reason("ما هو الغرض من هَذِهِ الفِكْرَةِ الرئيسية؟", True) == qs.REASON_MISSING_CONTEXT


# --- inside the validators -----------------------------------------------------------------------

OPTIONS = ["إلهام الإبداع", "تكنولوجيا الهواتف", "تقنيات الاتصالات", "تجربة الابتكار"]


def test_the_observed_item_is_rejected_by_both_the_strict_and_the_relaxed_rules():
    assert qs.reject_reason("هذا الفكرة هي", OPTIONS, OPTIONS[0], True) == qs.REASON_MISSING_CONTEXT
    assert qs.relaxed_reject_reason("هذا الفكرة هي", OPTIONS, OPTIONS[0]) == qs.REASON_MISSING_CONTEXT


def test_an_english_item_is_rejected_by_the_strict_rules():
    opts = ["Clustering", "Classification", "Regression", "Association"]
    assert qs.reject_reason("What is the concept being demonstrated?", opts, "Clustering", False) == qs.REASON_MISSING_CONTEXT
    assert qs.reject_reason("What groups similar objects together?", opts, "Clustering", False) is None


def test_the_setting_turns_the_rules_off(monkeypatch):
    monkeypatch.setattr(settings, "quiz_answerability_rules", False)
    assert qs.reject_reason("هذا الفكرة هي ما", OPTIONS, OPTIONS[0], True) is None


def test_existing_checks_still_come_first_and_are_not_loosened():
    dup = ["a", "a", "b", "c"]
    assert qs.reject_reason("What is the concept being demonstrated?", dup, "a", False) == qs.REASON_DUPLICATES
    assert qs.reject_reason("What does the diagram show about clustering?", ["Groups", "Trees", "Rules", "Lines"], "Groups", False) == qs.REASON_FIGURE
    assert qs.reject_reason("ما هو التصنيف؟ clustering", OPTIONS, OPTIONS[0], True) == qs.REASON_LATIN


# --- retried like any other rejection ---------------------------------------------------------------

CHUNK = qs.SourceChunk(text="x" * 300, source_filename="dm.pdf", page_number=3, content_type="text", lecture_id=1)


def reply(question, options=("Clustering", "Classification", "Regression", "Association"), answer="Clustering"):
    return json.dumps([{"question": question, "options": list(options), "answer": answer}], ensure_ascii=False)


@pytest.fixture()
def scripted(monkeypatch):
    def install(responses):
        queue = list(responses)
        monkeypatch.setattr(qs, "_ask_model", lambda prompt, model: queue.pop(0))
        return queue

    return install


def test_a_rejected_question_is_retried_and_the_next_good_one_is_used(scripted):
    scripted([reply("What is the concept being demonstrated?"), reply("What groups similar objects together?")])
    trace = qs.ChunkTrace("dm.pdf, page 3")
    result = qs._generate_one(CHUNK, False, "m", trace)
    assert result.question_text == "What groups similar objects together?"
    assert [a.outcome for a in trace.attempts] == [qs.REASON_MISSING_CONTEXT, qs.OUTCOME_ACCEPTED]


def test_an_arabic_item_rejected_in_both_phases_gives_nothing(scripted, monkeypatch):
    monkeypatch.setattr(qs, "STRICT_ATTEMPTS", 2)
    monkeypatch.setattr(qs, "RELAXED_ATTEMPTS", 2)
    bad = reply("هذا الفكرة هي", OPTIONS, OPTIONS[0])
    scripted([bad] * 4)
    trace = qs.ChunkTrace("dm.pdf, page 3")
    assert qs._generate_one(CHUNK, True, "m", trace) is None
    assert [a.phase for a in trace.attempts] == [qs.PHASE_STRICT] * 2 + [qs.PHASE_RELAXED] * 2
    assert {a.outcome for a in trace.attempts} == {qs.REASON_MISSING_CONTEXT}
