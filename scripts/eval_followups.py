"""Does retrieval find the right chunks for the SECOND question of a conversation?

    python scripts/eval_followups.py                 # compares query strategies
    python scripts/eval_followups.py --show f01 t04 # print the retrieved chunks

For each case in eval/followups.json it retrieves with several ways of building the query
and reports how often one of the top-3 chunks is about the right topic. Follow-ups
("explain that again") must find the PREVIOUS question's topic; topic changes must still
find the NEW one. Needs Chroma, the embedding model and (for the shipped strategy) Ollama.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import retrieval  # noqa: E402
from app.services.query_rewrite import retrieval_query  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def strategies() -> dict:
    """name -> function(previous, question) -> query string."""
    return {
        "current only (before)": lambda prev, q: q,
        "always add previous": lambda prev, q: f"{prev}\n{q}",
        "weighted (q, q, previous)": lambda prev, q: f"{q}\n{q}\n{prev}",
        "shipped (model rewrite)": lambda prev, q: retrieval_query(prev, q),
    }


def hit(chunks, terms) -> bool:
    low = [c.text.lower() for c in chunks]
    return any(t.lower() in text for text in low for t in terms)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--cases", default=str(ROOT / "eval" / "followups.json"))
    p.add_argument("--show", nargs="*", metavar="ID", help="print the top chunks for these cases")
    p.add_argument("--out", default=str(ROOT / "eval" / "results" / "followup_retrieval.json"))
    args = p.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
    strat = strategies()
    rows = []
    for case in cases:
        row = {"id": case["id"], "kind": case["kind"], "lang": case["lang"], "question": case["question"],
               "previous": case["previous"], "results": {}}
        for name, build in strat.items():
            query = build(case["previous"], case["question"])
            chunks = retrieval.retrieve(query, case["subject_id"])
            row["results"][name] = {
                "query": query,
                "hit": hit(chunks, case["topic_terms"]),
                "top3": [f"{c.source_filename} p{c.page_number}" for c in chunks],
            }
            if args.show and case["id"] in args.show:
                print(f"\n{case['id']} [{name}] query={query!r}")
                for c in chunks:
                    print(f"    {c.source_filename} p{c.page_number}: {c.text[:110]!r}")
        rows.append(row)

    print(f"{'strategy':<26}{'follow-ups':>14}{'topic changes':>16}   (top-3 contains the right topic)")
    for name in strat:
        cells = []
        for kind in ("follow_up", "topic_change"):
            sel = [r for r in rows if r["kind"] == kind]
            cells.append(f"{sum(r['results'][name]['hit'] for r in sel)}/{len(sel)}")
        print(f"{name:<26}{cells[0]:>14}{cells[1]:>16}")

    print("\nper case (X = miss):")
    print(f"{'':<5}{'kind':<14}" + "".join(f"{n[:22]:<24}" for n in strat))
    for r in rows:
        print(f"{r['id']:<5}{r['kind']:<14}" + "".join(f"{'hit' if r['results'][n]['hit'] else 'X':<24}" for n in strat))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nsaved {out}")


if __name__ == "__main__":
    main()
