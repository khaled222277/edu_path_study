"""Save the figure images for lectures that were ingested before we kept them.

    python scripts/backfill_lecture_images.py --dry-run
    python scripts/backfill_lecture_images.py
    python scripts/backfill_lecture_images.py --lecture-id 14

Re-extracts each lecture's images with the *same* filter as ingestion, writes
them to data/images/{lecture_id}/, and stamps `image_path` onto the Chroma
records that already hold their descriptions.

The vision model is never called. Every description in the corpus has been
reviewed and stays exactly as it is -- this only goes back for the image files
that extraction already decided were worth keeping.

Matching descriptions to images
-------------------------------
A description's Chroma id carries its position in the description list, which
is not its position in the image list: a figure whose description failed leaves
no record, and at least one lecture had a figure re-described and appended out
of order afterwards. Position is therefore not trustworthy.

Page number is. Both lists are built by walking the document in page order, so
the descriptions for page N are the images of page N in the same order. This
matches per page and reports any page where the two counts disagree instead of
guessing.
"""

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import SessionLocal  # noqa: E402
from app.models.lecture import Lecture  # noqa: E402
from app.services.lecture_images import PROJECT_ROOT  # noqa: E402
from app.services.vector_store import CONTENT_TYPE_IMAGE, get_collection  # noqa: E402
from ingestion.images import extract_images, save_images  # noqa: E402

log = logging.getLogger("backfill")

LECTURES_ROOT = PROJECT_ROOT / "data" / "lectures"


def find_pdf(filename: str) -> Path | None:
    """Locate the original PDF. Lecture rows store a bare filename, not a path."""
    matches = sorted(LECTURES_ROOT.rglob(filename))
    if len(matches) > 1:
        log.warning("  %s: %s copies found, using %s", filename, len(matches), matches[0])
    return matches[0] if matches else None


def description_records(collection, lecture_id: int) -> list[tuple[str, dict]]:
    """(id, metadata) for this lecture's image descriptions, in description order."""
    result = collection.get(
        where={
            "$and": [
                {"lecture_id": lecture_id},
                {"content_type": CONTENT_TYPE_IMAGE},
            ]
        },
        include=["metadatas"],
    )
    records = list(zip(result["ids"], result["metadatas"]))
    # Chroma does not promise an order; the trailing integer of the id is the
    # description index, which does.
    records.sort(key=lambda r: int(r[0].rsplit(":", 1)[1]))
    return records


def plan_lecture(collection, lecture: Lecture) -> tuple[list, list, list[str]] | None:
    """Work out which image file each description belongs to.

    Returns (images, [(chunk_id, metadata, image_key)], problems), or None if
    the lecture cannot be processed at all.
    """
    pdf = find_pdf(lecture.filename)
    if pdf is None:
        log.error("  no PDF found under data/lectures for %s", lecture.filename)
        return None

    records = description_records(collection, lecture.id)
    if not records:
        log.info("  no image descriptions in Chroma -- nothing to do")
        return [], [], []

    images = extract_images(str(pdf))

    images_by_page: dict[int, list] = defaultdict(list)
    for image in images:
        images_by_page[image.page_number].append(image)

    records_by_page: dict[int, list] = defaultdict(list)
    for chunk_id, metadata in records:
        records_by_page[int(metadata["page_number"])].append((chunk_id, metadata))

    problems: list[str] = []
    assignments: list[tuple[str, dict, tuple[int, int]]] = []

    for page in sorted(records_by_page):
        page_records = records_by_page[page]
        page_images = images_by_page.get(page, [])
        if len(page_images) != len(page_records):
            problems.append(
                f"page {page}: {len(page_records)} description(s) but "
                f"{len(page_images)} extracted image(s)"
            )
        for (chunk_id, metadata), image in zip(page_records, page_images):
            assignments.append((chunk_id, metadata, (page, image.page_index)))

    return images, assignments, problems


def backfill_lecture(collection, lecture: Lecture, dry_run: bool) -> dict:
    log.info("Lecture %s: %s", lecture.id, lecture.filename)

    planned = plan_lecture(collection, lecture)
    if planned is None:
        return {"lectures_failed": 1}
    images, assignments, problems = planned

    for problem in problems:
        log.warning("  MISMATCH %s -- those descriptions are left unlinked", problem)

    if not assignments and not images:
        return {}

    log.info(
        "  %s image(s) extracted, %s description(s) matched", len(images), len(assignments)
    )

    if dry_run:
        return {
            "images": len(images),
            "matched": len(assignments),
            "pages_mismatched": len(problems),
        }

    saved = save_images(images, lecture.id)
    log.info("  saved %s/%s image file(s)", len(saved), len(images))

    ids, metadatas = [], []
    missing_file = 0
    for chunk_id, metadata, key in assignments:
        relative_path = saved.get(key)
        if relative_path is None:  # the image could not be encoded
            missing_file += 1
            continue
        # Chroma replaces the metadata it is handed, so the existing fields are
        # carried through rather than re-specified.
        ids.append(chunk_id)
        metadatas.append({**metadata, "image_path": relative_path})

    if ids:
        collection.update(ids=ids, metadatas=metadatas)
    log.info("  stamped image_path on %s chunk(s)", len(ids))

    return {
        "images": len(images),
        "saved": len(saved),
        "updated": len(ids),
        "unencodable": missing_file,
        "pages_mismatched": len(problems),
    }


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
        help="report the matching, write no files and no metadata",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()

    db = SessionLocal()
    collection = get_collection()
    totals: dict[str, int] = defaultdict(int)

    try:
        query = db.query(Lecture).order_by(Lecture.id)
        if args.lecture_id:
            query = query.filter(Lecture.id.in_(args.lecture_id))
        lectures = query.all()

        if not lectures:
            log.error("No matching lectures.")
            return 1

        for lecture in lectures:
            for key, value in backfill_lecture(collection, lecture, args.dry_run).items():
                totals[key] += value
    finally:
        db.close()

    print("\n" + "=" * 58)
    print("DRY RUN -- nothing written" if args.dry_run else "BACKFILL COMPLETE")
    print("=" * 58)
    print(f"lectures processed   : {len(lectures)}")
    print(f"images extracted     : {totals['images']}")
    if not args.dry_run:
        print(f"image files written  : {totals['saved']}")
        print(f"chunks given a path  : {totals['updated']}")
        if totals["unencodable"]:
            print(f"  image unreadable   : {totals['unencodable']}")
    else:
        print(f"descriptions matched : {totals['matched']}")
    if totals["pages_mismatched"]:
        print(f"pages needing review : {totals['pages_mismatched']}  <- see MISMATCH above")
    if totals["lectures_failed"]:
        print(f"lectures skipped     : {totals['lectures_failed']}  <- PDF not found")
    print("=" * 58 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
