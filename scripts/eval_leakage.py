"""How often does a chat answer contain a character of a script it must not?

    python scripts/eval_leakage.py --chroma-copy DIR --db-file FILE --label before --out result.json
    python scripts/eval_leakage.py --compare before.json after.json

Asks every single-turn question of eval/qa_set.json for the sandbox subject through the real
streaming route, once per seed, and checks each STORED answer (what the student keeps) for U+FFFD and
for letters outside Arabic / Latin / Greek (CJK, Hangul, Kana, Cyrillic, Hebrew, Devanagari, Thai).
Reports the share of answers affected and the share of characters affected, per question language.
The answers are saved with the result so a filter can be judged offline on the very text that leaked;
the printed summary has counts only.

Runs on a throwaway SQLite database and a COPY of the Chroma store (see scripts/chat_sandbox.py).
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.chat_checks import disallowed_chars, script_of  # noqa: E402


def run(box, seeds, kinds=None) -> list[dict]:
    from app.controllers import chat_controller as cc

    items = json.loads((ROOT / "eval" / "qa_set.json").read_text(encoding="utf-8"))["items"]
    items = [i for i in items if i["subject_id"] == box.subject_id and i["kind"] != "follow_up"
             and (not kinds or i["kind"] in kinds)]
    rows = []
    for seed in seeds:
        cc.CHAT_OPTIONS = {**cc.CHAT_OPTIONS, "seed": seed}
        for item in items:
            out = box.stream(box.new_session(), item["question"])
            text = out["stored"] or ""
            bad = disallowed_chars(text)
            rows.append({
                "id": item["id"], "lang": item["lang"], "kind": item["kind"], "seed": seed,
                "chars": len(text), "disallowed": len(bad),
                "scripts": dict(Counter(script_of(c) for c in bad)),
                "streamed_equals_stored": out["streamed"] == out["stored"],
                "answer": text,
            })
            print(f"{item['id']} seed={seed} chars={len(text)} disallowed={len(bad)}", flush=True)
    return rows


def summarize(rows: list[dict]) -> dict:
    out = {}
    for lang in ("ar", "en", "all"):
        sel = [r for r in rows if lang == "all" or r["lang"] == lang]
        if not sel:
            continue
        chars = sum(r["chars"] for r in sel)
        out[lang] = {
            "answers": len(sel),
            "answers_with_leak": sum(1 for r in sel if r["disallowed"]),
            "answer_rate": sum(1 for r in sel if r["disallowed"]) / len(sel),
            "char_rate": sum(r["disallowed"] for r in sel) / max(1, chars),
            "by_script": dict(sum((Counter(r["scripts"]) for r in sel), Counter())),
            "by_seed": {str(s): sum(1 for r in sel if r["seed"] == s and r["disallowed"]) for s in sorted({r["seed"] for r in sel})},
        }
    return out


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--chroma-copy")
    p.add_argument("--db-file")
    p.add_argument("--seeds", type=int, nargs="+", default=[41, 42, 43])
    p.add_argument("--kinds", nargs="+")
    p.add_argument("--label", default="run")
    p.add_argument("--out")
    p.add_argument("--compare", nargs=2)
    args = p.parse_args()
    if args.compare:
        a, b = (json.loads(Path(x).read_text(encoding="utf-8")) for x in args.compare)
        for lang in a["summary"]:
            sa, sb = a["summary"][lang], b["summary"].get(lang)
            if sb:
                print(f"{lang:<4} answers with a leak {sa['answers_with_leak']}/{sa['answers']} -> {sb['answers_with_leak']}/{sb['answers']}"
                      f"   chars {100 * sa['char_rate']:.3f}% -> {100 * sb['char_rate']:.3f}%")
        return
    if not (args.chroma_copy and args.db_file and args.out):
        p.error("--chroma-copy, --db-file and --out are required")

    from scripts.chat_sandbox import Sandbox

    box = Sandbox(args.chroma_copy, args.db_file)
    try:
        rows = run(box, args.seeds, args.kinds)
    finally:
        box.close()
    summary = summarize(rows)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    Path(args.out).write_text(json.dumps(
        {"meta": {"label": args.label, "seeds": args.seeds, "model": box.settings.chat_model}, "summary": summary, "rows": rows},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved", args.out)


if __name__ == "__main__":
    main()
