"""Measure answer and quiz quality, per language, against eval/qa_set.json.

    python scripts/eval_quality.py retrieval                      # fast, no chat model needed
    python scripts/eval_quality.py chat --seeds 42 43 44          # answers + automatic checks
    python scripts/eval_quality.py quiz --quizzes 4               # quiz validity, duplicates, support
    python scripts/eval_quality.py compare A.json B.json          # two result files side by side
    python scripts/eval_quality.py review-sheet --chat A.json B.json --quiz A.json B.json
    python scripts/eval_quality.py grades --graded eval/review_graded.csv

Everything runs through the same functions the API uses. Nothing here changes Ollama's
configuration: `--model` only names a model to ask for, and Ollama must already have it
(`ollama pull ...`). Asking for a model other than the loaded one makes Ollama swap them.

The automatic checks are SIGNALS, not judgements. In particular nothing here can tell you
whether an Arabic answer reads well or is correct; that is what the review sheet is for.
"""

import argparse
import csv
import html
import json
import random
import re
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.controllers import chat_controller as cc  # noqa: E402
from app.services import quiz_service as qs  # noqa: E402
from app.services import retrieval  # noqa: E402
from app.services.llm import get_client, keep_alive  # noqa: E402
from app.services.query_rewrite import retrieval_query  # noqa: E402

QA_PATH = ROOT / "eval" / "qa_set.json"
RESULTS = ROOT / "eval" / "results"

CITATION_RE = re.compile(r"\[([^\[\]]+?),\s*page\s*(\d+)\]", re.IGNORECASE)
LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-']*")
WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
RETRIEVAL_KS = (3, 5, 10, 20)


# ------------------------------------------------------------------------- data


def load_items(path: Path = QA_PATH, only: list[str] | None = None) -> list[dict]:
    items = json.loads(Path(path).read_text(encoding="utf-8"))["items"]
    return [i for i in items if not only or i["id"] in only]


def gold_pages(item: dict) -> set[tuple[str, int]]:
    return {(g["file"], g["page"]) for g in item.get("gold", [])}


def corpus_pages() -> set[tuple[str, int]]:
    """Every (file, page) that has at least one chunk -- 'does the cited page exist'."""
    from app.services import vector_store

    metas = vector_store.get_collection().get(include=["metadatas"])["metadatas"]
    return {(m["source_filename"], int(m["page_number"])) for m in metas}


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    except OSError:
        return "unknown"


def mean_sd(values: list[float]) -> tuple[float, float]:
    values = [v for v in values if v is not None]
    if not values:
        return float("nan"), 0.0
    return statistics.mean(values), (statistics.stdev(values) if len(values) > 1 else 0.0)


def fmt_ms(values: list[float], pct: bool = True) -> str:
    m, sd = mean_sd(values)
    if m != m:
        return "n/a"
    scale = 100 if pct else 1
    return f"{m * scale:5.1f}" + (f" ±{sd * scale:.1f}" if len(values) > 1 else "")


# -------------------------------------------------------------------- retrieval


def hit_at(chunks, gold: set, k: int) -> bool:
    return any((c.source_filename, c.page_number) in gold for c in chunks[:k])


def first_gold_rank(chunks, gold: set) -> int | None:
    for i, c in enumerate(chunks, start=1):
        if (c.source_filename, c.page_number) in gold:
            return i
    return None


def retrieval_row(item: dict, trace, query: str) -> dict:
    gold = gold_pages(item)
    row = {
        "id": item["id"], "lang": item["lang"], "kind": item["kind"], "query": query,
        "strategy": trace.strategy, "top_score": round(trace.top_score, 5),
        "final_pages": [[c.source_filename, c.page_number] for c in trace.final],
        "hit_final": hit_at(trace.final, gold, len(trace.final)),
        "hit_reranked3": hit_at(trace.reranked, gold, 3),
        "rank_candidates": first_gold_rank(trace.candidates, gold),
        "rank_reranked": first_gold_rank(trace.reranked, gold),
    }
    for k in RETRIEVAL_KS:
        if k <= max(len(trace.candidates), 10):
            row[f"hit_cand{k}"] = hit_at(trace.candidates, gold, k)
    return row


def run_retrieval(items, top_k: int, candidate_k: int, rewrite: bool = True) -> list[dict]:
    rows = []
    for item in items:
        if item["kind"] == "out_of_scope":
            continue
        query = item["question"]
        if rewrite and item.get("previous"):
            query = retrieval_query(item["previous"], item["question"])
        trace = retrieval.retrieve_detailed(query, item["subject_id"], candidate_k, top_k)
        rows.append(retrieval_row(item, trace, query))
    return rows


def summarize_retrieval(rows: list[dict]) -> dict:
    out = {}
    for lang in sorted({r["lang"] for r in rows}) + ["all"]:
        sel = [r for r in rows if lang == "all" or r["lang"] == lang]
        if not sel:
            continue
        cell = {"n": len(sel)}
        for key in ("hit_cand3", "hit_cand5", "hit_cand10", "hit_cand20", "hit_reranked3", "hit_final"):
            vals = [r[key] for r in sel if key in r]
            if vals:
                cell[key] = sum(vals) / len(vals)
        ranks = [r["rank_candidates"] for r in sel]
        cell["mrr_candidates"] = sum(1 / x for x in ranks if x) / len(ranks)
        out[lang] = cell
    return out


def print_retrieval(summary: dict, title: str = "") -> None:
    print(f"\n{title}\n{'':<6}{'n':>3}{'cand@3':>9}{'cand@5':>9}{'cand@10':>9}{'cand@20':>9}{'rerank@3':>10}{'FINAL':>8}{'MRR(cand)':>11}")
    for lang, c in summary.items():
        p = lambda k: f"{100 * c[k]:5.0f}%" if k in c else "     -"
        print(f"{lang:<6}{c['n']:>3}{p('hit_cand3'):>9}{p('hit_cand5'):>9}{p('hit_cand10'):>9}{p('hit_cand20'):>9}{p('hit_reranked3'):>10}{p('hit_final'):>8}{c['mrr_candidates']:>11.2f}")


# -------------------------------------------------------------------------- chat


def strip_citations(text: str) -> str:
    return CITATION_RE.sub("", text)


def answer_language(text: str) -> str:
    return qs.detect_language(strip_citations(text))


def context_vocabulary(chunks, question: str) -> set[str]:
    """Lower-cased words in the retrieved context and the question: legitimate technical terms."""
    vocab = set()
    for text in [question, *(c.text for c in chunks)]:
        vocab.update(w.lower() for w in LATIN_WORD_RE.findall(text))
    return vocab


def script_purity(answer: str, vocab: set[str]) -> dict:
    """How much Latin script an Arabic answer contains, and how much of it is NOT a term from the context."""
    text = strip_citations(answer)
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return {"latin_share": 0.0, "unexplained_share": 0.0, "unexplained_words": []}
    latin = sum(1 for ch in letters if "a" <= ch.lower() <= "z")
    unexplained = [w for w in LATIN_WORD_RE.findall(text) if len(w) >= 3 and w.lower() not in vocab]
    return {
        "latin_share": latin / len(letters),
        "unexplained_share": sum(len(w) for w in unexplained) / len(letters),
        "unexplained_words": unexplained,
    }


def citation_check(answer: str, final_pages: set, corpus: set) -> dict:
    cited = [(f.strip(), int(p)) for f, p in CITATION_RE.findall(answer)]
    return {
        "n_cited": len(cited),
        "n_exist": sum(1 for c in cited if c in corpus),
        "n_retrieved": sum(1 for c in cited if c in final_pages),
    }


def chat_metrics(item: dict, parsed, final_chunks, corpus: set) -> dict:
    final_pages = {(c.source_filename, c.page_number) for c in final_chunks}
    expect_refusal = item["kind"] == "out_of_scope"
    m = {
        "refused": parsed.refused,
        "refusal_correct": parsed.refused == expect_refusal,
        "language_ok": answer_language(parsed.text) == item["lang"],
        "hit_final": hit_at(final_chunks, gold_pages(item), len(final_chunks)) if not expect_refusal else None,
    }
    m.update(citation_check(parsed.text, final_pages, corpus))
    if item["lang"] == "ar":
        m.update(script_purity(parsed.text, context_vocabulary(final_chunks, item["question"])))
    return m


class _Session:
    def __init__(self, messages):
        self.messages = [SimpleNamespace(role=r, content=c) for r, c in messages]


def ask_chat(question: str, subject_id: int, history: list[tuple[str, str]], previous: str | None,
             model: str, seed: int, top_k: int, candidate_k: int, rewrite: bool = True):
    """One chat turn exactly as the API runs it. Returns (ParsedReply, final chunks, trace, query)."""
    query = retrieval_query(previous, question) if rewrite else question
    trace = retrieval.retrieve_detailed(query, subject_id, candidate_k, top_k)
    session = _Session([*history, ("user", question)])
    messages = cc._build_messages(session, question, trace.final)
    response = get_client().chat(
        model=model, messages=messages, options={**cc.CHAT_OPTIONS, "seed": seed}, keep_alive=keep_alive()
    )
    parsed = cc.finalize_answer(response["message"]["content"], question)
    return parsed, trace.final, trace, query


def run_chat(items, seeds, model, top_k, candidate_k, rewrite=True, progress=True) -> list[dict]:
    corpus = corpus_pages()
    rows = []
    for item in items:
        for seed in seeds:
            history: list[tuple[str, str]] = []
            if item.get("previous"):
                # Answer the earlier question first, with the same model and seed, so the
                # follow-up sees a realistic conversation instead of a made-up one.
                prev_parsed, *_ = ask_chat(item["previous"], item["subject_id"], [], None, model, seed, top_k, candidate_k, rewrite)
                history = [("user", item["previous"]), ("assistant", prev_parsed.text)]
            parsed, final, trace, query = ask_chat(
                item["question"], item["subject_id"], history, item.get("previous"), model, seed, top_k, candidate_k, rewrite
            )
            row = {
                "id": item["id"], "lang": item["lang"], "kind": item["kind"], "seed": seed,
                "question": item["question"], "previous": item.get("previous"), "query": query,
                "answer": parsed.text, "sources": [
                    {"file": c.source_filename, "page": c.page_number, "type": c.content_type, "snippet": re.sub(r"\s+", " ", c.text)[:260]}
                    for c in (final if not parsed.refused else final)
                ],
                "metrics": chat_metrics(item, parsed, final, corpus),
            }
            rows.append(row)
        if progress:
            print(f"  {item['id']} done", flush=True)
    return rows


def _rate(rows, key, where=lambda r: True, pred=lambda v: bool(v)):
    sel = [r["metrics"][key] for r in rows if where(r) and r["metrics"].get(key) is not None]
    return (sum(1 for v in sel if pred(v)) / len(sel)) if sel else None


def per_seed(rows, fn) -> list[float]:
    """fn(rows_of_one_seed) for every seed -> list of values (for mean ± sd across seeds)."""
    by_seed = defaultdict(list)
    for r in rows:
        by_seed[r["seed"]].append(r)
    return [v for v in (fn(rs) for _, rs in sorted(by_seed.items())) if v is not None]


def summarize_chat(rows: list[dict]) -> dict:
    out = {}
    for lang in sorted({r["lang"] for r in rows}):
        sel = [r for r in rows if r["lang"] == lang]
        inscope = lambda r: r["kind"] != "out_of_scope"
        oos = lambda r: r["kind"] == "out_of_scope"

        def share_retrieved(rs):
            cited = sum(r["metrics"]["n_cited"] for r in rs if inscope(r))
            return sum(r["metrics"]["n_retrieved"] for r in rs if inscope(r)) / cited if cited else None

        def share_exist(rs):
            cited = sum(r["metrics"]["n_cited"] for r in rs if inscope(r))
            return sum(r["metrics"]["n_exist"] for r in rs if inscope(r)) / cited if cited else None

        cell = {
            "n_answers": len(sel),
            "retrieval_hit": per_seed(sel, lambda rs: _rate(rs, "hit_final", inscope)),
            "language_ok": per_seed(sel, lambda rs: _rate(rs, "language_ok")),
            "oos_refused": per_seed(sel, lambda rs: _rate(rs, "refused", oos)),
            "false_refusal": per_seed(sel, lambda rs: _rate(rs, "refused", inscope)),
            "has_citation": per_seed(sel, lambda rs: _rate(rs, "n_cited", lambda r: inscope(r) and not r["metrics"]["refused"], lambda v: v > 0)),
            "citations_retrieved": per_seed(sel, share_retrieved),
            "citations_exist": per_seed(sel, share_exist),
        }
        if lang == "ar":
            cell["latin_share"] = per_seed(sel, lambda rs: statistics.mean(r["metrics"]["latin_share"] for r in rs))
            cell["unexplained_share"] = per_seed(sel, lambda rs: statistics.mean(r["metrics"]["unexplained_share"] for r in rs))
            cell["leaky_answers"] = per_seed(sel, lambda rs: _rate(rs, "unexplained_words", None or (lambda r: True), lambda v: len(v) > 0))
        out[lang] = cell
    return out


CHAT_LABELS = [
    ("retrieval_hit", "gold page in the prompt's chunks (in-scope)"),
    ("language_ok", "answer language == question language"),
    ("oos_refused", "out-of-scope questions refused"),
    ("false_refusal", "in-scope questions wrongly refused"),
    ("has_citation", "non-refused answers carrying a citation"),
    ("citations_retrieved", "citations that name a retrieved page"),
    ("citations_exist", "citations that name a page that exists"),
    ("latin_share", "Arabic: Latin letters / all letters"),
    ("unexplained_share", "Arabic: Latin letters NOT a lecture term"),
    ("leaky_answers", "Arabic: answers with a non-lecture Latin word"),
]


def print_chat(summary: dict, title: str = "") -> None:
    print(f"\n{title}")
    for lang, cell in summary.items():
        print(f"\n  [{lang.upper()}]  {cell['n_answers']} answers  (mean ± sd over seeds, %)")
        for key, label in CHAT_LABELS:
            if key in cell:
                print(f"    {label:<48}{fmt_ms(cell[key])}")


# -------------------------------------------------------------------------- quiz


def tokens(text: str) -> set[str]:
    return {w.lower() for w in WORD_RE.findall(text) if len(w) > 1}


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / len(ta | tb) if ta | tb else 0.0


def duplicate_questions(questions: list[str], threshold: float = 0.6) -> int:
    """How many questions repeat an EARLIER question in the same quiz (word overlap >= threshold)."""
    return sum(1 for i, q in enumerate(questions) if any(jaccard(q, p) >= threshold for p in questions[:i]))


def lexical_support(answer: str, chunk_text: str) -> float | None:
    """Share of the answer's words (4+ letters) found in the source chunk. Same language only."""
    words = [w for w in tokens(answer) if len(w) >= 4]
    if not words:
        return None
    low = chunk_text.lower()
    return sum(1 for w in words if w in low) / len(words)


def support_by_embedding(chunk_text: str, options: list[str], correct: int) -> dict:
    """Does the correct option sit closer to the source chunk than the distractors? (cross-lingual, approximate)"""
    import numpy as np

    from app.services import vector_store

    vectors = np.array(vector_store.get_embedding_function()([chunk_text[:1500], *options]), dtype=float)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    sims = vectors[1:] @ vectors[0]
    others = [s for i, s in enumerate(sims) if i != correct]
    return {"correct_is_closest": bool(int(np.argmax(sims)) == correct), "margin": float(sims[correct] - np.mean(others))}


def run_quiz(language: str, n_quizzes: int, model: str, seed_base: int, subjects=(7, 6), progress=True) -> list[dict]:
    rows = []
    for i in range(n_quizzes):
        subject, seed = subjects[i % len(subjects)], seed_base + i
        trace = qs.QuizTrace()
        try:
            questions = qs.generate_quiz(subject, 5, language, seed=seed, model=model, trace=trace)
        except Exception as exc:  # a failed quiz is a data point, not a crash
            questions = []
            print(f"  ! {language} quiz {i}: {type(exc).__name__}", flush=True)
        calls = [a for c in trace.chunks for a in c.attempts]
        items = []
        for q in questions:
            items.append({
                "question": q.question_text, "options": q.options, "correct": q.correct_answer_index,
                "citation": q.source.citation, "chunk": re.sub(r"\s+", " ", q.source.text)[:700],
                "strict_ok": qs.valid_item(q.question_text, q.options, q.options[q.correct_answer_index], language == "ar"),
                "lexical_support": lexical_support(q.options[q.correct_answer_index], q.source.text) if language == "en" else None,
                **{f"emb_{k}": v for k, v in support_by_embedding(q.source.text, q.options, q.correct_answer_index).items()},
            })
        rows.append({
            "lang": language, "quiz": i, "subject_id": subject, "seed": seed, "model": model,
            "requested": 5, "produced": len(items), "calls": len(calls),
            "accepted_calls": sum(1 for a in calls if a.outcome == qs.OUTCOME_ACCEPTED),
            "first_attempt_ok": sum(1 for c in trace.chunks if c.attempts and c.attempts[0].outcome == qs.OUTCOME_ACCEPTED),
            "chunks": len(trace.chunks), "rejections": dict(Counter(a.outcome for a in calls if a.outcome != qs.OUTCOME_ACCEPTED)),
            "duplicate_questions": duplicate_questions([x["question"] for x in items]),
            "total_s": round(trace.total_s, 2), "items": items,
        })
        if progress:
            print(f"  {language} quiz {i}: {len(items)}/5, {len(calls)} calls", flush=True)
    return rows


def summarize_quiz(rows: list[dict]) -> dict:
    out = {}
    for lang in sorted({r["lang"] for r in rows}):
        sel = [r for r in rows if r["lang"] == lang]
        items = [x for r in sel for x in r["items"]]
        calls, produced = sum(r["calls"] for r in sel), sum(r["produced"] for r in sel)
        emb = [x for x in items if "emb_correct_is_closest" in x]
        lex = [x["lexical_support"] for x in items if x.get("lexical_support") is not None]
        out[lang] = {
            "quizzes": len(sel), "questions": produced, "requested": sum(r["requested"] for r in sel),
            "reply_validity": sum(r["accepted_calls"] for r in sel) / calls if calls else None,
            "first_attempt_pass": sum(r["first_attempt_ok"] for r in sel) / max(sum(r["chunks"] for r in sel), 1),
            "calls_per_question": calls / produced if produced else None,
            "strict_valid": sum(x["strict_ok"] for x in items) / len(items) if items else None,
            "duplicate_rate": sum(r["duplicate_questions"] for r in sel) / produced if produced else None,
            "quizzes_with_duplicate": sum(1 for r in sel if r["duplicate_questions"]) / len(sel),
            "answer_closest_to_source": sum(x["emb_correct_is_closest"] for x in emb) / len(emb) if emb else None,
            "mean_margin": statistics.mean(x["emb_margin"] for x in emb) if emb else None,
            "lexical_support": statistics.mean(lex) if lex else None,
            "mean_seconds": statistics.mean(r["total_s"] for r in sel),
            "rejections": dict(sum((Counter(r["rejections"]) for r in sel), Counter())),
        }
    return out


def print_quiz(summary: dict, title: str = "") -> None:
    print(f"\n{title}")
    for lang, c in summary.items():
        p = lambda v: "   n/a" if v is None else f"{100 * v:5.1f}%"
        print(f"\n  [{lang.upper()}]  {c['quizzes']} quizzes, {c['questions']}/{c['requested']} questions")
        print(f"    replies accepted (validity)                     {p(c['reply_validity'])}")
        print(f"    chunks that passed on the first attempt         {p(c['first_attempt_pass'])}")
        print(f"    model calls per question                        {c['calls_per_question']:.2f}")
        print(f"    shipped questions re-checked against strict     {p(c['strict_valid'])}   (relaxed Arabic items fail this by design)")
        print(f"    questions repeating one earlier in their quiz   {p(c['duplicate_rate'])}   ({100 * c['quizzes_with_duplicate']:.0f}% of quizzes have one)")
        print(f"    correct option closest to the source chunk      {p(c['answer_closest_to_source'])}   (chance = 25%; approximate, embedding-based)")
        if c["lexical_support"] is not None:
            print(f"    correct answer's words found in the chunk       {p(c['lexical_support'])}   (English only; approximate)")
        print(f"    rejection reasons: {c['rejections']}")


# ------------------------------------------------------------------- review sheet


def build_pool(chat_files, quiz_files):
    """All gradable items from the given result files, each tagged with the variant it came from."""
    chat, quiz = [], []
    for path in chat_files or []:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        label = data["meta"].get("label") or Path(path).stem
        for r in data["rows"]:
            chat.append({"variant": label, "file": str(path), "row": r})
    for path in quiz_files or []:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        label = data["meta"].get("label") or Path(path).stem
        for quiz_row in data["rows"]:
            for n, it in enumerate(quiz_row["items"]):
                quiz.append({"variant": label, "file": str(path), "row": quiz_row, "item": it, "n": n})
    return chat, quiz


def pick_balanced(pool: list[dict], n: int, rng: random.Random, lang_of, variant_of) -> list[dict]:
    """n items, as even as possible across (language, variant), never the same chat question twice per variant."""
    groups = defaultdict(list)
    for entry in pool:
        groups[(lang_of(entry), variant_of(entry))].append(entry)
    for g in groups.values():
        rng.shuffle(g)
    keys = sorted(groups)
    picked, i = [], 0
    while len(picked) < n and any(groups[k] for k in keys):
        k = keys[i % len(keys)]
        if groups[k]:
            picked.append(groups[k].pop())
        i += 1
    return picked


def make_review(chat_files, quiz_files, n_chat: int, n_quiz: int, seed: int):
    rng = random.Random(seed)
    chat, quiz = build_pool(chat_files, quiz_files)
    chosen = [
        ("chat", e) for e in pick_balanced(chat, n_chat, rng, lambda e: e["row"]["lang"], lambda e: e["variant"])
    ] + [
        ("quiz", e) for e in pick_balanced(quiz, n_quiz, rng, lambda e: e["row"]["lang"], lambda e: e["variant"])
    ]
    rng.shuffle(chosen)
    sheet, key = [], {}
    for n, (kind, e) in enumerate(chosen, start=1):
        rid = f"R{n:03d}"
        if kind == "chat":
            r = e["row"]
            sources = " | ".join(f"{s['file']} p{s['page']}: {s['snippet'][:160]}" for s in r["sources"])
            sheet.append({"id": rid, "type": "chat", "language": r["lang"], "question": r["question"],
                          "sources": sources, "answer": r["answer"], "options": "", "marked_correct": ""})
            key[rid] = {"variant": e["variant"], "type": "chat", "lang": r["lang"], "qa_id": r["id"], "seed": r["seed"], "file": e["file"]}
        else:
            it, q = e["item"], e["row"]
            letters = "ABCD"
            options = "\n".join(f"{letters[i]}. {o}" for i, o in enumerate(it["options"]))
            sheet.append({"id": rid, "type": "quiz", "language": q["lang"], "question": it["question"],
                          "sources": f"{it['citation']}: {it['chunk'][:300]}", "answer": "", "options": options,
                          "marked_correct": letters[it["correct"]]})
            key[rid] = {"variant": e["variant"], "type": "quiz", "lang": q["lang"], "quiz": q["quiz"], "seed": q["seed"], "n": e["n"], "file": e["file"]}
    return sheet, key


SHEET_COLUMNS = ["id", "type", "language", "question", "sources", "answer", "options", "marked_correct",
                 "correct_0_1_2", "grounded_y_n", "arabic_quality_1_5", "notes"]


def write_sheet(sheet: list[dict], key: dict, out_csv: Path, out_html: Path, out_key: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:  # BOM so Excel reads Arabic correctly
        w = csv.DictWriter(f, fieldnames=SHEET_COLUMNS)
        w.writeheader()
        for row in sheet:
            w.writerow({**row, "correct_0_1_2": "", "grounded_y_n": "", "arabic_quality_1_5": "", "notes": ""})
    out_key.write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    cards = []
    for row in sheet:
        body = f"<div class='q' dir='auto'>{html.escape(row['question'])}</div>"
        if row["type"] == "chat":
            body += f"<div class='lbl'>Model answer</div><div class='a' dir='auto'>{html.escape(row['answer'])}</div>"
        else:
            opts = "".join(f"<li dir='auto'>{html.escape(o[3:])}</li>" for o in row["options"].split("\n"))
            body += (f"<div class='lbl'>Options (the model marked <b>{row['marked_correct']}</b> as correct)</div>"
                     f"<ol type='A' class='opts'>{opts}</ol>")
        cards.append(
            f"<section><h3>{row['id']} <small>{row['type']} · {row['language']}</small></h3>{body}"
            f"<div class='lbl'>Sources shown to the model</div><div class='src' dir='auto'>{html.escape(row['sources'])}</div>"
            "<table class='grade'><tr><th>Correct (0/1/2)</th><th>Grounded in the sources (y/n)</th>"
            "<th>Arabic language quality (1-5)</th><th>Notes</th></tr><tr><td></td><td></td><td></td><td></td></tr></table></section>"
        )
    out_html.write_text(
        "<!doctype html><meta charset='utf-8'><title>Review sheet</title><style>"
        "body{font:15px/1.5 system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem}"
        "section{border:1px solid #cbd5e1;border-radius:8px;padding:1rem;margin:1.2rem 0;break-inside:avoid}"
        "h3{margin:.2rem 0}small{color:#64748b;font-weight:400}.q{font-weight:600;margin:.4rem 0}"
        ".lbl{color:#64748b;font-size:12px;text-transform:uppercase;margin-top:.7rem}.a,.src{white-space:pre-wrap}"
        ".src{font-size:13px;color:#334155}table.grade{width:100%;border-collapse:collapse;margin-top:.8rem}"
        ".grade th,.grade td{border:1px solid #cbd5e1;padding:.4rem;text-align:left;font-size:12px}.grade td{height:2.2rem}"
        "</style><h1>Review sheet</h1><p>Grade each item in the CSV (columns on the right). This page is for reading.</p>"
        + "".join(cards), encoding="utf-8")


def read_graded(path: Path, key: dict) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            k = key.get(r["id"])
            if not k:
                continue
            def num(v):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
            grounded = (r.get("grounded_y_n") or "").strip().lower()
            rows.append({**k, "id": r["id"], "correct": num(r.get("correct_0_1_2")),
                         "grounded": {"y": 1.0, "n": 0.0}.get(grounded[:1]) if grounded else None,
                         "arabic_quality": num(r.get("arabic_quality_1_5")), "notes": r.get("notes", "")})
    return rows


def summarize_grades(rows: list[dict]) -> dict:
    out = {}
    groups = defaultdict(list)
    for r in rows:
        groups[(r["variant"], r["lang"], r["type"])].append(r)
    for (variant, lang, kind), rs in sorted(groups.items()):
        graded = [r for r in rs if r["correct"] is not None]
        out[f"{variant} | {lang} | {kind}"] = {
            "n": len(rs), "graded": len(graded),
            "mean_correct_0_2": statistics.mean(r["correct"] for r in graded) if graded else None,
            "pct_fully_correct": sum(1 for r in graded if r["correct"] == 2) / len(graded) if graded else None,
            "pct_grounded": (lambda g: sum(g) / len(g) if g else None)([r["grounded"] for r in rs if r["grounded"] is not None]),
            "mean_arabic_quality": (lambda g: statistics.mean(g) if g else None)([r["arabic_quality"] for r in rs if r["arabic_quality"] is not None]),
        }
    return out


# ----------------------------------------------------------------------- compare


def compare(a_path: str, b_path: str) -> None:
    a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (a_path, b_path))
    print(f"\nA = {a['meta'].get('label')}   B = {b['meta'].get('label')}   (mode {a['meta']['mode']})")
    if a["meta"]["mode"] != b["meta"]["mode"]:
        raise SystemExit("different kinds of result files")
    mode = a["meta"]["mode"]
    for lang in sorted(set(a["summary"]) & set(b["summary"])):
        print(f"\n--- {lang.upper()}")
        sa, sb = a["summary"][lang], b["summary"][lang]
        for key in sa:
            va, vb = sa[key], sb.get(key)
            if isinstance(va, list):
                print(f"  {key:<26}{fmt_ms(va):>14}{fmt_ms(vb or []):>14}")
            elif isinstance(va, (int, float)) and not isinstance(va, bool) and isinstance(vb, (int, float)):
                print(f"  {key:<26}{va:>14.3f}{vb:>14.3f}")


# -------------------------------------------------------------------------- main


def write_results(mode: str, label: str, meta: dict, rows, summary) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"quality_{mode}_{label}.json"
    meta = {"mode": mode, "label": label, "commit": git_commit(), "date": time.strftime("%Y-%m-%d %H:%M"), **meta}
    path.write_text(json.dumps({"meta": meta, "summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def add_common(p) -> None:
    p.add_argument("--set", default=str(QA_PATH), help="question set (default eval/qa_set.json)")
    p.add_argument("--only", nargs="+", metavar="ID")
    p.add_argument("--top-k", type=int, default=settings.top_k)
    p.add_argument("--candidate-k", type=int, default=settings.candidate_k)
    p.add_argument("--min-confidence", type=float, default=None, help="override RERANK_MIN_CONFIDENCE")
    p.add_argument("--no-rewrite", action="store_true", help="do not rewrite follow-ups before searching")
    p.add_argument("--label", default="baseline")


def apply_common(args) -> dict:
    if args.min_confidence is not None:
        retrieval.RERANK_MIN_CONFIDENCE = args.min_confidence
    return {"top_k": args.top_k, "candidate_k": args.candidate_k, "min_confidence": retrieval.RERANK_MIN_CONFIDENCE,
            "rewrite": not args.no_rewrite, "reranker": getattr(settings, "reranker", "flashrank")}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="mode", required=True)

    r = sub.add_parser("retrieval", help="gold-chunk recall at each stage")
    add_common(r)

    c = sub.add_parser("chat", help="answers and automatic checks")
    add_common(c)
    c.add_argument("--seeds", type=int, nargs="+", default=[42, 43, 44])
    c.add_argument("--model", default=settings.chat_model, help="Ollama model tag to ask for (must already be pulled)")

    q = sub.add_parser("quiz", help="quiz validity, duplicates and support")
    q.add_argument("--label", default="baseline")
    q.add_argument("--quizzes", type=int, default=4, help="quizzes of 5 questions per language")
    q.add_argument("--languages", nargs="+", default=["ar", "en"])
    q.add_argument("--model", default=settings.quiz_model)
    q.add_argument("--seed-base", type=int, default=20000)

    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("a")
    cmp_.add_argument("b")

    rs = sub.add_parser("review-sheet", help="blind sheet for a human reviewer")
    rs.add_argument("--chat", nargs="*", default=[])
    rs.add_argument("--quiz", nargs="*", default=[])
    rs.add_argument("--n-chat", type=int, default=30)
    rs.add_argument("--n-quiz", type=int, default=30)
    rs.add_argument("--seed", type=int, default=7)
    rs.add_argument("--out-dir", default=str(ROOT / "eval"))

    g = sub.add_parser("grades", help="summarise a graded review sheet")
    g.add_argument("--graded", required=True)
    g.add_argument("--key", default=str(ROOT / "eval" / "review_key.json"))

    args = p.parse_args()

    if args.mode == "compare":
        return compare(args.a, args.b)

    if args.mode == "retrieval":
        meta = apply_common(args)
        items = load_items(args.set, args.only)
        rows = run_retrieval(items, args.top_k, args.candidate_k, not args.no_rewrite)
        summary = summarize_retrieval(rows)
        print_retrieval(summary, f"retrieval [{args.label}]  top_k={args.top_k} candidates={args.candidate_k}")
        print("\nsaved", write_results("retrieval", args.label, meta, rows, summary))

    elif args.mode == "chat":
        meta = apply_common(args)
        settings.chat_model = args.model  # the follow-up rewrite uses the same model
        cc.CHAT_MODEL = args.model
        meta.update(model=args.model, seeds=args.seeds)
        rows = run_chat(load_items(args.set, args.only), args.seeds, args.model, args.top_k, args.candidate_k, not args.no_rewrite)
        summary = summarize_chat(rows)
        print_chat(summary, f"chat [{args.label}]  model={args.model}  seeds={args.seeds}")
        print("\nsaved", write_results("chat", args.label, meta, rows, summary))

    elif args.mode == "quiz":
        rows = []
        for lang in args.languages:
            rows += run_quiz(lang, args.quizzes, args.model, args.seed_base)
        summary = summarize_quiz(rows)
        print_quiz(summary, f"quiz [{args.label}]  model={args.model}")
        print("\nsaved", write_results("quiz", args.label, {"model": args.model}, rows, summary))

    elif args.mode == "review-sheet":
        sheet, key = make_review(args.chat, args.quiz, args.n_chat, args.n_quiz, args.seed)
        out = Path(args.out_dir)
        write_sheet(sheet, key, out / "review_sheet.csv", out / "review_sheet.html", out / "review_key.json")
        counts = Counter((r["type"], r["language"]) for r in sheet)
        print(f"wrote {len(sheet)} items to {out}: {dict(counts)}")

    elif args.mode == "grades":
        key = json.loads(Path(args.key).read_text(encoding="utf-8"))
        summary = summarize_grades(read_graded(Path(args.graded), key))
        print(f"{'variant | lang | type':<40}{'n':>4}{'graded':>8}{'mean correct':>14}{'% fully':>9}{'% grounded':>12}{'AR quality':>12}")
        pct = lambda v: "   -" if v is None else f"{100 * v:5.0f}%"
        num = lambda v: "   -" if v is None else f"{v:5.2f}"
        for name, c in summary.items():
            print(f"{name:<40}{c['n']:>4}{c['graded']:>8}{num(c['mean_correct_0_2']):>14}{pct(c['pct_fully_correct']):>9}{pct(c['pct_grounded']):>12}{num(c['mean_arabic_quality']):>12}")


if __name__ == "__main__":
    main()
