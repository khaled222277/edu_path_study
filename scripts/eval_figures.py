"""Do figure-description chunks crowd out lecture text? Compare the FIGURE_CHUNKS modes on the eval set.

    python scripts/eval_figures.py --chroma-copy DIR --db-file FILE --out result.json

For every question of eval/qa_set.json for the sandbox subject, retrieve with each mode (normal,
demote, exclude; see app/config.py) and record: did a kept chunk come from a gold (file, page) --
the strict hit the other retrieval evaluations use --, how many of the kept chunks are figure
descriptions, how many contain the "No Arabic text is present" style boilerplate, and whether the
question mentions a figure (the only case where the modes do nothing). Retrieval only; no model is called.
Out-of-scope questions have no gold, so they only count towards the figure share.

Runs on a COPY of the Chroma store (see scripts/chat_sandbox.py).
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.chat_checks import BOILERPLATE_RE  # noqa: E402

MODES = ("normal", "demote", "exclude")


def run(box) -> list[dict]:
    from app.services import retrieval

    items = json.loads((ROOT / "eval" / "qa_set.json").read_text(encoding="utf-8"))["items"]
    items = [i for i in items if i["subject_id"] == box.subject_id]
    rows = []
    for item in items:
        gold = {(g["file"], g["page"]) for g in item.get("gold", [])}
        for mode in MODES:
            box.settings.figure_chunks = mode
            kept = retrieval.retrieve_detailed(item["question"], box.subject_id).final
            rows.append({
                "id": item["id"], "lang": item["lang"], "kind": item["kind"], "mode": mode,
                "asks_for_figure": retrieval.asks_for_figure(item["question"]),
                "has_gold": bool(gold),
                "hit": bool(gold) and any((c.source_filename, c.page_number) in gold for c in kept),
                "kept": len(kept),
                "figures": sum(c.content_type == "image_description" for c in kept),
                "boilerplate": sum(bool(BOILERPLATE_RE.search(c.text)) for c in kept),
                "pages": [(c.source_filename, c.page_number, c.content_type) for c in kept],
            })
    box.settings.figure_chunks = "normal"
    return rows


def run_followups(box) -> list[dict]:
    """eval/followups.json, searched with the shipped query (the model's rewrite), one row per mode.

    A case is a hit when one of the kept chunks contains a topic term, the coarse proxy that file defines.
    """
    from app.services import retrieval
    from app.services.query_rewrite import retrieval_query

    cases = json.loads((ROOT / "eval" / "followups.json").read_text(encoding="utf-8"))["cases"]
    rows = []
    for case in cases:
        if case["subject_id"] != box.subject_id:
            continue
        query = retrieval_query(case["previous"], case["question"])
        for mode in MODES:
            box.settings.figure_chunks = mode
            kept = retrieval.retrieve_detailed(query, box.subject_id).final
            low = [c.text.lower() for c in kept]
            rows.append({"id": case["id"], "kind": case["kind"], "mode": mode, "query": query,
                         "hit": any(t.lower() in t2 for t2 in low for t in case["topic_terms"]),
                         "figures": sum(c.content_type == "image_description" for c in kept), "kept": len(kept)})
    box.settings.figure_chunks = "normal"
    return rows


def summarize(rows: list[dict]) -> dict:
    out = {}
    for group, pick in (("all", lambda r: True), ("ar", lambda r: r["lang"] == "ar"), ("en", lambda r: r["lang"] == "en"),
                        ("no figure asked", lambda r: not r["asks_for_figure"]),
                        ("figure asked", lambda r: r["asks_for_figure"])):
        for mode in MODES:
            sel = [r for r in rows if r["mode"] == mode and pick(r)]
            gold = [r for r in sel if r["has_gold"]]
            kept = sum(r["kept"] for r in sel)
            out[f"{group}/{mode}"] = {
                "questions": len(sel), "with_gold": len(gold),
                "hits": sum(r["hit"] for r in gold),
                "hit_rate": sum(r["hit"] for r in gold) / len(gold) if gold else None,
                "figure_share": sum(r["figures"] for r in sel) / max(1, kept),
                "boilerplate_share": sum(r["boilerplate"] for r in sel) / max(1, kept),
            }
    return out


def regressions(rows: list[dict]) -> list[dict]:
    """Questions that hit under `normal` and miss under another mode."""
    by = defaultdict(dict)
    for r in rows:
        by[r["id"]][r["mode"]] = r
    out = []
    for qid, modes in by.items():
        for mode in ("demote", "exclude"):
            if modes["normal"]["hit"] and not modes[mode]["hit"]:
                out.append({"id": qid, "lang": modes[mode]["lang"], "kind": modes[mode]["kind"], "mode": mode,
                            "asks_for_figure": modes[mode]["asks_for_figure"]})
    return out


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--chroma-copy", required=True)
    p.add_argument("--db-file", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()

    from scripts.chat_sandbox import Sandbox

    box = Sandbox(args.chroma_copy, args.db_file)
    try:
        rows = run(box)
        follow = run_followups(box)
    finally:
        box.close()
    summary = summarize(rows)
    print(f"{'group/mode':<26}{'n':>4}{'gold':>6}{'hits':>6}{'hit%':>7}{'fig%':>7}{'boiler%':>9}")
    for name, c in summary.items():
        hr = "  n/a" if c["hit_rate"] is None else f"{100 * c['hit_rate']:5.0f}"
        print(f"{name:<26}{c['questions']:>4}{c['with_gold']:>6}{c['hits']:>6}{hr:>7}{100 * c['figure_share']:>7.0f}{100 * c['boilerplate_share']:>9.0f}")
    reg = regressions(rows)
    print("\nhit under normal, miss under another mode:", reg or "none")
    print()
    print("follow-up set (query = the model's rewrite), topic term in a kept chunk / figure share of kept chunks:")
    for kind in ("follow_up", "topic_change"):
        for mode in MODES:
            sel = [r for r in follow if r["kind"] == kind and r["mode"] == mode]
            print(f"  {kind:<13}{mode:<9}{sum(r['hit'] for r in sel)}/{len(sel)}   figures {100 * sum(r['figures'] for r in sel) / max(1, sum(r['kept'] for r in sel)):.0f}%")
    Path(args.out).write_text(json.dumps({"summary": summary, "regressions": reg, "followups": follow, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
