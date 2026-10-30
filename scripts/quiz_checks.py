"""Rule-based classification of generated quiz items, for scripts/eval_quiz_quality.py.

Self-contained (no import from `app`) and deliberately broader than the app's own validator, so a
validator can be measured by it without being circular. These are SIGNALS for finding problems, not
judgements: nothing here can say whether Arabic reads well or whether a question is correct. A sample
is always printed for a human to review.

Classes (an item can carry several flags; `primary` picks one for the headline rate):
  deictic     the question relies on something not in its text (this/that/the following/the figure,
              هذا/هذه/هذان/الشكل/الصورة/التالي ...), or is a bare "what is this"
  fragment    no question form: no question mark and no interrogative word, or very short
  grammar     cheap Arabic demonstrative-agreement suspicion (هذا + a feminine -ة noun, or هذه + masculine)
  off_topic   proxy only (needs the embedding model, see eval_quiz_quality): the question + correct
              option is far from the course material in embedding space
"""

import re
import unicodedata

_TASHKEEL = re.compile(r"[ً-ٰٟـ]")

# --- deixis -----------------------------------------------------------------------------------------
_EN_DEICTIC = re.compile(
    r"\b(this|that|these|those)\s+(is|are|was|were|means?|refers?|represents?|shows?|idea|concept|term|"
    r"statement|sentence|slide|page|diagram|figure|picture|image|chart|table|example|equation|formula|"
    r"code|snippet|graph|plot|model|method|approach|technique|algorithm|process)\b"
    r"|\bwhat\s+(is|are|does)\s+(this|that|these|those|it)\b"
    r"|\b(shown|illustrated|depicted|displayed|presented|demonstrated|described)\b"
    r"|\b(the\s+)?(above|below)\b"
    r"|\bthe\s+(given|following)\s+(data|table|figure|diagram|image|text|example|code|passage|slide|statement)\b"
    r"|\b(figure|diagram|image|picture|chart|graph|plot|slide|screenshot|logo|illustration|snippet)\b"
    r"|\bin\s+the\s+(table|example|text|passage)\b"
    r"|\beach\s+cell\b",
    re.IGNORECASE,
)
_AR_DEICTIC = re.compile(
    r"(?:^|\s)(هذا|هذه|هذان|هاتان|هؤلاء|ذلك|تلك|هذي|هاذا)(?:\s|$)"
    r"|التالي|التالية|التاليه|المذكور|المذكورة|الآتي|الاتي|أعلاه|اعلاه|المعطى|المعروض|الموضح|الموضحة|"
    r"الشكل|الصورة|الصوره|الرسم|المخطط|الجدول|الشريحة|الشريحه|الشعار|الفقرة|النص"
)
# a demonstrative followed by (at most) a verb-less copula: the question is only a pointer
_EN_BARE = re.compile(r"^\s*(what|which|who)?\s*(is|are)?\s*(this|that|these|those)\b[^.?]{0,25}[?.]?\s*$", re.IGNORECASE)
_AR_BARE = re.compile(r"^\s*(?:ما|ماذا|من)?\s*(?:هو|هي)?\s*(هذا|هذه|ذلك|تلك|هذي)(?:\s+\S+){0,2}\s*(?:هي|هو|؟|\?)?\s*$")

# --- question form -----------------------------------------------------------------------------------
_EN_Q = re.compile(
    r"\b(what|which|who|whom|whose|when|where|why|how|is|are|does|do|did|can|could|should|would|will|"
    r"identify|select|choose|name|define|explain|describe|state|list|complete|find|calculate|compute)\b",
    re.IGNORECASE,
)
_AR_Q = re.compile(
    r"(?:^|\s)(ما|ماذا|من|متى|أين|اين|كيف|لماذا|لم|هل|أي|اي|أيّ|كم|أيها|ايها|عرف|عرّف|اذكر|اشرح|حدد|اختر|"
    r"وضح|صف|قارن|أكمل|اكمل|ما هو|ما هي)(?:\s|$)|[؟?]"
)


def _norm(text: str) -> str:
    return _TASHKEEL.sub("", unicodedata.normalize("NFKC", text or ""))


def is_deictic(question: str, lang: str) -> bool:
    q = _norm(question)
    if lang == "ar":
        return bool(_AR_DEICTIC.search(q) or _AR_BARE.match(q))
    return bool(_EN_DEICTIC.search(q) or _EN_BARE.match(q))


def is_fragment(question: str, lang: str) -> bool:
    q = _norm(question).strip()
    words = q.split()
    if len(q) < 12 or len(words) < 3:
        return True
    has_mark = "?" in q or "؟" in q
    return not has_mark and not (_AR_Q if lang == "ar" else _EN_Q).search(q)


def is_grammar_suspect(question: str, lang: str) -> bool:
    """Arabic only, and cheap: a masculine demonstrative before a feminine -ة noun, or the reverse."""
    if lang != "ar":
        return False
    words = _norm(question).split()
    for a, b in zip(words, words[1:]):
        b = re.sub(r"[^؀-ۿ]", "", b)
        if a == "هذا" and b.endswith("ة") and len(b) > 3:
            return True
        if a == "هذه" and b and not b.endswith(("ة", "ات", "ى", "اء")) and len(b) > 3 and b.startswith("ال") is False:
            return True
    return False


def classify(question: str, lang: str) -> dict:
    flags = {
        "deictic": is_deictic(question, lang),
        "fragment": is_fragment(question, lang),
        "grammar": is_grammar_suspect(question, lang),
    }
    flags["self_contained"] = not any(flags.values())
    flags["primary"] = next((k for k in ("deictic", "fragment", "grammar") if flags[k]), "self_contained")
    return flags


# --- source chunk features --------------------------------------------------------------------------

_TITLE = re.compile(
    r"^\s*(agenda|outline|contents?|table of contents|references?|bibliography|thank\s*you|thanks|questions\??|"
    r"q\s*&\s*a|any questions|the end|summary|recap|objectives?|learning outcomes|overview|"
    r"شكرا|شكراً|المراجع|المحتويات|الأسئلة|ملخص)\b",
    re.IGNORECASE,
)


def chunk_features(text: str, content_type: str) -> dict:
    text = text or ""
    chars = [c for c in text if not c.isspace()]
    alpha = sum(1 for c in chars if c.isalpha())
    words = re.findall(r"[^\W\d_]{3,}", text, re.UNICODE)
    return {
        "figure": content_type == "image_description",
        "length": len(text),
        "alpha_share": round(alpha / len(chars), 3) if chars else 0.0,
        "real_words": len(words),
        "title_like": bool(_TITLE.match(text)),
        "short": len(text) < 400,
        "low_info": len(words) < 25,
    }
