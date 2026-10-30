"""Reproduce the two-turn conversation that went wrong, and trace every stage of the second turn.

    python scripts/repro_followup.py --chroma-copy DIR --db-file FILE --seeds 41 42 43 --out result.json

Turn 1 "حسنا ما هو التصنيف" (what is classification), turn 2 "okay i can't get it could repeat all
that in arabic". Per run it records the rewritten query, the vector candidates, the chunks kept
after reranking (and which are figure descriptions), the model's raw output, what the student saw,
and the automatic checks: disallowed scripts, a repeated paragraph, the "no Arabic text" boilerplate.

Runs on a throwaway SQLite database and a COPY of the Chroma store (see scripts/chat_sandbox.py).
Both the plain route and the streaming route are driven, each in its own session.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TURN_1 = "حسنا ما هو التصنيف"
TURN_2 = "okay i can't get it could repeat all that in arabic"

from scripts.chat_checks import BOILERPLATE_RE, disallowed_chars, repeated_paragraph, script_of  # noqa: E402


def chunk_row(c, kind: str | None = None) -> dict:
    return {"file": c.source_filename, "page": c.page_number, "type": c.content_type,
            "score": round(c.score, 4), "no_arabic_boilerplate": bool(BOILERPLATE_RE.search(c.text)),
            "snippet": re.sub(r"\s+", " ", c.text)[:120]}


def run(box, seeds, routes) -> list[dict]:
    from app.controllers import chat_controller as cc
    from app.services import retrieval

    rows = []
    for route in routes:
        for seed in seeds:
            cc.CHAT_OPTIONS = {**cc.CHAT_OPTIONS, "seed": seed}
            trace = {}

            real_query, real_retrieve, real_final = cc.retrieval_query, cc.retrieve, cc.finalize_answer
            cc.retrieval_query = lambda prev, q: trace.__setitem__("query", real_query(prev, q)) or trace["query"]

            def traced_retrieve(query, subject_id, *a, **k):
                t = retrieval.retrieve_detailed(query, subject_id, *a, **k)
                trace["retrieval"] = t
                return t.final

            cc.retrieve = traced_retrieve
            cc.finalize_answer = lambda raw, *a, **k: trace.__setitem__("raw", raw) or real_final(raw, *a, **k)
            try:
                sid = box.new_session()
                ask = box.stream if route == "stream" else box.plain
                first = ask(sid, TURN_1)
                trace.clear()
                second = ask(sid, TURN_2)
            finally:
                cc.retrieval_query, cc.retrieve, cc.finalize_answer = real_query, real_retrieve, real_final

            t = trace.get("retrieval") or retrieval.RetrievalTrace([], [], [], "not searched", 0.0)
            final_text = second["stored"] or ""
            raw = trace.get("raw") or ""
            kept = [chunk_row(c) for c in t.final]
            chars = disallowed_chars(final_text)
            rows.append({
                "route": route, "seed": seed,
                "turn1_answer": first["stored"], "turn1_disallowed": len(disallowed_chars(first["stored"] or "")),
                "query": trace.get("query"), "searched": "retrieval" in trace,
                "candidates": [chunk_row(c) for c in t.candidates],
                "kept": kept, "strategy": t.strategy, "top_score": round(t.top_score, 5),
                "kept_figure_chunks": sum(1 for k in kept if k["type"] == "image_description"),
                "kept_boilerplate_chunks": sum(1 for k in kept if k["no_arabic_boilerplate"]),
                "cand_figure_chunks": sum(1 for k in (chunk_row(c) for c in t.candidates) if k["type"] == "image_description"),
                "raw": raw, "final": final_text,
                "refused_sources_shown": len(second["citations"] or []),
                "raw_disallowed": [f"{c} {script_of(c)}" for c in disallowed_chars(raw)],
                "final_disallowed": [f"{c} {script_of(c)}" for c in chars],
                "repeated_paragraph_raw": repeated_paragraph(raw),
                "repeated_paragraph_final": repeated_paragraph(final_text),
                "boilerplate_in_answer": bool(BOILERPLATE_RE.search(final_text)),
                "streamed_equals_stored": (second.get("streamed") == second["stored"]) if route == "stream" else None,
            })
            print(f"{route} seed={seed}: searched={'retrieval' in trace} query={trace.get('query')!r} figs_kept={rows[-1]['kept_figure_chunks']}/"
                  f"{len(kept)} disallowed={len(chars)} repeat={rows[-1]['repeated_paragraph_final']}", flush=True)
    return rows


def sweep_retrieval(box) -> list[dict]:
    """Turn 2 after every eval question as the previous question: what does the search return?

    No answer is generated, so this is cheap. Each case is searched twice: with the follow-up
    rewrite (what the app does) and with the question exactly as typed (what the app does when the
    rewrite call fails or times out)."""
    from app.services import retrieval
    from app.services.query_rewrite import retrieval_query

    items = json.loads((Path(__file__).resolve().parents[1] / "eval" / "qa_set.json").read_text(encoding="utf-8"))["items"]
    rows = []
    for item in items:
        if item["subject_id"] != box.subject_id:
            continue
        rewritten = retrieval_query(item["question"], TURN_2)
        for label, query in (("rewritten", rewritten), ("as_typed", TURN_2)):
            t = retrieval.retrieve_detailed(query, box.subject_id)
            kept = [chunk_row(c) for c in t.final]
            rows.append({"previous": item["question"], "previous_lang": item["lang"], "mode": label, "query": query,
                         "kept": kept, "kept_figures": sum(k["type"] == "image_description" for k in kept),
                         "kept_boilerplate": sum(k["no_arabic_boilerplate"] for k in kept)})
        r = rows[-2]
        print(f"prev={item['question'][:40]!r} rewritten={r['query'][:60]!r} figs={r['kept_figures']}/3", flush=True)
    return rows


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--chroma-copy", required=True)
    p.add_argument("--db-file", required=True)
    p.add_argument("--seeds", type=int, nargs="+", default=list(range(41, 53)))
    p.add_argument("--routes", nargs="+", default=["plain", "stream"], choices=["plain", "stream"])
    p.add_argument("--out", required=True)
    p.add_argument("--no-rewrite", action="store_true",
                   help="search turn 2 exactly as typed (what happens when the rewrite call fails or times out)")
    p.add_argument("--turn1", default=TURN_1, help="override the first question (e.g. to force a long first answer)")
    p.add_argument("--sweep", action="store_true", help="retrieval-only sweep over previous questions (no answers)")
    args = p.parse_args()

    from scripts.chat_sandbox import Sandbox

    globals()["TURN_1"] = args.turn1
    box = Sandbox(args.chroma_copy, args.db_file)
    if args.no_rewrite:
        box.settings.chat_followup_rewrite = False
    try:
        rows = sweep_retrieval(box) if args.sweep else run(box, args.seeds, args.routes)
    finally:
        box.close()
    Path(args.out).write_text(json.dumps({"turn1": TURN_1, "turn2": TURN_2, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved", args.out)


if __name__ == "__main__":
    main()
