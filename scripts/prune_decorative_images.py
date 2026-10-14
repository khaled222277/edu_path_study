"""Re-classify already-ingested figures and remove the decorative ones.

    python scripts/prune_decorative_images.py --dry-run
    python scripts/prune_decorative_images.py
    python scripts/prune_decorative_images.py --lecture-id 6

Logos, badges, textbook covers and stock photos cleared the size/aspect filter,
got described, and became citations -- a student asking about data mining could
be shown the cover of Tan et al. instead of a diagram. This asks the vision
model the same CONTENT/DECORATIVE question ingestion now asks, once per
already-stored figure, and removes the ones it rejects: the Chroma chunk goes,
the PNG goes, nothing can cite them again.

Anything classified CONTENT is left completely untouched -- its description is
not regenerated, its file is not rewritten, its metadata is not modified. The
descriptions in the corpus have been reviewed and this pass is not entitled to
change them.

Classification runs against the *saved PNG*, not a fresh extraction from the
PDF, so the bytes being judged are exactly the bytes being served.

Every removal is written to a report file (--report, default
data/decorative_pruned.txt) with lecture, page, and the description that was
stored, so the classifier can be audited for over-rejection.
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import SessionLocal  # noqa: E402
from app.models.lecture import Lecture  # noqa: E402
from app.services.lecture_images import IMAGES_ROOT  # noqa: E402
from app.services.vector_store import CONTENT_TYPE_IMAGE, get_collection  # noqa: E402
from ingestion.images import (  # noqa: E402
    FALLBACK_VISION_MODEL,
    LABEL_DECORATIVE,
    VISION_MODEL,
    classify_image,
)

log = logging.getLogger("prune")

DEFAULT_REPORT = IMAGES_ROOT.parent / "decorative_pruned.txt"


def image_records(collection, lecture_id: int) -> list[tuple[str, dict, str]]:
    """(chunk_id, metadata, description) for one lecture, in description order."""
    result = collection.get(
        where={
            "$and": [{"lecture_id": lecture_id}, {"content_type": CONTENT_TYPE_IMAGE}]
        },
        include=["metadatas", "documents"],
    )
    records = list(zip(result["ids"], result["metadatas"], result["documents"]))
    records.sort(key=lambda r: int(r[0].rsplit(":", 1)[1]))
    return records


def review_lecture(collection, lecture: Lecture, args) -> tuple[list[dict], dict]:
    """Classify every stored figure. Returns (rejections, counters)."""
    records = image_records(collection, lecture.id)
    if not records:
        log.info("Lecture %s: %s -- no figures", lecture.id, lecture.filename)
        return [], {}

    log.info("Lecture %s: %s (%s figures)", lecture.id, lecture.filename, len(records))

    rejections: list[dict] = []
    counters = {"reviewed": 0, "content": 0, "decorative": 0, "unclassified": 0, "missing": 0}

    for chunk_id, metadata, description in records:
        relative_path = metadata.get("image_path")
        path = IMAGES_ROOT / relative_path if relative_path else None
        if path is None or not path.is_file():
            # Nothing on disk to judge. Leaving it alone is the conservative
            # choice: the description is still valid lecture material even
            # though no image can be shown for it.
            counters["missing"] += 1
            log.warning(
                "  page %s: no image file (%s) -- left alone",
                metadata.get("page_number"), relative_path or "no image_path",
            )
            continue

        counters["reviewed"] += 1
        verdict = classify_image(
            path.read_bytes(), args.vision_model, args.fallback_vision_model or None
        )

        if verdict is None:
            counters["unclassified"] += 1
            log.warning(
                "  page %s: no verdict -- kept", metadata.get("page_number")
            )
            continue

        if verdict != LABEL_DECORATIVE:
            counters["content"] += 1
            continue

        counters["decorative"] += 1
        rejections.append(
            {
                "lecture_id": lecture.id,
                "filename": lecture.filename,
                "page_number": metadata.get("page_number"),
                "chunk_id": chunk_id,
                "image_path": relative_path,
                "description": (description or "").strip(),
            }
        )
        log.info(
            "  DECORATIVE page %s (%s): %s",
            metadata.get("page_number"), relative_path,
            (description or "").strip()[:90].replace("\n", " "),
        )

    return rejections, counters


def apply_removals(collection, rejections: list[dict]) -> tuple[int, int]:
    """Delete the chunks then the files. Returns (chunks_deleted, files_deleted)."""
    if not rejections:
        return 0, 0

    # Chunk first: if the file delete fails afterwards we are left with an
    # orphaned PNG that nothing links to, which is harmless. The reverse order
    # would leave a citable chunk pointing at a file that no longer exists.
    collection.delete(ids=[r["chunk_id"] for r in rejections])

    files_deleted = 0
    for rejection in rejections:
        path = IMAGES_ROOT / rejection["image_path"]
        try:
            path.unlink()
            files_deleted += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not delete %s (%s)", path, exc)

    return len(rejections), files_deleted


def write_report(path: Path, rejections: list[dict], dry_run: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Decorative figures {'identified' if dry_run else 'removed'} "
        f"{datetime.now().isoformat(timespec='seconds')}",
        f"# {len(rejections)} figure(s)",
        "",
    ]
    for rejection in rejections:
        lines += [
            f"lecture {rejection['lecture_id']}  {rejection['filename']}  "
            f"page {rejection['page_number']}",
            f"  file  : {rejection['image_path']}",
            f"  chunk : {rejection['chunk_id']}",
            f"  stored description: {rejection['description'][:400]}",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Report written to %s", path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--lecture-id",
        type=int,
        action="append",
        help="only this lecture (repeatable); default is every lecture",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="classify and report, delete nothing",
    )
    parser.add_argument("--vision-model", default=VISION_MODEL)
    parser.add_argument(
        "--fallback-vision-model",
        default=FALLBACK_VISION_MODEL,
        help="used when the primary gives no verdict; '' to disable",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="where to write the removal list (default %(default)s)",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()

    db = SessionLocal()
    collection = get_collection()
    totals = {"reviewed": 0, "content": 0, "decorative": 0, "unclassified": 0, "missing": 0}
    rejections: list[dict] = []

    try:
        query = db.query(Lecture).order_by(Lecture.id)
        if args.lecture_id:
            query = query.filter(Lecture.id.in_(args.lecture_id))
        lectures = query.all()

        if not lectures:
            log.error("No matching lectures.")
            return 1

        for lecture in lectures:
            lecture_rejections, counters = review_lecture(collection, lecture, args)
            rejections += lecture_rejections
            for key, value in counters.items():
                totals[key] += value

        chunks_deleted = files_deleted = 0
        if not args.dry_run:
            chunks_deleted, files_deleted = apply_removals(collection, rejections)

        write_report(args.report, rejections, args.dry_run)
    finally:
        db.close()

    print("\n" + "=" * 58)
    print("DRY RUN -- nothing removed" if args.dry_run else "PRUNE COMPLETE")
    print("=" * 58)
    print(f"lectures reviewed    : {len(lectures)}")
    print(f"figures classified   : {totals['reviewed']}")
    print(f"  content (untouched): {totals['content']}")
    print(f"  decorative         : {totals['decorative']}")
    if totals["unclassified"]:
        print(f"  no verdict (kept)  : {totals['unclassified']}")
    if totals["missing"]:
        print(f"  no image file      : {totals['missing']}  <- left alone")
    if not args.dry_run:
        print(f"chunks deleted       : {chunks_deleted}")
        print(f"PNG files deleted    : {files_deleted}")
    print(f"report               : {args.report}")
    print("=" * 58 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
