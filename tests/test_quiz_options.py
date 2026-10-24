"""Quiz settings, and the two-pass chunk budget (give up on a hard chunk early)."""

import json

import pytest

from app.config import Settings, settings
from app.services import quiz_service as qs
from app.services.llm import LLMUnavailableError

AR_OPTS = ["التجميع", "التصنيف", "الانحدار", "الارتباط"]
AR_Q = "ما هي عملية تجميع الكائنات المتشابهة؟"
EN_OPTS = ["Clustering", "Classification", "Regression", "Association"]


def reply(question, options, answer):
    return json.dumps([{"question": question, "options": options, "answer": answer}], ensure_ascii=False)


GOOD_AR = reply(AR_Q, AR_OPTS, AR_OPTS[0])
LEAKY_AR = reply("ما هو مفهوم clustering؟", AR_OPTS, AR_OPTS[0])  # strict rejects, relaxed accepts
GOOD_EN = reply("What groups similar objects?", EN_OPTS, "Clustering")
DUP_EN = reply("What groups similar objects?", ["Clustering", "Clustering", "Regression", "Association"], "Clustering")


def chunk(tag):
    """A chunk whose text identifies it inside the prompt the model is sent."""
    return qs.SourceChunk(
        text=(tag * 300), source_filename=f"{tag}.pdf", page_number=1, content_type="text", lecture_id=ord(tag)
    )


A, B, C = chunk("A"), chunk("B"), chunk("C")


# --- settings -----------------------------------------------------------------


def test_defaults():
    s = Settings(_env_file=None)
    assert (s.quiz_temperature, s.quiz_strict_attempts, s.quiz_relaxed_attempts) == (0.5, 8, 4)
    assert s.quiz_first_pass_attempts == 2


def test_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("QUIZ_TEMPERATURE", "0.3")
    monkeypatch.setenv("QUIZ_STRICT_ATTEMPTS", "5")
    monkeypatch.setenv("QUIZ_FIRST_PASS_ATTEMPTS", "3")
    s = Settings(_env_file=None)
    assert (s.quiz_temperature, s.quiz_strict_attempts, s.quiz_first_pass_attempts) == (0.3, 5, 3)


@pytest.mark.parametrize(
    "overrides",
    [
        {"quiz_temperature": -0.1}, {"quiz_temperature": 2.5},
        {"quiz_strict_attempts": 0}, {"quiz_strict_attempts": 21},
        {"quiz_relaxed_attempts": -1}, {"quiz_relaxed_attempts": 21},
        {"quiz_first_pass_attempts": -1}, {"quiz_first_pass_attempts": 21},
    ],
)
def test_out_of_range_values_are_rejected(overrides):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **overrides)


def test_boundaries_are_accepted():
    Settings(_env_file=None, quiz_temperature=0.0, quiz_strict_attempts=1, quiz_relaxed_attempts=0, quiz_first_pass_attempts=0)
    Settings(_env_file=None, quiz_temperature=2.0, quiz_strict_attempts=20, quiz_relaxed_attempts=20, quiz_first_pass_attempts=20)


def test_module_constants_come_from_settings():
    assert qs.STRICT_ATTEMPTS == settings.quiz_strict_attempts
    assert qs.RELAXED_ATTEMPTS == settings.quiz_relaxed_attempts
    assert qs.QUIZ_OPTIONS["temperature"] == settings.quiz_temperature


def test_the_validation_rules_were_not_touched_by_any_of_this():
    """Spot-check that the rules a first pass must not loosen still reject."""
    assert qs.reject_reason("ما هو مفهوم clustering؟", AR_OPTS, AR_OPTS[0], True) == qs.REASON_LATIN
    assert qs.reject_reason("q?!", ["a1", "a1", "b2", "c3"], "a1", False) == qs.REASON_DUPLICATES
    assert qs.reject_reason("q?!", ["Option 1", "b2", "c3", "d4"], "b2", False) == qs.REASON_PLACEHOLDER
    assert qs.reject_reason("q?!", ["a1", "b2", "c3"], "a1", False) == qs.REASON_OPTION_COUNT


# --- the two-pass budget ---------------------------------------------------------


@pytest.fixture()
def model(monkeypatch):
    """Script the model per chunk: plan[tag] is a list of replies, the last one repeats."""

    def install(plan, first_pass=2):
        monkeypatch.setattr(settings, "quiz_first_pass_attempts", first_pass)
        seen = {tag: 0 for tag in plan}
        order = []

        def fake(prompt, _model):
            tag = next(t for t in plan if t * 50 in prompt)
            replies = plan[tag]
            reply_ = replies[min(seen[tag], len(replies) - 1)]
            seen[tag] += 1
            order.append(tag)
            return reply_

        monkeypatch.setattr(qs, "_ask_model", fake)
        return seen, order

    return install


def run(chunks, count, language="en"):
    import app.services.quiz_service as module

    module.sample_chunks = lambda *a, **k: chunks  # restored by the monkeypatch in `sampled`
    trace = qs.QuizTrace()
    questions = qs.generate_quiz(1, count, language, trace=trace)
    return questions, trace


@pytest.fixture(autouse=True)
def sampled(monkeypatch):
    monkeypatch.setattr(qs, "sample_chunks", qs.sample_chunks)  # so run() can replace and it is undone


def test_a_hard_chunk_is_given_up_on_after_the_first_pass_budget(model):
    seen, order = model({"A": [DUP_EN], "B": [GOOD_EN]})
    questions, trace = run([A, B], count=1)
    assert [q.source.source_filename for q in questions] == ["B.pdf"]
    assert order == ["A", "A", "B"]  # two tries on A, then straight on to B
    assert seen == {"A": 2, "B": 1}


def test_a_chunk_that_succeeds_inside_the_budget_is_unaffected(model):
    seen, order = model({"A": [DUP_EN, GOOD_EN], "B": [GOOD_EN]})
    questions, _ = run([A, B], count=2)
    assert order == ["A", "A", "B"]
    assert [q.source.source_filename for q in questions] == ["A.pdf", "B.pdf"]


def test_deferred_chunks_are_resumed_only_when_the_quiz_is_still_short(model):
    # A needs 5 attempts, B never works. Pass 1: A 2, B 2. Pass 2 resumes A.
    seen, order = model({"A": [DUP_EN] * 4 + [GOOD_EN], "B": [DUP_EN]})
    questions, trace = run([A, B], count=1)
    assert [q.source.source_filename for q in questions] == ["A.pdf"]
    assert order == ["A", "A", "B", "B", "A", "A", "A"]
    assert seen == {"A": 5, "B": 2}  # B was never resumed: the quiz was already full
    assert trace.chunks[0].calls == 5 and trace.chunks[0].accepted


def test_a_resumed_chunk_gets_only_what_is_left_of_its_old_budget(model):
    seen, _ = model({"A": [DUP_EN], "B": [DUP_EN]})
    with pytest.raises(qs.QuizGenerationError):
        run([A, B], count=1)
    assert seen == {"A": qs.STRICT_ATTEMPTS, "B": qs.STRICT_ATTEMPTS}  # 8, not 2 + 8


def test_arabic_total_effort_per_chunk_is_unchanged(model):
    seen, _ = model({"A": [reply(AR_Q, [*AR_OPTS[:3], AR_OPTS[0]], AR_OPTS[0])]})
    with pytest.raises(qs.QuizGenerationError):
        run([A], count=1, language="ar")
    assert seen["A"] == qs.STRICT_ATTEMPTS + qs.RELAXED_ATTEMPTS


def test_relaxed_rules_are_not_used_while_untried_chunks_remain(model):
    """The quality guarantee: a chunk that only the lenient rules accept loses to a clean one."""
    seen, _ = model({"A": [LEAKY_AR], "B": [GOOD_AR]})
    questions, trace = run([A, B], count=1, language="ar")
    assert [q.source.source_filename for q in questions] == ["B.pdf"]
    assert not any(a.phase == qs.PHASE_RELAXED for c in trace.chunks for a in c.attempts)
    assert qs.contains_arabic(questions[0].question_text) and "clustering" not in questions[0].question_text


def test_relaxed_rules_remain_the_last_resort(model):
    model({"A": [LEAKY_AR]})
    questions, trace = run([A], count=1, language="ar")
    assert len(questions) == 1 and trace.chunks[0].accepted_phase == qs.PHASE_RELAXED
    phases = [a.phase for a in trace.chunks[0].attempts]
    assert phases == [qs.PHASE_STRICT] * qs.STRICT_ATTEMPTS + [qs.PHASE_RELAXED]


def test_first_pass_only_ever_uses_strict_rules(model):
    model({"A": [LEAKY_AR], "B": [LEAKY_AR], "C": [GOOD_AR]})
    _, trace = run([A, B, C], count=1, language="ar")
    first_pass = [a for c in trace.chunks for a in c.attempts[:2]]
    assert all(a.phase == qs.PHASE_STRICT for a in first_pass)


def test_zero_restores_the_old_one_chunk_at_a_time_behaviour(model):
    seen, order = model({"A": [DUP_EN] * 4 + [GOOD_EN], "B": [GOOD_EN]}, first_pass=0)
    questions, _ = run([A, B], count=1)
    assert order == ["A"] * 5  # A gets its whole budget before anything else
    assert [q.source.source_filename for q in questions] == ["A.pdf"]


def test_zero_and_arabic_still_relaxes_immediately(model):
    seen, _ = model({"A": [LEAKY_AR], "B": [GOOD_AR]}, first_pass=0)
    questions, trace = run([A, B], count=1, language="ar")
    assert questions[0].source.source_filename == "A.pdf"  # the old behaviour, by request
    assert trace.chunks[0].accepted_phase == qs.PHASE_RELAXED


def test_a_budget_larger_than_the_strict_cap_is_capped_and_still_defers_relaxed(model):
    model({"A": [LEAKY_AR], "B": [GOOD_AR]}, first_pass=20)
    questions, trace = run([A, B], count=1, language="ar")
    assert questions[0].source.source_filename == "B.pdf"
    assert trace.chunks[0].calls == qs.STRICT_ATTEMPTS  # all strict, no relaxed yet


def test_questions_keep_chunk_order_within_a_pass(model):
    model({"A": [GOOD_EN], "B": [DUP_EN], "C": [GOOD_EN]})
    questions, _ = run([A, B, C], count=2)
    assert [q.source.source_filename for q in questions] == ["A.pdf", "C.pdf"]


def test_the_trace_follows_a_chunk_across_both_passes(model):
    model({"A": [DUP_EN] * 3 + [GOOD_EN], "B": [DUP_EN]})
    _, trace = run([A, B], count=1)
    chunk_a = trace.chunks[0]
    assert [a.outcome for a in chunk_a.attempts] == [qs.REASON_DUPLICATES] * 3 + [qs.OUTCOME_ACCEPTED]
    assert chunk_a.accepted and chunk_a.wall_s > 0
    assert len(trace.chunks) == 2  # one trace per chunk, not per pass


def test_deferral_is_marked_in_the_log(model, caplog):
    model({"A": [DUP_EN], "B": [GOOD_EN]})
    with caplog.at_level("INFO"):
        run([A, B], count=1)
    events = [json.loads(r.getMessage().split(" ", 1)[1]) for r in caplog.records if r.getMessage().startswith("quiz_item ")]
    assert [(e["citation"], e["final"]) for e in events] == [("A.pdf, page 1", False), ("B.pdf, page 1", True)]


def test_an_outage_in_the_second_pass_still_surfaces(model, monkeypatch):
    seen, _ = model({"A": [DUP_EN], "B": [DUP_EN]})
    real = qs._ask_model
    calls = {"n": 0}

    def flaky(prompt, m):
        calls["n"] += 1
        if calls["n"] == 5:  # first call of the second pass
            raise LLMUnavailableError("down")
        return real(prompt, m)

    monkeypatch.setattr(qs, "_ask_model", flaky)
    with pytest.raises(LLMUnavailableError):
        run([A, B], count=1)


def test_the_partial_result_is_kept_when_only_some_chunks_work(model):
    model({"A": [GOOD_EN], "B": [DUP_EN], "C": [DUP_EN]})
    questions, trace = run([A, B, C], count=3)
    assert len(questions) == 1 and trace.produced == 1 and trace.error is None
