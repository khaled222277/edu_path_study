"""Tell "do something with your last answer" apart from "look something up".

"Say that in Arabic", "repeat all that", "I don't get it" are about the answer the student
already has. Searching the lectures with them returns whatever chunks sit nearest to those few
words -- in a measured case a logo and a DNA figure -- and the model then answers THAT. Such a
turn must be served from the previous answer alone (see chat_controller.transform_turn).

The rule is deliberately a CLOSED vocabulary rather than a list of phrases. A message counts as a
transform request only if

  * every word in it is request vocabulary -- politeness, pronouns, "that/it/all/again", request
    verbs ("translate", "repeat", "simplify"), languages ("Arabic"), format words ("simpler",
    "example", "steps"), confusion words ("get", "understand") -- and
  * at least one of them says what to do (a verb, a confusion word, a format word or a language).

So any word naming a topic makes it an ordinary question: "what is translation in NLP",
"explain overfitting again" and "give an example of entropy" all contain a word outside the
vocabulary and are searched as before. Question words (what, why, how, which...) are outside
the vocabulary on purpose, and so are the words that ask for NEW material ("more", "another",
"else", "elaborate", "tell me"). A miss here costs nothing new -- the turn is searched exactly as
it was before this module existed -- whereas a false hit would answer a real question from an old
answer, so the rule errs towards missing.

No model call: the follow-up rewrite already costs one per turn, and a wrong flag from it would
be a false hit with no way to see why. Whether it is allowed to apply at all (there must be a
previous answer) is decided by the caller.
"""

import re
import unicodedata
from dataclasses import dataclass

LANG_AR = "ar"
LANG_EN = "en"

_TASHKEEL_RE = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")  # marks + tatweel
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("’", "'").replace("'", "")  # can't -> cant
    text = _TASHKEEL_RE.sub("", text)
    for src, dst in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ى", "ي"), ("ة", "ه")):
        text = text.replace(src, dst)
    return text


def _words(spec: str) -> frozenset[str]:
    return frozenset(_normalize(spec).split())


# What a student says to say "do it to your last answer".
_VERBS = _words(
    "translate retranslate repeat say rephrase reword restate simplify summarize summarise shorten "
    "rewrite redo paraphrase retell clarify explain describe write put give show make makes "
    # Arabic
    "ترجم ترجمه ترجمها ترجمته اعد اعده اعيد اعادة كرر كررها كرره ابسط بسط بسطها بسطه لخص لخصها لخصه "
    "تلخيص تبسطها تبسطه تبسط تلخصها تلخصه تلخص تشرحها تشرحه تشرح تترجمها تترجمه تترجم تعيد تعيدها تكرر تكررها اعدها اعدلي اشرح اشرحها اشرحه اشرحلي شرح وضح وضحها وضحه صياغه اعطني اعطيني اعط هات قل قلها قله اكتب اكتبها"
)
_CONFUSION = _words(
    "get got understand understood follow followed clear confusing confused lost sense "
    "فهمت فاهم فاهمه افهم فهمتش مفهمتش واضح معقد صعب"
)
_FORMAT = _words(
    "simple simpler simply easier easy shorter short briefly brief detail detailed words terms version "
    "example examples summary tldr bullets bullet points step steps "
    "بسيط اسهل مختصر اختصار موجز بايجاز باختصار تفصيل كلمات مثال امثله مثلا خطوات نقاط"
)
_LANGUAGES = _words(
    "english arabic language "
    "عربي عربيه العربيه بالعربي بالعربيه للعربيه انجليزي انجليزيه الانجليزيه بالانجليزي بالانجليزيه "
    "للانجليزيه انكليزي انكليزيه بالانكليزيه اللغه باللغه لغه للعربي للانجليزي للانكليزي الانجليزي الانكليزي العربي بالانكليزي"
)
# Words that carry no request of their own.
_FILLER = _words(
    "ok okay oh well so hmm please pls kindly thanks thank you could can would will might may should "
    "maybe just now then also and or but again once for me to my the a an of in into on at by with as from "
    "is are was were be it its this that those these them all everything whole entire above previous "
    "last earlier your answer response reply message text explanation said wrote written i im ive id "
    "we us let lets want need like cant cannot can not dont do does did didnt isnt wont no yes sorry "
    "hey hi hello bro mate "
    # Arabic
    "ممكن لو سمحت من فضلك رجاء ارجوك طيب حسنا ماشي اوكي يا انت انا لي علي ان اني في ب ال الي الى الي "
    "هذا هذه هذي ده دي دا ذلك تلك هو هي كل كله كلها الكلام كلام هاد الاجابه اجابتك الرد ردك الشرح "
    "ما سبق السابق السابقه اعلاه فوق هنا مره اخرى تاني ثانيه تانيه مش لم لا لن غير "
)
_VOCABULARY = _VERBS | _CONFUSION | _FORMAT | _LANGUAGES | _FILLER
_REQUEST_WORDS = _VERBS | _CONFUSION | _FORMAT | _LANGUAGES

_ARABIC_MENTION = re.compile(r"\barabic\b|عربي")
_ENGLISH_MENTION = re.compile(r"\benglish\b|انجليزي|انكليزي")


@dataclass(frozen=True)
class TransformRequest:
    """The student wants the previous answer worked on. `language` is the one to reply in."""

    language: str


def _requested_language(normalized: str) -> str | None:
    """The last language the request names ("from arabic to english" -> english), if any."""
    mentions = [(m.start(), LANG_AR) for m in _ARABIC_MENTION.finditer(normalized)]
    mentions += [(m.start(), LANG_EN) for m in _ENGLISH_MENTION.finditer(normalized)]
    return max(mentions)[1] if mentions else None


def find_transform_request(question: str, fallback_language: str) -> TransformRequest | None:
    """A TransformRequest if `question` is only about the previous answer, else None.

    `fallback_language` ("en" | "ar") is the reply language when the request names none: the
    language the student wrote in.
    """
    normalized = _normalize(question or "")
    words = _WORD_RE.findall(normalized)
    if not words or len(words) > 20:
        return None
    if not all(w in _VOCABULARY or w.isdigit() for w in words):
        return None
    if not any(w in _REQUEST_WORDS for w in words):
        return None
    return TransformRequest(_requested_language(normalized) or fallback_language)
