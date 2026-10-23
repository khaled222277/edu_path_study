"""Retrieve lecture chunks for a query: vector search -> rerank -> top N.

Two stages because bge-m3 is a bi-encoder (fast, approximate) while FlashRank is
a cross-encoder (slow, accurate). Pull a wide-ish candidate set cheaply, then let
the cross-encoder pick the few that actually answer the question.
"""

import logging
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


@lru_cache(maxsize=1)
def get_ranker() -> Ranker:
    """Reranker singleton -- loading the ONNX model is expensive."""
    RERANKER_CACHE.mkdir(parents=True, exist_ok=True)
    return Ranker(model_name=RERANKER_MODEL, cache_dir=str(RERANKER_CACHE))


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
    if not query.strip():
        return []

    result = retry_if_stale(
        lambda: get_collection().query(
            query_texts=[query],
            n_results=candidate_k,
            where={"subject_id": subject_id},
            include=["documents", "metadatas", "distances"],
        )
    )

    documents = result["documents"][0]
    metadatas = result["metadatas"][0]
    distances = result["distances"][0]
    if not documents:
        return []

    passages = [
        {"id": i, "text": doc, "meta": meta}
        for i, (doc, meta) in enumerate(zip(documents, metadatas))
    ]
    ranked = get_ranker().rerank(RerankRequest(query=query, passages=passages))
    top_score = max((r["score"] for r in ranked), default=0.0)

    if top_score >= RERANK_MIN_CONFIDENCE:
        selected = [(r["text"], r["meta"], float(r["score"])) for r in ranked[:top_k]]
        strategy = "reranked"
    else:
        # Cross-encoder has no signal here; keep bge-m3's ordering.
        selected = [
            (documents[i], metadatas[i], 1.0 - float(distances[i]))
            for i in range(min(top_k, len(documents)))
        ]
        strategy = "vector-order (reranker unconfident)"

    chunks = [
        RetrievedChunk(
            text=text,
            source_filename=meta.get("source_filename", "unknown"),
            page_number=int(meta.get("page_number", 0)),
            content_type=meta.get("content_type", "text"),
            lecture_id=int(meta.get("lecture_id", 0)),
            subject_id=int(meta.get("subject_id", 0)),
            score=score,
            image_path=meta.get("image_path") or None,
        )
        for text, meta, score in selected
    ]

    log.info(
        "subject %s: %s candidates -> %s via %s (rerank top=%.5f)",
        subject_id, len(documents), len(chunks), strategy, top_score,
    )
    return chunks


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
