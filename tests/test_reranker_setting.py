"""RERANKER selects the cross-encoder; the default stays FlashRank."""

from types import SimpleNamespace

import pytest

from app.config import Settings, settings
from app.services import retrieval

# The real function, captured before conftest's offline guard replaces it with a stub.
_real_get_ranker = retrieval.get_ranker


def test_default_is_flashrank_on_cpu(monkeypatch):
    monkeypatch.delenv("RERANKER", raising=False)
    monkeypatch.delenv("RERANKER_DEVICE", raising=False)
    s = Settings(_env_file=None)
    assert (s.reranker, s.reranker_device) == ("flashrank", "cpu")


@pytest.mark.parametrize("value", ["flashrank", "bge-m3"])
def test_valid_choices(value):
    assert Settings(_env_file=None, reranker=value).reranker == value


@pytest.mark.parametrize("value", ["", "bge", "cohere", "FlashRank", "flashrank "])
def test_unknown_rerankers_are_rejected_at_startup(value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, reranker=value)


@pytest.mark.parametrize("value", ["gpu", "cuda:", "tpu"])
def test_bad_devices_are_rejected(value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, reranker_device=value)


@pytest.fixture()
def builds(monkeypatch):
    """Replace both constructors with recorders; the real models are far too heavy for a test."""
    made = []
    monkeypatch.setattr(retrieval, "Ranker", lambda **kw: made.append(("flashrank", kw)) or "FLASH")
    monkeypatch.setattr(retrieval, "BgeReranker", lambda device="cpu": made.append(("bge-m3", device)) or "BGE")
    retrieval._build_ranker.cache_clear()
    yield made
    retrieval._build_ranker.cache_clear()


def test_the_setting_picks_the_reranker(builds, monkeypatch):
    monkeypatch.setattr(settings, "reranker", "flashrank")
    assert _real_get_ranker() == "FLASH"
    monkeypatch.setattr(settings, "reranker", "bge-m3")
    monkeypatch.setattr(settings, "reranker_device", "cuda:1")
    assert _real_get_ranker() == "BGE"
    assert [m[0] for m in builds] == ["flashrank", "bge-m3"]
    assert builds[1][1] == "cuda:1", "the configured device reaches the reranker"


def test_each_reranker_is_built_once(builds, monkeypatch):
    monkeypatch.setattr(settings, "reranker", "bge-m3")
    for _ in range(5):
        _real_get_ranker()
    assert len(builds) == 1


def test_switching_back_does_not_reload(builds, monkeypatch):
    for name in ("flashrank", "bge-m3", "flashrank", "bge-m3"):
        monkeypatch.setattr(settings, "reranker", name)
        _real_get_ranker()
    assert len(builds) == 2


class FakeCrossEncoder:
    def __init__(self, scores):
        self.scores, self.pairs = scores, None

    def predict(self, pairs):
        self.pairs = list(pairs)
        return self.scores


def make_bge(scores):
    bge = retrieval.BgeReranker.__new__(retrieval.BgeReranker)  # skip the heavy constructor
    bge._model = FakeCrossEncoder(scores)
    return bge


def test_bge_scores_every_passage_against_the_query_and_sorts_best_first():
    passages = [{"id": 0, "text": "alpha", "meta": {"p": 1}}, {"id": 1, "text": "beta", "meta": {"p": 2}}, {"id": 2, "text": "gamma", "meta": {}}]
    bge = make_bge([0.2, 0.9, 0.5])
    ranked = bge.rerank(SimpleNamespace(query="Q", passages=passages))
    assert bge._model.pairs == [("Q", "alpha"), ("Q", "beta"), ("Q", "gamma")]
    assert [r["id"] for r in ranked] == [1, 2, 0]
    assert [r["score"] for r in ranked] == [0.9, 0.5, 0.2]
    assert ranked[0]["meta"] == {"p": 2} and ranked[0]["text"] == "beta", "same record shape as FlashRank's output"


def test_bge_scores_are_plain_floats():
    import numpy as np

    # sentence-transformers returns numpy values; json and comparisons downstream want floats
    ranked = make_bge(np.array([0.5], dtype=np.float32)).rerank(SimpleNamespace(query="Q", passages=[{"id": 0, "text": "a", "meta": {}}]))
    assert type(ranked[0]["score"]) is float


def test_retrieval_works_unchanged_with_a_bge_style_ranker(monkeypatch):
    from .test_retrieval import FakeCollection

    monkeypatch.setattr(retrieval, "get_collection", lambda: FakeCollection())
    monkeypatch.setattr(retrieval, "get_ranker", lambda: make_bge([0.1, 0.2, 0.9, 0.3, 0.4, 0.5]))
    # scores listed per candidate in vector order -> best is candidate 2
    chunks = retrieval.retrieve("q", subject_id=7)
    assert [c.text for c in chunks] == ["doc2", "doc5", "doc4"]
    assert chunks[0].score == pytest.approx(0.9)
