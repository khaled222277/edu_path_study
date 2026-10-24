"""Compare chat answers before and after a change to how the prompt is built.

    python scripts/eval_chat_prompt.py                       # eval/chat_questions.json, 2 seeds
    python scripts/eval_chat_prompt.py --seeds 42 43 44 --out eval/results/run.json

For each question it retrieves the lecture chunks once (so retrieval cannot differ),
builds the prompt both ways, asks the real chat model, and records the answer, the
prompt/answer token counts and a few automatic quality checks. A person should still
read the answers: the checks only catch the obvious.

  BEFORE  the question is in the history *and* in the context block (the old prompt)
  AFTER   the history holds only earlier turns; the question appears once, in the
          context block (what chat_controller builds now)

Token counts come from Ollama. Its prompt cache would hide most of a repeated system
prompt, so a throwaway request resets the cache before every call.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.controllers import chat_controller as cc  # noqa: E402
from app.services import retrieval  # noqa: E402
from app.services.llm import get_client, keep_alive  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CITATION_RE = re.compile(r"\[([^\]\[]+?),\s*page\s*(\d+)\]", re.IGNORECASE)
ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿ]")
LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


def stub_session(history: list[dict]):
    """The bit of a ChatSession that prompt building reads: `.messages`."""
    return SimpleNamespace(messages=[SimpleNamespace(**m) for m in history])


def build(case: dict, chunks, variant: str) -> list[dict]:
    prior = case.get("history", [])
    if variant == "before":
        # What the old code did: the new question had already been added to the
        # session, so it reached the model once as a history turn ...
        session = stub_session([*prior, {"role": "user", "content": case["question"]}])
        body = (
            cc.USER_TEMPLATE.format(context=retrieval.format_context(chunks), question=case["question"])
            if chunks else cc.NO_CONTEXT_TEMPLATE.format(question=case["question"])
        )
        history = [{"role": m.role, "content": m.content} for m in session.messages[-cc.HISTORY_TURNS * 2:]]
        # ... and once more inside the context block.
        return [{"role": "system", "content": cc.SYSTEM_PROMPT}, *history, {"role": "user", "content": body}]
    # AFTER: the production builder, given the same session (question already added).
    session = stub_session([*prior, {"role": "user", "content": case["question"]}])
    return cc._build_messages(session, case["question"], chunks)


def reset_cache(client) -> None:
    client.chat(model=cc.CHAT_MODEL, messages=[{"role": "user", "content": "reset"}],
                options={"num_predict": 1, "temperature": 0}, keep_alive=keep_alive())


def ask(client, messages: list[dict], seed: int) -> dict:
    reset_cache(client)
    t0 = time.perf_counter()
    r = client.chat(model=cc.CHAT_MODEL, messages=messages,
                    options={**cc.CHAT_OPTIONS, "seed": seed}, keep_alive=keep_alive())
    return {
        "answer": (r["message"]["content"] or "").strip(),
        "prompt_tokens": r.get("prompt_eval_count"),
        "answer_tokens": r.get("eval_count"),
        "seconds": round(time.perf_counter() - t0, 2),
    }


def checks(case: dict, answer: str, chunks) -> dict:
    cited = CITATION_RE.findall(answer)
    files = {c.source_filename for c in chunks}
    letters = LETTER_RE.findall(answer)
    arabic_share = sum(bool(ARABIC_RE.match(ch)) for ch in letters) / len(letters) if letters else 0.0
    not_found = cc.NOT_FOUND_MARKER.lower() in answer.lower()
    terms = case.get("expect_terms", [])
    low = answer.lower()
    hit = sum(any(alt.lower() in low for alt in group) for group in terms)
    unanswerable = case["kind"] == "unanswerable"
    return {
        "cites": bool(cited),
        "citations_real": all(f.strip() in files for f, _ in cited) if cited else None,
        "language_ok": (arabic_share > 0.5) if case["lang"] == "ar" else (arabic_share < 0.2),
        "says_not_found": not_found,
        "not_found_correct": (not_found == unanswerable),
        "terms_hit": f"{hit}/{len(terms)}" if terms else "n/a",
        "terms_all": (hit == len(terms)) if terms else None,
        "empty": not answer,
    }


def worse(before: dict, after: dict) -> list[str]:
    """Automatic checks that got worse going from BEFORE to AFTER."""
    problems = []
    for key in ("language_ok", "not_found_correct"):
        if before[key] and not after[key]:
            problems.append(key)
    if before["cites"] and not after["cites"]:
        problems.append("lost its citations")
    if before["citations_real"] and after["citations_real"] is False:
        problems.append("cites a source it was not given")
    if before["terms_all"] and after["terms_all"] is False:
        problems.append(f"missing expected terms ({after['terms_hit']} vs {before['terms_hit']})")
    if after["empty"] and not before["empty"]:
        problems.append("empty answer")
    return problems


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--questions", default=str(ROOT / "eval" / "chat_questions.json"))
    p.add_argument("--out", default=str(ROOT / "eval" / "results" / "duplicate_question.json"))
    p.add_argument("--seeds", type=int, nargs="+", default=[42, 43])
    p.add_argument("--only", nargs="+", metavar="ID", help="run just these question ids, e.g. q18 q20")
    p.add_argument("--report", metavar="RESULTS.json", help="render a saved run as markdown and exit")
    p.add_argument("--markdown", metavar="OUT.md", help="with --report: write here instead of stdout")
    args = p.parse_args()

    if args.report:
        text = to_markdown(json.loads(Path(args.report).read_text(encoding="utf-8")))
        if args.markdown:
            Path(args.markdown).write_text(text, encoding="utf-8")
            print(f"wrote {args.markdown}")
        else:
            print(text)
        return

    cases = json.loads(Path(args.questions).read_text(encoding="utf-8"))["questions"]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    client = get_client()
    results = []
    for case in cases:
        chunks = retrieval.retrieve(case["question"], case["subject_id"])
        row = {"id": case["id"], "kind": case["kind"], "lang": case["lang"], "question": case["question"],
               "chunks": [c.citation for c in chunks], "runs": {}}
        for variant in ("before", "after"):
            messages = build(case, chunks, variant)
            row["runs"][variant] = []
            for seed in args.seeds:
                out = ask(client, messages, seed)
                out["seed"] = seed
                out["checks"] = checks(case, out["answer"], chunks)
                row["runs"][variant].append(out)
            row["runs"][variant + "_messages"] = len(messages)
        results.append(row)
        b, a = row["runs"]["before"][0], row["runs"]["after"][0]
        print(f"{case['id']} {case['kind']:<12} prompt tokens {b['prompt_tokens']} -> {a['prompt_tokens']}", flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"seeds": args.seeds, "model": cc.CHAT_MODEL, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    summarize(results)
    print(f"\nsaved {out}")


def _flags(c: dict) -> str:
    bits = [
        "cites" if c["cites"] else "NO citation",
        "lang ok" if c["language_ok"] else "WRONG LANGUAGE",
        "says not-found" if c["says_not_found"] else "answers",
        f"terms {c['terms_hit']}",
    ]
    if c["citations_real"] is False:
        bits.append("CITES UNKNOWN SOURCE")
    return ", ".join(bits)


def to_markdown(data: dict) -> str:
    """The run as a readable side-by-side: token counts, checks, and the first seed's answers."""
    results = data["results"]
    seeds = data["seeds"]
    lines = [f"# Chat prompt: BEFORE vs AFTER removing the duplicated question", "",
             f"Model `{data['model']}`, seeds {seeds}. Answers shown are for seed {seeds[0]}. "
             "BEFORE = the question is a bare history turn *and* inside the context block "
             "(the current behaviour). AFTER = only inside the context block.", ""]
    tb = sum(r["runs"]["before"][0]["prompt_tokens"] or 0 for r in results)
    ta = sum(r["runs"]["after"][0]["prompt_tokens"] or 0 for r in results)
    lines += [f"**Prompt tokens, all {len(results)} questions:** {tb} -> {ta} ({100 * (ta - tb) / tb:+.1f}%)", ""]
    lines += ["| id | kind | lang | prompt tokens before -> after | answer tokens before -> after |", "| --- | --- | --- | --- | --- |"]
    for r in results:
        b, a = r["runs"]["before"][0], r["runs"]["after"][0]
        lines.append(f"| {r['id']} | {r['kind']} | {r['lang']} | {b['prompt_tokens']} -> {a['prompt_tokens']} | {b['answer_tokens']} -> {a['answer_tokens']} |")
    lines.append("")
    for r in results:
        b, a = r["runs"]["before"][0], r["runs"]["after"][0]
        lines += [f"## {r['id']} - {r['kind']} ({r['lang']})", "", f"**Q:** {r['question']}", "",
                  f"Retrieved: {', '.join(r['chunks']) or '(nothing)'}", ""]
        for name, x in (("BEFORE", b), ("AFTER", a)):
            lines += [f"**{name}** - {x['prompt_tokens']} prompt / {x['answer_tokens']} answer tokens; {_flags(x['checks'])}", "",
                      "> " + x["answer"].replace("\n", "\n> "), ""]
        problems = [f"seed {sb['seed']}: {', '.join(worse(sb['checks'], sa['checks']))}"
                    for sb, sa in zip(r["runs"]["before"], r["runs"]["after"]) if worse(sb["checks"], sa["checks"])]
        if problems:
            lines += ["*Automatic checks that got worse:* " + "; ".join(problems), ""]
    return "\n".join(lines)


def summarize(results: list[dict]) -> None:
    tb = sum(r["runs"]["before"][0]["prompt_tokens"] or 0 for r in results)
    ta = sum(r["runs"]["after"][0]["prompt_tokens"] or 0 for r in results)
    print(f"\nprompt tokens, all questions (seed 1): {tb} -> {ta}  ({100 * (ta - tb) / tb:+.1f}%)")
    flagged = 0
    for r in results:
        for sb, sa in zip(r["runs"]["before"], r["runs"]["after"]):
            problems = worse(sb["checks"], sa["checks"])
            if problems:
                flagged += 1
                print(f"  WORSE  {r['id']} seed {sb['seed']}: {', '.join(problems)}")
    print(f"automatic checks that got worse: {flagged} of {len(results) * len(results[0]['runs']['before'])} comparisons")


if __name__ == "__main__":
    main()
