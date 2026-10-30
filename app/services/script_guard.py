"""Keep characters of the wrong script out of chat answers.

The chat model sometimes writes a Chinese/Japanese word, a Russian word, or a U+FFFD replacement
character in the middle of an Arabic or English sentence. Measured: in Arabic answers about
"a model (or function)" it wrote "(أو 関係)" or "(أو 関�function)", and for "attribute" it wrote
"الأтрибут". Dropping only the foreign characters of the second leaves a broken fragment ("الأ"),
so the unit is the WORD (a run of letters):

  * a word that mixes foreign letters with Arabic letters is broken whichever way it is cut: dropped whole;
  * any other word loses only its foreign characters, so a Latin technical term that followed
    them ("関�function") is kept, and a foreign word on its own ("関係") disappears.

What is allowed is everything not on the deny list below: Arabic, Latin (with accents), Greek
(formula letters), digits, mathematical alphanumerics, symbols and punctuation.

Stream-safe: the filter holds back only a word that contains Arabic or foreign letters, and only until
the next non-letter, so a student sees text at most one word late (English streams token by token), and what is streamed is exactly what is stored and what the
done event carries (both routes put the raw model output through the same filter before anything
else reads it). The other state is whether the last character written was whitespace, so that
dropping a word between two spaces does not leave a double space.
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


def _in_word(ch: str) -> bool:
    return ch.isalpha() or is_foreign(ch) or unicodedata.category(ch).startswith("M")


def _is_arabic(ch: str) -> bool:
    return not is_foreign(ch) and unicodedata.name(ch, "").startswith("ARABIC")


class ScriptFilter:
    """Drops foreign-script characters from text fed in pieces. `feed` returns what may be shown now.

    `dropped` counts every character removed, including the Arabic letters of a word dropped whole.
    """

    def __init__(self) -> None:
        self.dropped = 0
        self._word: list[str] = []
        self._last_was_space = True  # nothing written yet: a leading space after a drop is not wanted either
        self._gap = False  # something was dropped since the last character written (survives piece boundaries)

    def feed(self, piece: str) -> str:
        out: list[str] = []
        for ch in piece:
            if _in_word(ch):
                if self._word or _is_arabic(ch) or is_foreign(ch):
                    self._word.append(ch)  # may still turn out to be a broken word: hold it back
                else:
                    self._write(ch, out)  # Latin, Greek...: nothing later can make it need dropping
                continue
            self._end_word(out)
            if self._gap and ch.isspace() and self._last_was_space:
                self._gap = False
                continue  # "a <dropped> b" would otherwise leave two spaces
            self._write(ch, out)
        return "".join(out)

    def _write(self, ch: str, out: list[str]) -> None:
        self._gap = False
        out.append(ch)
        self._last_was_space = ch.isspace()

    def flush(self) -> str:
        out: list[str] = []
        self._end_word(out)
        return "".join(out)

    def _end_word(self, out: list[str]) -> None:
        word, self._word = self._word, []
        if not word:
            return
        foreign = [ch for ch in word if is_foreign(ch)]
        if not foreign:
            kept = word
        elif any(_is_arabic(ch) for ch in word):
            kept = []
        else:
            kept = [ch for ch in word if ch not in foreign]
        self.dropped += len(word) - len(kept)
        for ch in kept:
            self._write(ch, out)
        if len(kept) < len(word):
            self._gap = not kept


def strip_foreign(text: str) -> tuple[str, int]:
    """(text without foreign-script characters, how many characters were dropped)."""
    guard = ScriptFilter()
    return guard.feed(text) + guard.flush(), guard.dropped
