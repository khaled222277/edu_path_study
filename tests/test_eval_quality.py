"""The evaluation's own metrics: a number nobody has tested is a number nobody should trust."""

import csv
import importlib.util
import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("eval_quality", ROOT / "scripts" / "eval_quality.py")
eq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eq)


def chunk(file="a.pdf", page=1, text="k-means uses centroids"):
    return SimpleNamespace(source_filename=file, page_number=page, text=text, content_type="text")


# --- the question set itself ----------------------------------------------------------------------


def test_the_question_set_has_the_promised_shape():
    items = eq.load_items()
    langs = [i["lang"] for i in items]
    kinds = [i["kind"] for i in items]
    assert len(items) >= 40
    assert langs.count("ar") >= 25 and langs.count("en") >= 15
    assert kinds.count("out_of_scope") >= 6
    assert {"narrow", "broad", "follow_up", "figure", "out_of_scope"} <= set(kinds)
    assert len({i["id"] for i in items}) == len(items), "ids are unique"


def test_every_item_is_complete():
    for i in eq.load_items():
        assert i["question"].strip() and i["expected_answer"].strip() and i["subject_id"] in (6, 7)
        assert isinstance(i["uncertain"], bool)
        if i["uncertain"]:
            assert i["uncertain_why"].strip()
        if i["kind"] == "out_of_scope":
            assert i["gold"] == []
        else:
            assert i["gold"], f"{i['id']} has no gold"
        if i["kind"] == "follow_up":
            assert i["previous"].strip()


def test_arabic_questions_are_arabic_and_english_ones_are_not():
    for i in eq.load_items():
        has_arabic = any("؀" <= ch <= "ۿ" for ch in i["question"])
        assert has_arabic == (i["lang"] == "ar"), i["id"]


def test_gold_names_a_real_page_of_the_right_subject():
    """The gold pages must exist in the corpus snapshot the set was built from."""
    pytest.importorskip("chromadb")
    from app.services import vector_store

    chroma = ROOT / "data" / "chroma_db"
    if not chroma.exists():
        pytest.skip("no ingested corpus on this machine")
    import chromadb

    metas = chromadb.PersistentClient(path=str(chroma)).get_collection("lecture_corpus", embedding_function=None).get(include=["metadatas"])["metadatas"]
    have = {(m["source_filename"], int(m["page_number"]), int(m["subject_id"])) for m in metas}
    missing = [(i["id"], g) for i in eq.load_items() for g in i["gold"] if (g["file"], g["page"], i["subject_id"]) not in have]
    assert not missing


# --- retrieval metrics ------------------------------------------------------------------------------


def test_hit_at_looks_only_at_the_first_k():
    gold = {("g.pdf", 5)}
    chunks = [chunk("x.pdf", 1), chunk("x.pdf", 2), chunk("g.pdf", 5)]
    assert not eq.hit_at(chunks, gold, 2) and eq.hit_at(chunks, gold, 3)
    assert eq.first_gold_rank(chunks, gold) == 3 and eq.first_gold_rank(chunks[:2], gold) is None


def test_hit_requires_the_same_file_and_page():
    assert not eq.hit_at([chunk("g.pdf", 6)], {("g.pdf", 5)}, 1)
    assert not eq.hit_at([chunk("h.pdf", 5)], {("g.pdf", 5)}, 1)


def test_retrieval_summary_is_per_language_and_overall():
    rows = [
        {"lang": "ar", "hit_cand3": True, "hit_cand5": True, "hit_cand10": True, "hit_reranked3": False, "hit_final": False, "rank_candidates": 2},
        {"lang": "ar", "hit_cand3": False, "hit_cand5": True, "hit_cand10": True, "hit_reranked3": True, "hit_final": True, "rank_candidates": 4},
        {"lang": "en", "hit_cand3": True, "hit_cand5": True, "hit_cand10": True, "hit_reranked3": True, "hit_final": True, "rank_candidates": 1},
    ]
    s = eq.summarize_retrieval(rows)
    assert s["ar"]["hit_cand3"] == 0.5 and s["ar"]["hit_final"] == 0.5 and s["en"]["hit_final"] == 1.0
    assert s["all"]["n"] == 3 and s["all"]["hit_cand10"] == 1.0
    assert s["ar"]["mrr_candidates"] == pytest.approx((1 / 2 + 1 / 4) / 2)


# --- script purity -----------------------------------------------------------------------------------


def test_citations_are_not_counted_as_leaked_latin():
    answer = "تُحسب الإنتروبيا هكذا [kdd_lec06_classifacation_1.pdf, page 21]."
    p = eq.script_purity(answer, set())
    assert p["latin_share"] == 0.0 and p["unexplained_words"] == []


def test_a_lecture_term_is_explained_but_other_english_is_a_leak():
    vocab = {"k-means", "centroid"}
    ok = eq.script_purity("تعمل خوارزمية k-means باستخدام centroid لكل مجموعة", vocab)
    assert ok["unexplained_words"] == [] and ok["latin_share"] > 0
    bad = eq.script_purity("تعمل الخوارزمية بشكل efficient جدا", vocab)
    assert bad["unexplained_words"] == ["efficient"] and bad["unexplained_share"] > 0


def test_short_latin_fragments_are_ignored():
    assert eq.script_purity("القيمة x و y", set())["unexplained_words"] == []


def test_a_purely_english_answer_is_all_leak():
    p = eq.script_purity("This is entirely English text without any Arabic.", set())
    assert p["latin_share"] == 1.0 and p["unexplained_share"] > 0.7


def test_context_vocabulary_includes_the_question():
    v = eq.context_vocabulary([chunk(text="Apriori prunes")], "What is k-means?")
    assert {"apriori", "prunes", "k-means"} <= v


# --- citations -----------------------------------------------------------------------------------------


def test_citation_check_separates_existing_from_retrieved():
    corpus = {("a.pdf", 1), ("b.pdf", 2), ("c.pdf", 3)}
    retrieved = {("a.pdf", 1)}
    c = eq.citation_check("x [a.pdf, page 1] y [b.pdf, page 2] z [zzz.pdf, page 9]", retrieved, corpus)
    assert c == {"n_cited": 3, "n_exist": 2, "n_retrieved": 1}


def test_an_answer_without_citations_has_none():
    assert eq.citation_check("no citations here", set(), set())["n_cited"] == 0


def test_page_wording_is_case_insensitive_and_spacing_tolerant():
    assert eq.citation_check("[a.pdf,page 3]", {("a.pdf", 3)}, {("a.pdf", 3)})["n_retrieved"] == 1
    assert eq.citation_check("[a.pdf, Page 3]", {("a.pdf", 3)}, {("a.pdf", 3)})["n_retrieved"] == 1


# --- chat metrics ------------------------------------------------------------------------------------------


def parsed(text, refused=False):
    return SimpleNamespace(text=text, refused=refused)


def item(kind="narrow", lang="en", gold=(("a.pdf", 1),), question="What is it?"):
    return {"kind": kind, "lang": lang, "gold": [{"file": f, "page": p} for f, p in gold], "question": question}


def test_refusing_an_out_of_scope_question_is_correct_and_answering_it_is_not():
    corpus = {("a.pdf", 1)}
    assert eq.chat_metrics(item("out_of_scope", gold=()), parsed("not covered", True), [chunk()], corpus)["refusal_correct"]
    assert not eq.chat_metrics(item("out_of_scope", gold=()), parsed("It is Paris.", False), [chunk()], corpus)["refusal_correct"]


def test_refusing_an_in_scope_question_is_a_false_refusal():
    m = eq.chat_metrics(item(), parsed("not covered", True), [chunk()], {("a.pdf", 1)})
    assert m["refused"] and not m["refusal_correct"]


def test_language_check_follows_the_question_language():
    corpus = {("a.pdf", 1)}
    assert eq.chat_metrics(item(lang="ar"), parsed("هذا جواب عربي كامل"), [chunk()], corpus)["language_ok"]
    assert not eq.chat_metrics(item(lang="ar"), parsed("This answer is in English"), [chunk()], corpus)["language_ok"]
    assert not eq.chat_metrics(item(lang="en"), parsed("هذا جواب عربي كامل"), [chunk()], corpus)["language_ok"]


def test_out_of_scope_items_have_no_retrieval_hit_metric():
    assert eq.chat_metrics(item("out_of_scope", gold=()), parsed("x", True), [chunk()], set())["hit_final"] is None


def test_arabic_answers_get_purity_metrics_english_ones_do_not():
    corpus = {("a.pdf", 1)}
    assert "latin_share" in eq.chat_metrics(item(lang="ar"), parsed("جواب"), [chunk()], corpus)
    assert "latin_share" not in eq.chat_metrics(item(lang="en"), parsed("answer"), [chunk()], corpus)


def _row(seed, kind, lang="en", **m):
    base = {"refused": False, "language_ok": True, "hit_final": True, "n_cited": 0, "n_exist": 0, "n_retrieved": 0}
    return {"lang": lang, "kind": kind, "seed": seed, "metrics": {**base, **m}}


def test_chat_summary_gives_one_value_per_seed():
    rows = [
        _row(1, "narrow", hit_final=True), _row(1, "narrow", hit_final=False),
        _row(2, "narrow", hit_final=True), _row(2, "narrow", hit_final=True),
        _row(1, "out_of_scope", refused=True), _row(2, "out_of_scope", refused=False),
    ]
    s = eq.summarize_chat(rows)["en"]
    assert s["retrieval_hit"] == [0.5, 1.0]
    assert s["oos_refused"] == [1.0, 0.0]
    assert s["false_refusal"] == [0.0, 0.0]
    assert eq.fmt_ms(s["retrieval_hit"]) == " 75.0 ±35.4"


def test_citation_shares_are_pooled_not_averaged_per_answer():
    rows = [_row(1, "narrow", n_cited=4, n_exist=4, n_retrieved=1), _row(1, "narrow", n_cited=1, n_exist=1, n_retrieved=1)]
    s = eq.summarize_chat(rows)["en"]
    assert s["citations_retrieved"] == [pytest.approx(2 / 5)] and s["citations_exist"] == [1.0]


# --- quiz metrics ---------------------------------------------------------------------------------------------


def test_duplicate_questions_count_repeats_of_an_earlier_one():
    qs_ = ["What is the Apriori principle?", "What is the Apriori principle", "How does k-means assign points?", "What is the apriori principle ?"]
    assert eq.duplicate_questions(qs_) == 2
    assert eq.duplicate_questions(["a b c d", "x y z w"]) == 0
    assert eq.duplicate_questions([]) == 0


def test_duplicates_work_for_arabic():
    assert eq.duplicate_questions(["ما هي عملية التجميع في البيانات", "ما هي عملية التجميع في البيانات؟"]) == 1


def test_lexical_support_measures_words_found_in_the_chunk():
    chunk_text = "K-means defines the center of a cluster as the mean point"
    assert eq.lexical_support("the mean point of a cluster", chunk_text) == 1.0
    assert eq.lexical_support("completely unrelated banana answer", chunk_text) == 0.0
    assert eq.lexical_support("it is", chunk_text) is None, "no word long enough to judge"


# --- the review sheet -----------------------------------------------------------------------------------------------


def fake_chat(tmp, label, n=12):
    rows = [{"id": f"q{i}", "lang": "ar" if i % 2 else "en", "kind": "narrow", "seed": 1, "question": f"Q{i}?",
             "answer": f"A{i}", "sources": [{"file": "f.pdf", "page": i, "type": "text", "snippet": "snippet"}]} for i in range(n)]
    path = tmp / f"chat_{label}.json"
    path.write_text(json.dumps({"meta": {"label": label, "mode": "chat"}, "rows": rows}), encoding="utf-8")
    return str(path)


def fake_quiz(tmp, label, n=3):
    rows = [{"lang": "ar" if i % 2 else "en", "quiz": i, "seed": i, "items": [
        {"question": f"Q{i}.{j}", "options": ["a", "b", "c", "d"], "correct": j % 4, "citation": "f.pdf, page 1", "chunk": "chunk text"} for j in range(5)]}
        for i in range(n)]
    path = tmp / f"quiz_{label}.json"
    path.write_text(json.dumps({"meta": {"label": label, "mode": "quiz"}, "rows": rows}), encoding="utf-8")
    return str(path)


def test_the_sheet_is_blind_balanced_and_shuffled(tmp_path):
    sheet, key = eq.make_review([fake_chat(tmp_path, "A"), fake_chat(tmp_path, "B")],
                                [fake_quiz(tmp_path, "A"), fake_quiz(tmp_path, "B")], 10, 10, seed=3)
    assert len(sheet) == 20 and [r["id"] for r in sheet] == [f"R{n:03d}" for n in range(1, 21)]
    for row in sheet:
        assert set(row) == {"id", "type", "language", "question", "sources", "answer", "options", "marked_correct"}
        assert "variant" not in row and "seed" not in row
    assert set(key) == {r["id"] for r in sheet}
    chat_variants = Counter_(v["variant"] for v in key.values() if v["type"] == "chat")
    assert chat_variants["A"] == chat_variants["B"] == 5, "variants are evenly represented"
    langs = Counter_(r["language"] for r in sheet if r["type"] == "chat")
    assert abs(langs["ar"] - langs["en"]) <= 2
    assert [r["type"] for r in sheet] != sorted(r["type"] for r in sheet), "chat and quiz are mixed"


def Counter_(it):
    from collections import Counter

    return Counter(it)


def test_the_same_seed_gives_the_same_sheet(tmp_path):
    files = ([fake_chat(tmp_path, "A")], [fake_quiz(tmp_path, "A")])
    a, _ = eq.make_review(*files, 5, 5, seed=11)
    b, _ = eq.make_review(*files, 5, 5, seed=11)
    c, _ = eq.make_review(*files, 5, 5, seed=12)
    assert a == b and a != c


def test_asking_for_more_than_exists_returns_what_exists(tmp_path):
    sheet, _ = eq.make_review([fake_chat(tmp_path, "A", n=3)], [], 30, 30, seed=1)
    assert len(sheet) == 3


def test_a_quiz_row_shows_options_and_the_marked_answer_but_not_the_variant(tmp_path):
    sheet, key = eq.make_review([], [fake_quiz(tmp_path, "A")], 0, 4, seed=1)
    row = sheet[0]
    assert row["options"].splitlines()[0].startswith("A. ") and row["marked_correct"] in "ABCD"
    assert row["answer"] == "" and key[row["id"]]["variant"] == "A"


def test_csv_and_html_are_written_with_empty_grading_columns(tmp_path):
    sheet, key = eq.make_review([fake_chat(tmp_path, "A")], [fake_quiz(tmp_path, "A")], 4, 4, seed=1)
    eq.write_sheet(sheet, key, tmp_path / "s.csv", tmp_path / "s.html", tmp_path / "k.json")
    with open(tmp_path / "s.csv", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 8 and list(rows[0])[-4:] == ["correct_0_1_2", "grounded_y_n", "arabic_quality_1_5", "notes"]
    assert all(r["correct_0_1_2"] == "" and r["notes"] == "" for r in rows)
    page = (tmp_path / "s.html").read_text(encoding="utf-8")
    assert "dir='auto'" in page and "R001" in page and "variant" not in page.lower()
    assert json.loads((tmp_path / "k.json").read_text(encoding="utf-8")) == key


def test_arabic_survives_the_csv_round_trip(tmp_path):
    sheet = [{"id": "R001", "type": "chat", "language": "ar", "question": "ما هي الإنتروبيا؟", "sources": "s", "answer": "مقياس", "options": "", "marked_correct": ""}]
    eq.write_sheet(sheet, {"R001": {"variant": "A", "type": "chat", "lang": "ar"}}, tmp_path / "s.csv", tmp_path / "s.html", tmp_path / "k.json")
    raw = (tmp_path / "s.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "BOM so Excel opens it as UTF-8"
    assert "ما هي الإنتروبيا؟" in raw.decode("utf-8-sig")


# --- feeding grades back -------------------------------------------------------------------------------------------------


def graded_csv(tmp, rows):
    path = tmp / "graded.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=eq.SHEET_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in eq.SHEET_COLUMNS})
    return path


KEY = {
    "R001": {"variant": "base", "type": "chat", "lang": "ar"}, "R002": {"variant": "base", "type": "chat", "lang": "ar"},
    "R003": {"variant": "new", "type": "chat", "lang": "ar"}, "R004": {"variant": "new", "type": "quiz", "lang": "en"},
}


def test_grades_are_joined_to_variants_and_averaged(tmp_path):
    path = graded_csv(tmp_path, [
        {"id": "R001", "correct_0_1_2": "2", "grounded_y_n": "y", "arabic_quality_1_5": "4"},
        {"id": "R002", "correct_0_1_2": "0", "grounded_y_n": "N", "arabic_quality_1_5": "2"},
        {"id": "R003", "correct_0_1_2": "2", "grounded_y_n": "y", "arabic_quality_1_5": "5"},
        {"id": "R004", "correct_0_1_2": "1", "grounded_y_n": "", "arabic_quality_1_5": ""},
    ])
    s = eq.summarize_grades(eq.read_graded(path, KEY))
    base = s["base | ar | chat"]
    assert base["n"] == 2 and base["mean_correct_0_2"] == 1.0 and base["pct_fully_correct"] == 0.5
    assert base["pct_grounded"] == 0.5 and base["mean_arabic_quality"] == 3.0
    assert s["new | ar | chat"]["mean_correct_0_2"] == 2.0
    quiz = s["new | en | quiz"]
    assert quiz["mean_correct_0_2"] == 1.0 and quiz["pct_grounded"] is None and quiz["mean_arabic_quality"] is None


def test_ungraded_and_unknown_rows_are_tolerated(tmp_path):
    path = graded_csv(tmp_path, [{"id": "R001"}, {"id": "R999", "correct_0_1_2": "2"}])
    s = eq.summarize_grades(eq.read_graded(path, KEY))
    assert list(s) == ["base | ar | chat"] and s["base | ar | chat"]["graded"] == 0 and s["base | ar | chat"]["mean_correct_0_2"] is None


def test_a_mistyped_grade_is_ignored_not_fatal(tmp_path):
    path = graded_csv(tmp_path, [{"id": "R001", "correct_0_1_2": "two", "grounded_y_n": "maybe", "arabic_quality_1_5": "n/a"}])
    row = eq.read_graded(path, KEY)[0]
    assert row["correct"] is None and row["arabic_quality"] is None and row["grounded"] is None


def test_mean_sd_helpers():
    assert eq.mean_sd([1.0, 3.0]) == (2.0, pytest.approx(1.41421356))
    assert eq.mean_sd([]) [0] != eq.mean_sd([])[0]  # NaN
    assert eq.fmt_ms([0.5]) == " 50.0" and eq.fmt_ms([]) == "n/a"


def test_mrr_counts_a_miss_as_zero_rather_than_ignoring_it():
    rows = [
        {"lang": "en", "hit_cand3": True, "hit_cand5": True, "hit_cand10": True, "hit_reranked3": True, "hit_final": True, "rank_candidates": 1},
        {"lang": "en", "hit_cand3": False, "hit_cand5": False, "hit_cand10": False, "hit_reranked3": False, "hit_final": False, "rank_candidates": None},
    ]
    assert eq.summarize_retrieval(rows)["en"]["mrr_candidates"] == 0.5


class _FakeEmbedder:
    """Maps text to a fixed vector so the geometry is known."""

    def __init__(self, table):
        self.table = table

    def __call__(self, texts):
        return [self.table[t] for t in texts]


def _support(monkeypatch, correct):
    from app.services import vector_store

    table = {"CHUNK": [1.0, 0.0, 0.0], "near": [0.9, 0.1, 0.0], "far1": [0.0, 1.0, 0.0], "far2": [0.0, 0.0, 1.0], "far3": [0.1, 0.0, 1.0]}
    monkeypatch.setattr(vector_store, "get_embedding_function", lambda device=None: _FakeEmbedder(table))
    return eq.support_by_embedding("CHUNK", ["far1", "near", "far2", "far3"], correct)


def test_embedding_support_is_true_only_when_the_correct_option_is_the_closest(monkeypatch):
    assert _support(monkeypatch, correct=1)["correct_is_closest"] is True
    assert _support(monkeypatch, correct=0)["correct_is_closest"] is False


def test_embedding_margin_is_correct_minus_the_mean_of_the_others(monkeypatch):
    good = _support(monkeypatch, correct=1)["margin"]
    bad = _support(monkeypatch, correct=0)["margin"]
    assert good > 0.3 and bad < 0


# --- reusing queries so configurations compare exactly -------------------------------------------


def _result_with_queries(tmp, mapping):
    path = tmp / "r.json"
    path.write_text(json.dumps({"meta": {}, "rows": [{"id": k, "query": v} for k, v in mapping.items()]}), encoding="utf-8")
    return str(path)


def test_load_queries_maps_ids_to_the_earlier_search_text(tmp_path):
    q = eq.load_queries(_result_with_queries(tmp_path, {"a19": "rewritten text", "e01": "plain"}))
    assert q == {"a19": "rewritten text", "e01": "plain"}
    assert eq.load_queries(None) == {} and eq.load_queries("") == {}


def test_a_reused_query_is_searched_and_the_rewrite_is_not_called(monkeypatch):
    seen = []

    class Trace:
        candidates, reranked, final, strategy, top_score = [], [], [], "reranked", 0.5

    monkeypatch.setattr(eq.retrieval, "retrieve_detailed", lambda q, s, c, k: seen.append(q) or Trace())

    def boom(*a):
        raise AssertionError("must not rewrite when the query is supplied")

    monkeypatch.setattr(eq, "retrieval_query", boom)
    item = {"id": "a19", "lang": "ar", "kind": "follow_up", "subject_id": 7, "question": "وما هي عيوبها؟", "previous": "كيف تعمل k-means؟", "gold": []}
    rows = eq.run_retrieval([item], 3, 10, rewrite=True, queries={"a19": "ما هي عيوب k-means؟"})
    assert seen == ["ما هي عيوب k-means؟"] and rows[0]["query"] == "ما هي عيوب k-means؟"


def test_without_a_supplied_query_a_follow_up_is_still_rewritten(monkeypatch):
    class Trace:
        candidates, reranked, final, strategy, top_score = [], [], [], "reranked", 0.5

    monkeypatch.setattr(eq.retrieval, "retrieve_detailed", lambda q, s, c, k: Trace())
    monkeypatch.setattr(eq, "retrieval_query", lambda prev, q: f"REWRITTEN({prev}|{q})")
    item = {"id": "x", "lang": "en", "kind": "follow_up", "subject_id": 7, "question": "why?", "previous": "what is X?", "gold": []}
    assert eq.run_retrieval([item], 3, 10)[0]["query"] == "REWRITTEN(what is X?|why?)"
    assert eq.run_retrieval([item], 3, 10, rewrite=False)[0]["query"] == "why?"


def test_final_chars_counts_the_context_the_prompt_gets():
    class T:
        candidates = []
        reranked = []
        final = [chunk(text="x" * 100), chunk(text="y" * 50)]
        strategy, top_score = "reranked", 0.5

    row = eq.retrieval_row({"id": "i", "lang": "en", "kind": "narrow", "gold": []}, T(), "q")
    assert row["final_chars"] == 150
    assert eq.summarize_retrieval([{**row, "rank_candidates": None, "hit_cand3": False, "hit_cand5": False, "hit_cand10": False}])["en"]["mean_final_chars"] == 150


def test_chroma_path_points_the_search_at_another_store(tmp_path, monkeypatch):
    from app.services import vector_store

    cleared = []
    monkeypatch.setattr(vector_store, "reset_cache", lambda **kw: cleared.append(1))
    monkeypatch.setattr(vector_store, "CHROMA_PATH", Path("/the/real/store"))
    args = SimpleNamespace(min_confidence=None, chroma_path=str(tmp_path), top_k=3, candidate_k=10, no_rewrite=False, reuse_queries=None)
    meta = eq.apply_common(args)
    assert vector_store.CHROMA_PATH == tmp_path.resolve() and cleared and meta["chroma_path"] == str(tmp_path)


def test_two_variants_are_paired_on_the_same_questions(tmp_path):
    sheet, key = eq.make_review([fake_chat(tmp_path, "A"), fake_chat(tmp_path, "B")], [], 10, 0, seed=5)
    assert len(sheet) == 10
    by_q = defaultdict_list()
    for rid, k in key.items():
        by_q[k["qa_id"]].append(k["variant"])
    assert len(by_q) == 5 and all(sorted(v) == ["A", "B"] for v in by_q.values()), "each question once per variant"
    assert "variant" not in json.dumps(sheet)


def defaultdict_list():
    from collections import defaultdict

    return defaultdict(list)


def test_only_questions_every_variant_answered_are_paired(tmp_path):
    a = fake_chat(tmp_path, "A", n=6)
    b = fake_chat(tmp_path, "B", n=2)
    _, key = eq.make_review([a, b], [], 10, 0, seed=1)
    assert {k["qa_id"] for k in key.values()} <= {"q0", "q1"}


def test_paired_choice_uses_one_seed_per_question():
    chat = [
        {"variant": v, "row": {"id": "q1", "lang": "en", "seed": s, "question": "?", "answer": "", "sources": []}}
        for v in ("A", "B") for s in (44, 42, 43)
    ]
    picked = eq.pick_paired(chat, ["A", "B"], 2, random.Random(0))
    assert sorted((e["variant"], e["row"]["seed"]) for e in picked) == [("A", 42), ("B", 42)]


def test_the_committed_review_sheet_is_blind_balanced_and_ungraded():
    sheet_path, key_path = ROOT / "eval" / "review_sheet.csv", ROOT / "eval" / "review_key.json"
    if not sheet_path.exists():
        pytest.skip("review sheet not generated")
    with open(sheet_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    key = json.loads(key_path.read_text(encoding="utf-8"))
    assert len(rows) == 60 and {r["id"] for r in rows} == set(key)
    kinds = Counter_((r["type"], r["language"]) for r in rows)
    assert kinds[("quiz", "ar")] == kinds[("quiz", "en")] == 15
    assert abs(kinds[("chat", "ar")] - kinds[("chat", "en")]) <= 4
    text = sheet_path.read_text(encoding="utf-8-sig") + (ROOT / "eval" / "review_sheet.html").read_text(encoding="utf-8")
    for leak in ("baseline", "bge_k3_c10", "flashrank", "bge-m3"):
        assert leak not in text, f"the sheet reveals '{leak}'"
    assert all(not r["correct_0_1_2"] and not r["notes"] for r in rows), "nothing is pre-graded"
    chat_variants = Counter_(v["variant"] for v in key.values() if v["type"] == "chat")
    assert chat_variants == {"baseline": 15, "bge_k3_c10": 15}


def test_variant_labels_never_appear_in_any_sheet_field(tmp_path):
    sheet, _ = eq.make_review([fake_chat(tmp_path, "SECRETA"), fake_chat(tmp_path, "SECRETB")], [fake_quiz(tmp_path, "SECRETC")], 8, 4, seed=2)
    assert "SECRET" not in json.dumps(sheet, ensure_ascii=False)
    eq.write_sheet(sheet, {}, tmp_path / "s.csv", tmp_path / "s.html", tmp_path / "k.json")
    assert "SECRET" not in (tmp_path / "s.csv").read_text(encoding="utf-8-sig") + (tmp_path / "s.html").read_text(encoding="utf-8")
