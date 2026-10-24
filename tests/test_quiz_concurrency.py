"""QUIZ_CONCURRENCY: parallel chunks, never more than needed, nothing else changes."""

import threading
import time

import pytest

from app.config import Settings, settings
from app.services import quiz_service as qs
from app.services.llm import LLMUnavailableError

from .test_quiz_options import DUP_EN, chunk
from .test_quiz_stream import good

TAGS = "ABCDEFGH"


@pytest.fixture()
def model(monkeypatch):
    """Script the model per chunk, recording calls, overlap and timing.

    plan[tag] is a list of replies (the last repeats); an Exception instance is
    raised. `delays[tag]` makes that chunk's calls slow.
    """

    def install(plan, delays=None, concurrency=1, default_delay=0.0):
        monkeypatch.setattr(settings, "quiz_concurrency", concurrency)
        chunks = [chunk(t) for t in plan]
        monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: chunks)
        lock = threading.Lock()
        state = {"inflight": 0, "max_inflight": 0, "calls": [], "finished": [], "seen": {t: 0 for t in plan}}

        def fake(prompt, _model):
            tag = next(t for t in plan if t * 50 in prompt)
            with lock:
                state["inflight"] += 1
                state["max_inflight"] = max(state["max_inflight"], state["inflight"])
                state["calls"].append(tag)
                n = state["seen"][tag]
                state["seen"][tag] += 1
            try:
                time.sleep((delays or {}).get(tag, default_delay))
                replies = plan[tag]
                item = replies[min(n, len(replies) - 1)]
                if isinstance(item, Exception):
                    raise item
                return item
            finally:
                with lock:
                    state["inflight"] -= 1
                    state["finished"].append(tag)

        monkeypatch.setattr(qs, "_ask_model", fake)
        return state

    return install


def tags_of(questions):
    return [q.source.source_filename[0] for q in questions]


# --- the setting -------------------------------------------------------------------


def test_default_is_one_so_nothing_changes_unless_asked():
    assert Settings(_env_file=None).quiz_concurrency == 1


@pytest.mark.parametrize("value", [0, -1, 9])
def test_out_of_range_values_are_rejected(value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, quiz_concurrency=value)


@pytest.mark.parametrize("value", [1, 2, 3, 8])
def test_valid_values_are_accepted(value):
    assert Settings(_env_file=None, quiz_concurrency=value).quiz_concurrency == value


def test_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("QUIZ_CONCURRENCY", "3")
    assert Settings(_env_file=None).quiz_concurrency == 3


# --- concurrency 1 is the old behaviour -----------------------------------------------


def test_one_is_strictly_sequential_and_in_chunk_order(model):
    state = model({t: [good(t)] for t in "ABC"}, default_delay=0.01, concurrency=1)
    assert tags_of(qs.generate_quiz(1, 3, "en")) == ["A", "B", "C"]
    assert state["max_inflight"] == 1


# --- it actually runs in parallel ---------------------------------------------------------


@pytest.mark.parametrize("c", [2, 3])
def test_chunks_overlap_and_the_quiz_gets_faster(model, c):
    plan = {t: [good(t)] for t in "ABCD"}
    seq = model(plan, default_delay=0.15, concurrency=1)
    t0 = time.perf_counter()
    qs.generate_quiz(1, 4, "en")
    sequential = time.perf_counter() - t0
    assert seq["max_inflight"] == 1

    par = model(plan, default_delay=0.15, concurrency=c)
    t0 = time.perf_counter()
    out = qs.generate_quiz(1, 4, "en")
    parallel = time.perf_counter() - t0

    assert par["max_inflight"] == min(c, 4)
    assert sorted(tags_of(out)) == ["A", "B", "C", "D"]
    assert parallel < sequential * 0.7, f"{parallel:.2f}s vs {sequential:.2f}s"


def test_questions_are_yielded_in_the_order_they_finish(model):
    model({"A": [good("A")], "B": [good("B")]}, delays={"A": 0.3, "B": 0.0}, concurrency=2)
    assert tags_of(qs.generate_quiz(1, 2, "en")) == ["B", "A"]


def test_the_first_question_is_available_before_the_slow_one_finishes(model):
    model({"A": [good("A")], "B": [good("B")]}, delays={"A": 0.4, "B": 0.0}, concurrency=2)
    t0 = time.perf_counter()
    first = next(qs.iter_quiz(1, 2, "en"))
    assert first.source.source_filename[0] == "B" and time.perf_counter() - t0 < 0.25


# --- never more than needed ------------------------------------------------------------------


def test_never_more_in_flight_than_questions_still_needed(model):
    state = model({t: [good(t)] for t in TAGS}, default_delay=0.05, concurrency=3)
    assert len(qs.generate_quiz(1, 1, "en")) == 1
    assert state["max_inflight"] == 1, "one question wanted, so one call at a time"

    state = model({t: [good(t)] for t in TAGS}, default_delay=0.05, concurrency=3)
    qs.generate_quiz(1, 2, "en")
    assert state["max_inflight"] == 2


def test_stops_starting_chunks_once_the_quiz_is_full(model):
    state = model({t: [good(t)] for t in TAGS}, default_delay=0.03, concurrency=3)
    out = qs.generate_quiz(1, 3, "en")
    assert len(out) == 3
    assert sorted(set(state["calls"])) == ["A", "B", "C"], "chunks D.. are never started"


def test_a_surplus_result_is_dropped_not_added(model):
    """Two chunks finish together but only one question is wanted."""
    model({"A": [good("A")], "B": [good("B")]}, default_delay=0.05, concurrency=2)
    assert len(qs.generate_quiz(1, 1, "en")) == 1


def test_a_failed_chunk_is_replaced_by_the_next_one(model):
    state = model({"A": [DUP_EN], "B": [good("B")], "C": [good("C")], "D": [good("D")]}, default_delay=0.02, concurrency=2)
    out = qs.generate_quiz(1, 2, "en")
    assert sorted(tags_of(out)) == ["B", "C"]
    assert "D" not in state["calls"] or len(out) == 2


# --- correctness under threads ---------------------------------------------------------------------


def test_each_question_comes_from_its_own_chunk(model):
    model({t: [good(t)] for t in TAGS}, default_delay=0.02, concurrency=3)
    for q in qs.generate_quiz(1, 6, "en"):
        tag = q.source.source_filename[0]
        assert f"part {tag}?" in q.question_text, "a question must carry the chunk it was written from"


def test_each_chunk_keeps_its_own_trace(model):
    model({"A": [DUP_EN, good("A")], "B": [good("B")], "C": [good("C")]}, default_delay=0.02, concurrency=3)
    trace = qs.QuizTrace()
    qs.generate_quiz(1, 3, "en", trace=trace)
    by_chunk = {c.citation[0]: c for c in trace.chunks}
    assert by_chunk["A"].calls == 2 and by_chunk["B"].calls == 1 and by_chunk["C"].calls == 1
    assert all(c.accepted for c in by_chunk.values())
    assert trace.produced == 3


def test_the_same_questions_come_out_as_with_concurrency_one(model):
    plan = {t: [good(t)] for t in "ABCDEF"}
    model(plan, concurrency=1)
    sequential = sorted(q.question_text for q in qs.generate_quiz(1, 4, "en"))
    model(plan, concurrency=3, default_delay=0.01)
    parallel = sorted(q.question_text for q in qs.generate_quiz(1, 4, "en"))
    assert len(sequential) == len(parallel) == 4
    # the same chunks, in a possibly different order, since the plan has no failures
    assert sequential == sorted(q for q in parallel)


def test_the_second_pass_runs_concurrently_too(model):
    """Two hard chunks, each needing a 4th attempt: resumed in parallel."""
    hard = [DUP_EN, DUP_EN, DUP_EN, good("A")]
    state = model({"A": hard, "B": [DUP_EN] * 3 + [good("B")]}, default_delay=0.1, concurrency=2)
    out = qs.generate_quiz(1, 2, "en")
    assert sorted(tags_of(out)) == ["A", "B"]
    assert state["max_inflight"] == 2


def test_validation_is_unchanged_by_concurrency(model):
    """Concurrency must never accept what the rules reject."""
    model({"A": [DUP_EN], "B": [DUP_EN]}, default_delay=0.01, concurrency=2)
    with pytest.raises(qs.QuizGenerationError):
        qs.generate_quiz(1, 2, "en")


# --- failures and cancellation -------------------------------------------------------------------------


def test_an_outage_in_one_worker_surfaces_and_stops_the_others(model):
    state = model(
        {"A": [good("A")], "B": [LLMUnavailableError("down")], "C": [good("C")], "D": [good("D")], "E": [good("E")]},
        delays={"A": 0.1, "B": 0.0, "C": 0.1}, concurrency=3,
    )
    trace = qs.QuizTrace()
    with pytest.raises(LLMUnavailableError):
        qs.generate_quiz(1, 5, "en", trace=trace)
    assert trace.error == "unavailable"
    time.sleep(0.3)
    assert "D" not in state["calls"] and "E" not in state["calls"], "nothing new starts after the outage"


def test_cancelling_stops_new_work_and_retries(model, monkeypatch):
    cancel = threading.Event()
    state = model({t: [DUP_EN] for t in TAGS}, default_delay=0.05, concurrency=2)
    gen = qs.iter_quiz(1, 5, "en", cancel=cancel)
    threading.Timer(0.08, cancel.set).start()
    assert list(gen) == []
    time.sleep(0.3)
    started = len(state["calls"])
    time.sleep(0.2)
    assert len(state["calls"]) == started, "no model calls after cancellation settles"
    assert started < 16


def test_closing_the_generator_halts_the_workers(model):
    state = model({t: [good(t)] for t in TAGS}, default_delay=0.05, concurrency=3)
    gen = qs.iter_quiz(1, 6, "en")
    next(gen)
    gen.close()
    time.sleep(0.4)
    settled = len(state["calls"])
    time.sleep(0.3)
    assert len(state["calls"]) == settled
    assert settled < 8, "the rest of the chunks were never started"


def test_streaming_still_delivers_progressively_under_concurrency(model):
    model({t: [good(t)] for t in "ABCD"}, delays={"A": 0.0, "B": 0.1, "C": 0.2, "D": 0.3}, concurrency=2)
    gen = qs.iter_quiz(1, 4, "en")
    t0 = time.perf_counter()
    stamps = [(q.source.source_filename[0], time.perf_counter() - t0) for q in gen]
    assert [s[0] for s in stamps][0] == "A"
    assert stamps[0][1] < 0.1 < stamps[-1][1], "the first arrives long before the last"


# --- cancellation, in detail ---------------------------------------------------------------


def test_cancelling_does_not_start_new_chunks_under_concurrency(model):
    """After Stop no further chunk is begun, so no phantom trace is left for work that never ran."""
    model({t: [good(t)] for t in TAGS}, delays={"A": 0.0, "B": 0.25}, concurrency=2)
    cancel, trace = threading.Event(), qs.QuizTrace()
    gen = qs.iter_quiz(1, 5, "en", trace=trace, cancel=cancel)
    assert next(gen).source.source_filename[0] == "A"  # B is still being written
    cancel.set()
    assert list(gen) == []
    assert sorted(c.citation[0] for c in trace.chunks) == ["A", "B"], "no third chunk was begun"


def test_a_result_that_finishes_after_stop_is_dropped(model):
    state = model({"A": [good("A")], "B": [good("B")]}, delays={"A": 0.05, "B": 0.3}, concurrency=2)
    cancel = threading.Event()
    gen = qs.iter_quiz(1, 2, "en", cancel=cancel)
    first = next(gen)
    cancel.set()
    assert list(gen) == [], "B finishes after the student left; it must not be delivered"
    assert first.source.source_filename[0] == "A" and "B" in state["calls"]


def test_a_sequential_result_that_finishes_after_stop_is_dropped_too(model, monkeypatch):
    cancel = threading.Event()
    model({"A": [good("A")], "B": [good("B")]}, concurrency=1)
    real = qs._ask_model
    monkeypatch.setattr(qs, "_ask_model", lambda p, m: (cancel.set(), real(p, m))[1])
    assert list(qs.iter_quiz(1, 2, "en", cancel=cancel)) == []


def test_run_jobs_halts_its_stragglers_when_it_stops():
    """Used on its own, the helper still tells in-flight jobs to stop retrying."""
    halt = threading.Event()
    made = []

    def jobs():
        for i in range(10):
            made.append(i)
            yield i, (lambda: (time.sleep(0.05), None)[1])

    gen = qs._run_jobs(jobs(), lambda: 10, 3, lambda: False, halt)
    next(gen)
    assert not halt.is_set()
    gen.close()
    assert halt.is_set()


def test_run_jobs_with_one_worker_never_leaves_the_calling_thread():
    threads = set()

    def job():
        threads.add(threading.current_thread())
        return None

    list(qs._run_jobs(iter([(i, job) for i in range(3)]), lambda: 3, 1, lambda: False, threading.Event()))
    assert threads == {threading.current_thread()}
