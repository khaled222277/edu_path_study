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
import threading
import time
from dataclasses import dataclass, field

import httpx

from app.config import settings
from app.services.llm import LLM_ERRORS, LLMUnavailableError, get_client, keep_alive
from app.services.vector_store import get_collection, retry_if_stale

log = logging.getLogger(__name__)

DEFAULT_QUESTION_COUNT = 5

QUIZ_MODEL = settings.quiz_model

# Carried over from the V1 prototype: models leak Latin script and placeholder
# options into Arabic quizzes often enough that generation has to be validated
# and retried rather than trusted. Strict pass first, then a relaxed pass that
# only loosens the Latin-script rule, and only for Arabic.
STRICT_ATTEMPTS = settings.quiz_strict_attempts
RELAXED_ATTEMPTS = settings.quiz_relaxed_attempts

# Retries only help if sampling varies between them -- at temperature 0 all
# eight attempts would return the same rejected output.
QUIZ_OPTIONS = {"temperature": settings.quiz_temperature, "num_predict": 700}

def experiment_config() -> dict:
    """The quiz settings that change behaviour, for the benchmark's result files."""
    return {"first_pass_attempts": settings.quiz_first_pass_attempts}


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


# --- instrumentation ------------------------------------------------------
#
# Measurement only: nothing here changes what is generated or accepted. Every
# model reply becomes one AttemptTrace, grouped per chunk and per quiz, so
# scripts/bench_quiz.py (and the structured log lines below) can say where the
# time goes and why replies are rejected.

OUTCOME_ACCEPTED = "accepted"
PHASE_STRICT = "strict"
PHASE_RELAXED = "relaxed"


@dataclass
class AttemptTrace:
    """One model call and what became of its reply."""

    phase: str  # PHASE_STRICT | PHASE_RELAXED
    model_s: float  # waiting on Ollama
    check_s: float  # parsing, normalising and validating the reply
    outcome: str  # OUTCOME_ACCEPTED, or the first rejection category
    reasons: list[str] = field(default_factory=list)  # every rejected item's category
    # From Ollama's own counters, so "the model wrote more" can be told apart from
    # "the GPU got slower". None when the call did not report them.
    tokens_out: int | None = None
    tokens_in: int | None = None
    gen_s: float | None = None  # time Ollama spent generating tokens


@dataclass
class ChunkTrace:
    """Everything it took to turn one chunk into a question (or fail to)."""

    citation: str
    attempts: list[AttemptTrace] = field(default_factory=list)
    accepted: bool = False
    accepted_phase: str | None = None
    wall_s: float = 0.0

    @property
    def calls(self) -> int:
        return len(self.attempts)


@dataclass
class QuizTrace:
    """One generate_quiz() call, end to end."""

    language: str = ""
    requested: int = 0
    produced: int = 0
    sampling_s: float = 0.0
    total_s: float = 0.0
    error: str | None = None  # "timeout" | "unavailable" | "no_valid_question" | None
    chunks: list[ChunkTrace] = field(default_factory=list)


def _log_event(name: str, **fields) -> None:
    """One machine-readable log line: `quiz_item {...json...}`."""
    log.info("%s %s", name, json.dumps(fields, ensure_ascii=False, default=str))


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


REASON_SHORT_QUESTION = "question_too_short"
REASON_OPTION_COUNT = "wrong_option_count"
REASON_DUPLICATES = "duplicate_options"
REASON_PLACEHOLDER = "placeholder_option"
REASON_ANSWER_MISSING = "answer_not_in_options"
REASON_FIGURE = "references_figure"
REASON_LATIN = "latin_leakage"
REASON_NO_ARABIC = "no_arabic"
REASON_MALFORMED = "malformed_json"
REASON_EMPTY = "empty_response"
REASON_BAD_ITEM = "bad_item_shape"


def arabic_violation(text) -> str | None:
    """Why `text` fails the Arabic-script purity check, or None if it passes.

    Latin only inside short bracketed terms; and it must contain some Arabic.
    """
    value = str(text or "")
    if not contains_arabic(value):
        return REASON_NO_ARABIC
    # Latin outside parentheses/brackets is leakage.
    if _LATIN_RE.search(_BRACKETED_RE.sub("", value)):
        return REASON_LATIN
    # Inside them, only short tokens like "(SQL)" are tolerated.
    for segment in _BRACKETED_CAPTURE_RE.findall(value):
        if _LATIN_RE.search(segment) and len(segment.strip()) > MAX_BRACKETED_LATIN:
            return REASON_LATIN
    return None


def is_all_arabic_required(text, in_arabic: bool) -> bool:
    """Arabic-script purity check: Latin only inside short bracketed terms."""
    if not in_arabic:
        return True
    return arabic_violation(text) is None


def reject_reason(
    question: str, options: list[str], answer: str, in_arabic: bool
) -> str | None:
    """Why the strict rules reject this item, or None if they accept it.

    valid_item() is defined as `reject_reason(...) is None`, so the label can
    never disagree with the decision.
    """
    if not question or len(question) < 3:
        return REASON_SHORT_QUESTION
    if len(options) != 4:
        return REASON_OPTION_COUNT
    if len({o.strip() for o in options}) != 4:
        return REASON_DUPLICATES
    if any(is_bad_placeholder(o) for o in options):
        return REASON_PLACEHOLDER
    if answer not in options:
        return REASON_ANSWER_MISSING
    if references_unseen_source(question):
        return REASON_FIGURE
    if in_arabic:
        for text in (question, *options, answer):
            problem = arabic_violation(text)
            if problem is not None:
                return problem
    return None


def valid_item(question: str, options: list[str], answer: str, in_arabic: bool) -> bool:
    return reject_reason(question, options, answer, in_arabic) is None


def relaxed_reject_reason(question: str, options: list[str], answer: str) -> str | None:
    """The relaxed (Arabic fallback) rules: only the Latin-script rule is dropped.

    Still needs Arabic in the question, distinct non-placeholder options and a
    matching answer. The deixis rule is a quality gate rather than a script rule,
    so it holds here too; an unanswerable question is no better for being Arabic.
    """
    if not question:
        return REASON_SHORT_QUESTION
    if not contains_arabic(question):
        return REASON_NO_ARABIC
    if any(is_bad_placeholder(o) for o in options):
        return REASON_PLACEHOLDER
    if len({o.strip() for o in options}) != 4:
        return REASON_DUPLICATES
    if answer not in options:
        return REASON_ANSWER_MISSING
    if references_unseen_source(question):
        return REASON_FIGURE
    return None


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


def _normalize_ex(item) -> tuple[tuple[str, list[str], str] | None, str | None]:
    """Clean one raw item into ((question, options, answer), None), or (None, reason).

    The model does not always return a list of objects -- it sometimes emits a
    list of bare strings -- so the shape is checked rather than assumed.
    """
    if not isinstance(item, dict):
        return None, REASON_BAD_ITEM

    question = clean_text(_first_key(item, _QUESTION_KEYS, ""))
    raw_options = _first_key(item, _OPTION_KEYS, []) or []
    if not isinstance(raw_options, list):
        return None, REASON_OPTION_COUNT
    options = [clean_text(o) for o in raw_options if clean_text(o)]
    answer = clean_text(_first_key(item, _ANSWER_KEYS, ""))

    if len(options) != 4:
        return None, REASON_OPTION_COUNT
    if answer not in options:
        # Models often answer with a letter instead of the option text.
        if answer.upper() in ("A", "B", "C", "D"):
            answer = options[("A", "B", "C", "D").index(answer.upper())]
        else:
            return None, REASON_ANSWER_MISSING
    return (question, options, answer), None


def _normalize(item) -> tuple[str, list[str], str] | None:
    return _normalize_ex(item)[0]


# Ollama's counters for the call just made on this thread, picked up by the trace.
_call_stats = threading.local()


def _take_call_stats() -> tuple[int | None, int | None, float | None]:
    value = getattr(_call_stats, "value", None)
    _call_stats.value = None
    return value or (None, None, None)


def _ask_model(prompt: str, model: str) -> str:
    try:
        response = get_client().generate(
            model=model,
            prompt=prompt,
            options=QUIZ_OPTIONS,
            # Same value as chat: if the two models are the same, a quiz must not
            # reset the model's expiry to something shorter than chat set.
            keep_alive=keep_alive(),
        )
    except LLM_ERRORS as exc:
        # Ollama down / hung / model missing: retrying the same call a dozen
        # more times per chunk cannot help, so surface it instead of "rejecting".
        raise LLMUnavailableError(str(exc)) from exc
    eval_ns = response.get("eval_duration")
    _call_stats.value = (
        response.get("eval_count"),
        response.get("prompt_eval_count"),
        eval_ns / 1e9 if eval_ns else None,
    )
    return response.get("response") or ""


def _unavailable_kind(exc: LLMUnavailableError) -> str:
    return "timeout" if isinstance(exc.__cause__, httpx.TimeoutException) else "unavailable"


def _generate_one(
    chunk: SourceChunk,
    in_arabic: bool,
    model: str,
    trace: ChunkTrace | None = None,
    *,
    strict_attempts: int | None = None,
    relaxed_attempts: int | None = None,
    final: bool = True,
) -> GeneratedQuestion | None:
    """One validated question from one chunk, or None if the attempts ran out.

    By default the chunk gets its whole budget (STRICT_ATTEMPTS under the full
    rules, then, for Arabic, RELAXED_ATTEMPTS under the relaxed ones). Passing
    smaller budgets lets generate_quiz make a cheap first pass over the chunks and
    come back later; `trace` then carries the earlier attempts, and `final` says
    whether giving up here is the end of the chunk or only a deferral.
    """
    trace = trace if trace is not None else ChunkTrace(chunk.citation)
    strict_n = STRICT_ATTEMPTS if strict_attempts is None else strict_attempts
    relaxed_n = RELAXED_ATTEMPTS if relaxed_attempts is None else relaxed_attempts
    prompt = _build_prompt(chunk.text, in_arabic)
    rejected = 0

    def attempt(strict: bool) -> GeneratedQuestion | None:
        nonlocal rejected
        phase = PHASE_STRICT if strict else PHASE_RELAXED
        t0 = time.perf_counter()
        _take_call_stats()  # drop anything left from an earlier call
        try:
            raw = _ask_model(prompt, model)
        except LLMUnavailableError as exc:
            kind = _unavailable_kind(exc)
            trace.attempts.append(
                AttemptTrace(phase, time.perf_counter() - t0, 0.0, kind, [kind])
            )
            raise
        t1 = time.perf_counter()
        tokens_out, tokens_in, gen_s = _take_call_stats()

        reasons: list[str] = []
        result: GeneratedQuestion | None = None
        # V1 wrapped the whole attempt so any malformed response simply became
        # another retry; keep that, rather than letting one odd shape abort the
        # quiz mid-generation.
        try:
            items = _parse_items(raw)
        except Exception as exc:
            log.debug("quiz generation attempt failed to parse (%s)", exc)
            rejected += 1
            reasons.append(REASON_MALFORMED)
            items = None

        if items is not None and not isinstance(items, list):
            rejected += 1
            reasons.append(REASON_MALFORMED)
            items = None

        if items is not None and not items:
            # Nothing usable: no JSON list at all, or an explicitly empty one.
            reasons.append(REASON_EMPTY if "[" in raw and "]" in raw else REASON_MALFORMED)

        for item in items or []:
            normalized, why = _normalize_ex(item)
            if normalized is None:
                rejected += 1
                reasons.append(why)
                continue
            question, options, answer = normalized

            if strict:
                why = reject_reason(question, options, answer, in_arabic)
            else:
                why = relaxed_reject_reason(question, options, answer)
            if why is not None:
                rejected += 1
                reasons.append(why)
                continue

            result = GeneratedQuestion(
                question_text=question,
                options=options,
                correct_answer_index=options.index(answer),
                source=chunk,
            )
            break

        trace.attempts.append(
            AttemptTrace(
                phase,
                t1 - t0,
                time.perf_counter() - t1,
                OUTCOME_ACCEPTED if result is not None else (reasons[0] if reasons else REASON_EMPTY),
                reasons,
                tokens_out,
                tokens_in,
                gen_s,
            )
        )
        return result

    started = time.perf_counter()
    result: GeneratedQuestion | None = None
    try:
        for _ in range(strict_n):
            result = attempt(strict=True)
            if result is not None:
                trace.accepted_phase = PHASE_STRICT
                if rejected:
                    log.info(
                        "  %s: accepted after %s rejected generation(s)",
                        chunk.citation, rejected,
                    )
                return result

        # Strict Arabic-only kept failing; fall back to allowing bracketed Latin.
        if in_arabic and relaxed_n > 0:
            log.warning(
                "  %s: strict Arabic validation failed %s times -- relaxing",
                chunk.citation, sum(1 for a in trace.attempts if a.phase == PHASE_STRICT),
            )
            for _ in range(relaxed_n):
                result = attempt(strict=False)
                if result is not None:
                    trace.accepted_phase = PHASE_RELAXED
                    log.info("  %s: accepted under relaxed validation", chunk.citation)
                    return result

        if final:
            log.warning("  %s: no valid question after all attempts", chunk.citation)
        else:
            log.info("  %s: no valid question yet -- trying another chunk first", chunk.citation)
        return None
    finally:
        trace.accepted = result is not None
        trace.wall_s += time.perf_counter() - started
        _log_event(
            "quiz_item",
            citation=chunk.citation,
            lang="ar" if in_arabic else "en",
            accepted=trace.accepted,
            phase=trace.accepted_phase,
            final=final or trace.accepted,
            calls=trace.calls,
            wall_s=round(trace.wall_s, 3),
            model_s=round(sum(a.model_s for a in trace.attempts), 3),
            check_s=round(sum(a.check_s for a in trace.attempts), 4),
            outcomes=[a.outcome for a in trace.attempts],
        )


def generate_quiz(
    subject_id: int,
    count: int = DEFAULT_QUESTION_COUNT,
    language: str | None = None,
    seed: int | None = None,
    model: str = QUIZ_MODEL,
    trace: QuizTrace | None = None,
) -> list[GeneratedQuestion]:
    """Generate up to `count` validated MCQ items spread across the subject.

    `trace`, if given, is filled with timings and rejection reasons. It is
    observation only: results are identical with or without it.
    """
    in_arabic = (language or LANG_EN) == LANG_AR
    trace = trace if trace is not None else QuizTrace()
    trace.language = LANG_AR if in_arabic else LANG_EN
    trace.requested = count
    started = time.perf_counter()

    try:
        # Over-sample so chunks that never yield a valid question can be replaced.
        t0 = time.perf_counter()
        chunks = sample_chunks(subject_id, count=count * 2, seed=seed)
        trace.sampling_s = time.perf_counter() - t0
        if not chunks:
            return []

        first_pass = min(settings.quiz_first_pass_attempts, STRICT_ATTEMPTS)
        questions: list[GeneratedQuestion] = []
        deferred: list[tuple[SourceChunk, ChunkTrace]] = []

        # Pass 1: a few attempts per chunk, then on to the next. A chunk that has
        # already failed is a worse bet than a fresh one, and the sample holds twice
        # as many chunks as the quiz needs.
        for chunk in chunks:
            if len(questions) >= count:
                break
            log.info("generating question from %s", chunk.citation)
            chunk_trace = ChunkTrace(chunk.citation)
            trace.chunks.append(chunk_trace)  # before generating, so a crash keeps it
            if first_pass > 0:
                question = _generate_one(
                    chunk, in_arabic, model, chunk_trace,
                    strict_attempts=first_pass, relaxed_attempts=0, final=False,
                )
                if question is None:
                    deferred.append((chunk, chunk_trace))
            else:
                question = _generate_one(chunk, in_arabic, model, chunk_trace)
            if question is not None:
                questions.append(question)

        # Pass 2, only if still short: give the hard chunks the rest of their budget,
        # including the relaxed Arabic rules. Nothing is accepted here that would not
        # have been accepted before; it just happens later, and only when needed.
        for chunk, chunk_trace in deferred:
            if len(questions) >= count:
                break
            strict_done = sum(1 for a in chunk_trace.attempts if a.phase == PHASE_STRICT)
            question = _generate_one(
                chunk, in_arabic, model, chunk_trace,
                strict_attempts=max(STRICT_ATTEMPTS - strict_done, 0),
                relaxed_attempts=RELAXED_ATTEMPTS,
            )
            if question is not None:
                questions.append(question)
        trace.produced = len(questions)

        log.info(
            "generated %s/%s questions for subject %s (%s)",
            len(questions), count, subject_id, "ar" if in_arabic else "en",
        )
        if not questions:
            trace.error = "no_valid_question"
            raise QuizGenerationError(
                f"no valid question from {len(chunks)} chunk(s) after all retries"
            )
        return questions
    except LLMUnavailableError as exc:
        trace.error = _unavailable_kind(exc)
        raise
    finally:
        trace.total_s = time.perf_counter() - started
        _log_event(
            "quiz_summary",
            subject_id=subject_id,
            lang=trace.language,
            requested=count,
            produced=trace.produced,
            total_s=round(trace.total_s, 3),
            sampling_s=round(trace.sampling_s, 3),
            calls=sum(c.calls for c in trace.chunks),
            error=trace.error,
        )
