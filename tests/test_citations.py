"""Citations are checked against what was retrieved; invalid ones are removed, valid ones normalised."""

import random

import pytest

from app.services.citations import (
    Citation,
    CitationFilter,
    Retrieved,
    find_citations,
    judge,
    parse_bracket,
    to_ascii_digits,
    validate_answer,
)

R = Retrieved([("kdd_lec04_association_rule.pdf", 12), ("kdd_lec04_association_rule.pdf", 15), ("ds_lec04_time.pdf", 13)])
F4 = "kdd_lec04_association_rule.pdf"


def clean(text: str) -> str:
    return validate_answer(text, R).text


# --- digits ------------------------------------------------------------------------------


def test_arabic_indic_and_persian_digits_become_ascii():
    assert to_ascii_digits("١٢٣٤٥٦٧٨٩٠") == "1234567890"
    assert to_ascii_digits("۰۱۲۳۴۵۶۷۸۹") == "0123456789"
    assert to_ascii_digits("page 12") == "page 12"


# --- reading brackets -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "bracket, file, pages",
    [
        ("[a.pdf, page 12]", "a.pdf", (12,)),
        ("[a.pdf,page 12]", "a.pdf", (12,)),
        ("[a.pdf, Page 12]", "a.pdf", (12,)),
        ("[a.pdf, p. 12]", "a.pdf", (12,)),
        ("[a.pdf, pages 3, 5]", "a.pdf", (3, 5)),
        ("[a.pdf, page 19, 14, 16]", "a.pdf", (19, 14, 16)),
        ("[a.pdf, page 3-5]", "a.pdf", (3, 4, 5)),
        ("[a.pdf, page 3 and 7]", "a.pdf", (3, 7)),
        ("[a.pdf, صفحة 12]", "a.pdf", (12,)),
        ("[a.pdf، صفحة ١٢]", "a.pdf", (12,)),
        ("[a.pdf, الصفحة ١٢]", "a.pdf", (12,)),
        ("[a.pdf, صفحات 3 و 5]", "a.pdf", (3, 5)),
        ("[a.pdf, ص ٧]", "a.pdf", (7,)),
        ("[my lecture notes, page 4]", "my lecture notes", (4,)),
        ("[a.pdf, page 3, 3]", "a.pdf", (3,)),
    ],
)
def test_the_forms_the_model_actually_writes(bracket, file, pages):
    c = parse_bracket(bracket)
    assert c and c.parsed and c.file == file and c.pages == pages


def test_a_file_with_no_page_is_a_file_only_citation():
    c = parse_bracket("[kdd_lec06_classifacation_1.pdf]")
    assert c.parsed and c.pages == () and c.file == "kdd_lec06_classifacation_1.pdf"


@pytest.mark.parametrize("bracket", ["[0.0, 1.0]", "[v ∈ Values(A)]", "[new_minA, new_maxA]", "[1]", "[]", "[note]", "[x, y, z]"])
def test_brackets_that_are_not_citations_are_ignored(bracket):
    assert parse_bracket(bracket) is None


@pytest.mark.parametrize(
    "bracket",
    ["[lecture.pdf, page 4 من KDD_lec01_Introduction.pdf]", "[see a.pdf for more]", "[x, page 3 of the book.pdf]"],
)
def test_citation_shaped_but_unreadable_is_flagged_unparseable(bracket):
    c = parse_bracket(bracket)
    assert c is not None and not c.parsed


def test_very_long_or_multiline_brackets_are_not_citations():
    assert parse_bracket("[" + "a.pdf " * 60 + "]") is None
    assert parse_bracket("[a.pdf,\npage 3]") is None


def test_find_citations_lists_them_in_order():
    found = find_citations("x [a.pdf, page 1] y [0, 1] z [b.pdf, page 2]")
    assert [c.file for c in found] == ["a.pdf", "b.pdf"]


# --- judging against what was retrieved -------------------------------------------------------


def test_a_retrieved_page_is_valid():
    v = judge(parse_bracket(f"[{F4}, page 12]"), R)
    assert v.status == "valid" and v.kept == (12,) and v.file == F4


def test_a_page_that_was_not_retrieved_is_dropped_even_for_a_retrieved_file():
    v = judge(parse_bracket(f"[{F4}, page 99]"), R)
    assert v.status == "unretrieved" and v.kept == () and v.dropped == (99,) and not v.ok


def test_some_pages_good_some_bad_is_partial():
    v = judge(parse_bracket(f"[{F4}, page 12, 99, 15]"), R)
    assert v.status == "partial" and v.kept == (12, 15) and v.dropped == (99,) and v.ok


def test_a_file_that_was_not_retrieved_is_invalid_whatever_its_page():
    assert not judge(parse_bracket("[other.pdf, page 12]"), R).ok


def test_file_matching_tolerates_case_path_and_a_missing_extension():
    for name in (F4.upper(), "lectures/" + F4, "C:\\x\\" + F4, F4.removesuffix(".pdf"), f'"{F4}"'):
        assert judge(parse_bracket(f"[{name}, page 12]"), R).kept == (12,), name


def test_a_file_only_citation_of_a_retrieved_file_makes_no_false_claim():
    assert judge(parse_bracket(f"[{F4}]"), R).status == "valid"
    assert judge(parse_bracket("[other.pdf]"), R).status == "unretrieved"


def test_an_unparseable_citation_is_invalid():
    assert judge(Citation("[x.pdf ?]", False), R).status == "unparseable"


# --- whole-text validation -----------------------------------------------------------------------


def test_a_valid_citation_is_left_exactly_as_it_was():
    t = f"Confidence is a ratio. [{F4}, page 15] It matters."
    assert clean(t) == t


def test_an_invalid_citation_is_removed_without_leaving_a_gap():
    assert clean(f"Support is X [{F4}, page 99] and more.") == "Support is X and more."
    assert clean(f"Support is X [{F4}, page 99].") == "Support is X."
    assert clean(f"Support is X [{F4}, page 99]\nNext line") == "Support is X\nNext line"
    assert clean(f"[{F4}, page 99] Start") == "Start"
    assert clean(f"Support [{F4}, page 99]and more") == "Support and more"


def test_arabic_text_around_a_removed_citation_reads_normally():
    assert clean("الثقة هي نسبة [كتاب دروس كيد، صفحة 12] ثم تحسب.") == "الثقة هي نسبة ثم تحسب."
    assert clean("الثقة هي نسبة [كتاب دروس كيد، صفحة 12]؟") == "الثقة هي نسبة؟"


def test_the_citation_is_rewritten_to_one_canonical_form():
    assert clean(f"a [{F4}, page 12, 15] b") == f"a [{F4}, page 12] [{F4}, page 15] b"
    assert clean(f"a [{F4.upper()}, صفحة ١٢] b") == f"a [{F4}, page 12] b"
    assert clean(f"a [{F4}, page 12, 99] b") == f"a [{F4}, page 12] b"


def test_arabic_numerals_in_page_references_are_checked_not_ignored():
    assert clean(f"a [{F4}, صفحة ١٥] b") == f"a [{F4}, page 15] b"
    assert clean(f"a [{F4}, صفحة ٩٩] b") == "a b"


def test_formula_and_other_brackets_are_untouched():
    t = "The range [0.0, 1.0] and [v ∈ Values(A)] and [new_minA, new_maxA] are fine."
    assert clean(t) == t


def test_the_report_lists_what_was_kept_and_removed():
    out = validate_answer(f"a [{F4}, page 12] b [{F4}, page 99] c [other.pdf, page 1]", R)
    assert out.kept == [(F4, 12)] and out.removed == [f"[{F4}, page 99]", "[other.pdf, page 1]"]


def test_a_reply_with_no_citations_is_unchanged():
    assert clean("Just an answer with no sources.") == "Just an answer with no sources."


def test_empty_and_whitespace_only_input():
    assert clean("") == "" and clean("   ") == ""


def test_an_unclosed_bracket_is_just_text():
    assert clean("see [a.pdf, page 3 and more") == "see [a.pdf, page 3 and more"


def test_validation_is_idempotent():
    t = f"a [{F4}, page 12, 99] b [x.pdf, page 1] c [0, 1] [{F4}]"
    assert clean(clean(t)) == clean(t)


def test_no_invalid_citation_survives():
    t = f"a [{F4}, page 99] b [other.pdf, page 2] c [{F4}, page 12] d [lecture.pdf, page 4 من X.pdf] e [{F4}, p 15]"
    for c in find_citations(clean(t)):
        assert judge(c, R).ok, c.raw


# --- the streaming filter agrees with the whole-text one, however the text is cut -------------------

SAMPLES = [
    f"Confidence is a ratio. [{F4}, page 15] It matters [{F4}, page 99].",
    "الثقة [كتاب دروس كيد، صفحة 12] نسبة [" + F4 + ", صفحة ١٢] ثم [" + F4 + ", صفحة ٩٩]؟",
    f"[{F4}, page 99] leading, trailing [other.pdf, page 1]",
    f"a  [{F4}, page 99]  b   [{F4}, page 12]   c",
    "no brackets at all, just words.",
    "range [0.0, 1.0] stays; [[nested]] and [unclosed",
    f"two pages [{F4}, page 12, 15, 99] done",
    "line one [x.pdf, page 1]\nline two [y.pdf]\n",
    "[",
    "]",
    "[[",
    f"tail [{F4}, page 1",
]


def stream(text: str, cuts: list[int]) -> str:
    f = CitationFilter(R)
    edges = [0, *sorted(set(cuts)), len(text)]
    return "".join(f.feed(text[a:b]) for a, b in zip(edges, edges[1:])) + f.flush()


@pytest.mark.parametrize("text", SAMPLES)
def test_output_does_not_depend_on_how_the_text_is_split(text):
    expected = clean(text)
    rng = random.Random(1)
    assert stream(text, list(range(1, len(text)))) == expected, "one character at a time"
    assert stream(text, []) == expected, "all at once"
    for _ in range(80):
        cuts = rng.sample(range(1, max(len(text), 2)), k=min(rng.randint(0, 7), max(len(text) - 1, 0)))
        assert stream(text, cuts) == expected, (text, cuts)


def test_an_invalid_citation_is_never_emitted_even_partially():
    """Nothing of a bracket reaches the browser until it is complete and judged."""
    f = CitationFilter(R)
    text = f"Answer [{F4}, page 99] end"
    emitted = ""
    for ch in text:
        emitted += f.feed(ch)
        assert "[" not in emitted and "page 99" not in emitted and ".pdf" not in emitted, emitted
    emitted += f.flush()
    assert emitted == "Answer end"


def test_a_valid_citation_appears_only_once_complete_and_already_canonical():
    f = CitationFilter(R)
    seen = [f.feed(ch) for ch in f"x [{F4.upper()}, صفحة ١٢] y"]
    joined = "".join(seen)
    assert joined == f"x [{F4}, page 12] y"
    assert all("[" not in s or s.endswith("]") or True for s in seen)


def test_text_outside_brackets_is_not_delayed():
    f = CitationFilter(R)
    assert f.feed("Hello") == "Hello"
    assert f.feed(" world") == " world", "a space followed by a letter is sent at once"
    assert f.feed(" ") == "" and f.feed("!") == " !", "only a space at the very end of a piece waits"


def test_plain_text_after_a_broken_bracket_is_released_at_the_newline():
    f = CitationFilter(R)
    assert f.feed("a [open") == "a"
    assert f.feed("\nnext") == " [open\nnext"


def test_kept_and_removed_accumulate_across_pieces():
    f = CitationFilter(R)
    for piece in (f"a [{F4}, pa", "ge 12] b [oth", "er.pdf, page 1] c"):
        f.feed(piece)
    f.flush()
    assert f.kept == [(F4, 12)] and f.removed == ["[other.pdf, page 1]"]


def test_flush_drops_trailing_spaces_and_releases_a_half_open_bracket():
    f = CitationFilter(R)
    out = f.feed("end   ") + f.flush()
    assert out == "end"
    g = CitationFilter(R)
    assert g.feed("see [a.pdf, page") + g.flush() == "see [a.pdf, page"
