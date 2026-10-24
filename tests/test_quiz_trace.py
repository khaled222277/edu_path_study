"""Quiz instrumentation: reasons are named, decisions are unchanged, timings are recorded."""

import itertools
import json

import httpx
import pytest

from app.services import quiz_service as qs
from app.services.llm import LLMUnavailableError

# --- the old rules, verbatim, as the reference the refactor must match ---------


def old_valid_item(question, options, answer, in_arabic):
    if not question or len(question) < 3:
        return False
    if len(options) != 4:
        return False
    if len({o.strip() for o in options}) != 4:
        return False
    if any(qs.is_bad_placeholder(o) for o in options):
        return False
    if answer not in options:
        return False
    if qs.references_unseen_source(question):
        return False
    if in_arabic:
        if not old_is_all_arabic(question):
            return False
        if any(not old_is_all_arabic(o) for o in options):
            return False
        if not old_is_all_arabic(answer):
            return False
    return True


def old_is_all_arabic(text):
    value = str(text or "")
    if not qs.contains_arabic(value):
        return False
    if qs._LATIN_RE.search(qs._BRACKETED_RE.sub("", value)):
        return False
    for segment in qs._BRACKETED_CAPTURE_RE.findall(value):
        if qs._LATIN_RE.search(segment) and len(segment.strip()) > qs.MAX_BRACKETED_LATIN:
            return False
    return True


def old_relaxed_ok(question, options, answer):
    return (
        bool(question)
        and qs.contains_arabic(question)
        and not any(qs.is_bad_placeholder(o) for o in options)
        and len({o.strip() for o in options}) == 4
        and answer in options
        and not qs.references_unseen_source(question)
    )


QUESTIONS = [
    "", "ab", "What is clustering?", "ما هو التجميع؟", "ما هو clustering؟",
    "What does this diagram show?", "ما الذي يوضحه هذا الشكل؟", "ما هي لغة (SQL) هنا؟",
    "ما هو (a very long english clause) هنا؟", "123456", "ما هو التجميع 42؟",
]
OPTION_SETS = [
    ["التجميع", "التصنيف", "الانحدار", "الارتباط"],
    ["Clustering", "Classification", "Regression", "Association"],
    ["التجميع", "التصنيف", "الانحدار", "التجميع"],  # duplicate
    ["التجميع", "التصنيف", "الانحدار", " التجميع "],  # duplicate after strip
    ["التجميع", "التصنيف", "الانحدار", "Option 4"],  # placeholder
    ["التجميع", "التصنيف", "الانحدار", "الخيار 3"],  # arabic placeholder
    ["التجميع", "التصنيف", "الانحدار"],  # three
    ["التجميع", "التصنيف", "الانحدار", "الارتباط", "الخامس"],  # five
    ["التجميع (SQL)", "التصنيف", "الانحدار", "الارتباط"],  # short bracket latin
    ["التجميع (a long english clause)", "التصنيف", "الانحدار", "الارتباط"],
    ["1", "2", "3", "4"],
    ["التجميع", "التصنيف", "Regression الانحدار", "الارتباط"],
]


def corpus():
    for q, opts, in_ar in itertools.product(QUESTIONS, OPTION_SETS, (False, True)):
        for answer in (opts[0] if opts else "", "not an option"):
            yield q, opts, answer, in_ar


def test_refactored_strict_decision_matches_the_old_rules_everywhere():
    cases = list(corpus())
    assert len(cases) > 500
    for q, opts, ans, in_ar in cases:
        assert qs.valid_item(q, opts, ans, in_ar) == old_valid_item(q, opts, ans, in_ar), (q, opts, ans, in_ar)
        assert (qs.reject_reason(q, opts, ans, in_ar) is None) == old_valid_item(q, opts, ans, in_ar)


def test_refactored_relaxed_decision_matches_the_old_rules_everywhere():
    for q, opts, ans, _ in corpus():
        assert (qs.relaxed_reject_reason(q, opts, ans) is None) == old_relaxed_ok(q, opts, ans), (q, opts, ans)


def test_is_all_arabic_required_matches_the_old_function():
    texts = QUESTIONS + [o for s in OPTION_SETS for o in s] + [None, 5]
    for t in texts:
        assert qs.is_all_arabic_required(t, True) == old_is_all_arabic(t), t
        assert qs.is_all_arabic_required(t, False) is True


# --- every rejection has the right name ----------------------------------------

AR_Q = "ما هي عملية تجميع الكائنات المتشابهة؟"
AR = ["التجميع", "التصنيف", "الانحدار", "الارتباط"]


@pytest.mark.parametrize(
    "question, options, answer, in_ar, reason",
    [
        ("", AR, AR[0], True, qs.REASON_SHORT_QUESTION),
        (AR_Q, AR[:3], AR[0], True, qs.REASON_OPTION_COUNT),
        (AR_Q, [*AR[:3], AR[0]], AR[0], True, qs.REASON_DUPLICATES),
        (AR_Q, [*AR[:3], "Option 4"], AR[0], True, qs.REASON_PLACEHOLDER),
        (AR_Q, AR, "غير موجود", True, qs.REASON_ANSWER_MISSING),
        ("What does this diagram show?", ["a1", "b2", "c3", "d4"], "a1", False, qs.REASON_FIGURE),
        ("ما هو مفهوم clustering؟", AR, AR[0], True, qs.REASON_LATIN),
        (AR_Q, [*AR[:3], "Regression الارتباط"], "التجميع", True, qs.REASON_LATIN),
        (AR_Q, ["1", "2", "3", "4"], "1", True, qs.REASON_NO_ARABIC),
    ],
)
def test_each_strict_rejection_is_named(question, options, answer, in_ar, reason):
    assert qs.reject_reason(question, options, answer, in_ar) == reason


def test_relaxed_rejections_are_named():
    assert qs.relaxed_reject_reason("What is X?", AR, AR[0]) == qs.REASON_NO_ARABIC
    assert qs.relaxed_reject_reason(AR_Q, [*AR[:3], AR[0]], AR[0]) == qs.REASON_DUPLICATES
    assert qs.relaxed_reject_reason("ما هو clustering؟", AR, AR[0]) is None  # latin tolerated here


@pytest.mark.parametrize(
    "item, reason",
    [
        ("just a string", qs.REASON_BAD_ITEM),
        ({"question": "q", "options": "not a list", "answer": "a"}, qs.REASON_OPTION_COUNT),
        ({"question": "q", "options": ["a", "b", "c"], "answer": "a"}, qs.REASON_OPTION_COUNT),
        ({"question": "q", "options": ["a1", "b2", "c3", "d4"], "answer": "zzz"}, qs.REASON_ANSWER_MISSING),
    ],
)
def test_normalisation_failures_are_named(item, reason):
    assert qs._normalize_ex(item) == (None, reason)
    assert qs._normalize(item) is None


def test_normalise_still_maps_a_letter_answer():
    out, why = qs._normalize_ex({"question": "q?", "options": ["a1", "b2", "c3", "d4"], "answer": "C"})
    assert why is None and out[2] == "c3"


# --- the trace ---------------------------------------------------------------------

CHUNK = qs.SourceChunk(text="x" * 300, source_filename="dm.pdf", page_number=3, content_type="text", lecture_id=1)
GOOD_EN = json.dumps([{"question": "What groups similar objects?", "options": ["Clustering", "Classification", "Regression", "Association"], "answer": "Clustering"}])
LEAKY_AR = json.dumps([{"question": "ما هو مفهوم clustering؟", "options": AR, "answer": AR[0]}], ensure_ascii=False)
GOOD_AR = json.dumps([{"question": AR_Q, "options": AR, "answer": AR[0]}], ensure_ascii=False)
DUP = json.dumps([{"question": "What groups similar objects?", "options": ["Clustering", "Clustering", "Regression", "Association"], "answer": "Clustering"}])


@pytest.fixture()
def scripted(monkeypatch):
    def install(responses, default=DUP):
        queue = list(responses)

        def fake(prompt, model):
            item = queue.pop(0) if queue else default
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(qs, "_ask_model", fake)

    return install


def test_every_reply_becomes_one_attempt_with_its_outcome(scripted):
    scripted(["not json", "[]", DUP, GOOD_EN])
    trace = qs.ChunkTrace("c")
    assert qs._generate_one(CHUNK, False, "m", trace) is not None
    assert [a.outcome for a in trace.attempts] == [
        qs.REASON_MALFORMED, qs.REASON_EMPTY, qs.REASON_DUPLICATES, qs.OUTCOME_ACCEPTED,
    ]
    assert trace.calls == 4 and trace.accepted and trace.accepted_phase == qs.PHASE_STRICT
    assert all(a.phase == qs.PHASE_STRICT for a in trace.attempts)


def test_timings_are_recorded_and_split_between_model_and_checking(scripted, monkeypatch):
    import time as _time

    def slow(prompt, model):
        _time.sleep(0.05)
        return GOOD_EN

    monkeypatch.setattr(qs, "_ask_model", slow)
    trace = qs.ChunkTrace("c")
    qs._generate_one(CHUNK, False, "m", trace)
    (a,) = trace.attempts
    assert a.model_s >= 0.05 and a.check_s < a.model_s
    assert trace.wall_s >= a.model_s


def test_arabic_trace_shows_the_strict_to_relaxed_switch(scripted):
    scripted([LEAKY_AR] * (qs.STRICT_ATTEMPTS + 1))
    trace = qs.ChunkTrace("c")
    assert qs._generate_one(CHUNK, True, "m", trace) is not None
    phases = [a.phase for a in trace.attempts]
    assert phases == [qs.PHASE_STRICT] * qs.STRICT_ATTEMPTS + [qs.PHASE_RELAXED]
    assert [a.outcome for a in trace.attempts[:-1]] == [qs.REASON_LATIN] * qs.STRICT_ATTEMPTS
    assert trace.attempts[-1].outcome == qs.OUTCOME_ACCEPTED and trace.accepted_phase == qs.PHASE_RELAXED


def test_a_failed_chunk_is_traced_too(scripted):
    scripted([], default=DUP)
    trace = qs.ChunkTrace("c")
    assert qs._generate_one(CHUNK, False, "m", trace) is None
    assert trace.calls == qs.STRICT_ATTEMPTS and not trace.accepted and trace.accepted_phase is None


@pytest.mark.parametrize(
    "cause, kind",
    [(httpx.ReadTimeout("slow"), "timeout"), (ConnectionError("down"), "unavailable")],
)
def test_outages_are_traced_as_timeout_or_unavailable(monkeypatch, cause, kind):
    def boom(prompt, model):
        raise LLMUnavailableError("x") from cause

    monkeypatch.setattr(qs, "_ask_model", boom)
    trace = qs.ChunkTrace("c")
    with pytest.raises(LLMUnavailableError):
        qs._generate_one(CHUNK, False, "m", trace)
    assert [a.outcome for a in trace.attempts] == [kind]


def test_quiz_trace_totals_and_sampling_time(monkeypatch, scripted):
    import time as _time

    def sample(*a, **k):
        _time.sleep(0.03)
        return [CHUNK, CHUNK]

    monkeypatch.setattr(qs, "sample_chunks", sample)
    scripted([GOOD_EN, DUP, GOOD_EN])
    trace = qs.QuizTrace()
    out = qs.generate_quiz(1, 2, "en", trace=trace)
    assert len(out) == 2 and trace.produced == 2 and trace.requested == 2
    assert trace.language == "en" and trace.error is None
    assert trace.sampling_s >= 0.03
    assert [c.calls for c in trace.chunks] == [1, 2]
    assert trace.total_s >= trace.sampling_s


def test_quiz_trace_keeps_partial_data_when_the_model_goes_away(monkeypatch, scripted):
    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: [CHUNK, CHUNK])
    scripted([GOOD_EN, LLMUnavailableError("down")])
    trace = qs.QuizTrace()
    with pytest.raises(LLMUnavailableError):
        qs.generate_quiz(1, 2, "en", trace=trace)
    assert trace.error == "unavailable" and len(trace.chunks) == 2
    assert trace.chunks[0].accepted and not trace.chunks[1].accepted


def test_quiz_trace_marks_total_failure(monkeypatch, scripted):
    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: [CHUNK])
    scripted([], default=DUP)
    trace = qs.QuizTrace()
    with pytest.raises(qs.QuizGenerationError):
        qs.generate_quiz(1, 1, "en", trace=trace)
    assert trace.error == "no_valid_question"


def test_tracing_does_not_change_the_result(monkeypatch, scripted):
    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: [CHUNK, CHUNK])
    replies = [DUP, GOOD_EN, GOOD_EN]
    scripted(list(replies))
    plain = qs.generate_quiz(1, 2, "en")
    scripted(list(replies))
    traced = qs.generate_quiz(1, 2, "en", trace=qs.QuizTrace())
    assert [(q.question_text, q.options, q.correct_answer_index) for q in plain] == [
        (q.question_text, q.options, q.correct_answer_index) for q in traced
    ]


def test_structured_log_lines_are_valid_json(monkeypatch, scripted, caplog):
    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: [CHUNK])
    scripted([DUP, GOOD_EN])
    with caplog.at_level("INFO"):
        qs.generate_quiz(1, 1, "en")
    lines = {
        m.split(" ", 1)[0]: json.loads(m.split(" ", 1)[1])
        for m in (r.getMessage() for r in caplog.records)
        if m.startswith(("quiz_item ", "quiz_summary "))
    }
    assert lines["quiz_item"]["calls"] == 2 and lines["quiz_item"]["outcomes"] == [qs.REASON_DUPLICATES, qs.OUTCOME_ACCEPTED]
    assert lines["quiz_summary"]["produced"] == 1 and lines["quiz_summary"]["calls"] == 2
