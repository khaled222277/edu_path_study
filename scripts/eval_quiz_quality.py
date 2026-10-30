"""How many generated quiz questions can be answered without a slide, and which source chunks cause the bad ones?

    python scripts/eval_quiz_quality.py generate --chroma-copy DIR --quizzes 20 --label baseline --out result.json
    python scripts/eval_quiz_quality.py report result.json [--sample 12]
    python scripts/eval_quiz_quality.py compare before.json after.json

`generate` makes N quizzes per language (5 questions each) with the real quiz service against a COPY of the
Chroma store (it refuses a path inside the real data/ folder; no database is used) and records, for every
item, the question, options, the source chunk's features and the rule-based flags of scripts/quiz_checks.py,
plus how close the question + correct answer is to the nearest chunk of the subject (an off-topic PROXY: it
needs a threshold, so `report` shows the lowest-scoring items for a human to judge instead of deciding).
It also records per quiz: questions produced, model calls, accepted calls (validity) and seconds.

Nothing here changes Ollama's configuration; it only sends generate requests to the model already in use.
"""

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL_DATA = (ROOT / "data").resolve()
sys.path.insert(0, str(ROOT))

from scripts.quiz_checks import chunk_features, classify  # noqa: E402

SUBJECT_ID = 7


def use_copy(path: str) -> None:
    p = Path(path).resolve()
    if p == REAL_DATA or REAL_DATA in p.parents:
        raise SystemExit(f"refusing to use {p}: it is inside the real data/ folder")
    if not p.exists():
        raise SystemExit(f"{p} does not exist: copy data/chroma_db there first")
    from app.services import vector_store

    vector_store.CHROMA_PATH = p
    vector_store.reset_cache()


def topic_similarity(question: str, answer: str) -> float:
    """Cosine similarity to the nearest chunk of the subject (cross-lingual via bge-m3)."""
    from app.services import vector_store

    res = vector_store.get_collection().query(
        query_texts=[f"{question} {answer}"], n_results=1, where={"subject_id": SUBJECT_ID}, include=["distances"]
    )
    return round(1.0 - float(res["distances"][0][0]), 4)


def generate(args) -> list[dict]:
    from app.services import quiz_service as qs

    rows: list[dict] = []
    for lang in args.languages:
        for i in range(args.quizzes):
            seed = args.seed_base + i
            trace = qs.QuizTrace()
            t0 = time.perf_counter()
            error = None
            try:
                questions = qs.generate_quiz(SUBJECT_ID, args.count, lang, seed=seed, trace=trace)
            except qs.QuizGenerationError:
                questions, error = [], "no_valid_question"
            seconds = time.perf_counter() - t0
            calls = [a for c in trace.chunks for a in c.attempts]
            quiz = {
                "lang": lang, "quiz": i, "seed": seed, "requested": args.count, "produced": len(questions),
                "calls": len(calls), "accepted_calls": sum(1 for a in calls if a.outcome == qs.OUTCOME_ACCEPTED),
                "rejections": dict(Counter(a.outcome for a in calls if a.outcome != qs.OUTCOME_ACCEPTED)),
                "seconds": round(seconds, 1), "error": error,
            }
            items = []
            for q in questions:
                correct = q.options[q.correct_answer_index]
                items.append({
                    "question": q.question_text, "options": q.options, "correct": q.correct_answer_index,
                    "source": {"file": q.source.source_filename, "page": q.source.page_number, "text": q.source.text,
                               **chunk_features(q.source.text, q.source.content_type)},
                    "flags": classify(q.question_text, lang),
                    "topic_sim": topic_similarity(q.question_text, correct),
                })
            rows.append({**quiz, "items": items})
            print(f"{lang} quiz {i}: {len(items)}/{args.count} in {seconds:.0f}s, "
                  f"bad={sum(not it['flags']['self_contained'] for it in items)}", flush=True)
    return rows


def reflag(rows: list[dict]) -> list[dict]:
    """Score every item with the CURRENT rules in quiz_checks, so two result files are compared like for like."""
    for r in rows:
        for it in r["items"]:
            it["flags"] = classify(it["question"], r["lang"])
    return rows


def summarize(rows: list[dict]) -> dict:
    out = {}
    for lang in sorted({r["lang"] for r in rows}):
        qs_ = [r for r in rows if r["lang"] == lang]
        items = [it for r in qs_ for it in r["items"]]
        n = max(1, len(items))
        calls = sum(r["calls"] for r in qs_)
        sims = [it["topic_sim"] for it in items]
        out[lang] = {
            "quizzes": len(qs_), "items": len(items),
            "produced_per_quiz": round(statistics.mean(r["produced"] for r in qs_), 2),
            "validity": round(sum(r["accepted_calls"] for r in qs_) / calls, 3) if calls else None,
            "seconds_per_quiz": round(statistics.mean(r["seconds"] for r in qs_), 1),
            "self_contained": round(sum(it["flags"]["self_contained"] for it in items) / n, 3),
            "deictic": round(sum(it["flags"]["deictic"] for it in items) / n, 3),
            "fragment": round(sum(it["flags"]["fragment"] for it in items) / n, 3),
            "grammar_suspect": round(sum(it["flags"]["grammar"] for it in items) / n, 3),
            "topic_sim_median": round(statistics.median(sims), 3) if sims else None,
            "rejections": dict(sum((Counter(r["rejections"]) for r in qs_), Counter())),
        }
    return out


def by_source(rows: list[dict]) -> dict:
    """Bad-item rate by the kind of chunk the item was made from."""
    kinds = {
        "figure description": lambda s: s["figure"],
        "lecture text": lambda s: not s["figure"],
        "title/agenda-like": lambda s: s["title_like"],
        "short (<400 chars)": lambda s: s["short"],
        "low information (<25 words)": lambda s: s["low_info"],
        "alpha share < 0.6": lambda s: s["alpha_share"] < 0.6,
    }
    out = {}
    for lang in sorted({r["lang"] for r in rows}):
        items = [it for r in rows if r["lang"] == lang for it in r["items"]]
        for name, pick in kinds.items():
            sel = [it for it in items if pick(it["source"])]
            bad = sum(not it["flags"]["self_contained"] for it in sel)
            out[f"{lang}/{name}"] = {"items": len(sel), "bad": bad, "share_of_all_items": round(len(sel) / max(1, len(items)), 3),
                                     "bad_rate": round(bad / len(sel), 3) if sel else None}
    return out


def report(path: str, sample: int) -> None:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = reflag(data["rows"])
    print(f"\n== {data['meta']['label']} ({data['meta'].get('model')}) ==")
    for lang, c in summarize(rows).items():
        print(f"\n[{lang}] {c['quizzes']} quizzes, {c['items']} items")
        for k, v in c.items():
            if k not in ("quizzes", "items"):
                print(f"   {k:<20}{v}")
    print("\nBad-item rate by source chunk type (an item can be in several rows):")
    for k, v in by_source(rows).items():
        print(f"   {k:<40}items={v['items']:<4}bad={v['bad']:<4}share={v['share_of_all_items']:<7}bad_rate={v['bad_rate']}")
    print("\nSAMPLE FOR HUMAN REVIEW (not judged here). Arabic quality is yours to judge.")
    for lang in sorted({r["lang"] for r in rows}):
        items = [(r, it) for r in rows if r["lang"] == lang for it in r["items"]]
        flagged = [x for x in items if not x[1]["flags"]["self_contained"]][: sample // 2]
        lowest = sorted(items, key=lambda x: x[1]["topic_sim"])[: sample // 4]
        clean = [x for x in items if x[1]["flags"]["self_contained"]][: sample // 4]
        for title, group in (("flagged by rule", flagged), ("lowest topic similarity", lowest), ("passed all rules", clean)):
            print(f"\n--- {lang}: {title}")
            for r, it in group:
                s = it["source"]
                print(f"  Q: {it['question']}\n     options: {' | '.join(it['options'])}  (correct #{it['correct'] + 1})\n"
                      f"     flags={[k for k in ('deictic', 'fragment', 'grammar') if it['flags'][k]]} sim={it['topic_sim']} "
                      f"source={s['file']} p{s['page']} figure={s['figure']} len={s['length']} words={s['real_words']}")


def compare(a_path: str, b_path: str) -> None:
    a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (a_path, b_path))
    sa, sb = summarize(reflag(a["rows"])), summarize(reflag(b["rows"]))
    print(f"A = {a['meta']['label']}   B = {b['meta']['label']}")
    for lang in sa:
        if lang in sb:
            print(f"\n[{lang}]")
            for k in sa[lang]:
                if k != "rejections":
                    print(f"   {k:<20}{sa[lang][k]!s:>10} -> {sb[lang][k]!s:<10}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="mode", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--chroma-copy", required=True)
    g.add_argument("--quizzes", type=int, default=20)
    g.add_argument("--count", type=int, default=5)
    g.add_argument("--languages", nargs="+", default=["ar", "en"])
    g.add_argument("--seed-base", type=int, default=31000)
    g.add_argument("--label", default="baseline")
    g.add_argument("--out", required=True)
    r = sub.add_parser("report")
    r.add_argument("result")
    r.add_argument("--sample", type=int, default=12)
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    args = p.parse_args()

    if args.mode == "report":
        return report(args.result, args.sample)
    if args.mode == "compare":
        return compare(args.a, args.b)

    use_copy(args.chroma_copy)
    from app.config import settings

    rows = reflag(generate(args))
    meta = {"label": args.label, "model": settings.quiz_model, "quizzes": args.quizzes, "count": args.count,
            "seed_base": args.seed_base, "date": time.strftime("%Y-%m-%d %H:%M")}
    Path(args.out).write_text(json.dumps({"meta": meta, "summary": summarize(rows), "by_source": by_source(rows), "rows": rows},
                                         ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved", args.out)


if __name__ == "__main__":
    main()
