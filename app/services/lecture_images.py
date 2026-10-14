"""Where the figures extracted from lecture PDFs live on disk, and how they are named.

Chroma holds the vision-model *description* of each figure; the image file
itself lands here, under a directory per lecture. Splitting it that way keeps
the access check cheap: the lecture id is right there in the URL, so serving an
image is a lecture lookup plus the same university/year rule chat and quiz use.

Ingestion, the backfill script and the serving route all derive names through
this module so a file written months ago still resolves today.
"""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
IMAGES_ROOT = PROJECT_ROOT / "data" / "images"

# The API path the serving router is mounted at. Kept here so the URLs baked
# into chat citations cannot drift from the route that has to answer them.
ROUTE_PREFIX = "/lectures/images"

# page_{page}_{index}.png, where index counts the kept images *within that page*
# rather than across the document. A per-page counter is what makes the name
# reproducible: re-extracting the same PDF later yields the same names even if
# an earlier page's images were described in a different order.
FILENAME_RE = re.compile(r"^page_(\d+)_(\d+)\.png$")


def image_filename(page_number: int, page_index: int) -> str:
    return f"page_{page_number}_{page_index}.png"


def relative_image_path(lecture_id: int, page_number: int, page_index: int) -> str:
    """The value stored in Chroma metadata as `image_path`.

    Relative to IMAGES_ROOT and always POSIX-separated, so a corpus built on
    Windows still resolves when the API runs on Linux.
    """
    return f"{lecture_id}/{image_filename(page_number, page_index)}"


def lecture_dir(lecture_id: int) -> Path:
    return IMAGES_ROOT / str(lecture_id)


def image_url(relative_path: str) -> str:
    """API path that serves `relative_path`, for the frontend to load."""
    return f"{ROUTE_PREFIX}/{relative_path}"


def resolve(lecture_id: int, filename: str) -> Path | None:
    """Absolute path of a stored image, or None if it is not one of ours.

    `filename` comes straight off the URL, so it is matched against
    FILENAME_RE rather than sanitised -- a whitelist of the exact shape we
    write means "..", separators and absolute paths never reach the
    filesystem at all.
    """
    if not FILENAME_RE.match(filename):
        return None
    path = lecture_dir(lecture_id) / filename
    return path if path.is_file() else None
