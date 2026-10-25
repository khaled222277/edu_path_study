"""scripts/eval_citations.py: the measurement the citation fix is judged by."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("eval_citations", ROOT / "scripts" / "eval_citations.py")
ec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ec)

R = {("a.pdf", 3), ("a.pdf", 4), ("b.pdf", 1)}


def test_classify_each_kind():
    assert ec.classify("[a.pdf, page 3]", R) == "ok"
    assert ec.classify("[a.pdf, page 9]", R) == "bad_page"
    assert ec.classify("[zzz.pdf, page 3]", R) == "bad_file"
    assert ec.classify("[a.pdf]", R) == "unreadable"
    assert ec.classify("[كتاب دروس كيد، صفحة 12]", R) == "unreadable"
    assert ec.classify("[0.0, 1.0]", R) is None and ec.classify("[v ∈ Values(A)]", R) is None


def test_several_pages_in_one_bracket_need_all_of_them_retrieved():
    assert ec.classify("[a.pdf, page 3, 4]", R) == "ok"
    assert ec.classify("[a.pdf, page 3, 99]", R) == "bad_page"


def test_arabic_indic_digits_are_read():
    assert ec.classify("[a.pdf, صفحة ٣]", R) == "ok"
    assert ec.classify("[a.pdf, صفحة ٩]", R) == "bad_page"


def row(answer, seed=1, lang="en", kind="narrow"):
    return {"lang": lang, "kind": kind, "seed": seed, "answer": answer, "sources": [{"file": "a.pdf", "page": 3}]}


def test_analyse_counts_strict_and_lenient_separately_per_seed():
    rows = [row("x [a.pdf, page 3] y [a.pdf, page 8]"), row("z [a.pdf]"), row("none", seed=2)]
    r = ec.analyse(rows)["en"]
    assert r[1]["strict_cited"] == 2 and r[1]["strict_bad"] == 1
    assert r[1]["lenient_cited"] == 3 and r[1]["unreadable"] == 1 and r[1]["bad_page"] == 1
    assert r[1]["answers_with_bad"] == 2 and r[2]["answers_with_any"] == 0


def test_out_of_scope_answers_are_excluded():
    assert ec.analyse([row("[a.pdf, page 8]", kind="out_of_scope")])["en"] == {}


def test_rate_skips_seeds_with_nothing_to_divide():
    assert ec.rate({1: {"a": 1, "b": 2}, 2: {"a": 0, "b": 0}}, "a", "b") == [0.5]
