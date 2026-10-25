"""Inline citations in a model answer: find them, check them against what was retrieved.

The model is told to cite as ``[lecture.pdf, page 12]``. It does not always: it invents page
numbers, cites a file it was never shown, writes a made-up Arabic "book title" instead of a
filename, lists several pages in one bracket, or writes the page in Arabic-Indic digits.
A citation that points at something the student was not given is worse than none, so every
citation is judged against the chunks actually retrieved for that turn:

  * a page the turn retrieved is kept, rewritten to one canonical form ``[file.pdf, page N]``
    (ASCII digits, one page per bracket);
  * anything else that LOOKS like a citation is removed from the text;
  * brackets that are not citations -- ``[0.0, 1.0]``, ``[v in Values(A)]`` -- are left alone.

Removing (rather than marking) was chosen on purpose: a flag such as "[unverified]" or "[?]"
is one more thing for a student to puzzle over, whereas a sentence with no citation simply
reads as uncited, which is true. The sources the student can open are the structured list
that comes with the reply, and that only ever holds retrieved chunks.

`CitationFilter` does this one character at a time, so the text streamed to the browser is
identical to the text stored, however the model's output happens to be split into pieces.
"""

import re
from dataclasses import dataclass, field

_DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

_D = r"[0-9٠-٩۰-۹]"
_PAGE_KW = r"(?:الصفحات|الصفحة|صفحات|صفحة|pages|page|pp\.|p\.|ص)"
_ONE = rf"{_D}+(?:\s*[-–]\s*{_D}+)?"
_NUMS = rf"{_ONE}(?:\s*(?:,|،|&|and|و)\s*{_ONE})*"
_SEP = r"(?:\s*[,،;:]\s*|\s+)"

# [file.pdf, page 12]  /  [file.pdf, pages 3, 5]  /  [ملف, صفحة ١٢]
_FULL_RE = re.compile(rf"^\[\s*(?P<file>[^\[\],،;]+?){_SEP}(?P<kw>{_PAGE_KW})\s*(?P<nums>{_NUMS})\s*\]$", re.IGNORECASE)
# [file.pdf]
_FILE_ONLY_RE = re.compile(r"^\[\s*(?P<file>[^\[\],،;]+?\.pdf)\s*\]$", re.IGNORECASE)
# something that ends like a page reference, even if the head is rubbish
_PAGE_TAIL_RE = re.compile(rf"[,،;:]\s*{_PAGE_KW}\s*{_D}", re.IGNORECASE)

MAX_BRACKET_CHARS = 200


def to_ascii_digits(text: str) -> str:
    """Arabic-Indic and Persian digits to 0-9."""
    return text.translate(_DIGIT_MAP)


def _pages(nums: str) -> tuple[int, ...]:
    """'3, 5' -> (3, 5); '3-5' -> (3, 4, 5). Ranges longer than 10 pages are kept as their ends."""
    pages: list[int] = []
    for part in re.split(r"\s*(?:,|،|&|and|و)\s*", to_ascii_digits(nums).strip()):
        if not part:
            continue
        span = re.split(r"\s*[-–]\s*", part)
        lo, hi = int(span[0]), int(span[-1])
        pages.extend(range(lo, hi + 1) if 0 <= hi - lo <= 10 else (lo, hi))
    return tuple(dict.fromkeys(pages))


@dataclass(frozen=True)
class Citation:
    raw: str  # exactly as the model wrote it, brackets included
    parsed: bool  # False: looks like a citation but has no readable file + page
    file: str | None = None  # as written
    pages: tuple[int, ...] = ()  # empty for a file-only citation


def parse_bracket(bracket: str) -> Citation | None:
    """None if `bracket` ("[...]") is not citation-shaped; otherwise a Citation."""
    if len(bracket) > MAX_BRACKET_CHARS + 2 or "\n" in bracket:
        return None
    m = _FULL_RE.match(bracket)
    if m:
        pages = _pages(m.group("nums"))
        if pages:
            return Citation(bracket, True, m.group("file").strip(), pages)
    m = _FILE_ONLY_RE.match(bracket)
    if m:
        return Citation(bracket, True, m.group("file").strip(), ())
    inner = bracket[1:-1]
    if ".pdf" in inner.lower() or _PAGE_TAIL_RE.search(inner):
        return Citation(bracket, False)
    return None


_BRACKET_RE = re.compile(r"\[[^\[\]\n]{1,%d}\]" % MAX_BRACKET_CHARS)


def find_citations(text: str) -> list[Citation]:
    """Every citation-shaped bracket in `text`, in order."""
    return [c for c in (parse_bracket(m.group()) for m in _BRACKET_RE.finditer(text)) if c]


def _basename(name: str) -> str:
    return re.split(r"[\\/]", name.strip().strip("\"'`“”«»"))[-1].strip().lower()


class Retrieved:
    """The (file, page) pairs one turn actually retrieved."""

    def __init__(self, pairs) -> None:
        self.pairs = {(f, int(p)) for f, p in pairs}
        self._files = {f.lower(): f for f, _ in self.pairs}
        self._files.update({f.lower().removesuffix(".pdf"): f for f, _ in self.pairs})

    @classmethod
    def from_chunks(cls, chunks) -> "Retrieved":
        return cls((c.source_filename, c.page_number) for c in chunks)

    def file(self, written: str) -> str | None:
        """The retrieved file `written` names (case, path and a missing .pdf tolerated), or None."""
        return self._files.get(_basename(written))


@dataclass(frozen=True)
class Verdict:
    status: str  # valid | partial | unretrieved | unparseable
    file: str | None  # canonical retrieved filename, when the file was matched
    kept: tuple[int, ...] = ()
    dropped: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in ("valid", "partial")


def judge(citation: Citation, retrieved: Retrieved) -> Verdict:
    if not citation.parsed:
        return Verdict("unparseable", None)
    file = retrieved.file(citation.file or "")
    if file is None:
        return Verdict("unretrieved", None, (), citation.pages)
    if not citation.pages:
        return Verdict("valid", file)  # names a retrieved file, claims no page: nothing false
    kept = tuple(p for p in citation.pages if (file, p) in retrieved.pairs)
    dropped = tuple(p for p in citation.pages if (file, p) not in retrieved.pairs)
    if not kept:
        return Verdict("unretrieved", file, (), dropped)
    return Verdict("partial" if dropped else "valid", file, kept, dropped)


def render(citation: Citation, verdict: Verdict) -> str:
    """The canonical text for a citation that survived: one bracket per kept page."""
    if not verdict.kept:
        return f"[{verdict.file}]"  # file-only
    return " ".join(f"[{verdict.file}, page {p}]" for p in verdict.kept)


# --- the filter ----------------------------------------------------------------------


_NO_SPACE_BEFORE = set(".,;:!?)]}،؛؟…")


@dataclass
class ValidatedAnswer:
    text: str
    kept: list[tuple[str, int]] = field(default_factory=list)  # (file, page) cited, in order
    removed: list[str] = field(default_factory=list)  # brackets taken out, as written


class CitationFilter:
    """Checks citations as text streams in; output is independent of how the text is split.

    Anything from a "[" to its "]" is held back until the bracket is complete, so an invalid
    citation is never sent to the browser and then taken back. Spaces before a bracket are
    held too, so removing the bracket leaves no gap. Everything else passes straight through.
    """

    def __init__(self, retrieved: Retrieved) -> None:
        self.retrieved = retrieved
        self.kept: list[tuple[str, int]] = []
        self.removed: list[str] = []
        self._spaces = ""  # spaces/tabs seen but not yet emitted
        self._bracket: str | None = None  # text since an open "[", else None
        self._after_drop = False  # the last thing was a removed citation
        self._emitted = False
        self._last = ""  # last character emitted

    def feed(self, piece: str) -> str:
        out: list[str] = []
        for ch in piece:
            self._step(ch, out)
        return "".join(out)

    def flush(self) -> str:
        """End of the text: a half-open bracket was just text; trailing spaces are dropped."""
        out: list[str] = []
        if self._bracket is not None:
            held, self._bracket = self._bracket, None
            self._release(held, out)
        self._spaces = ""
        return "".join(out)

    # -- internals --

    def _emit(self, text: str, out: list[str]) -> None:
        if text:
            out.append(text)
            self._emitted = True
            self._last = text[-1]

    def _release(self, text: str, out: list[str]) -> None:
        """`text` is ordinary text after all: send it, with the spaces that preceded it."""
        lead = self._spaces if self._emitted else ""
        self._spaces = ""
        self._after_drop = False
        self._emit(lead + text, out)

    def _step(self, ch: str, out: list[str]) -> None:
        if self._bracket is not None:
            self._in_bracket(ch, out)
            return
        if ch in " \t":
            self._spaces += ch
            return
        if ch == "[":
            self._bracket = "["
            return
        # ordinary character
        if self._after_drop and not self._spaces and self._emitted and ch not in _NO_SPACE_BEFORE and ch not in "\n\r" and not self._last.isspace():
            self._emit(" ", out)  # "A [x]B" -> "A B", not "AB"
        lead = self._spaces if self._emitted else ""
        self._spaces = ""
        self._after_drop = False
        self._emit(lead + ch, out)

    def _in_bracket(self, ch: str, out: list[str]) -> None:
        if ch == "[":
            # the earlier "[" was plain text; this one may open a citation
            held = self._bracket
            self._bracket = None
            self._release(held, out)
            self._bracket = "["
            return
        if ch == "\n" or len(self._bracket) >= MAX_BRACKET_CHARS + 1:
            held = self._bracket
            self._bracket = None
            self._release(held, out)
            self._step(ch, out)
            return
        self._bracket += ch
        if ch != "]":
            return
        bracket, self._bracket = self._bracket, None
        citation = parse_bracket(bracket)
        if citation is None:
            self._release(bracket, out)
            return
        verdict = judge(citation, self.retrieved)
        if verdict.ok:
            self.kept.extend((verdict.file, p) for p in verdict.kept)
            self._release(render(citation, verdict), out)
            return
        self.removed.append(bracket)
        self._spaces = ""  # the gap before it goes with it
        self._after_drop = True


def validate_answer(text: str, retrieved: Retrieved) -> ValidatedAnswer:
    """`text` with every citation checked against `retrieved` (see the module docstring)."""
    f = CitationFilter(retrieved)
    out = f.feed(text) + f.flush()
    return ValidatedAnswer(out, f.kept, f.removed)
