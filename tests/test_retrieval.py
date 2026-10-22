"""Retrieval: vector search -> rerank (only when confident) -> top 3."""

import pytest
from flashrank import RerankRequest

from app.services import retrieval


def _meta(i):
    return {
        "source_filename": f"lec{i}.pdf", "page_number": i, "content_type": "text",
        "lecture_id": 1, "subject_id": 7,
    }


class FakeCollection:
    def __init__(self, n=6):
        self.n = n
        self.queries = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        n = kwargs["n_results"]
        docs = [f"doc{i}" for i in range(min(self.n, n))]
        return {
            "documents": [docs],
            "metadatas": [[_meta(i) for i in range(len(docs))]],
            # Vector order: doc0 is closest.
            "distances": [[0.1 * (i + 1) for i in range(len(docs))]],
        }


class FakeRanker:
    """Scores passages by the table given; unlisted passages score 0."""

    def __init__(self, scores_by_id):
        self.scores_by_id = scores_by_id
        self.called = False

    def rerank(self, request: RerankRequest):
        self.called = True
        out = [{**p, "score": self.scores_by_id.get(p["id"], 0.0)} for p in request.passages]
        return sorted(out, key=lambda r: r["score"], reverse=True)


@pytest.fixture()
def wire(monkeypatch):
    def install(collection, ranker):
        monkeypatch.setattr(retrieval, "get_collection", lambda: collection)
        monkeypatch.setattr(retrieval, "get_ranker", lambda: ranker)

    return install


def test_confident_reranker_reorders_and_keeps_top_three(wire):
    # Reranker prefers doc4, then doc2, doc5, doc0 -- the opposite of vector order.
    ranker = FakeRanker({4: 0.99, 2: 0.8, 5: 0.5, 0: 0.3})
    wire(FakeCollection(), ranker)

    chunks = retrieval.retrieve("what is clustering?", subject_id=7)

    assert ranker.called
    assert [c.text for c in chunks] == ["doc4", "doc2", "doc5"]
    assert len(chunks) == retrieval.TOP_K == 3
    assert chunks[0].score == pytest.approx(0.99)


def test_reranker_skipped_when_confidence_below_threshold(wire):
    # Best rerank score 0.0008 < 0.1: its (wrong) order must be ignored.
    ranker = FakeRanker({5: 0.0008, 4: 0.0005, 3: 0.0001})
    wire(FakeCollection(), ranker)

    chunks = retrieval.retrieve("ما هي طرق معالجة البيانات الناقصة؟", subject_id=7)

    assert ranker.called  # it ran, but its ordering was discarded
    assert [c.text for c in chunks] == ["doc0", "doc1", "doc2"]  # vector order
    # Score falls back to cosine similarity = 1 - distance.
    assert chunks[0].score == pytest.approx(0.9)
    assert chunks[2].score == pytest.approx(0.7)


def test_threshold_boundary_counts_as_confident(wire):
    ranker = FakeRanker({3: retrieval.RERANK_MIN_CONFIDENCE})
    wire(FakeCollection(), ranker)
    chunks = retrieval.retrieve("q", subject_id=7)
    assert chunks[0].text == "doc3"


def test_fewer_candidates_than_top_k(wire):
    wire(FakeCollection(n=2), FakeRanker({1: 0.9}))
    chunks = retrieval.retrieve("q", subject_id=7)
    assert [c.text for c in chunks] == ["doc1", "doc0"]


def test_query_is_scoped_to_the_subject(wire):
    collection = FakeCollection()
    wire(collection, FakeRanker({0: 0.9}))
    retrieval.retrieve("q", subject_id=7)
    assert collection.queries[0]["where"] == {"subject_id": 7}
    assert collection.queries[0]["n_results"] == retrieval.CANDIDATE_K


def test_empty_results_return_no_chunks_without_reranking(wire):
    ranker = FakeRanker({})
    wire(FakeCollection(n=0), ranker)
    assert retrieval.retrieve("q", subject_id=7) == []
    assert not ranker.called


def test_blank_query_never_touches_chroma(monkeypatch):
    def boom():
        raise AssertionError("collection must not be opened for a blank query")

    monkeypatch.setattr(retrieval, "get_collection", boom)
    assert retrieval.retrieve("   ", subject_id=7) == []


def test_image_chunk_exposes_image_url():
    chunk = retrieval.RetrievedChunk(
        text="t", source_filename="f.pdf", page_number=2,
        content_type="image_description", lecture_id=1, subject_id=7, score=0.5,
        image_path="1/page_2_0.png",
    )
    assert chunk.image_url == "/lectures/images/1/page_2_0.png"
    chunk.content_type = "text"
    assert chunk.image_url is None
