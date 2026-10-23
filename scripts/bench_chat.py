"""Time the chat hot path, with no database writes.

    python scripts/bench_chat.py --subject-id 7
    python scripts/bench_chat.py --subject-id 7 --skip-llm   # retrieval only

Reports, in seconds:
  * the pieces of retrieval separately (open collection, Chroma query, rerank),
    for the first call (cold) and calls 2..N (warm)
  * one full chat turn: retrieval + prompt build + the chat model

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

    print("\n(b) one full chat turn (retrieval + prompt + chat model), warm")

    class _Stub:  # _build_messages only reads .messages
        messages: list = []

    def turn():
        chunks = retrieval.retrieve(args.question, args.subject_id)
        messages = chat_controller._build_messages(_Stub(), args.question, chunks)
        t0 = time.perf_counter()
        chat_controller.get_client().chat(
            model=chat_controller.CHAT_MODEL,
            messages=messages,
            options=chat_controller.CHAT_OPTIONS,
        )
        return time.perf_counter() - t0

    total, llm = timed(turn)
    print(f"end to end {fmt(total).strip()}  (model call {fmt(llm).strip()}, "
          f"retrieval+prompt {fmt(total - llm).strip()})")


if __name__ == "__main__":
    main()
