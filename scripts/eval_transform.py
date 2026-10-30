"""Measure turns that work on the previous answer ("say that in Arabic") against look-alike real questions.

    python scripts/eval_transform.py --chroma-copy DIR --db-file FILE --label after --out result.json
    python scripts/eval_transform.py --compare before.json after.json

Cases are in eval/transform_cases.json: 20 transform requests (10 English, 10 Arabic) and 10
near-misses that look like them but are real questions. Each is a two-turn conversation: the
`previous` question is answered for real (once per seed, then every case that follows it is replayed
from that same first turn), then the case's question goes through the real streaming route.

Per case: was the lectures search called, does the answer stay on the previous topic (transform) or
on the new one (near-miss), is it in the language asked for, which sources did it show and were any
of them NOT in the previous turn's, characters of a disallowed script, a repeated paragraph, and
talk about a logo/DNA figure. Run it against an older checkout to get a "before": it uses only the
public behaviour of the routes.

Runs on a throwaway SQLite database and a COPY of the Chroma store (see scripts/chat_sandbox.py).
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.chat_checks import CITATION_RE, FIGURE_TALK_RE, disallowed_chars, repeated_paragraph  # noqa: E402

CASES = ROOT / "eval" / "transform_cases.json"


def on_topic(answer: str, terms: list[str]) -> bool | None:
    """Does the answer contain one of the topic terms? None when the case has no terms to check."""
    if not terms:
        return None
    low = answer.lower()
    return any(t.lower() in low for t in terms)


def run(box, seeds, only=None) -> list[dict]:
    from app.controllers import chat_controller as cc
    from app.services.quiz_service import detect_language

    spec = json.loads(CASES.read_text(encoding="utf-8"))
    previous = spec["previous"]
    cases = [c for c in spec["cases"] if not only or c["id"] in only]
    searched = {"n": 0}
    real_retrieve = cc.retrieve

    def counting_retrieve(*a, **k):
        searched["n"] += 1
        return real_retrieve(*a, **k)

    cc.retrieve = counting_retrieve
    rows = []
    try:
        for seed in seeds:
            cc.CHAT_OPTIONS = {**cc.CHAT_OPTIONS, "seed": seed}
            first_turns = {}
            for pid in sorted({c["previous"] for c in cases}):
                sid = box.new_session()
                out = box.stream(sid, previous[pid]["question"])
                first_turns[pid] = (out["stored"], out["citations"] or [])
            for case in cases:
                q1 = previous[case["previous"]]
                answer1, sources1 = first_turns[case["previous"]]
                sid = box.new_session()
                box.seed_exchange(sid, q1["question"], answer1, sources1)
                searched["n"] = 0
                out = box.stream(sid, case["question"])
                text = out["stored"] or ""
                pages1 = {(s["source_filename"], s["page_number"]) for s in sources1}
                pages2 = [(s["source_filename"], s["page_number"]) for s in out["citations"] or []]
                terms = q1["topic_terms"] if case["kind"] == "transform" else case.get("topic_terms", [])
                rows.append({
                    "id": case["id"], "kind": case["kind"], "seed": seed, "question": case["question"],
                    "question_lang": detect_language(case["question"]), "expect_lang": case["expect_lang"],
                    "searched": searched["n"] > 0,
                    "on_topic": on_topic(text, terms),
                    "language_ok": detect_language(CITATION_RE.sub("", text)) == case["expect_lang"],
                    "sources": pages2, "new_sources": sum(1 for p in pages2 if p not in pages1),
                    "figure_talk": bool(FIGURE_TALK_RE.search(text)),
                    "disallowed": len(disallowed_chars(text)),
                    "repeated_paragraph": repeated_paragraph(text),
                    "streamed_equals_stored": out["streamed"] == out["stored"],
                    "previous_answer": answer1, "answer": text,
                })
                r = rows[-1]
                print(f"{case['id']} seed={seed} searched={r['searched']} on_topic={r['on_topic']} "
                      f"lang_ok={r['language_ok']} new_sources={r['new_sources']} "
                      f"figure_talk={r['figure_talk']} disallowed={r['disallowed']}", flush=True)
    finally:
        cc.retrieve = real_retrieve
    return rows


def _share(values) -> float | None:
    values = [v for v in values if v is not None]
    return sum(1 for v in values if v) / len(values) if values else None


def summarize(rows: list[dict]) -> dict:
    """Per (kind, question language): the share of rows passing each check."""
    out = {}
    for kind in ("transform", "near_miss"):
        for lang in ("en", "ar", "all"):
            sel = [r for r in rows if r["kind"] == kind and (lang == "all" or r["question_lang"] == lang)]
            if not sel:
                continue
            cell = {
                "n": len(sel),
                "searched": _share(r["searched"] for r in sel),
                "on_topic": _share(r["on_topic"] for r in sel),
                "language_ok": _share(r["language_ok"] for r in sel),
                "figure_talk": _share(r["figure_talk"] for r in sel),
                "disallowed_answers": _share(bool(r["disallowed"]) for r in sel),
                "repeated_paragraph": _share(r["repeated_paragraph"] for r in sel),
            }
            if kind == "transform":
                cell["no_new_sources"] = _share(r["new_sources"] == 0 for r in sel)
            else:
                cell["has_sources"] = _share(bool(r["sources"]) for r in sel)
                cell["new_sources_vs_previous"] = _share(r["new_sources"] > 0 for r in sel)
            by_seed = defaultdict(list)
            for r in sel:
                by_seed[r["seed"]].append(r)
            cell["on_topic_by_seed"] = [_share(r["on_topic"] for r in rs) for _, rs in sorted(by_seed.items())]
            out[f"{kind}/{lang}"] = cell
    return out


def pct(v) -> str:
    return "   n/a" if v is None else f"{100 * v:5.0f}%"


def print_summary(summary: dict, title: str) -> None:
    print(f"\n{title}")
    keys = ["searched", "on_topic", "language_ok", "no_new_sources", "has_sources", "new_sources_vs_previous",
            "figure_talk", "disallowed_answers", "repeated_paragraph"]
    print(f"{'':<16}{'n':>4}" + "".join(f"{k[:11]:>13}" for k in keys))
    for name, cell in summary.items():
        print(f"{name:<16}{cell['n']:>4}" + "".join(f"{pct(cell.get(k)):>13}" for k in keys))


def compare(a_path: str, b_path: str) -> None:
    a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (a_path, b_path))
    print(f"\nA = {a['meta']['label']}   B = {b['meta']['label']}")
    for name in a["summary"]:
        if name not in b["summary"]:
            continue
        print(f"\n--- {name}   (n = {a['summary'][name]['n']} -> {b['summary'][name]['n']})")
        for k, va in a["summary"][name].items():
            vb = b["summary"][name].get(k)
            if k != "n" and not isinstance(va, list):
                print(f"  {k:<26}{pct(va):>8} -> {pct(vb):>6}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--chroma-copy")
    p.add_argument("--db-file")
    p.add_argument("--seeds", type=int, nargs="+", default=[41, 42, 43])
    p.add_argument("--only", nargs="+")
    p.add_argument("--label", default="run")
    p.add_argument("--out")
    p.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"))
    args = p.parse_args()
    if args.compare:
        return compare(*args.compare)
    if not (args.chroma_copy and args.db_file and args.out):
        p.error("--chroma-copy, --db-file and --out are required")

    from scripts.chat_sandbox import Sandbox

    box = Sandbox(args.chroma_copy, args.db_file)
    try:
        rows = run(box, args.seeds, args.only)
    finally:
        box.close()
    summary = summarize(rows)
    print_summary(summary, f"[{args.label}] seeds={args.seeds}")
    Path(args.out).write_text(json.dumps(
        {"meta": {"label": args.label, "seeds": args.seeds, "model": box.settings.chat_model}, "summary": summary, "rows": rows},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved", args.out)


if __name__ == "__main__":
    main()
