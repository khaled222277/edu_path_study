"""Keep characters of the wrong script out of chat answers.

The chat model sometimes writes a Chinese/Japanese word, or a U+FFFD replacement character, in the
middle of an Arabic or English sentence. Measured on the lecture eval set, the most common slot is
"a model (or function)": Arabic answers wrote "(أو 関係)" or "(أو 関�function)" for it. Nothing the
student can use is lost by dropping those characters: the surrounding sentence is complete without
them, and where a Latin technical term followed them ("関�function") it is kept.

What is allowed is everything that is not on the deny list below, so Arabic, Latin (with accents),
Greek (formula letters), digits, mathematical alphanumerics, symbols and punctuation all pass. The
deny list is the scripts that appeared or could plausibly appear from a multilingual model plus
U+FFFD, CJK punctuation and half-width kana/hangul.

Stream-safe by construction: a character is judged on its own, so there is no partial state to hold
back, and the text a student sees streamed is exactly the text that is stored (both routes put the
raw model output through the same filter before anything else reads it). The one piece of state is
whether the last character written was whitespace, so that dropping a word between two spaces does
not leave a double space.
"""

import unicodedata

_DENIED_SCRIPTS = (
    "CJK", "HANGUL", "HIRAGANA", "KATAKANA", "BOPOMOFO", "CYRILLIC", "HEBREW", "DEVANAGARI", "BENGALI",
    "TAMIL", "THAI", "LAO ", "KHMER", "MYANMAR", "TIBETAN", "GEORGIAN", "ARMENIAN", "ETHIOPIC", "SINHALA",
)


def is_foreign(ch: str) -> bool:
    """True for a character that must not appear in a chat answer."""
    if ch.isascii():
        return False
    if ch == "�" or "　" <= ch <= "〿" or "｡" <= ch <= "ￜ":
        return True
    if ch.isalpha() or unicodedata.category(ch).startswith("M"):
        name = unicodedata.name(ch, "")
        return name.startswith(_DENIED_SCRIPTS)
    return False


def foreign_count(text: str) -> int:
    return sum(1 for ch in text if is_foreign(ch))


class ScriptFilter:
    """Drops foreign-script characters from text fed in pieces. `feed` returns what may be shown now."""

    def __init__(self) -> None:
        self.dropped = 0
        self._last_was_space = True  # nothing written yet: a leading space after a drop is not wanted either
        self._gap = False  # a character was dropped since the last one written (survives piece boundaries)

    def feed(self, piece: str) -> str:
        out = []
        for ch in piece:
            if is_foreign(ch):
                self.dropped += 1
                self._gap = True
                continue
            if self._gap and ch.isspace() and self._last_was_space:
                self._gap = False
                continue  # "a <dropped> b" would otherwise leave two spaces
            self._gap = False
            out.append(ch)
            self._last_was_space = ch.isspace()
        return "".join(out)

    def flush(self) -> str:
        return ""


def strip_foreign(text: str) -> tuple[str, int]:
    """(text without foreign-script characters, how many were dropped)."""
    guard = ScriptFilter()
    return guard.feed(text), guard.dropped
