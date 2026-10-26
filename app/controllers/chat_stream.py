"""Streaming variant of a chat turn, as newline-delimited JSON events.

Event order on the wire, one JSON object per line:

    {"type": "citations", "citations": [...]}     retrieval is done, sources known
    {"type": "token", "text": "..."}              zero or more, in order
    {"type": "done", "message": {...}, "citations": [...]}
    {"type": "error", "detail": "..."}            instead of "done", mid-stream only

`done.citations` is authoritative: the plain route hides sources when the answer
says "not found in the lecture material", which can only be known once the answer
is complete, so the early `citations` event may be withdrawn by an empty list.

Nothing is stored until the answer is complete. A failure or a client that goes
away mid-answer leaves no partial message behind, matching the plain route, which
stores nothing when the model call fails.

Why async. The plain routes are sync `def`s and run in FastAPI's threadpool, which
is fine for a call that lasts a few seconds. A stream holds its connection for the
whole answer, and a sync generator would pin one of the ~40 worker threads per
active stream, starving every other request. Here the Ollama stream is awaited on
the event loop (no thread held) and only the blocking work -- the database, the
vector search -- is pushed to the threadpool.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator

import anyio
from fastapi import HTTPException, status
from fastapi.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from app.controllers import chat_controller as cc
from app.schemas.chat import MessageOut
from app.services.citations import CitationFilter, Retrieved
from app.services.refusal import RefusalFilter
from app.services.llm import get_async_client, keep_alive
from app.services.retrieval import RetrievedChunk

log = logging.getLogger(__name__)

# Sent to the client on a mid-stream failure. Generic on purpose: the cause is
# logged, never echoed.
STREAM_ERROR_DETAIL = "The answer was interrupted. Please try again."

NDJSON = "application/x-ndjson"


def encode_event(event: dict) -> bytes:
    return (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


citation_payload = cc.citation_payload


async def _aclose(stream) -> None:
    """Close an async generator even while the surrounding task is being cancelled.

    Closing the Ollama stream closes its HTTP connection, which is what makes
    Ollama abandon the generation. Shielded because anyio cancellation is
    level-triggered: an unshielded await here would be cancelled again at once.
    """
    if stream is None:
        return
    with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
        await stream.aclose()


async def open_chat_stream(messages: list[dict], model: str | None = None):
    """Start the model and wait for its first chunk. Returns (stream, first_chunk).

    The first chunk is awaited here, before any response exists, so that "Ollama
    is down" is still a real HTTP 503 -- the same one the plain route gives --
    rather than an error event on a 200. The cost is that the citations event
    leaves with the first token instead of during prompt processing; with the
    model resident that gap is a fraction of a second.
    """
    stream = None
    try:
        stream = await get_async_client().chat(
            model=model or cc.CHAT_MODEL,
            messages=messages,
            options=cc.CHAT_OPTIONS,
            keep_alive=keep_alive(),
            stream=True,
        )
        first = await anext(stream, None)  # None: the model produced nothing at all
    except Exception as exc:
        await _aclose(stream)
        log.exception("chat model call failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=cc.MODEL_UNAVAILABLE_DETAIL,
        ) from exc
    except BaseException:  # cancelled, e.g. server shutdown: still release Ollama
        await _aclose(stream)
        raise
    return stream, first


def _text_of(part) -> str:
    return (part.get("message") or {}).get("content") or ""


async def stream_events(
    turn: cc.PreparedTurn, stream, first_part
) -> AsyncIterator[bytes]:
    """The response body. Yields encoded events; stores the exchange on success only."""
    try:
        yield encode_event({"type": "citations", "citations": citation_payload(turn.chunks)})

        raw: list[str] = []
        # Two filters in a row, so nothing the student must not see ever reaches the
        # browser: the refusal token is removed, then every citation is checked against
        # this turn's retrieved chunks (an invalid one is never sent, a valid one is sent
        # already in its canonical form). Each holds back only what could still change.
        shown = RefusalFilter()
        cited = CitationFilter(Retrieved.from_chunks(turn.chunks))
        emitted = False
        part = first_part
        while part is not None:
            text = _text_of(part)
            if text:
                raw.append(text)
                visible = cited.feed(shown.feed(text))
                if visible:
                    emitted = True
                    yield encode_event({"type": "token", "text": visible})
            part = await anext(stream, None)
        tail = cited.feed(shown.flush()) + cited.flush()
        if tail:
            emitted = True
            yield encode_event({"type": "token", "text": tail})

        parsed = cc.finalize_answer("".join(raw), turn.question, turn.chunks)
        cc.trace_reply(turn.session_id, "".join(raw), parsed, turn.chunks)
        if not emitted:
            # The model said nothing visible (no reply, or only the token): show the
            # same stand-in text the plain route would store.
            yield encode_event({"type": "token", "text": parsed.text})

        # Only now, with a complete answer, touch the database.
        sources = citation_payload(cc.visible_chunks(parsed.refused, turn.chunks))
        reply = await run_in_threadpool(
            cc.persist_exchange, turn.session_id, turn.question, parsed.text, sources
        )
        yield encode_event(
            {
                "type": "done",
                "message": MessageOut.model_validate(reply).model_dump(mode="json"),
                "citations": sources,
            }
        )
    except asyncio.CancelledError:
        log.info("client went away mid-answer; nothing stored")
        raise
    except Exception:
        log.exception("chat stream failed")
        yield encode_event({"type": "error", "detail": STREAM_ERROR_DETAIL})
    finally:
        await _aclose(stream)


class ClosingStreamingResponse(StreamingResponse):
    """StreamingResponse that always closes its body generator, promptly.

    When a client disconnects, Starlette cancels `stream_response`. If that lands
    while the generator is suspended at a `yield` the generator never sees the
    cancellation, and its cleanup (which releases the Ollama call) would wait for
    garbage collection. Closing it here makes the release deterministic.

    `on_close`, if given, runs once the body has ended however it ended. It exists
    for work that must happen even when the body generator never started (a client
    that disconnects before the first chunk leaves an unstarted generator, whose own
    cleanup code never runs).
    """

    def __init__(self, *args, on_close=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.on_close = on_close

    async def stream_response(self, send) -> None:
        try:
            await super().stream_response(send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            with anyio.CancelScope(shield=True):
                if aclose is not None:
                    with contextlib.suppress(Exception):
                        await aclose()
                if self.on_close is not None:
                    with contextlib.suppress(Exception):
                        await self.on_close()


def streaming_response(turn: cc.PreparedTurn, stream, first_part) -> ClosingStreamingResponse:
    return ClosingStreamingResponse(
        stream_events(turn, stream, first_part),
        media_type=NDJSON,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
