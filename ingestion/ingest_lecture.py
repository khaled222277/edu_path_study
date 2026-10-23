"""Ingest one lecture PDF into Postgres + ChromaDB.

    python ingest_lecture.py --university-id 1 --subject-id 3 --file lecture.pdf
    python ingest_lecture.py --university-id 1 --subject-id 3 --file lecture.pdf --dry-run

Idempotent: the file's SHA-256 is recorded on the Lecture row, so re-running on
an unchanged file is skipped.
"""

import argparse
import hashlib
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.models.lecture import Lecture  # noqa: E402
from app.models.subject import Subject  # noqa: E402
from app.services.vector_store import (  # noqa: E402
    CONTENT_TYPE_IMAGE,
    CONTENT_TYPE_TEXT,
    get_collection,
)
from ingestion.chunking import CHUNK_OVERLAP, CHUNK_SIZE, chunk_document  # noqa: E402
from ingestion.images import (  # noqa: E402
    FALLBACK_VISION_MODEL,
    VISION_MODEL,
    FigureIssue,
    classify_images,
    describe_images,
    extract_images,
    save_images,
)

try:
    from ingestion.extract_text import extract_pdf_pages
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "ingestion/extract_text.py is missing (or failed to import): "
        f"{exc}\nDrop your PDF extraction utility in there first."
    ) from exc

log = logging.getLogger("ingest")

# The page marker written between pages when the per-page texts are joined back
# into one document. Must stay in sync with chunking.PAGE_MARKER_RE.
PAGE_MARKER = "[Page {page}]"

# DISABLED (0.0) after auditing every page it rejected across the corpus.
#
# The idea was to drop OCR symbol soup from pictorial pages. It does catch that,
# but code listings and data tables are punctuation-dense and score identically:
# across 11 rejected pages, noise spanned 0.737-0.811 and *real* content spanned
# 0.744-0.845 -- fully overlapping, so no threshold separates them. It was
# discarding the PlayTennis training set, sklearn examples, and the Information
# Gain worked example. Symbol soup ranks poorly in retrieval anyway, so keeping
# it costs far less than losing teaching material.
#
# Raise via --ocr-min-alnum if a corpus ever genuinely needs it.
OCR_MIN_ALNUM_RATIO = 0.0


def alnum_ratio(text: str) -> float:
    """Alphanumeric share of non-whitespace characters.

    str.isalnum() is Unicode-aware, so Arabic letters count as alphanumeric --
    an Arabic page OCR'd cleanly scores ~1.0, not 0.
    """
    dense = [ch for ch in text if not ch.isspace()]
    if not dense:
        return 0.0
    return sum(ch.isalnum() for ch in dense) / len(dense)


def gate_ocr_noise(
    pages: list[dict], min_ratio: float = OCR_MIN_ALNUM_RATIO
) -> list[tuple[int, float, int]]:
    """Blank OCR text that is mostly symbols. Mutates `pages`; returns rejects.

    Gated pages end up with empty text, so compose_document() drops them and no
    chunk can cite them -- the page's content then rests on its LLaVA
    description alone.
    """
    rejected: list[tuple[int, float, int]] = []
    for page in pages:
        if page.get("method") != "ocr":
            continue
        text = page.get("text") or ""
        if not text.strip():
            continue
        ratio = alnum_ratio(text)
        if ratio < min_ratio:
            rejected.append((page["page"], ratio, len(text)))
            page["text"] = ""
            page["method"] = "ocr_rejected"
    return rejected


def compose_document(pages: list[dict]) -> str:
    """Join per-page texts into one document with page markers.

    Byte-identical to extract_text.extract_pdf_text(), but built from pages we
    already have -- calling extract_pdf_text() and extraction_report()
    separately would extract the file twice, meaning every scanned page gets
    OCR'd twice per ingest. Mirrors that function exactly: "\\n\\n" between
    pages, and empty pages omitted entirely.
    """
    return "\n\n".join(
        f"{PAGE_MARKER.format(page=page['page'])}\n{page['text']}"
        for page in pages
        if page["text"]
    )


def report_from_pages(pages: list[dict]) -> dict:
    """Extraction report derived from an already-extracted page list."""
    methods = Counter((page.get("method") or "unknown").lower() for page in pages)
    native = methods.get("native", 0)
    ocr = methods.get("ocr", 0)
    ocr_rejected = methods.get("ocr_rejected", 0)
    return {
        "total_pages": len(pages),
        "native": native,
        "ocr": ocr,
        "ocr_rejected": ocr_rejected,
        # Anything else (e.g. "ocr_failed") counts as failed.
        "failed": len(pages) - native - ocr - ocr_rejected,
        # Empty pages are dropped by compose_document, so no chunk can cite them.
        "empty": sum(1 for page in pages if not (page.get("text") or "").strip()),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest a lecture PDF.")
    parser.add_argument("--university-id", type=int, required=True)
    parser.add_argument("--subject-id", type=int, required=True)
    parser.add_argument("--file", required=True, help="path to the lecture PDF")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="extract, chunk and detect images, print the summary, write nothing",
    )
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE)
    parser.add_argument("--chunk-overlap", type=int, default=CHUNK_OVERLAP)
    parser.add_argument(
        "--vision-model",
        default=VISION_MODEL,
        help="Ollama vision model for figure descriptions (default %(default)s)",
    )
    parser.add_argument(
        "--fallback-vision-model",
        default=FALLBACK_VISION_MODEL,
        help="used when the primary returns nothing; '' to disable "
        "(default %(default)s)",
    )
    parser.add_argument(
        "--ocr-min-alnum",
        type=float,
        default=OCR_MIN_ALNUM_RATIO,
        help="discard OCR text below this alphanumeric ratio (default %(default)s)",
    )
    parser.add_argument(
        "--skip-images", action="store_true", help="skip image description entirely"
    )
    parser.add_argument(
        "--skip-image-classification",
        action="store_true",
        help="keep every extracted image without asking whether it is decorative",
    )
    parser.add_argument("--device", default="cpu", help="embedding device (cpu/cuda)")
    return parser.parse_args()


def print_figure_issues(
    issues: list[FigureIssue], pdf_path: Path, lecture_id: int | None
) -> None:
    """Which figures the vision model failed on, by lecture and page.

    Ollama errors are otherwise only log warnings, which scroll past; a skipped
    figure is permanently missing from chat and quizzes until re-ingested.
    """
    if not issues:
        return
    where = f"lecture {lecture_id} ({pdf_path.name})" if lecture_id is not None else pdf_path.name
    skipped = sorted((i for i in issues if not i.kept), key=lambda i: i.page_number)
    unclassified = sorted((i for i in issues if i.kept), key=lambda i: i.page_number)

    print("-" * 58)
    if skipped:
        print(f"WARNING: {len(skipped)} figure(s) SKIPPED after errors (not stored):")
        for i in skipped:
            print(f"  {where} page {i.page_number}: {i.reason}")
    if unclassified:
        print(f"{len(unclassified)} figure(s) kept WITHOUT a content/decorative verdict:")
        for i in unclassified:
            print(f"  {where} page {i.page_number}: {i.reason}")
    if skipped:
        print("Fix the cause (is Ollama up? is the vision model pulled?), then re-ingest.")


def print_summary(
    *,
    pdf_path: Path,
    report: dict,
    chunk_count: int,
    image_count: int,
    decorative_count: int,
    described_count: int,
    image_failures: int,
    figure_issues: list[FigureIssue],
    saved_count: int | None,
    markers_found: bool,
    dry_run: bool,
    lecture_id: int | None,
) -> None:
    pages = report.get("total_pages") or report.get("pages")
    native = report.get("native")
    ocr = report.get("ocr")
    failed = report.get("failed")

    print("\n" + "=" * 58)
    print(f"{'DRY RUN -- nothing written' if dry_run else 'INGESTION COMPLETE'}")
    print("=" * 58)
    print(f"file                : {pdf_path.name}")
    if lecture_id is not None:
        print(f"lecture_id          : {lecture_id}")
    print(f"pages processed     : {pages}")
    print(f"  native extraction : {native}")
    print(f"  OCR fallback      : {ocr}")
    if report.get("ocr_rejected"):
        print(f"  OCR discarded     : {report['ocr_rejected']}  <- noise; image description only")
    print(f"  failed            : {failed}")
    if report.get("empty"):
        print(f"  empty (no text)   : {report['empty']}  <- omitted from the document")
    print(f"text chunks created : {chunk_count}")
    print(f"images kept         : {image_count}")
    if decorative_count:
        print(f"  decorative dropped: {decorative_count}  <- logos/covers/branding")
    print(f"images described    : {described_count}")
    if image_failures:
        print(f"  description failed: {image_failures}")
    if saved_count is not None:
        print(f"images saved to disk: {saved_count}")
        if saved_count < image_count:
            print(f"  could not encode  : {image_count - saved_count}")
    print_figure_issues(figure_issues, pdf_path, lecture_id)
    if not markers_found:
        print("WARNING: no page markers found -- chunks report page 0.")
    print("=" * 58 + "\n")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()

    pdf_path = Path(args.file).expanduser().resolve()
    if not pdf_path.is_file():
        log.error("No such file: %s", pdf_path)
        return 1

    db = SessionLocal()
    try:
        subject = db.get(Subject, args.subject_id)
        if subject is None:
            log.error("Subject id=%s not found.", args.subject_id)
            return 1
        if subject.university_id != args.university_id:
            log.error(
                "Subject %s belongs to university %s, not %s.",
                subject.id, subject.university_id, args.university_id,
            )
            return 1

        log.info("Subject: %s %s (year %s)", subject.code, subject.name, subject.year)

        log.info("Hashing %s ...", pdf_path.name)
        content_hash = sha256_file(pdf_path)

        existing = db.scalar(
            select(Lecture).where(Lecture.content_hash == content_hash)
        )
        if existing is not None:
            log.info(
                "SKIP: already ingested as lecture id=%s (%s) on %s. "
                "File content is unchanged.",
                existing.id, existing.filename, existing.ingested_at,
            )
            return 0

        # Single extraction pass: both the document text and the report are
        # derived from these pages, so scanned pages are OCR'd exactly once.
        log.info("Extracting text ...")
        pages = extract_pdf_pages(str(pdf_path))

        rejected = gate_ocr_noise(pages, args.ocr_min_alnum)
        for page_no, ratio, length in rejected:
            log.info(
                "  page %s: OCR output discarded as noise "
                "(alnum ratio %.2f < %.2f, %s chars) -- relying on its image description",
                page_no, ratio, args.ocr_min_alnum, length,
            )

        document = compose_document(pages)
        report = report_from_pages(pages)
        log.info(
            "  %s pages (%s native, %s OCR kept, %s OCR discarded, %s failed)",
            report["total_pages"], report["native"], report["ocr"],
            report["ocr_rejected"], report["failed"],
        )

        log.info("Chunking (%s chars / %s overlap) ...", args.chunk_size, args.chunk_overlap)
        chunks, markers_found = chunk_document(
            document, chunk_size=args.chunk_size, chunk_overlap=args.chunk_overlap
        )
        if not markers_found:
            log.warning(
                "No page markers matched in the extracted text -- every chunk "
                "will report page 0. Check chunking.PAGE_MARKER_RE against your "
                "extract_pdf_text() marker format."
            )
        log.info("  %s chunks", len(chunks))

        images = []
        descriptions = []
        image_failures = 0
        figure_issues: list[FigureIssue] = []
        decorative_count = 0
        if args.skip_images:
            log.info("Skipping images (--skip-images).")
        else:
            log.info("Extracting embedded images ...")
            images = extract_images(str(pdf_path))
            log.info("  %s images passed the size/aspect filter", len(images))

            if images and not args.skip_image_classification:
                # Before describing: a textbook cover costs the same minute of
                # vision-model time as a real diagram, and then goes on to
                # mislead a student as a citation. Rejecting here means it is
                # never described, never saved and never citable.
                log.info("Classifying images as content vs decorative ...")
                images, decorative, unclassified = classify_images(
                    images,
                    args.vision_model,
                    args.fallback_vision_model or None,
                    source=pdf_path.name,
                    issues=figure_issues,
                )
                decorative_count = len(decorative)
                log.info(
                    "  %s content, %s decorative%s",
                    len(images), decorative_count,
                    f", {unclassified} unclassified (kept)" if unclassified else "",
                )

            if images:
                log.info(
                    "Describing images with %s (fallback: %s) ...",
                    args.vision_model, args.fallback_vision_model or "none",
                )
                descriptions, image_failures = describe_images(
                    images,
                    args.vision_model,
                    args.fallback_vision_model or None,
                    issues=figure_issues,
                )

        if args.dry_run:
            print_summary(
                pdf_path=pdf_path,
                report=report,
                chunk_count=len(chunks),
                image_count=len(images),
                decorative_count=decorative_count,
                described_count=len(descriptions),
                image_failures=image_failures,
                figure_issues=figure_issues,
                saved_count=None,
                markers_found=markers_found,
                dry_run=True,
                lecture_id=None,
            )
            return 0

        # The Lecture row is flushed first so chunk metadata can carry a real
        # lecture_id, but only committed once Chroma has accepted everything --
        # a Chroma failure must not leave a Lecture row claiming success.
        lecture = Lecture(
            subject_id=subject.id,
            filename=pdf_path.name,
            content_hash=content_hash,
        )
        db.add(lecture)
        db.flush()

        # Only now is there a lecture id to file the images under. If Chroma
        # later rejects the batch the transaction rolls back and these files are
        # orphaned under an id no Lecture row claims -- harmless, and the next
        # successful ingest of the same PDF overwrites them.
        saved = save_images(images, lecture.id)
        if images:
            log.info("Saved %s/%s images to data/images/%s/", len(saved), len(images), lecture.id)

        base_metadata = {
            "university_id": subject.university_id,
            "year": subject.year,
            "subject_id": subject.id,
            "lecture_id": lecture.id,
            "source_filename": pdf_path.name,
        }

        ids, documents, metadatas = [], [], []

        for chunk in chunks:
            ids.append(f"{content_hash}:text:{chunk.index}")
            documents.append(chunk.text)
            metadatas.append(
                {**base_metadata, "page_number": chunk.page_number,
                 "content_type": CONTENT_TYPE_TEXT}
            )

        for description in descriptions:
            ids.append(f"{content_hash}:image:{description.index}")
            documents.append(description.text)
            metadata = {
                **base_metadata,
                "page_number": description.page_number,
                "content_type": CONTENT_TYPE_IMAGE,
            }
            # Absent when the image could not be encoded; the citation then
            # falls back to filename + page, exactly as before.
            relative_path = saved.get((description.page_number, description.page_index))
            if relative_path:
                metadata["image_path"] = relative_path
            metadatas.append(metadata)

        if not documents:
            log.error("Nothing to index -- no text chunks and no image descriptions.")
            db.rollback()
            return 1

        log.info("Embedding and upserting %s records into Chroma ...", len(documents))
        collection = get_collection(device=args.device)
        collection.upsert(ids=ids, documents=documents, metadatas=metadatas)

        db.commit()

        print_summary(
            pdf_path=pdf_path,
            report=report,
            chunk_count=len(chunks),
            image_count=len(images),
            decorative_count=decorative_count,
            described_count=len(descriptions),
            image_failures=image_failures,
            figure_issues=figure_issues,
            saved_count=len(saved),
            markers_found=markers_found,
            dry_run=False,
            lecture_id=lecture.id,
        )
        return 0

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
