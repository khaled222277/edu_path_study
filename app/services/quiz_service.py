"""Quiz generation over a subject's lecture corpus.

Sampling is deliberately different from chat retrieval: chat wants the few
chunks closest to one question, whereas a quiz wants breadth. Drawing the top-N
for a single query would produce five questions from one lecture, so chunks are
instead spread round-robin across the subject's lectures.
"""

import json
import logging
import random
import re
from dataclasses import dataclass

from app.config import settings
from app.services.llm import LLM_ERRORS, LLMUnavailableError, get_client
from app.services.vector_store import get_collection, retry_if_stale

log = logging.getLogger(__name__)

DEFAULT_QUESTION_COUNT = 5

QUIZ_MODEL = settings.quiz_model

# Carried over from the V1 prototype: models leak Latin script and placeholder
# options into Arabic quizzes often enough that generation has to be validated
# and retried rather than trusted. Strict pass first, then a relaxed pass that
# only loosens the Latin-script rule, and only for Arabic.
STRICT_ATTEMPTS = 8
RELAXED_ATTEMPTS = 4

# Retries only help if sampling varies between them -- at temperature 0 all
# eight attempts would return the same rejected output.
QUIZ_OPTIONS = {"temperature": 0.5, "num_predict": 700}

# Short chunks (slide titles, stray fragments) cannot support a fair question
# with four plausible options.
MIN_CHUNK_CHARS = 220

LANG_AR = "ar"
LANG_EN = "en"


class QuizGenerationError(Exception):
    """Lecture material existed but no chunk yielded a valid question."""


@dataclass
class GeneratedQuestion:
    """One validated multiple-choice item, with the chunk it came from."""

    question_text: str
    options: list[str]
    correct_answer_index: int
    source: "SourceChunk"


@dataclass
class SourceChunk:
    """A chunk chosen as the grounding for one question."""

    text: str
    source_filename: str
    page_number: int
    content_type: str
    lecture_id: int

    @property
    def citation(self) -> str:
        return f"{self.source_filename}, page {self.page_number}"


def _is_arabic(ch: str) -> bool:
    return "؀" <= ch <= "ۿ" or "ݐ" <= ch <= "ݿ"


def detect_language(text: str) -> str:
    """Which language the student is writing in.

    A plain Arabic-character ratio does not work, because the two mixed cases
    point opposite ways: "ما هو الـ clustering في data mining؟" is 31% Arabic and
    wants Arabic, while "Explain الانحدار الخطي briefly" is 48% Arabic and wants
    English. What separates them is the script the *sentence* is framed in --
    Arabic question words versus an English instruction carrying an Arabic term.
    So mixed text is decided by the script it opens in.
    """
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return LANG_EN

    ratio = sum(1 for ch in letters if _is_arabic(ch)) / len(letters)
    if ratio >= 0.7:
        return LANG_AR
    if ratio < 0.15:
        return LANG_EN
    return LANG_AR if _is_arabic(letters[0]) else LANG_EN


def sample_chunks(
    subject_id: int,
    count: int = DEFAULT_QUESTION_COUNT,
    min_chars: int = MIN_CHUNK_CHARS,
    seed: int | None = None,
) -> list[SourceChunk]:
    """Pick `count` chunks spread as widely as possible across the subject.

    Round-robins over lectures so a five-question quiz draws on five different
    lectures where the subject has that many, and never twice from one page.
    """
    stored = retry_if_stale(
        lambda: get_collection().get(
            where={"subject_id": subject_id}, include=["documents", "metadatas"]
        )
    )
    documents = stored.get("documents") or []
    metadatas = stored.get("metadatas") or []

    by_lecture: dict[int, list[SourceChunk]] = {}
    skipped_decorative = 0
    for text, meta in zip(documents, metadatas):
        if not text or len(text) < min_chars:
            continue
        # Logos and title slides carry no concept to examine. Only figure
        # descriptions are screened -- lecture text is never decoration.
        if meta.get("content_type") == "image_description" and is_decorative(text):
            skipped_decorative += 1
            continue
        lecture_id = int(meta.get("lecture_id", 0))
        by_lecture.setdefault(lecture_id, []).append(
            SourceChunk(
                text=text,
                source_filename=meta.get("source_filename", "unknown"),
                page_number=int(meta.get("page_number", 0)),
                content_type=meta.get("content_type", "text"),
                lecture_id=lecture_id,
            )
        )

    if not by_lecture:
        log.warning("subject %s has no chunks long enough to build a quiz", subject_id)
        return []

    rng = random.Random(seed)
    for chunks in by_lecture.values():
        rng.shuffle(chunks)

    lecture_ids = sorted(by_lecture)
    rng.shuffle(lecture_ids)

    selected: list[SourceChunk] = []
    used_pages: set[tuple[int, int]] = set()
    round_index = 0

    # Take at most one per lecture per pass, so lectures are only revisited once
    # every other lecture has contributed.
    while len(selected) < count:
        progressed = False
        for lecture_id in lecture_ids:
            if len(selected) >= count:
                break
            pool = by_lecture[lecture_id]
            if round_index >= len(pool):
                continue
            chunk = pool[round_index]
            key = (chunk.lecture_id, chunk.page_number)
            if key in used_pages:
                continue
            selected.append(chunk)
            used_pages.add(key)
            progressed = True
        if not progressed:
            break
        round_index += 1

    log.info(
        "sampled %s chunks for subject %s across %s lecture(s) "
        "(%s decorative figure(s) skipped)",
        len(selected), subject_id, len({c.lecture_id for c in selected}),
        skipped_decorative,
    )
    return selected


# --- validation (ported from V1, thresholds unchanged) --------------------

# Keep Arabic, Arabic-Indic digits, basic Latin, digits, whitespace and common
# punctuation. Drops Chinese/Cyrillic/garbled characters the model sometimes
# leaks.
_CLEAN_RE = re.compile(
    r"[^؀-ۿ٠-٩a-zA-Z0-9\s\.,!?؟،؛\(\)\[\]\-_/:%]"
)
_WHITESPACE_RE = re.compile(r"\s+")
# "Option 4", "Choice2", "الخيار 3", "خيار 1" -- not a general heuristic.
_PLACEHOLDER_RE = re.compile(
    r"^(option|choice|الخيار|خيار)\s*\d+$", flags=re.IGNORECASE
)
_BRACKETED_RE = re.compile(r"[\(\[].*?[\)\]]")
_BRACKETED_CAPTURE_RE = re.compile(r"[\(\[]([^)\]]+)[\)\]]")
_ARABIC_RE = re.compile(r"[؀-ۿ]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_FENCE_RE = re.compile(r"```(?:json)?|```")

MAX_BRACKETED_LATIN = 10  # "(SQL)" is fine, a whole English clause is not

# A question that points at the source ("what does the diagram show?") is
# unanswerable -- the student only ever sees generated text. Banning the phrases
# in the prompt is not enough on its own: told not to say "this diagram", the
# model simply switched to "the diagram", so this is enforced at validation.
# "table" is deliberately absent: tables are frequently the content itself
# (the PlayTennis training table), not a pointer at an unseen image.
_DEIXIS_RE = re.compile(
    # Determiner + up to two modifiers + a figure noun, so "this scatter plot"
    # and "the decision tree diagram" are caught, not just "this plot".
    r"\b(?:this|that|these|those|the|given|above|following)\s+"
    r"(?:\w+\s+){0,2}"
    r"(?:figure|diagram|image|chart|graph|slide|logo|screenshot|picture"
    r"|illustration|visual|plot)\b"
    r"|\b(?:shown|illustrated|depicted|described|displayed|presented)\s+"
    r"(?:in|by|above|below)\b"
    r"|هذا الشكل|هذه الصورة|هذا الشعار|هذه الشريحة|هذا المخطط|هذا الرسم"
    r"|الشكل التالي|الصورة التالية"
    r"|في الشكل|في الصورة|في المخطط|في الرسم|في الشاشة|في الشريحة"
    r"|هذه الشاشة|الشكل أعلاه|الصورة أعلاه",
    re.IGNORECASE,
)

# Figure descriptions that are pure decoration -- a logo or title slide has no
# concept to examine, so no prompt can make a fair question out of one.
#
# Merely containing "logo" is far too blunt: a real KDD process flowchart that
# mentions "the university logo in the corner", and a genuine machine-learning
# diagram whose title sits in "an orange banner", both get wrongly excluded.
# What separates them is whether the decorative word is the description's
# *subject* or an incidental detail -- so whichever kind of figure-noun the
# description reaches first decides it.
_DECORATIVE_NOUN_RE = re.compile(
    r"\b(logo|banner|watermark|letterhead|title slide|cover slide)\b", re.IGNORECASE
)
# "figure" is deliberately excluded -- it is a generic lead-in ("Figure: A
# rectangular banner..."), not a statement of what the image contains.
_CONTENT_NOUN_RE = re.compile(
    r"\b(diagram|chart|graph|plot|table|flowchart|screenshot|tree|map|matrix"
    r"|histogram|equation|formula|architecture|topology|timeline)\b",
    re.IGNORECASE,
)


def references_unseen_source(text: str) -> bool:
    """True if the question points at material the student cannot see."""
    return bool(_DEIXIS_RE.search(str(text or "")))


def is_decorative(chunk_text: str) -> bool:
    """True if a figure description is decoration rather than teaching content.

    Decided by which figure-noun the description mentions first: "This is a
    logo ..." is decoration, while "This is a flowchart ... and the university
    logo in the corner" is content that merely contains one.
    """
    text = str(chunk_text or "")
    decorative = _DECORATIVE_NOUN_RE.search(text)
    if decorative is None:
        return False
    content = _CONTENT_NOUN_RE.search(text)
    return content is None or decorative.start() < content.start()


def contains_arabic(text: str) -> bool:
    return bool(_ARABIC_RE.search(str(text)))


def clean_text(value) -> str:
    text = str(value or "").strip()
    text = _CLEAN_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def is_bad_placeholder(option) -> bool:
    text = str(option or "").strip()
    if not text:
        return True
    return bool(_PLACEHOLDER_RE.match(text))


def is_all_arabic_required(text, in_arabic: bool) -> bool:
    """Arabic-script purity check: Latin only inside short bracketed terms."""
    if not in_arabic:
        return True
    value = str(text or "")
    if not contains_arabic(value):
        return False
    # Latin outside parentheses/brackets is leakage.
    if _LATIN_RE.search(_BRACKETED_RE.sub("", value)):
        return False
    # Inside them, only short tokens like "(SQL)" are tolerated.
    for segment in _BRACKETED_CAPTURE_RE.findall(value):
        if _LATIN_RE.search(segment) and len(segment.strip()) > MAX_BRACKETED_LATIN:
            return False
    return True


def valid_item(question: str, options: list[str], answer: str, in_arabic: bool) -> bool:
    if not question or len(question) < 3:
        return False
    if len(options) != 4:
        return False
    if len({o.strip() for o in options}) != 4:
        return False
    if any(is_bad_placeholder(o) for o in options):
        return False
    if answer not in options:
        return False
    if references_unseen_source(question):
        return False
    if in_arabic:
        if not is_all_arabic_required(question, True):
            return False
        if any(not is_all_arabic_required(o, True) for o in options):
            return False
        if not is_all_arabic_required(answer, True):
            return False
    return True


# --- generation -----------------------------------------------------------


def _build_prompt(context: str, in_arabic: bool) -> str:
    lang = "Arabic" if in_arabic else "English"
    script_rule = (
        "Write the question, all options, and the answer in Arabic script only. "
        "Do NOT use any Latin/English letters at all."
        if in_arabic
        else ""
    )
    arabic_constraint = (
        "- all text must be Arabic only; remove any English terms" if in_arabic else ""
    )
    return f"""
You are creating a multiple-choice quiz from the provided Context ONLY.
Language: {lang}
Count: 1
{script_rule}

Return ONLY valid JSON (no markdown, no extra text) as a list of exactly 1 object with this schema:
{{
  "question": "the question text",
  "options": ["<first answer>", "<second answer>", "<third answer>", "<fourth answer>"],
  "answer": "must repeat one of the four option strings exactly"
}}

The angle-bracket placeholders above are a schema, not content. Replace each one
with a real answer. Never output single letters such as "A", "B", "C", "D" as
options -- every option must be meaningful text a student could evaluate.

The JSON keys must stay exactly "question", "options" and "answer" in English.
Only their values are written in {lang}.

Constraints:
- options must be 4 distinct strings
- answer must match one option exactly
- avoid hallucinations: do not use info outside Context
- The Context may be a written description of a figure. Do NOT ask what the
  figure shows. Ask about the concept the figure teaches, phrased as if it came
  from a textbook that has no pictures.
- The question must never mention a figure, diagram, chart, plot, image, slide
  or logo, and must never say "shown in", "illustrated in" or "described in".
  The student cannot see the source -- only the text you generate.
  Instead of asking what the source displays, take the subject matter it
  displays and ask about that directly. Write your own wording; do not copy any
  phrasing from these instructions.
- Ask about the concept, definition, relationship or process being taught --
  not about incidental details like chapter numbers, colours, or how many
  columns something has.
{arabic_constraint}

Context:
{context}
""".strip()


def _parse_items(raw: str) -> list[dict]:
    """Pull the JSON list out of a model response that may be fenced."""
    clean = _FENCE_RE.sub("", raw).strip()
    start, end = clean.find("["), clean.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    return json.loads(clean[start : end + 1]) or []


# Asked for an Arabic quiz, the model frequently translates the JSON keys too
# ({"سؤال": ..., "خيارات": [...], "الإجابة": ...}), which would otherwise be
# discarded as malformed despite the content being perfectly valid. Same idea as
# V1's question/q and options/choices aliases, extended to Arabic.
_QUESTION_KEYS = ("question", "q", "سؤال", "السؤال")
_OPTION_KEYS = ("options", "choices", "خيارات", "الخيارات", "اختيارات", "الاختيارات")
_ANSWER_KEYS = ("answer", "جواب", "الجواب", "إجابة", "الإجابة", "الاجابة")


def _first_key(item: dict, keys: tuple[str, ...], default=None):
    for key in keys:
        value = item.get(key)
        if value:
            return value
    return default


def _normalize(item) -> tuple[str, list[str], str] | None:
    """Clean one raw item into (question, options, answer), or None.

    The model does not always return a list of objects -- it sometimes emits a
    list of bare strings -- so the shape is checked rather than assumed.
    """
    if not isinstance(item, dict):
        return None

    question = clean_text(_first_key(item, _QUESTION_KEYS, ""))
    raw_options = _first_key(item, _OPTION_KEYS, []) or []
    if not isinstance(raw_options, list):
        return None
    options = [clean_text(o) for o in raw_options if clean_text(o)]
    answer = clean_text(_first_key(item, _ANSWER_KEYS, ""))

    if len(options) != 4:
        return None
    if answer not in options:
        # Models often answer with a letter instead of the option text.
        if answer.upper() in ("A", "B", "C", "D"):
            answer = options[("A", "B", "C", "D").index(answer.upper())]
        else:
            return None
    return question, options, answer


def _ask_model(prompt: str, model: str) -> str:
    try:
        response = get_client().generate(
            model=model, prompt=prompt, options=QUIZ_OPTIONS
        )
    except LLM_ERRORS as exc:
        # Ollama down / hung / model missing: retrying the same call a dozen
        # more times per chunk cannot help, so surface it instead of "rejecting".
        raise LLMUnavailableError(str(exc)) from exc
    return response.get("response") or ""


def _generate_one(
    chunk: SourceChunk, in_arabic: bool, model: str
) -> GeneratedQuestion | None:
    """One validated question from one chunk, or None if every attempt failed."""
    prompt = _build_prompt(chunk.text, in_arabic)
    rejected = 0

    def attempt(strict: bool) -> GeneratedQuestion | None:
        nonlocal rejected
        # V1 wrapped the whole attempt so any malformed response simply became
        # another retry; keep that, rather than letting one odd shape abort the
        # quiz mid-generation.
        raw = _ask_model(prompt, model)  # LLMUnavailableError propagates
        try:
            items = _parse_items(raw)
        except Exception as exc:
            log.debug("quiz generation attempt failed to parse (%s)", exc)
            rejected += 1
            return None

        if not isinstance(items, list):
            rejected += 1
            return None

        for item in items:
            normalized = _normalize(item)
            if normalized is None:
                rejected += 1
                continue
            question, options, answer = normalized

            if strict:
                ok = valid_item(question, options, answer, in_arabic)
            else:
                # Relaxed: still needs Arabic, distinct non-placeholder options
                # and a matching answer -- only the Latin-script rule is dropped.
                # The deixis rule is a quality gate rather than a script rule, so
                # it holds here too; an unanswerable question is no better for
                # being Arabic.
                ok = (
                    bool(question)
                    and contains_arabic(question)
                    and not any(is_bad_placeholder(o) for o in options)
                    and len({o.strip() for o in options}) == 4
                    and answer in options
                    and not references_unseen_source(question)
                )
            if not ok:
                rejected += 1
                continue

            return GeneratedQuestion(
                question_text=question,
                options=options,
                correct_answer_index=options.index(answer),
                source=chunk,
            )
        return None

    for _ in range(STRICT_ATTEMPTS):
        result = attempt(strict=True)
        if result is not None:
            if rejected:
                log.info(
                    "  %s: accepted after %s rejected generation(s)",
                    chunk.citation, rejected,
                )
            return result

    # Strict Arabic-only kept failing; fall back to allowing bracketed Latin.
    if in_arabic:
        log.warning(
            "  %s: strict Arabic validation failed %s times -- relaxing",
            chunk.citation, STRICT_ATTEMPTS,
        )
        for _ in range(RELAXED_ATTEMPTS):
            result = attempt(strict=False)
            if result is not None:
                log.info("  %s: accepted under relaxed validation", chunk.citation)
                return result

    log.warning("  %s: no valid question after all attempts", chunk.citation)
    return None


def generate_quiz(
    subject_id: int,
    count: int = DEFAULT_QUESTION_COUNT,
    language: str | None = None,
    seed: int | None = None,
    model: str = QUIZ_MODEL,
) -> list[GeneratedQuestion]:
    """Generate up to `count` validated MCQ items spread across the subject."""
    in_arabic = (language or LANG_EN) == LANG_AR

    # Over-sample so chunks that never yield a valid question can be replaced.
    chunks = sample_chunks(subject_id, count=count * 2, seed=seed)
    if not chunks:
        return []

    questions: list[GeneratedQuestion] = []
    for chunk in chunks:
        if len(questions) >= count:
            break
        log.info("generating question from %s", chunk.citation)
        question = _generate_one(chunk, in_arabic, model)
        if question is not None:
            questions.append(question)

    log.info(
        "generated %s/%s questions for subject %s (%s)",
        len(questions), count, subject_id, "ar" if in_arabic else "en",
    )
    if not questions:
        raise QuizGenerationError(
            f"no valid question from {len(chunks)} chunk(s) after all retries"
        )
    return questions
