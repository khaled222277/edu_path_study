"""Chunk the extracted document text while keeping each chunk traceable to a page.

`extract_pdf_text()` returns one string for the whole document with page markers
between pages. Those marker lines are stripped *before* splitting -- if they are
left in, they act as hard split points and fragment every page boundary into
useless stubs, and they pollute the embedding. Page spans are tracked in the
cleaned string's coordinate space, so each chunk still maps back to its page via
its character offset.
"""

import re
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config import settings

CHUNK_SIZE = settings.chunk_size
CHUNK_OVERLAP = settings.chunk_overlap

# Deliberately permissive: matches a delimiter-ish line carrying a page number,
# e.g. "--- Page 3 ---", "[Page 3]", "=== PAGE 3 ===", "--- صفحة 3 ---".
PAGE_MARKER_RE = re.compile(
    r"^[^\w\n]*(?:page|pg|صفحة)\s*[:#]?\s*(\d+)\s*[^\w\n]*$",
    re.IGNORECASE | re.MULTILINE,
)

UNKNOWN_PAGE = 0


@dataclass(frozen=True)
class PageSpan:
    page_number: int
    start: int  # offset into the *cleaned* document
    end: int


@dataclass(frozen=True)
class Chunk:
    text: str
    page_number: int
    index: int


def strip_markers(document: str) -> tuple[str, list[PageSpan]]:
    """Remove page-marker lines, returning the cleaned text and its page spans."""
    matches = list(PAGE_MARKER_RE.finditer(document))
    if not matches:
        return document, []

    parts: list[str] = []
    spans: list[PageSpan] = []
    cursor = 0  # offset into the cleaned string being assembled

    # Any preamble before the first marker belongs to no page.
    preamble = document[: matches[0].start()]
    if preamble:
        parts.append(preamble)
        spans.append(PageSpan(UNKNOWN_PAGE, cursor, cursor + len(preamble)))
        cursor += len(preamble)

    for i, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(document)
        body = document[body_start:body_end]

        parts.append(body)
        spans.append(
            PageSpan(int(match.group(1)), cursor, cursor + len(body))
        )
        cursor += len(body)

    return "".join(parts), spans


def page_for_offset(spans: list[PageSpan], offset: int) -> int:
    """Page number whose span contains `offset`, else UNKNOWN_PAGE."""
    for span in spans:
        if span.start <= offset < span.end:
            return span.page_number
    return spans[-1].page_number if spans else UNKNOWN_PAGE


def chunk_document(
    document: str,
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> tuple[list[Chunk], bool]:
    """Split `document` into chunks tagged with their source page.

    Returns (chunks, markers_found). `markers_found` is False when the document
    carried no recognisable page markers -- callers should warn, since every
    chunk then reports UNKNOWN_PAGE.
    """
    cleaned, spans = strip_markers(document)

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        add_start_index=True,
        separators=["\n\n", "\n", "۔", ".", "،", ",", " ", ""],
    )

    chunks: list[Chunk] = []
    for piece in splitter.create_documents([cleaned]):
        text = piece.page_content.strip()
        if not text:
            continue
        offset = piece.metadata.get("start_index", 0)
        chunks.append(
            Chunk(
                text=text,
                page_number=page_for_offset(spans, offset) if spans else UNKNOWN_PAGE,
                index=len(chunks),
            )
        )

    return chunks, bool(spans)
