"""A language-neutral way for the model to say "the material does not cover this".

The old signal was an English sentence ("not found in the lecture material") that the
model was asked to write. In Arabic it wrote an Arabic refusal instead, which never
matched, so the retrieved sources were still shown under it. Now the model is asked to
begin such a reply with a fixed token. The token is detected here and removed before the
reply is stored or shown; whatever sentence follows it is in the student's own language.

The old English phrase is still honoured as a fallback, so a reply that refuses the old
way is treated the same.
"""

import re
from dataclasses import dataclass

REFUSAL_SENTINEL = "<<NOT_IN_MATERIAL>>"
LEGACY_REFUSAL_PHRASE = "not found in the lecture material"

# Tolerates the casing and spacing a model is likely to get slightly wrong.
_SENTINEL_RE = re.compile(r"<<\s*NOT[_ ]IN[_ ]MATERIAL\s*>>", re.IGNORECASE)

# Shown when a refusal has no sentence of its own (the model wrote only the token),
# or when the model produced nothing at all. Chosen by the language of the question.
DEFAULT_REFUSAL = {
    "en": "This is not covered in the lecture material.",
    "ar": "هذا غير مذكور في المادة العلمية المتاحة.",
}
DEFAULT_APOLOGY = {
    "en": "Sorry, I could not produce an answer. It may not be covered in the lecture material.",
    "ar": "عذرًا، لم أتمكن من تكوين إجابة. قد يكون هذا غير مذكور في المادة العلمية المتاحة.",
}


@dataclass(frozen=True)
class ParsedReply:
    text: str  # what is stored and shown: sentinel removed, never empty
    refused: bool  # the material did not cover the question


def parse_reply(raw: str | None, language: str = "en") -> ParsedReply:
    """Interpret a finished model reply. `language` ("en" | "ar") picks the default text."""
    text = (raw or "").strip()
    refused = bool(_SENTINEL_RE.search(text))
    text = _SENTINEL_RE.sub("", text).strip()
    if not refused and LEGACY_REFUSAL_PHRASE in text.lower():
        refused = True

    if not text:
        # Nothing to show: either only the token, or no reply at all. Both are refusals
        # as far as the sources go -- there is no answer for them to support.
        table = DEFAULT_REFUSAL if refused else DEFAULT_APOLOGY
        return ParsedReply(table.get(language, table["en"]), True)
    return ParsedReply(text, refused)


class RefusalFilter:
    """Removes the sentinel from a reply as it streams in.

    Text arrives in arbitrary pieces, so the token can be split anywhere ("<<NOT_" then
    "IN_MATERIAL>>"). Anything at the end of the buffer that could still turn into the
    token is held back until the next piece settles it; everything else is released at
    once. `refused` becomes true as soon as a whole token has been seen.
    """

    def __init__(self) -> None:
        self._held = ""
        self._emitted_any = False
        self.refused = False

    def feed(self, piece: str) -> str:
        buffer = self._held + piece
        buffer, count = _SENTINEL_RE.subn("", buffer)
        if count:
            self.refused = True
        hold = self._partial_suffix(buffer)
        release, self._held = buffer[: len(buffer) - hold], buffer[len(buffer) - hold :]
        return self._tidy(release)

    def flush(self) -> str:
        """The end of the reply: whatever was held back was not a token after all."""
        release, self._held = self._held, ""
        return self._tidy(release)

    def _tidy(self, text: str) -> str:
        # The reply usually starts "<<TOKEN>> sentence": drop the space the token left.
        if not self._emitted_any:
            text = text.lstrip()
        if text:
            self._emitted_any = True
        return text

    @staticmethod
    def _partial_suffix(buffer: str) -> int:
        """Length of the longest end of `buffer` that is a proper start of the sentinel."""
        sentinel = REFUSAL_SENTINEL.lower()
        low = buffer.lower()
        for size in range(min(len(sentinel) - 1, len(low)), 0, -1):
            if sentinel.startswith(low[-size:]):
                return size
        return 0
