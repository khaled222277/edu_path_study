"""FIGURE_CHUNKS: figure descriptions are demoted or left out unless the question mentions a figure.

Many figure descriptions end with boilerplate such as "No Arabic text is present", and a decorative
logo is described the same way on many slides, so for a question that is not about a figure they
compete with lecture text for the few slots the prompt has. The default ("normal") is unchanged.
"""

import pytest
from flashrank import RerankRequest

from app.config import settings
from app.services import retrieval

# vector order: a figure first, then text, figure, text, text, figure
KINDS = ["image_description", "text", "image_description", "text", "text", "image_description"]


class Collection:
    def __init__(self):
        self.queries = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        n = min(6, kwargs["n_results"])
        return {
            "documents": [[f"doc{i}" for i in range(n)]],
            "metadatas": [[{"source_filename": f"l{i}.pdf", "page_number": i, "content_type": KINDS[i],
                            "lecture_id": 1, "subject_id": 7} for i in range(n)]],
            "distances": [[0.1 * (i + 1) for i in range(n)]],
        }


class Ranker:
    def __init__(self, scores):
        self.scores = scores

    def rerank(self, request: RerankRequest):
        out = [{**p, "score": self.scores.get(p["id"], 0.0)} for p in request.passages]
        return sorted(out, key=lambda r: r["score"], reverse=True)


@pytest.fixture()
def wire(monkeypatch):
    def install(scores=None):
        col = Collection()
        monkeypatch.setattr(retrieval, "get_collection", lambda: col)
        monkeypatch.setattr(retrieval, "get_ranker", lambda: Ranker(scores or {}))
        return col

    return install


def mode(monkeypatch, value):
    monkeypatch.setattr(settings, "figure_chunks", value)


def texts(chunks):
    return [c.text for c in chunks]


# --- what counts as asking for a figure ----------------------------------------------------------

ASKS = [
    "What does the diagram of the Apriori algorithm show?", "describe figure 3", "what is in the picture on that slide",
    "explain the chart", "show me the image", "ماذا توضح الرسوم عن النموذج", "ماذا يوضح الشكل", "صف الصورة",
    "ما هو المخطط", "في الرسم البياني", "للشكل", "بالصورة",
]
DOES_NOT_ASK = [
    "what is classification", "How does binning smooth noisy data?", "ما هو التصنيف", "كيف تعمل خوارزمية k-means؟",
    "explain overfitting again", "ما هي خطوات عملية اكتشاف المعرفة", "define entropy",
]


@pytest.mark.parametrize("q", ASKS)
def test_questions_that_mention_a_figure_are_recognised(q):
    assert retrieval.asks_for_figure(q), q


@pytest.mark.parametrize("q", DOES_NOT_ASK)
def test_ordinary_questions_are_not(q):
    assert not retrieval.asks_for_figure(q), q


# --- the modes ------------------------------------------------------------------------------------


def test_normal_is_unchanged_and_filters_only_by_subject(wire, monkeypatch):
    mode(monkeypatch, "normal")
    col = wire()
    chunks = retrieval.retrieve("what is clustering?", 7)
    assert texts(chunks) == ["doc0", "doc1", "doc2"]
    assert col.queries[0]["where"] == {"subject_id": 7}


def test_the_default_is_normal():
    assert type(settings).model_fields["figure_chunks"].default == "normal"


def test_exclude_does_not_search_figures_for_an_ordinary_question(wire, monkeypatch):
    mode(monkeypatch, "exclude")
    col = wire()
    retrieval.retrieve("what is clustering?", 7)
    assert col.queries[0]["where"] == {"$and": [{"subject_id": 7}, {"content_type": {"$ne": "image_description"}}]}


def test_exclude_still_searches_figures_when_the_question_asks_for_one(wire, monkeypatch):
    mode(monkeypatch, "exclude")
    col = wire()
    retrieval.retrieve("what does the diagram of k-means show?", 7)
    assert col.queries[0]["where"] == {"subject_id": 7}


def test_demote_puts_lecture_text_first_in_vector_order(wire, monkeypatch):
    mode(monkeypatch, "demote")
    wire()  # reranker unconfident: vector order is used
    assert texts(retrieval.retrieve("ما هو التصنيف", 7)) == ["doc1", "doc3", "doc4"]


def test_demote_keeps_the_rerankers_order_inside_each_group(wire, monkeypatch):
    mode(monkeypatch, "demote")
    wire({0: 0.99, 3: 0.9, 2: 0.8, 4: 0.7, 1: 0.6})
    assert texts(retrieval.retrieve("what is clustering?", 7)) == ["doc3", "doc4", "doc1"]


def test_demote_uses_a_figure_when_there_is_not_enough_text(wire, monkeypatch):
    mode(monkeypatch, "demote")
    col = wire()
    chunks = retrieval.retrieve("what is clustering?", 7, candidate_k=3, top_k=3)
    assert texts(chunks) == ["doc1", "doc0", "doc2"], "text first, then the figures that were all there was"
    assert col.queries[0]["n_results"] == 3


def test_demote_does_nothing_when_the_question_asks_for_a_figure(wire, monkeypatch):
    mode(monkeypatch, "demote")
    wire()
    assert texts(retrieval.retrieve("what does the diagram show?", 7)) == ["doc0", "doc1", "doc2"]


def test_an_unknown_value_is_refused_at_startup():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(figure_chunks="sometimes")
