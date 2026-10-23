"""Time the chat hot path, with no database writes.

    python scripts/bench_chat.py --subject-id 7
    python scripts/bench_chat.py --subject-id 7 --skip-llm   # retrieval only
    python scripts/bench_chat.py --subject-id 7 --cold-llm   # unload the model first

Reports, in seconds:
  * the pieces of retrieval separately (open collection, Chroma query, rerank),
    for the first call (cold) and calls 2..N (warm)
  * chat turns: retrieval + prompt build + the chat model, streamed, reporting
    time to the first token (what a student waits for before text appears) and
    the total (what they waited for the whole answer before streaming existed).
    --cold-llm unloads the chat model first, so the first turn pays a real load.

It calls the same functions the chat route calls but never touches Postgres, so
it is safe to run against a real deployment. Needs Ollama running (unless
--skip-llm) and an ingested corpus for --subject-id.
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.controllers import chat_controller  # noqa: E402
from app.services import retrieval  # noqa: E402
from app.services.llm import keep_alive  # noqa: E402

QUESTION = "What is data mining and what are its main tasks?"


def timed(fn, *args, **kwargs):
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return time.perf_counter() - start, result


def retrieval_breakdown(question: str, subject_id: int) -> dict[str, float]:
    """One retrieval pass, split into its three stages."""
    t_open, collection = timed(retrieval.get_collection)
    t_query, result = timed(
        collection.query,
        query_texts=[question],
        n_results=retrieval.CANDIDATE_K,
        where={"subject_id": subject_id},
        include=["documents", "metadatas", "distances"],
    )
    passages = [
        {"id": i, "text": d, "meta": m}
        for i, (d, m) in enumerate(zip(result["documents"][0], result["metadatas"][0]))
    ]
    t_rank, _ = timed(
        lambda: retrieval.get_ranker().rerank(
            retrieval.RerankRequest(query=question, passages=passages)
        )
    )
    return {"get_collection": t_open, "chroma_query": t_query, "rerank": t_rank}


def fmt(x: float) -> str:
    return f"{x:7.3f}s"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--subject-id", type=int, required=True)
    parser.add_argument("--question", default=QUESTION)
    parser.add_argument("--warm-calls", type=int, default=4)
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument(
        "--cold-llm",
        action="store_true",
        help="unload the chat model before the first turn, so it measures a real load",
    )
    parser.add_argument("--warm-turns", type=int, default=2, help="turns after the first")
    parser.add_argument(
        "--warm-up-first",
        action="store_true",
        help="run the startup warm-up before call 1, as the API now does at boot",
    )
    args = parser.parse_args()

    print(f"subject {args.subject_id}, question: {args.question!r}\n")

    if args.warm_up_first:
        from app import main as app_main

        t, _ = timed(app_main.warm_up_models)
        print(f"startup warm-up (paid once at boot, off the request path): {fmt(t).strip()}\n")

    runs = [
        retrieval_breakdown(args.question, args.subject_id)
        for _ in range(1 + args.warm_calls)
    ]
    for r in runs:
        r["total"] = sum(r.values())

    print("(a) retrieval path, by stage")
    print(f"{'call':<10}{'get_collection':>16}{'chroma_query':>14}{'rerank':>10}{'total':>10}")
    for i, r in enumerate(runs, start=1):
        label = "1 (cold)" if i == 1 else f"{i} (warm)"
        print(
            f"{label:<10}{fmt(r['get_collection']):>16}{fmt(r['chroma_query']):>14}"
            f"{fmt(r['rerank']):>10}{fmt(r['total']):>10}"
        )
    warm = [r["total"] for r in runs[1:]]
    if warm:
        print(f"\nwarm median {fmt(statistics.median(warm)).strip()}, "
              f"cold {fmt(runs[0]['total']).strip()}")

    if args.skip_llm:
        return

    print("\n(b) chat turns, streamed (retrieval + prompt + chat model)")
    if args.cold_llm:
        unload_chat_model()
        print("    chat model unloaded before turn 1")

    turns = [stream_turn(args.question, args.subject_id) for _ in range(1 + args.warm_turns)]
    print(f"{'turn':<10}{'retrieval':>11}{'model TTFT':>12}{'FIRST TEXT':>12}{'TOTAL':>10}{'chunks':>8}")
    for i, t in enumerate(turns, start=1):
        label = ("1 (cold)" if args.cold_llm else "1 (first)") if i == 1 else f"{i} (warm)"
        first_text = t["retrieval"] + t["ttft"]
        total = t["retrieval"] + t["generation"]
        print(
            f"{label:<10}{fmt(t['retrieval']):>11}{fmt(t['ttft']):>12}"
            f"{fmt(first_text):>12}{fmt(total):>10}{t['chunks']:>8}"
        )
    last = turns[-1]
    print(
        f"\nwarm turn: first text after {fmt(last['retrieval'] + last['ttft']).strip()}; "
        f"without streaming nothing appears until {fmt(last['retrieval'] + last['generation']).strip()}"
    )


def unload_chat_model() -> None:
    """Evict the chat model (keep_alive=0) and wait until Ollama reports it gone."""
    client = chat_controller.get_client()
    client.generate(model=chat_controller.CHAT_MODEL, prompt="", keep_alive=0)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not client.ps().models:
            return
        time.sleep(0.25)
    print("    WARNING: model still resident after 15 s; the 'cold' turn may not be cold")


def stream_turn(question: str, subject_id: int) -> dict[str, float]:
    """One chat turn through the same functions the route uses, streamed."""

    class _Stub:  # _build_messages only reads .messages
        messages: list = []

    t0 = time.perf_counter()
    chunks = retrieval.retrieve(question, subject_id)
    messages = chat_controller._build_messages(_Stub(), question, chunks)
    retrieval_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    ttft = None
    for part in chat_controller.get_client().chat(
        model=chat_controller.CHAT_MODEL,
        messages=messages,
        options=chat_controller.CHAT_OPTIONS,
        keep_alive=keep_alive(),
        stream=True,
    ):
        if ttft is None and part["message"]["content"]:
            ttft = time.perf_counter() - t1
    generation = time.perf_counter() - t1
    return {
        "retrieval": retrieval_s,
        "ttft": ttft if ttft is not None else generation,
        "generation": generation,
        "chunks": len(chunks),
    }


if __name__ == "__main__":
    main()
