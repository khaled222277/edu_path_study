"""How often do inline citations in chat answers point at something that was not retrieved?

    python scripts/eval_citations.py eval/results/quality_chat_baseline.json
    python scripts/eval_citations.py A.json B.json          # side by side

Reads result files written by `eval_quality.py chat` (no model calls). For every answer it
looks at the bracketed citations the model wrote and judges them against the chunks that
were actually put in that turn's prompt (`sources` in each row). Two views:

  strict   only the exact form the prompt asks for, [file.pdf, page N]
  lenient  also bracket text that is plainly meant as a citation but is malformed:
           several pages in one bracket, a file with no page, Arabic digits, or a made-up
           name where the filename should be

The strict view alone under-counts: a citation the regex cannot read is invisible to it.
Rates are pooled per seed and reported as mean +/- sd over seeds.
"""

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
STRICT = re.compile(r"\[([^\[\]]+?),\s*page\s*(\d+)\]", re.IGNORECASE)
BRACKET = re.compile(r"\[[^\[\]\n]{1,200}\]")
CITATION_LIKE = re.compile(r"\.pdf|(?:page|pages|صفحة|صفحات|الصفحة|ص)\s*[0-9٠-٩]", re.IGNORECASE)
PAGE_NUMS = re.compile(r"(?:pages?|صفحات|صفحة|الصفحة|ص)\s*([0-9٠-٩][0-9٠-٩,\s،&و\-–]*)", re.IGNORECASE)
FILE = re.compile(r"[\w\-. ]+\.pdf", re.IGNORECASE)


def classify(bracket: str, retrieved: set[tuple[str, int]]) -> str | None:
    """None (not a citation) | 'ok' | 'bad_page' | 'bad_file' | 'unreadable' for one bracket."""
    if not CITATION_LIKE.search(bracket):
        return None
    files = FILE.findall(bracket)
    pages = PAGE_NUMS.search(bracket)
    if not files or not pages:
        return "unreadable"  # no filename, or no page: cannot be matched to anything
    name = files[0].strip()
    nums = [int(n) for n in re.findall(r"\d+", pages.group(1).translate(DIGITS))]
    known_files = {f for f, _ in retrieved}
    if name not in known_files:
        return "bad_file"
    return "ok" if all((name, n) in retrieved for n in nums) else "bad_page"


def analyse(rows: list[dict]) -> dict:
    """Per language, per seed: strict and lenient citation validity."""
    out = {}
    for lang in sorted({r["lang"] for r in rows}):
        by_seed = defaultdict(lambda: defaultdict(int))
        for r in rows:
            if r["lang"] != lang or r["kind"] == "out_of_scope":
                continue
            retrieved = {(s["file"], s["page"]) for s in r["sources"]}
            s = by_seed[r["seed"]]
            s["answers"] += 1
            strict = STRICT.findall(r["answer"])
            s["strict_cited"] += len(strict)
            s["strict_bad"] += sum(1 for f, p in strict if (f.strip(), int(p.translate(DIGITS))) not in retrieved)
            verdicts = [v for v in (classify(b, retrieved) for b in BRACKET.findall(r["answer"])) if v]
            s["lenient_cited"] += len(verdicts)
            for v in verdicts:
                s[v] += 1
            bad = [v for v in verdicts if v != "ok"]
            s["answers_with_bad"] += bool(bad)
            s["answers_with_any"] += bool(verdicts)
        out[lang] = dict(by_seed)
    return out


def rate(by_seed: dict, num: str, den: str) -> list[float]:
    return [s[num] / s[den] for s in by_seed.values() if s[den]]


def fmt(values: list[float]) -> str:
    if not values:
        return "   n/a"
    m = statistics.mean(values) * 100
    sd = statistics.stdev(values) * 100 if len(values) > 1 else 0.0
    return f"{m:5.1f} ±{sd:.1f}"


def lenient_bad(s: dict) -> int:
    return s["bad_page"] + s["bad_file"] + s["unreadable"]


def report(label: str, rows: list[dict]) -> None:
    result = analyse(rows)
    print(f"\n=== {label}")
    for lang, by_seed in result.items():
        seeds = len(by_seed)
        total = defaultdict(int)
        for s in by_seed.values():
            for k, v in s.items():
                total[k] += v
        print(f"  [{lang.upper()}]  {total['answers'] // seeds} in-scope answers x {seeds} seeds")
        print(f"    strict   citations written per answer-set              {total['strict_cited'] / seeds:5.1f}")
        print(f"    strict   % naming a page that was NOT retrieved         {fmt([s['strict_bad'] / s['strict_cited'] for s in by_seed.values() if s['strict_cited']])}")
        print(f"    lenient  citation-like brackets per answer-set          {total['lenient_cited'] / seeds:5.1f}  (strict regex misses {total['lenient_cited'] / seeds - total['strict_cited'] / seeds:.1f} of them)")
        print(f"    lenient  % not valid                                    {fmt([lenient_bad(s) / s['lenient_cited'] for s in by_seed.values() if s['lenient_cited']])}")
        print(f"             of the invalid: wrong page {total['bad_page'] / seeds:.1f}, file not retrieved {total['bad_file'] / seeds:.1f}, unreadable {total['unreadable'] / seeds:.1f}")
        print(f"    lenient  % of answers carrying at least one invalid     {fmt([s['answers_with_bad'] / s['answers_with_any'] for s in by_seed.values() if s['answers_with_any']])}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("files", nargs="+")
    args = p.parse_args()
    for path in args.files:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        report(data["meta"].get("label", Path(path).stem), data["rows"])


if __name__ == "__main__":
    main()
