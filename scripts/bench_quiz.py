"""Measure quiz generation: time, model calls, pass rates and why replies are rejected.

    python scripts/bench_quiz.py --label baseline --out bench_results/quiz_baseline.json
    python scripts/bench_quiz.py --report bench_results/quiz_baseline.json
    python scripts/bench_quiz.py --compare bench_results/quiz_baseline.json bench_results/quiz_x.json

A run generates real quizzes from the ingested lectures through the same
generate_quiz() the API calls, against the real Ollama and Chroma. It never
touches Postgres. Quiz i of every language uses seed (seed_base + i) and subject
subjects[i % len(subjects)], so two result files sample the *same chunks* and can
be compared like for like (the model's own sampling is still random).

Results are written after every quiz and the file is resumable (--resume), because
an Arabic run can take a long time.
"""

import argparse
import json
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services import quiz_service as qs  # noqa: E402
from app.services.llm import LLMUnavailableError  # noqa: E402


# --------------------------------------------------------------------------- run


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1],
        )
        return out.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def ollama_version() -> str:
    try:
        import httpx

        base = settings.ollama_base_url or "http://127.0.0.1:11434"
        return httpx.get(f"{base}/api/version", timeout=5).json().get("version", "?")
    except Exception:
        return "unreachable"


def trace_to_run(trace: qs.QuizTrace, subject_id: int, seed: int) -> dict:
    return {
        "language": trace.language,
        "subject_id": subject_id,
        "seed": seed,
        "requested": trace.requested,
        "produced": trace.produced,
        "total_s": round(trace.total_s, 4),
        "sampling_s": round(trace.sampling_s, 4),
        "error": trace.error,
        "chunks": [
            {
                "citation": c.citation,
                "accepted": c.accepted,
                "accepted_phase": c.accepted_phase,
                "wall_s": round(c.wall_s, 4),
                "calls": c.calls,
                "attempts": [
                    {
                        "phase": a.phase,
                        "model_s": round(a.model_s, 4),
                        "check_s": round(a.check_s, 5),
                        "outcome": a.outcome,
                        "reasons": a.reasons,
                    }
                    for a in c.attempts
                ],
            }
            for c in trace.chunks
        ],
    }


def config_snapshot() -> dict:
    """Everything that could explain a difference between two result files."""
    return {
        "quiz_model": qs.QUIZ_MODEL,
        "quiz_options": dict(qs.QUIZ_OPTIONS),
        "strict_attempts": qs.STRICT_ATTEMPTS,
        "relaxed_attempts": qs.RELAXED_ATTEMPTS,
        "min_chunk_chars": qs.MIN_CHUNK_CHARS,
        "keep_alive": settings.ollama_keep_alive,
        **getattr(qs, "experiment_config", lambda: {})(),
    }


def run_bench(args) -> None:
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    subjects = [int(x) for x in args.subjects.split(",")]
    languages = args.languages.split(",")

    if args.resume and out.exists():
        data = json.loads(out.read_text(encoding="utf-8"))
    else:
        data = {
            "meta": {
                "label": args.label,
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "git_commit": git_commit(),
                "ollama_version": ollama_version(),
                "count": args.count,
                "runs_per_language": args.runs,
                "subjects": subjects,
                "seed_base": args.seed_base,
                "config": config_snapshot(),
            },
            "runs": [],
        }
    done = {(r["language"], r["subject_id"], r["seed"]) for r in data["runs"]}

    # Pay the one-off embedder / model load before the first timed quiz.
    print("warming up ...", flush=True)
    from app import main as app_main

    app_main.warm_up_models()

    for lang in languages:
        for i in range(args.runs):
            subject = subjects[i % len(subjects)]
            seed = args.seed_base + i
            if (lang, subject, seed) in done:
                continue
            trace = qs.QuizTrace()
            try:
                qs.generate_quiz(subject, args.count, lang, seed=seed, trace=trace)
            except (qs.QuizGenerationError, LLMUnavailableError) as exc:
                print(f"  ! {lang} #{i}: {type(exc).__name__}", flush=True)
            run = trace_to_run(trace, subject, seed)
            data["runs"].append(run)
            out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            calls = sum(c["calls"] for c in run["chunks"])
            print(
                f"{lang} #{i:<2} subject {subject} seed {seed}: {run['produced']}/{args.count} "
                f"questions, {calls} calls, {run['total_s']:.1f}s"
                f"{'  ERROR ' + run['error'] if run['error'] else ''}",
                flush=True,
            )
    print(f"\nsaved {out}")
    report([out])


# ------------------------------------------------------------------------ report


def pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:5.1f}%" if whole else "   n/a"


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def summarize(runs: list[dict], language: str) -> dict | None:
    runs = [r for r in runs if r["language"] == language]
    if not runs:
        return None
    chunks = [c for r in runs for c in r["chunks"]]
    attempts = [a for c in chunks for a in c["attempts"]]
    produced = sum(r["produced"] for r in runs)
    total_s = sum(r["total_s"] for r in runs)
    model_s = sum(a["model_s"] for a in attempts)
    check_s = sum(a["check_s"] for a in attempts)
    sampling_s = sum(r["sampling_s"] for r in runs)
    accepted_chunks = [c for c in chunks if c["accepted"]]

    # Which attempt (within its phase) produced the accepted question.
    accepted_at: Counter = Counter()
    for c in accepted_chunks:
        last = c["attempts"][-1]
        index = sum(1 for a in c["attempts"] if a["phase"] == last["phase"])
        accepted_at[(last["phase"], index)] += 1

    rejected = [a for a in attempts if a["outcome"] != qs.OUTCOME_ACCEPTED]
    return {
        "quizzes": len(runs),
        "errored": sum(1 for r in runs if r["error"]),
        "requested": sum(r["requested"] for r in runs),
        "produced": produced,
        "total_s": total_s,
        "quiz_s": [r["total_s"] for r in runs],
        "chunks": len(chunks),
        "chunks_accepted": len(accepted_chunks),
        "calls": len(attempts),
        "calls_per_accepted": [c["calls"] for c in accepted_chunks],
        "calls_per_failed": [c["calls"] for c in chunks if not c["accepted"]],
        "accepted_at": accepted_at,
        "reply_reasons": Counter(a["outcome"] for a in rejected),
        "item_reasons": Counter(x for a in rejected for x in a["reasons"]),
        "sampling_s": sampling_s,
        "model_s": model_s,
        "check_s": check_s,
        "accepted_wall_s": [c["wall_s"] for c in accepted_chunks],
    }


def print_summary(label: str, runs: list[dict], language: str) -> None:
    s = summarize(runs, language)
    name = {"en": "ENGLISH", "ar": "ARABIC"}.get(language, language.upper())
    if s is None:
        return
    print(f"\n=== {label}: {name}  ({s['quizzes']} quizzes) ===")
    print(f"questions produced : {s['produced']}/{s['requested']}   quizzes with an error: {s['errored']}")
    print(
        f"quiz time          : mean {statistics.mean(s['quiz_s']):.1f}s  median {statistics.median(s['quiz_s']):.1f}s  "
        f"p90 {quantile(s['quiz_s'], 0.9):.1f}s  max {max(s['quiz_s']):.1f}s"
    )
    if s["produced"]:
        print(f"time per question  : {s['total_s'] / s['produced']:.1f}s (total time / questions produced)")
    print(
        f"chunks tried       : {s['chunks']}  accepted {s['chunks_accepted']} "
        f"({pct(s['chunks_accepted'], s['chunks']).strip()}), abandoned {s['chunks'] - s['chunks_accepted']}"
    )
    print(
        f"model calls        : {s['calls']} total, {s['calls'] / max(s['produced'], 1):.2f} per question produced; "
        f"{pct(s['chunks_accepted'], s['calls']).strip()} of replies were accepted"
    )

    dist = Counter(s["calls_per_accepted"])
    print("calls to get an accepted question (distribution):")
    for n in sorted(dist):
        print(f"   {n:>2} call(s): {dist[n]:>3} {'#' * dist[n]}")
    if s["calls_per_failed"]:
        print(f"   chunks abandoned after {Counter(s['calls_per_failed']).most_common()} (calls: chunks)")

    print("accepted on attempt:")
    for (phase, index), n in sorted(s["accepted_at"].items(), key=lambda kv: (kv[0][0] != "strict", kv[0][1])):
        print(f"   {phase:<7} #{index:<2}: {n:>3} {pct(n, s['chunks']).strip():>6}")

    rejected = sum(s["reply_reasons"].values())
    print(f"why replies were rejected ({rejected} of {s['calls']}):")
    for reason, n in s["reply_reasons"].most_common():
        print(f"   {reason:<24}{n:>4} {pct(n, rejected)}")

    total = s["total_s"] or 1
    other = max(total - s["model_s"] - s["check_s"] - s["sampling_s"], 0)
    print(
        f"where the time goes: model {pct(s['model_s'], total).strip()}  sampling {pct(s['sampling_s'], total).strip()}  "
        f"parse+validate {pct(s['check_s'], total).strip()}  other {pct(other, total).strip()}"
    )
    if s["calls"]:
        print(f"                     {s['model_s'] / s['calls']:.2f}s per model call; validation {1000 * s['check_s'] / s['calls']:.2f} ms per reply")


def report(paths: list[Path]) -> None:
    for path in paths:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        meta = data["meta"]
        print(f"\n##### {meta['label']}  ({meta['git_commit']}, ollama {meta['ollama_version']})")
        print(f"config: {json.dumps(meta['config'], ensure_ascii=False)}")
        for lang in ("en", "ar"):
            print_summary(meta["label"], data["runs"], lang)


def compare(a_path: str, b_path: str) -> None:
    a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (a_path, b_path))
    print(f"\nA = {a['meta']['label']}   B = {b['meta']['label']}")
    rows = [
        ("quiz time, mean (s)", lambda s: statistics.mean(s["quiz_s"]), "{:.1f}"),
        ("quiz time, p90 (s)", lambda s: quantile(s["quiz_s"], 0.9), "{:.1f}"),
        ("time per question (s)", lambda s: s["total_s"] / max(s["produced"], 1), "{:.1f}"),
        ("questions produced", lambda s: s["produced"], "{}"),
        ("model calls per question", lambda s: s["calls"] / max(s["produced"], 1), "{:.2f}"),
        ("replies accepted (%)", lambda s: 100 * s["chunks_accepted"] / max(s["calls"], 1), "{:.1f}"),
        ("chunks abandoned", lambda s: s["chunks"] - s["chunks_accepted"], "{}"),
        ("seconds per model call", lambda s: s["model_s"] / max(s["calls"], 1), "{:.2f}"),
    ]
    for lang in ("en", "ar"):
        sa, sb = summarize(a["runs"], lang), summarize(b["runs"], lang)
        if not sa or not sb:
            continue
        print(f"\n--- {lang.upper()}  ({sa['quizzes']} vs {sb['quizzes']} quizzes)")
        print(f"{'':<28}{'A':>10}{'B':>10}{'change':>10}")
        for name, fn, fmt in rows:
            va, vb = fn(sa), fn(sb)
            change = f"{100 * (vb - va) / va:+.0f}%" if va else "n/a"
            print(f"{name:<28}{fmt.format(va):>10}{fmt.format(vb):>10}{change:>10}")
        reasons = sorted(set(sa["reply_reasons"]) | set(sb["reply_reasons"]))
        if reasons:
            print("  rejected replies by reason (A -> B):")
            for r in reasons:
                print(f"     {r:<24}{sa['reply_reasons'].get(r, 0):>5} -> {sb['reply_reasons'].get(r, 0)}")


# -------------------------------------------------------------------------- main


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--label", default="run")
    p.add_argument("--out", default="bench_results/quiz_run.json")
    p.add_argument("--runs", type=int, default=10, help="quizzes per language")
    p.add_argument("--count", type=int, default=5, help="questions per quiz")
    p.add_argument("--subjects", default="7,6", help="comma list; quiz i uses subjects[i %% len]")
    p.add_argument("--languages", default="en,ar")
    p.add_argument("--seed-base", type=int, default=1000)
    p.add_argument("--resume", action="store_true", help="continue an existing --out file")
    p.add_argument("--report", metavar="FILE", help="print the report for FILE and exit")
    p.add_argument("--compare", nargs=2, metavar=("A", "B"), help="compare two result files and exit")
    args = p.parse_args()

    if args.report:
        report([Path(args.report)])
    elif args.compare:
        compare(*args.compare)
    else:
        run_bench(args)


if __name__ == "__main__":
    main()
