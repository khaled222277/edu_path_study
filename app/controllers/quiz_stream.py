"""A quiz delivered question by question, as newline-delimited JSON events.

Event order on the wire, one JSON object per line:

    {"type": "start", "attempt_id", "subject_id", "language", "requested"}
    {"type": "question", "index": 0, "question": {id, question_text, options, source_citation}}
    {"type": "question", "index": 1, ...}                       one per validated question
    {"type": "done", "attempt_id", "total_questions", "requested", "partial"}
    {"type": "error", "detail", "attempt_id", "total_questions", "partial": true}

`done.partial` is true when fewer than `requested` questions could be written
(the lecture material ran out of usable chunks). `error` replaces `done` when
generation broke off part-way -- Ollama died, say. Either way the questions already
sent are stored and answerable, and total_questions is how many there are: a failure
on one question never discards the ones before it. A failure before the first
question is not an event at all but the ordinary HTTP 503 (see begin_streamed_quiz).

A student who stops, or whose connection drops, keeps what was generated so far.

Why async, and what runs where: see app/controllers/chat_stream.py. The model calls
are blocking, so each is pushed to the threadpool; the worker is held only for the
length of one call, never for the whole stream.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

import anyio
from fastapi.concurrency import run_in_threadpool

from app.controllers import quiz_controller as qc
from app.controllers.chat_stream import NDJSON, ClosingStreamingResponse, encode_event
from app.schemas.quiz import QuizQuestionOut

log = logging.getLogger(__name__)

# Sent on a mid-stream failure. Generic on purpose: the cause is logged only.
QUIZ_STREAM_ERROR_DETAIL = "Some questions could not be generated. You can continue with the ones below."


def _question_event(index: int, payload: dict) -> bytes:
    return encode_event(
        {"type": "question", "index": index, "question": QuizQuestionOut(**payload).model_dump()}
    )


def _next_question(iterator):
    return next(iterator, None)


async def stream_events(started: qc.StreamedQuiz) -> AsyncIterator[bytes]:
    """The response body. Stores each question as it arrives; settles the quiz at the end."""
    kept: tuple[int, bool] | None = None
    try:
        yield encode_event(
            {
                "type": "start",
                "attempt_id": started.attempt_id,
                "subject_id": started.subject_id,
                "language": started.language,
                "requested": started.requested,
            }
        )
        # Control only returns here once that chunk has been sent, so from this point
        # the client has seen the quiz and it is worth keeping.
        started.delivered = True
        yield _question_event(0, started.first)

        index = 1
        while True:
            question = await run_in_threadpool(_next_question, started.questions)
            if question is None:
                break
            stored = await run_in_threadpool(qc.add_streamed_item, started.attempt_id, question)
            if stored is None:  # the student closed the quiz while it was being written
                break
            started.produced = index + 1
            yield _question_event(index, stored)
            index += 1

        kept = await run_in_threadpool(started.close)
        total, partial = kept if kept is not None else (index, index < started.requested)
        yield encode_event(
            {
                "type": "done",
                "attempt_id": started.attempt_id,
                "total_questions": total,
                "requested": started.requested,
                "partial": partial,
            }
        )
    except asyncio.CancelledError:
        log.info("client went away mid-quiz; keeping %s question(s)", started.produced)
        raise
    except Exception:
        log.exception("quiz stream failed")
        kept = await run_in_threadpool(started.close)
        total = kept[0] if kept is not None else started.produced
        yield encode_event(
            {
                "type": "error",
                "detail": QUIZ_STREAM_ERROR_DETAIL,
                "attempt_id": started.attempt_id,
                "total_questions": total,
                "partial": True,
            }
        )
    finally:
        # Idempotent: a no-op if close() already ran above.
        with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
            await run_in_threadpool(started.close)


def streaming_response(started: qc.StreamedQuiz) -> ClosingStreamingResponse:
    async def settle() -> None:
        # Covers a body generator that never started (client gone before the first
        # chunk), whose own `finally` can never run.
        await run_in_threadpool(started.close)

    return ClosingStreamingResponse(
        stream_events(started),
        media_type=NDJSON,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        on_close=settle,
    )
