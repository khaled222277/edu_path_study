"""Ingestion reports every figure the vision model failed on -- and changes nothing else."""

import pytest

from ingestion import images
from ingestion.images import ExtractedImage, FigureIssue
from ingestion.ingest_lecture import print_figure_issues, print_summary


def figure(page, page_index=0):
    return ExtractedImage(
        page_number=page, page_index=page_index, width=400, height=300,
        data=b"png-bytes", sha256=f"hash-{page}-{page_index}",
    )


class FakeVision:
    """Scripted per page: a str is a reply, an Exception is raised.

    `plan` maps page number -> list of outcomes (the last one repeats).
    """

    def __init__(self, plan, prompt_kind="describe"):
        self.plan = plan
        self.seen = {}

    def generate(self, *, model, prompt, images, options):
        page = int(images[0].decode().split(":")[1]) if images else 0
        outcomes = self.plan[page]
        n = self.seen.get(page, 0)
        self.seen[page] = n + 1
        outcome = outcomes[min(n, len(outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return {"response": outcome}


def tagged(page, page_index=0):
    """A figure whose bytes carry its page, so FakeVision can route by page."""
    img = figure(page, page_index)
    return ExtractedImage(
        page_number=page, page_index=page_index, width=400, height=300,
        data=f"page:{page}".encode(), sha256=img.sha256,
    )


@pytest.fixture()
def vision(monkeypatch):
    def install(plan):
        fake = FakeVision(plan)
        monkeypatch.setattr(images, "_client", lambda: fake)
        return fake

    return install


GOOD = "A flowchart of the KDD process with five labelled stages."


# --- description ------------------------------------------------------------


def test_skipped_figure_is_reported_with_page_and_reason(vision):
    vision({1: [GOOD], 7: [ConnectionError("connection refused")]})
    issues: list[FigureIssue] = []

    descriptions, failures = images.describe_images(
        [tagged(1), tagged(7)], "m", None, issues=issues
    )

    assert [d.page_number for d in descriptions] == [1]
    assert failures == 1
    assert len(issues) == 1
    assert issues[0].stage == "description" and issues[0].page_number == 7
    assert "ConnectionError" in issues[0].reason and "refused" in issues[0].reason
    assert issues[0].kept is False


def test_empty_response_and_repetition_loop_are_distinguished(vision):
    vision({3: [""], 4: ["the diagram shows a node " * 20]})  # >40 words, stuck repeating
    issues: list[FigureIssue] = []
    images.describe_images([tagged(3), tagged(4)], "m", None, issues=issues)

    by_page = {i.page_number: i.reason for i in issues}
    assert "empty response" in by_page[3]
    assert "repetition" in by_page[4]


def test_retry_that_succeeds_is_not_reported(vision):
    vision({5: ["", GOOD]})  # empty once, fine on the built-in retry
    issues: list[FigureIssue] = []
    descriptions, failures = images.describe_images([tagged(5)], "m", None, issues=issues)
    assert len(descriptions) == 1 and failures == 0 and issues == []


def test_fallback_model_rescue_is_not_reported(monkeypatch):
    class Picky:
        def generate(self, *, model, **kw):
            return {"response": GOOD if model == "big" else ""}

    monkeypatch.setattr(images, "_client", lambda: Picky())
    issues: list[FigureIssue] = []
    descriptions, failures = images.describe_images([tagged(2)], "small", "big", issues=issues)
    assert len(descriptions) == 1 and failures == 0 and issues == []


def test_collecting_issues_does_not_change_what_is_returned(vision):
    plan = {1: [GOOD], 2: [TimeoutError("slow")], 3: [GOOD], 4: [""]}
    figs = [tagged(p) for p in (1, 2, 3, 4)]

    vision(plan)
    plain, plain_failures = images.describe_images(figs, "m", None)
    vision(plan)
    watched, watched_failures = images.describe_images(figs, "m", None, issues=[])

    assert plain == watched
    assert plain_failures == watched_failures == 2


# --- classification ---------------------------------------------------------


def test_unclassified_figure_is_kept_and_reported(vision):
    vision({6: [ConnectionError("down")], 8: ["CONTENT"]})
    issues: list[FigureIssue] = []

    content, decorative, unclassified = images.classify_images(
        [tagged(6), tagged(8)], "m", None, issues=issues
    )

    assert [i.page_number for i in content] == [6, 8]  # still kept, as before
    assert decorative == [] and unclassified == 1
    assert len(issues) == 1
    assert issues[0].stage == "classification" and issues[0].kept is True
    assert issues[0].page_number == 6 and "ConnectionError" in issues[0].reason


def test_classification_gibberish_is_reported_as_no_verdict(vision):
    vision({9: ["I like turtles"]})
    issues: list[FigureIssue] = []
    images.classify_images([tagged(9)], "m", None, issues=issues)
    assert "verdict" in issues[0].reason


def test_decorative_figures_are_not_issues(vision):
    vision({1: ["DECORATIVE"]})
    issues: list[FigureIssue] = []
    content, decorative, _ = images.classify_images([tagged(1)], "m", None, issues=issues)
    assert len(decorative) == 1 and content == [] and issues == []


def test_functions_still_work_without_a_collector(vision):
    vision({1: ["CONTENT"]})
    assert images.classify_images([tagged(1)], "m", None)[2] == 0


# --- the printed summary ----------------------------------------------------


def test_summary_lists_skipped_figures_by_lecture_and_page(capsys, tmp_path):
    issues = [
        FigureIssue("description", 12, "ConnectionError: refused", kept=False),
        FigureIssue("description", 3, "empty response from qwen3-vl:4b", kept=False),
        FigureIssue("classification", 5, "TimeoutError", kept=True),
    ]
    print_figure_issues(issues, tmp_path / "kdd_lec01.pdf", lecture_id=42)
    out = capsys.readouterr().out

    assert "2 figure(s) SKIPPED" in out
    assert "lecture 42 (kdd_lec01.pdf) page 3: empty response" in out
    assert "lecture 42 (kdd_lec01.pdf) page 12: ConnectionError" in out
    assert out.index("page 3:") < out.index("page 12:")  # sorted by page
    assert "1 figure(s) kept WITHOUT" in out and "page 5" in out
    assert "re-ingest" in out


def test_dry_run_summary_has_no_lecture_id_but_names_the_file(capsys, tmp_path):
    print_figure_issues(
        [FigureIssue("description", 4, "boom", kept=False)], tmp_path / "a.pdf", None
    )
    out = capsys.readouterr().out
    assert "a.pdf page 4: boom" in out and "lecture None" not in out


def test_clean_run_prints_nothing_extra(capsys, tmp_path):
    print_figure_issues([], tmp_path / "a.pdf", 1)
    assert capsys.readouterr().out == ""


def test_full_summary_includes_the_figure_section(capsys, tmp_path):
    print_summary(
        pdf_path=tmp_path / "a.pdf", report={"total_pages": 10, "native": 10, "ocr": 0, "failed": 0},
        chunk_count=20, image_count=3, decorative_count=0, described_count=2,
        image_failures=1,
        figure_issues=[FigureIssue("description", 7, "ConnectionError: refused", kept=False)],
        saved_count=2, markers_found=True, dry_run=False, lecture_id=9,
    )
    out = capsys.readouterr().out
    assert "description failed: 1" in out
    assert "lecture 9 (a.pdf) page 7: ConnectionError" in out
    assert out.index("INGESTION COMPLETE") < out.index("SKIPPED") < out.rindex("=" * 58)
