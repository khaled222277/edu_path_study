"""Pull real diagrams out of a lecture PDF and describe them with a local LLaVA.

Lecture slides are full of furniture -- logos, header bars, bullet icons, rules.
Describing those wastes minutes of LLaVA time and pollutes retrieval, so images
are filtered on size and aspect ratio first, and repeated images (the university
logo on all 40 slides) are de-duplicated by content hash.

What the shape filter cannot catch -- textbook covers, branding badges, stock
photos, all of which are big and well-proportioned -- is caught by asking the
vision model one cheap binary question per image (see classify_images) before
any description is generated. Only CONTENT images are saved, described and
indexed.

Every image that survives both gates is written to disk (see save_images) so
the student can see the figure a citation points at, not just read its
description. Saving is downstream of filtering and independent of describing:
which images are kept is decided in exactly one place, here.
"""

import hashlib
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from app.config import settings
from app.services.llm import get_client
from app.services.lecture_images import (
    image_filename,
    lecture_dir,
    relative_image_path,
)

log = logging.getLogger(__name__)

# Filtering thresholds. Slide diagrams comfortably clear these; icons and
# header banners do not.
MIN_WIDTH = 150
MIN_HEIGHT = 150
MIN_ASPECT = 0.2  # taller than 1:5 -> a vertical rule or sidebar
MAX_ASPECT = 5.0  # wider than 5:1 -> a header/footer band
MIN_BYTES = 3072

# qwen3-vl reads text off slides far better than llava:latest, which scored
# 3/9 and 0/6 on labels in two verified figures where qwen3-vl:4b scored 9/9
# and 6/6. Override per-run with --vision-model.
VISION_MODEL = settings.vision_model

# qwen3-vl:4b intermittently returns an empty response, and on some figures it
# does so repeatedly -- a real clustering diagram was silently dropped from the
# KDD corpus that way. One retry clears the intermittent case; the fallback
# model covers the rest (8b described that figure correctly).
FALLBACK_VISION_MODEL = settings.fallback_vision_model
DESCRIBE_RETRIES = 1

# Asking a vision model to "name every label" sent llava into a repetition loop
# on low-resolution figures -- it invented hundreds of labels. Keep the prompt
# bounded and pair it with num_predict/repeat_penalty below.
DESCRIBE_PROMPT = (
    "This is a figure from a university Information Technology lecture slide. "
    "In at most 150 words, describe what it shows: the kind of figure (diagram, "
    "chart, table, screenshot), its main components and how they relate, and the "
    "concept it illustrates. "
    "Describe ONLY what is genuinely visible. Do not invent labels, text, "
    "numbers, or translations you cannot clearly read. "
    "If the figure contains Arabic text, transcribe that Arabic exactly; if it "
    "contains no Arabic, do not mention Arabic at all. "
    "If the figure is unclear or unreadable, say so in one sentence instead of "
    "guessing."
)

DESCRIBE_OPTIONS = {
    # 350 was enough for llava but truncated qwen3-vl mid-word on dense
    # multi-panel figures, which are exactly the ones worth describing well.
    # MAX_DESCRIPTION_CHARS below is the real backstop.
    "num_predict": 600,
    "temperature": 0.2,
    "repeat_penalty": 1.2,
}

# Backstop in case a model ignores num_predict.
MAX_DESCRIPTION_CHARS = 2000

# --- Decorative/content classification ------------------------------------
#
# The size/aspect/dedup filter above is about *shape*: it removes bullet icons
# and header bands. It cannot remove a 460x600 textbook cover photograph or a
# glossy "data MINING" badge, which look exactly like real figures by every
# measurement we can take. Four of those reached students as citations from
# kdd_lec01 alone.
#
# Keyword heuristics were considered and rejected: the OCR gate taught us they
# do not generalise, and a book-cover photo matches no "logo"-ish vocabulary at
# all. So the question is put to the vision model directly, before the (much
# more expensive) description is generated.

LABEL_CONTENT = "CONTENT"
LABEL_DECORATIVE = "DECORATIVE"

CLASSIFY_PROMPT = (
    "Is this an educational diagram, chart, table, graph, or figure containing "
    "course content? Or is it decorative (a logo, badge, book/textbook cover, "
    "stock photo, or branding element with no teaching content)? "
    "Answer with exactly one word: CONTENT or DECORATIVE."
)

# qwen3-vl reasons before it answers, and the reasoning is drawn from the SAME
# num_predict budget as the reply. At 20 tokens the budget was spent entirely on
# reasoning and `response` came back empty for all 14 probe images -- the
# verdict never got a chance to be emitted. 300 leaves room for the reasoning to
# finish and the one-word answer to land. (`think=False` does not help: the
# model still reasons and still returns nothing.)
CLASSIFY_OPTIONS = {"num_predict": 300, "temperature": 0.0}

# Matched as whole words: a reply may be bare ("DECORATIVE") or a sentence, and
# on the rare two-label reply the first one stated is the verdict.
_VERDICT_RE = re.compile(rf"\b({LABEL_CONTENT}|{LABEL_DECORATIVE})\b", re.IGNORECASE)


@dataclass
class ExtractedImage:
    page_number: int
    # Position among the kept images on this page (0-based). Together with
    # page_number it names the file on disk.
    page_index: int
    width: int
    height: int
    data: bytes
    sha256: str


@dataclass
class ImageDescription:
    page_number: int
    text: str
    index: int
    # Carried over from the ExtractedImage this describes, so the caller can
    # point the description's Chroma record at the right file.
    page_index: int


def _passes_filter(width: int, height: int, size_bytes: int) -> bool:
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return False
    if size_bytes < MIN_BYTES:
        return False
    if height == 0:
        return False
    aspect = width / height
    return MIN_ASPECT <= aspect <= MAX_ASPECT


def _is_degenerate(text: str, min_words: int = 40) -> bool:
    """True if the description looks like a vision-model runaway rather than prose.

    Two distinct failure modes show up on low-resolution slide figures:
      * label enumeration -- long comma-separated runs of invented one- and
        two-character labels ("A, B, C, ... AA, AB"). Token vocabulary is highly
        *varied*, so a uniqueness test misses it; the tell is tiny mean token
        length and heavy comma density.
      * stuck repetition -- the same phrase emitted over and over, which a
        uniqueness test does catch.
    """
    words = text.split()
    if len(words) < min_words:
        return False

    stripped = [w.lower().strip(",.;:()[]") for w in words]
    stripped = [w for w in stripped if w]
    if not stripped:
        return True

    unique_ratio = len(set(stripped)) / len(stripped)
    short_fraction = sum(1 for w in stripped if len(w) <= 2) / len(stripped)
    comma_density = text.count(",") / len(words)

    if unique_ratio < 0.25:  # stuck repetition
        return True
    if short_fraction > 0.45 and comma_density > 0.4:  # label enumeration
        return True
    return False


def extract_images(pdf_path: str) -> list[ExtractedImage]:
    """Embedded images that look like real figures, de-duplicated document-wide."""
    images: list[ExtractedImage] = []
    seen: set[str] = set()

    with fitz.open(pdf_path) as doc:
        for page_number, page in enumerate(doc, start=1):
            kept_on_page = 0
            for xref, *_ in page.get_images(full=True):
                try:
                    raw = doc.extract_image(xref)
                except Exception as exc:
                    log.warning(
                        "page %s: could not extract image %s (%s)", page_number, xref, exc
                    )
                    continue

                data = raw["image"]
                width, height = raw["width"], raw["height"]

                if not _passes_filter(width, height, len(data)):
                    continue

                digest = hashlib.sha256(data).hexdigest()
                if digest in seen:  # same logo/figure repeated across slides
                    continue
                seen.add(digest)

                images.append(
                    ExtractedImage(
                        page_number=page_number,
                        page_index=kept_on_page,
                        width=width,
                        height=height,
                        data=data,
                        sha256=digest,
                    )
                )
                kept_on_page += 1

    return images


def _write_png(data: bytes, path: Path) -> None:
    """Re-encode one embedded image as PNG.

    PDFs embed whatever their author had -- JPEG, JPEG 2000, CMYK, 1-bit masks.
    A browser has to render these, so they are normalised on the way out rather
    than written through with their original bytes. Pillow handles almost
    everything; PyMuPDF's rasteriser covers the JPX-type formats it does not.
    """
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.mode not in ("RGB", "RGBA", "L"):
                im = im.convert("RGB")
            im.save(path, format="PNG")
        return
    except Exception as exc:
        log.debug("Pillow could not decode %s (%s); trying PyMuPDF", path.name, exc)

    pixmap = fitz.Pixmap(data)
    if pixmap.n - pixmap.alpha >= 4:  # CMYK has no PNG representation
        pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
    pixmap.save(str(path))


def save_images(images: list[ExtractedImage], lecture_id: int) -> dict[tuple[int, int], str]:
    """Write each kept image to data/images/{lecture_id}/.

    Returns {(page_number, page_index): relative_path} for the ones that were
    written -- an image whose bytes no decoder accepts is skipped rather than
    failing the ingest, and simply gets no `image_path` in its metadata.
    """
    if not images:
        return {}

    destination = lecture_dir(lecture_id)
    destination.mkdir(parents=True, exist_ok=True)

    saved: dict[tuple[int, int], str] = {}
    for image in images:
        name = image_filename(image.page_number, image.page_index)
        try:
            _write_png(image.data, destination / name)
        except Exception as exc:
            log.warning("page %s: could not save %s (%s)", image.page_number, name, exc)
            continue
        saved[(image.page_number, image.page_index)] = relative_image_path(
            lecture_id, image.page_number, image.page_index
        )

    return saved


def _parse_verdict(text: str) -> str | None:
    match = _VERDICT_RE.search(text or "")
    return match.group(1).upper() if match else None


def classify_image(
    data: bytes,
    model: str = VISION_MODEL,
    fallback: str | None = FALLBACK_VISION_MODEL,
    retries: int = DESCRIBE_RETRIES,
) -> str | None:
    """LABEL_CONTENT, LABEL_DECORATIVE, or None if the model never gave a verdict.

    None means "we do not know", and every caller treats that as keep. Dropping
    a figure because a model call failed is the same mistake the OCR gate made:
    losing one real diagram costs a student far more than keeping one logo.
    """
    attempts = [model] * (retries + 1)
    if fallback and fallback != model:
        attempts += [fallback] * (retries + 1)

    for attempt_model in attempts:
        try:
            response = _client().generate(
                model=attempt_model,
                prompt=CLASSIFY_PROMPT,
                images=[data],
                options=CLASSIFY_OPTIONS,
            )
        except Exception as exc:
            log.warning("classification call failed on %s (%s)", attempt_model, exc)
            continue

        verdict = _parse_verdict(response.get("response") or "")
        if verdict:
            return verdict

    return None


def classify_images(
    images: list[ExtractedImage],
    model: str = VISION_MODEL,
    fallback: str | None = FALLBACK_VISION_MODEL,
    source: str = "",
) -> tuple[list[ExtractedImage], list[ExtractedImage], int]:
    """Split `images` into (content, decorative, unclassified_count).

    Unclassified images stay in `content` -- see classify_image. Every rejection
    is logged with its page so the classifier can be audited for over-rejection.
    """
    content: list[ExtractedImage] = []
    decorative: list[ExtractedImage] = []
    unclassified = 0

    for i, image in enumerate(images, start=1):
        verdict = classify_image(image.data, model, fallback)
        if verdict == LABEL_DECORATIVE:
            decorative.append(image)
            log.info(
                "  DECORATIVE %s page %s (image %s/%s, %sx%s) -- skipped, not described",
                source or "image", image.page_number, i, len(images),
                image.width, image.height,
            )
            continue
        if verdict is None:
            unclassified += 1
            log.warning(
                "  page %s: no classification verdict -- keeping the image",
                image.page_number,
            )
        content.append(image)

    return content, decorative, unclassified


def describe_image(image: ExtractedImage, model: str = VISION_MODEL) -> str | None:
    """One vision-model description, or None if the call failed."""
    try:
        response = _client().generate(
            model=model,
            prompt=DESCRIBE_PROMPT,
            images=[image.data],
            options=DESCRIBE_OPTIONS,
        )
    except Exception as exc:
        log.warning("page %s: description failed (%s)", image.page_number, exc)
        return None

    text = (response.get("response") or "").strip()
    if not text:
        log.warning(
            "page %s: model returned an empty description (%sx%s) -- skipped",
            image.page_number, image.width, image.height,
        )
        return None

    if len(text) > MAX_DESCRIPTION_CHARS:
        log.warning(
            "page %s: description truncated at %s chars (model ran long)",
            image.page_number, MAX_DESCRIPTION_CHARS,
        )
        text = text[:MAX_DESCRIPTION_CHARS].rstrip()

    if _is_degenerate(text):
        log.warning(
            "page %s: description looks degenerate (repetition loop) -- discarded",
            image.page_number,
        )
        return None

    return text


def describe_image_resilient(
    image: ExtractedImage,
    model: str = VISION_MODEL,
    fallback: str | None = FALLBACK_VISION_MODEL,
    retries: int = DESCRIBE_RETRIES,
) -> str | None:
    """Describe `image`, retrying and then falling back to a second model."""
    # Empty responses are stochastic in both models -- a figure that comes back
    # empty from qwen3-vl:4b often succeeds on a plain retry, and one that 8b
    # refuses may succeed on 4b. So retry each model rather than trusting a
    # single attempt from either.
    for attempt in range(retries + 1):
        text = describe_image(image, model)
        if text:
            return text
        if attempt < retries:
            log.info("  page %s: empty result, retrying %s", image.page_number, model)

    if fallback and fallback != model:
        for attempt in range(retries + 1):
            log.info("  page %s: falling back to %s", image.page_number, fallback)
            text = describe_image(image, fallback)
            if text:
                return text

    return None


def describe_images(
    images: list[ExtractedImage],
    model: str = VISION_MODEL,
    fallback: str | None = FALLBACK_VISION_MODEL,
) -> tuple[list[ImageDescription], int]:
    """Describe every image. Returns (descriptions, failure_count)."""
    descriptions: list[ImageDescription] = []
    failures = 0

    for i, image in enumerate(images, start=1):
        log.info(
            "  describing image %s/%s (page %s, %sx%s)",
            i, len(images), image.page_number, image.width, image.height,
        )
        text = describe_image_resilient(image, model, fallback)
        if text is None:
            failures += 1
            log.warning(
                "  page %s: no description after retry and fallback -- figure skipped",
                image.page_number,
            )
            continue
        descriptions.append(
            ImageDescription(
                page_number=image.page_number,
                text=text,
                index=len(descriptions),
                page_index=image.page_index,
            )
        )

    return descriptions, failures


def _client():
    """Ollama client with the (long) ingestion timeout."""
    return get_client(settings.ollama_ingest_timeout_seconds)
