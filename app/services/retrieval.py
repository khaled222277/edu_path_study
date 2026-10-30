"""Retrieve lecture chunks for a query: vector search -> rerank -> top N.

Two stages because bge-m3 is a bi-encoder (fast, approximate) while FlashRank is
a cross-encoder (slow, accurate). Pull a wide-ish candidate set cheaply, then let
the cross-encoder pick the few that actually answer the question.
"""

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from flashrank import Ranker, RerankRequest

from app.config import settings
from app.services import lecture_images
from app.services.vector_store import (
    CONTENT_TYPE_IMAGE,
    PROJECT_ROOT,
    get_collection,
    retry_if_stale,
)

log = logging.getLogger(__name__)

CANDIDATE_K = settings.candidate_k  # pulled from Chroma
TOP_K = settings.top_k  # kept after reranking

RERANKER_MODEL = "ms-marco-MultiBERT-L-12"
RERANKER_CACHE = PROJECT_ROOT / "data" / "flashrank"

# The reranker is reliable on English but collapses on a cross-lingual pair (an
# Arabic question against English lecture text): measured max scores were
# 0.69-0.999 for English queries and 0.0005-0.0008 for Arabic ones, and the
# Arabic reorderings were actively wrong -- it demoted "How to Handle Missing
# Data" out of the top 5 for "ما هي طرق معالجة البيانات الناقصة؟".
#
# bge-m3 is genuinely multilingual and ranks those correctly on its own, so when
# the cross-encoder is this unsure we keep the vector order rather than trust it.
# 0.1 sits three orders of magnitude above the Arabic scores and well below the
# lowest confident English one.
RERANK_MIN_CONFIDENCE = settings.rerank_min_confidence


# A question that names a figure wants figure descriptions. English by word, Arabic by stem: the
# common prefixes (ال, و, ب, ل, ف, ك) are removed first, so "الرسوم", "بالصورة" and "للشكل" all match.
# Deliberately generous (Arabic "بشكل", "in a way", matches too): a false "asks" only keeps the
# behaviour that existed before FIGURE_CHUNKS, whereas a miss hides the figure that was asked about.
_FIGURE_WORDS_EN = re.compile(
    r"\b(figures?|fig|diagrams?|charts?|images?|pictures?|photos?|graphs?|plots?|illustrations?|"
    r"drawings?|sketch(?:es)?|screenshots?|visuals?|infographics?|logos?)\b",
    re.IGNORECASE,
)
_FIGURE_STEMS_AR = frozenset(
    "شكل اشكال أشكال رسم رسوم رسمه رسمة مخطط مخططات صوره صورة صور رسومات منحني منحنى تخطيط شعار".split()
)
_AR_PREFIXES = ("وبال", "وال", "بال", "كال", "فال", "لل", "ال", "و", "ب", "ل", "ك", "ف")
_AR_WORD = re.compile(r"[؀-ۿ]+")
_AR_MARKS = re.compile(r"[ً-ٰٟـ]")


def asks_for_figure(question: str) -> bool:
    """True if the question mentions a figure, diagram, chart, image or picture."""
    if _FIGURE_WORDS_EN.search(question):
        return True
    for word in _AR_WORD.findall(question):
        word = _AR_MARKS.sub("", word)
        stems = {word} | {word[len(p):] for p in _AR_PREFIXES if word.startswith(p) and len(word) > len(p) + 1}
        if stems & _FIGURE_STEMS_AR:
            return True
    return False


def _demote_figures(chunks: list) -> list:
    """Lecture text first, then figure descriptions; the order inside each group is kept."""
    return [c for c in chunks if c.content_type != CONTENT_TYPE_IMAGE] + [
        c for c in chunks if c.content_type == CONTENT_TYPE_IMAGE
    ]


@dataclass
class RetrievedChunk:
    """One reranked chunk, carrying what the prompt needs to cite it."""

    text: str
    source_filename: str
    page_number: int
    content_type: str
    lecture_id: int
    subject_id: int
    score: float
    # Only set on image_description chunks, and only once the figure has been
    # saved to disk -- lectures ingested before that are still text-only.
    image_path: str | None = None

    @property
    def citation(self) -> str:
        return f"[{self.source_filename}, page {self.page_number}]"

    @property
    def image_url(self) -> str | None:
        """API path serving this chunk's figure, or None if there isn't one."""
        if self.content_type != CONTENT_TYPE_IMAGE or not self.image_path:
            return None
        return lecture_images.image_url(self.image_path)


BGE_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"


class BgeReranker:
    """BAAI/bge-reranker-v2-m3 behind the same `.rerank(RerankRequest)` as FlashRank.

    A multilingual cross-encoder, so an Arabic question can be scored against English
    slides. Scores are sigmoid probabilities in [0, 1].
    """

    def __init__(self, device: str = "cpu") -> None:
        from sentence_transformers import CrossEncoder

        from app.services.vector_store import resolve_device

        self._model = CrossEncoder(BGE_RERANKER_MODEL, device=resolve_device(device), max_length=512)

    def rerank(self, request) -> list[dict]:
        scores = self._model.predict([(request.query, p["text"]) for p in request.passages])
        ranked = [{**p, "score": float(s)} for p, s in zip(request.passages, scores)]
        ranked.sort(key=lambda r: r["score"], reverse=True)
        return ranked


@lru_cache(maxsize=2)
def _build_ranker(name: str, device: str):
    """One instance per (reranker, device) -- loading either model is expensive."""
    if name == "bge-m3":
        return BgeReranker(device)
    RERANKER_CACHE.mkdir(parents=True, exist_ok=True)
    return Ranker(model_name=RERANKER_MODEL, cache_dir=str(RERANKER_CACHE))


def get_ranker():
    """The configured reranker (RERANKER), built once."""
    return _build_ranker(settings.reranker, settings.reranker_device)


@dataclass
class RetrievalTrace:
    """Every stage of one retrieval, for evaluation. `final` is what the prompt gets."""

    candidates: list[RetrievedChunk]  # vector order, best first (score = 1 - distance)
    reranked: list[RetrievedChunk]  # cross-encoder order (score = rerank score)
    final: list[RetrievedChunk]
    strategy: str  # "reranked" | "vector-order (reranker unconfident)" | "empty"
    top_score: float  # best cross-encoder score


def _to_chunk(text: str, meta: dict, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        text=text,
        source_filename=meta.get("source_filename", "unknown"),
        page_number=int(meta.get("page_number", 0)),
        content_type=meta.get("content_type", "text"),
        lecture_id=int(meta.get("lecture_id", 0)),
        subject_id=int(meta.get("subject_id", 0)),
        score=score,
        image_path=meta.get("image_path") or None,
    )


def retrieve_detailed(
    query: str,
    subject_id: int,
    candidate_k: int = CANDIDATE_K,
    top_k: int = TOP_K,
) -> RetrievalTrace:
    """Like retrieve(), but returns the candidate list and the reranked list as well."""
    empty = RetrievalTrace([], [], [], "empty", 0.0)
    if not query.strip():
        return empty

    figures = settings.figure_chunks if not asks_for_figure(query) else "normal"
    where = {"subject_id": subject_id}
    if figures == "exclude":
        where = {"$and": [where, {"content_type": {"$ne": CONTENT_TYPE_IMAGE}}]}

    result = retry_if_stale(
        lambda: get_collection().query(
            query_texts=[query],
            n_results=candidate_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
    )

    documents = result["documents"][0]
    metadatas = result["metadatas"][0]
    distances = result["distances"][0]
    if not documents:
        return empty

    candidates = [
        _to_chunk(doc, meta, 1.0 - float(dist))
        for doc, meta, dist in zip(documents, metadatas, distances)
    ]
    passages = [
        {"id": i, "text": doc, "meta": meta}
        for i, (doc, meta) in enumerate(zip(documents, metadatas))
    ]
    ranked = get_ranker().rerank(RerankRequest(query=query, passages=passages))
    top_score = max((r["score"] for r in ranked), default=0.0)
    reranked = [_to_chunk(r["text"], r["meta"], float(r["score"])) for r in ranked]

    if figures == "demote":
        candidates, reranked = _demote_figures(candidates), _demote_figures(reranked)

    if top_score >= RERANK_MIN_CONFIDENCE:
        final, strategy = reranked[:top_k], "reranked"
    else:
        # Cross-encoder has no signal here; keep bge-m3's ordering.
        final, strategy = candidates[:top_k], "vector-order (reranker unconfident)"

    log.info(
        "subject %s: %s candidates -> %s via %s (rerank top=%.5f)",
        subject_id, len(documents), len(final), strategy, top_score,
    )
    return RetrievalTrace(candidates, reranked, final, strategy, float(top_score))


def retrieve(
    query: str,
    subject_id: int,
    candidate_k: int = CANDIDATE_K,
    top_k: int = TOP_K,
) -> list[RetrievedChunk]:
    """Top `top_k` chunks for `query`, scoped to one subject.

    The query is embedded by the collection's own embedding function (bge-m3,
    unprefixed) -- the same convention used at ingestion, so query and passage
    vectors share a space.
    """
    return retrieve_detailed(query, subject_id, candidate_k, top_k).final


def format_context(chunks: list[RetrievedChunk]) -> str:
    """Render chunks for the prompt, each tagged with its citation."""
    blocks = []
    for chunk in chunks:
        kind = (
            "figure description"
            if chunk.content_type == "image_description"
            else "lecture text"
        )
        blocks.append(
            f"[{chunk.source_filename}, page {chunk.page_number}] ({kind})\n"
            f"{chunk.text}"
        )
    return "\n\n---\n\n".join(blocks)
