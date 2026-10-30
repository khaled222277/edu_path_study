"""Automatic checks on a chat answer, shared by the measurement scripts (repro_followup, eval_transform,
eval_leakage). Self-contained on purpose: no import from `app`, so the same file measures an older
checkout and the current one when the two are compared.

These are SIGNALS, not judgements. They count disallowed characters and look for a repeated paragraph
or figure-description talk; they cannot say whether an answer is correct or reads well.
"""

import re
import unicodedata

BOILERPLATE_RE = re.compile(r"no arabic text|no text is present|no arabic", re.IGNORECASE)
# The words of an answer that is describing a decorative figure instead of the lecture.
FIGURE_TALK_RE = re.compile(
    r"\blogo\b|\brecap\b|\bdna\b|nucleotide|no arabic text|rewind|red square|الشعار|شعار|حمض نووي|الحمض النووي|لا يوجد نص عربي|لا يوجد نص",
    re.IGNORECASE,
)
_OK_LETTER = ("ARABIC", "LATIN", "GREEK")
CITATION_RE = re.compile(r"\[[^\[\]]+?,\s*page\s*\d+\]", re.IGNORECASE)


def disallowed_chars(text: str) -> list[str]:
    """Characters that must not be in a chat answer: U+FFFD, and letters of any script other than
    Arabic, Latin or Greek (CJK, Hangul, Kana, Cyrillic, Hebrew, Devanagari, Thai...), and CJK /
    full-width punctuation. Digits, math symbols, emoji-free punctuation and Greek formula letters pass."""
    found = []
    for ch in unicodedata.normalize("NFKC", text):
        if ch == "�":
            found.append(ch)
        elif ch.isalpha() and not unicodedata.name(ch, "").startswith(_OK_LETTER):
            found.append(ch)
        elif "　" <= ch <= "〿":
            found.append(ch)
    return found


def script_of(ch: str) -> str:
    if ch == "�":
        return "U+FFFD"
    name = unicodedata.name(ch, "")
    for key, label in (("CJK", "CJK"), ("HANGUL", "Hangul"), ("HIRAGANA", "Kana"), ("KATAKANA", "Kana"),
                       ("CYRILLIC", "Cyrillic"), ("HEBREW", "Hebrew"), ("DEVANAGARI", "Devanagari"),
                       ("THAI", "Thai")):
        if key in name:
            return label
    return "other"


def repeated_paragraph(text: str) -> bool:
    """The same paragraph (or a long sentence) twice."""
    norm = lambda s: re.sub(r"\W+", " ", s).strip().lower()
    paras = [norm(p) for p in re.split(r"\n\s*\n", text) if len(norm(p)) >= 40]
    sents = [norm(s) for s in re.split(r"(?<=[.!?؟])\s+|\n", text) if len(norm(s)) >= 40]
    return len(set(paras)) < len(paras) or len(set(sents)) < len(sents)
