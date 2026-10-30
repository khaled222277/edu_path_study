"""Which chunks a quiz may be made from: length, real words, boilerplate slides, figure descriptions.

Measured on the KDD lectures: the bad quiz questions ("what is the concept being demonstrated?") come
mostly from figure descriptions, and the sampling must still give a short quiz, not an error, when a
subject has few usable chunks.
"""

import pytest

from app.config import settings
from app.services import quiz_service as qs

PROSE = "Clustering groups similar objects so that objects in one cluster are alike and differ from other clusters. " * 3
TEXT = {"content_type": "text"}
FIGURE = {"content_type": "image_description"}


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    for name, value in (("quiz_include_figures", True), ("quiz_min_chunk_chars", 220), ("quiz_min_real_words", 25),
                        ("quiz_min_alpha_share", 0.5), ("quiz_skip_boilerplate_slides", True)):
        monkeypatch.setattr(settings, name, value)


def test_ordinary_lecture_text_is_eligible():
    assert qs.ineligible_reason(PROSE, TEXT) is None


def test_a_short_chunk_is_not():
    assert qs.ineligible_reason("Clustering groups objects.", TEXT) == qs.INELIGIBLE_SHORT
    assert qs.ineligible_reason("", TEXT) == qs.INELIGIBLE_SHORT


def test_the_minimum_length_is_a_setting(monkeypatch):
    monkeypatch.setattr(settings, "quiz_min_chunk_chars", len(PROSE) + 1)
    assert qs.ineligible_reason(PROSE, TEXT) == qs.INELIGIBLE_SHORT


def test_a_chunk_of_few_real_words_is_not():
    assert qs.ineligible_reason("a1 " * 120, TEXT) == qs.INELIGIBLE_LOW_WORDS
    assert qs.ineligible_reason(("ab " * 200), TEXT) == qs.INELIGIBLE_LOW_WORDS, "words of 1-2 letters are not real words"


def test_a_chunk_that_is_mostly_symbols_is_not(monkeypatch):
    symbols = PROSE + " = + - * / 0123456789 " * 60
    assert qs.ineligible_reason(symbols, TEXT) == qs.INELIGIBLE_LOW_ALPHA
    monkeypatch.setattr(settings, "quiz_min_alpha_share", 0.0)
    assert qs.ineligible_reason(symbols, TEXT) is None


@pytest.mark.parametrize("heading", ["Agenda", "Outline", "References", "Thank you!", "Questions?", "Q&A", "شكرا", "المراجع"])
def test_a_slide_that_is_only_a_boilerplate_heading_is_skipped(heading):
    assert qs.ineligible_reason(f"{heading}\n{PROSE}", TEXT) == qs.INELIGIBLE_BOILERPLATE


def test_a_real_slide_that_merely_mentions_the_word_is_kept():
    assert qs.ineligible_reason(f"Clustering references\n{PROSE}", TEXT) is None
    assert qs.ineligible_reason(f"{PROSE}\nReferences", TEXT) is None
    assert qs.ineligible_reason(f"Summary\n{PROSE}", TEXT) is None, "summary slides carry content"


def test_the_boilerplate_rule_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(settings, "quiz_skip_boilerplate_slides", False)
    assert qs.ineligible_reason(f"Agenda\n{PROSE}", TEXT) is None


def test_figure_descriptions_are_included_by_default_and_excluded_by_setting(monkeypatch):
    assert qs.ineligible_reason(PROSE, FIGURE) is None
    monkeypatch.setattr(settings, "quiz_include_figures", False)
    assert qs.ineligible_reason(PROSE, FIGURE) == qs.INELIGIBLE_FIGURE
    assert qs.ineligible_reason(PROSE, TEXT) is None, "lecture text is unaffected"


def test_a_decorative_figure_is_always_skipped():
    logo = "This is a logo. " + PROSE
    assert qs.ineligible_reason(logo, FIGURE) == qs.INELIGIBLE_DECORATIVE


# --- through sample_chunks -------------------------------------------------------------------------


class Collection:
    def __init__(self, entries):
        self.entries = entries

    def get(self, **kwargs):
        return {"documents": [t for t, _ in self.entries], "metadatas": [m for _, m in self.entries]}


def meta(lecture, page, kind="text"):
    return {"lecture_id": lecture, "page_number": page, "content_type": kind, "source_filename": f"lec{lecture}.pdf"}


def install(monkeypatch, entries):
    monkeypatch.setattr(qs, "get_collection", lambda: Collection(entries))


def test_sampling_still_spreads_across_lectures(monkeypatch):
    install(monkeypatch, [(PROSE + str(p), meta(l, p)) for l in (1, 2, 3) for p in range(1, 6)])
    chunks = qs.sample_chunks(7, count=3, seed=1)
    assert sorted(c.lecture_id for c in chunks) == [1, 2, 3]


def test_ineligible_chunks_are_never_sampled(monkeypatch):
    install(monkeypatch, [("short", meta(1, 1)), (f"Agenda\n{PROSE}", meta(1, 2)), (PROSE, meta(1, 3)),
                          (PROSE + "fig", meta(2, 1, "image_description"))])
    monkeypatch.setattr(settings, "quiz_include_figures", False)
    chunks = qs.sample_chunks(7, count=10, seed=1)
    assert [(c.lecture_id, c.page_number) for c in chunks] == [(1, 3)]


def test_a_subject_with_few_eligible_chunks_gives_a_short_sample_not_an_error(monkeypatch):
    install(monkeypatch, [(PROSE, meta(1, 1)), (PROSE + "b", meta(2, 1))] + [("tiny", meta(3, p)) for p in range(8)])
    assert len(qs.sample_chunks(7, count=10, seed=1)) == 2


def test_a_subject_with_no_eligible_chunk_gives_an_empty_sample(monkeypatch):
    install(monkeypatch, [("tiny", meta(1, 1))])
    assert qs.sample_chunks(7, count=5, seed=1) == []


def test_eligible_counts_per_lecture_with_reasons(monkeypatch):
    install(monkeypatch, [(PROSE, meta(1, 1)), ("tiny", meta(1, 2)), (f"Agenda\n{PROSE}", meta(1, 3)),
                          (PROSE + "f", meta(2, 1, "image_description"))])
    monkeypatch.setattr(settings, "quiz_include_figures", False)
    assert qs.eligibility_by_lecture(7) == {
        "lec1.pdf": {"chunks": 3, "eligible": 1, qs.INELIGIBLE_SHORT: 1, qs.INELIGIBLE_BOILERPLATE: 1},
        "lec2.pdf": {"chunks": 1, "eligible": 0, qs.INELIGIBLE_FIGURE: 1},
    }
