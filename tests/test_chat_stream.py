"""POST /chat/sessions/{id}/messages/stream: event order, errors, auth, limits, cleanup."""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from app.auth.jwt import create_access_token
from app.config import settings
from app.controllers import chat_controller as cc
from app.controllers import chat_stream
from app.db.session import get_db
from app.main import app
from app.models import ChatMessage, ChatSession
from app.models.chat import ROLE_ASSISTANT, ROLE_USER
from app.services.retrieval import RetrievedChunk

from .conftest import login
from .test_error_handling import LEAK, rate_limited  # noqa: F401  (fixture)

CHUNK = RetrievedChunk(
    text="Clustering groups similar objects.", source_filename="dm.pdf", page_number=4,
    content_type="text", lecture_id=1, subject_id=7, score=0.93,
)
IMAGE_CHUNK = RetrievedChunk(
    text="A flowchart.", source_filename="dm.pdf", page_number=9,
    content_type="image_description", lecture_id=1, subject_id=7, score=0.8,
    image_path="1/page_9_0.png",
)


@pytest.fixture()
def chat_session(db, world):
    session = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(session)
    db.commit()
    return session


@pytest.fixture(autouse=True)
def retrieval_returns(monkeypatch):
    """Retrieval is faked; individual tests can override."""
    monkeypatch.setattr(cc, "retrieve", lambda q, s: [CHUNK])


def url(session):
    return f"/chat/sessions/{session.id}/messages/stream"


def events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def stored(db):
    db.expire_all()
    return db.query(ChatMessage).order_by(ChatMessage.id).all()


def post(client, user, session, content="what is clustering?"):
    return login(client, user).post(url(session), json={"content": content})


# --- the happy path -----------------------------------------------------------


def test_event_order_is_citations_then_tokens_then_done(
    client, world, chat_session, fake_async_ollama, store_in_test_db
):
    fake_async_ollama(script=["Clus", "tering ", "groups ", "things."])
    r = post(client, world["alice"], chat_session)

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert r.headers["cache-control"] == "no-cache"
    evs = events(r)
    assert [e["type"] for e in evs] == ["citations", "token", "token", "token", "token", "done"]

    assert evs[0]["citations"] == [{
        "source_filename": "dm.pdf", "page_number": 4, "content_type": "text",
        "score": 0.93, "image_url": None,
    }]
    assert "".join(e["text"] for e in evs[1:5]) == "Clustering groups things."
    done = evs[-1]
    assert done["message"]["role"] == "assistant"
    assert done["message"]["content"] == "Clustering groups things."
    assert done["citations"] == evs[0]["citations"]


def test_exchange_is_stored_once_after_completion(
    client, db, world, chat_session, fake_async_ollama, store_in_test_db
):
    fake_async_ollama(script=["Hello ", "there."])
    evs = events(post(client, world["alice"], chat_session, "hi?"))

    rows = stored(db)
    assert [(m.role, m.content) for m in rows] == [
        (ROLE_USER, "hi?"), (ROLE_ASSISTANT, "Hello there."),
    ]
    assert evs[-1]["message"]["id"] == rows[1].id  # the id the client is told about


def test_figure_citations_carry_their_image_url(
    client, world, chat_session, fake_async_ollama, store_in_test_db, monkeypatch
):
    monkeypatch.setattr(cc, "retrieve", lambda q, s: [IMAGE_CHUNK])
    fake_async_ollama(script=["ok"])
    evs = events(post(client, world["alice"], chat_session))
    assert evs[0]["citations"][0]["image_url"] == "/lectures/images/1/page_9_0.png"


def test_arabic_text_survives_the_wire_intact(
    client, world, chat_session, fake_async_ollama, store_in_test_db
):
    fake_async_ollama(script=["التجميع ", "هو ", "تجميع الكائنات."])
    r = post(client, world["alice"], chat_session, "ما هو التجميع؟")
    assert "التجميع" in r.content.decode("utf-8")  # not \\u-escaped
    assert events(r)[-1]["message"]["content"] == "التجميع هو تجميع الكائنات."


def test_no_retrieval_results_behaves_like_the_plain_route(
    client, world, chat_session, fake_async_ollama, store_in_test_db, monkeypatch
):
    monkeypatch.setattr(cc, "retrieve", lambda q, s: [])
    fake = fake_async_ollama(script=["I do not know."])
    evs = events(post(client, world["alice"], chat_session))

    assert evs[0] == {"type": "citations", "citations": []}
    assert "no relevant lecture material" in fake.kwargs[0]["messages"][-1]["content"]
    assert evs[-1]["type"] == "done" and evs[-1]["citations"] == []


def test_not_found_answer_withdraws_the_citations_in_done(
    client, world, chat_session, fake_async_ollama, store_in_test_db
):
    """The plain route hides sources under a 'not found' answer; done is authoritative."""
    fake_async_ollama(script=["That is ", cc.NOT_FOUND_MARKER, "."])
    evs = events(post(client, world["alice"], chat_session))
    assert evs[0]["citations"]  # shown early, before the answer is known
    assert evs[-1]["citations"] == []  # withdrawn once it is


def test_an_empty_model_reply_gets_the_same_fallback_as_the_plain_route(
    client, db, world, chat_session, fake_async_ollama, store_in_test_db
):
    fake_async_ollama(script=[])
    evs = events(post(client, world["alice"], chat_session))
    fallback = cc.finalize_answer("", "what?").text
    assert [e["type"] for e in evs] == ["citations", "token", "done"]
    assert evs[1]["text"] == fallback
    assert stored(db)[-1].content == fallback


def test_prompt_matches_the_plain_route_exactly(
    client, db, world, fake_ollama, fake_async_ollama, store_in_test_db
):
    """Same history, same question -> byte-identical model request on both routes."""
    sessions = []
    for _ in range(2):
        s = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
        db.add(s)
        db.flush()
        db.add(ChatMessage(session_id=s.id, role=ROLE_USER, content="earlier question"))
        db.add(ChatMessage(session_id=s.id, role=ROLE_ASSISTANT, content="earlier answer"))
        sessions.append(s)
    db.commit()

    plain = fake_ollama(default={"message": {"content": "x"}})
    streamed = fake_async_ollama(script=["x"])
    c = login(client, world["alice"])
    assert c.post(f"/chat/sessions/{sessions[0].id}/messages", json={"content": "q?"}).status_code == 200
    assert c.post(url(sessions[1]), json={"content": "q?"}).status_code == 200

    p, s = plain.kwargs[0], streamed.kwargs[0]
    assert p["messages"] == s["messages"]
    assert p["options"] == s["options"] and p["model"] == s["model"]
    assert p["keep_alive"] == s["keep_alive"]
    assert s["stream"] is True


# --- auth, ownership, limits --------------------------------------------------


def test_missing_cookie_is_401(client, chat_session, fake_async_ollama):
    fake = fake_async_ollama(script=["x"])
    client.cookies.clear()
    assert client.post(url(chat_session), json={"content": "hi"}).status_code == 401
    assert fake.kwargs == []


def test_other_students_session_is_404_and_untouched(
    client, db, world, chat_session, fake_async_ollama, store_in_test_db
):
    fake = fake_async_ollama(script=["x"])
    r = post(client, world["bob"], chat_session)
    assert r.status_code == 404
    assert fake.kwargs == [] and stored(db) == []


def test_unknown_session_is_404(client, world, fake_async_ollama):
    fake_async_ollama(script=["x"])
    r = login(client, world["alice"]).post("/chat/sessions/99999/messages/stream", json={"content": "hi"})
    assert r.status_code == 404


def test_empty_message_is_rejected_by_validation(client, world, chat_session, fake_async_ollama):
    fake_async_ollama(script=["x"])
    r = login(client, world["alice"]).post(url(chat_session), json={"content": ""})
    assert r.status_code == 422


def test_rate_limit_applies_and_is_shared_with_the_plain_route(
    client, world, chat_session, fake_async_ollama, fake_ollama, store_in_test_db, rate_limited  # noqa: F811
):
    """20/min in total across both routes, not 20 each."""
    fake_async_ollama(script=["x"])
    fake_ollama(default={"message": {"content": "x"}})
    c = login(client, world["alice"])
    plain = f"/chat/sessions/{chat_session.id}/messages"

    codes = []
    for i in range(20):
        path = url(chat_session) if i % 2 == 0 else plain
        codes.append(c.post(path, json={"content": "hi"}).status_code)
    assert codes == [200] * 20

    over_stream = c.post(url(chat_session), json={"content": "hi"})
    over_plain = c.post(plain, json={"content": "hi"})
    assert over_stream.status_code == over_plain.status_code == 429
    assert over_stream.json() == {"detail": "Too many requests. Please slow down and try again shortly."}


# --- failures before the first token: a real HTTP error ----------------------


def _assert_clean_503(r):
    assert r.status_code == 503
    assert list(r.json()) == ["detail"]
    assert LEAK not in r.text and "Traceback" not in r.text


def test_ollama_down_before_first_token_is_a_clean_503(
    client, db, world, chat_session, fake_async_ollama, store_in_test_db
):
    fake_async_ollama(fail_on_open=ConnectionError(LEAK))
    r = post(client, world["alice"], chat_session)
    _assert_clean_503(r)
    assert r.json()["detail"] == cc.MODEL_UNAVAILABLE_DETAIL  # same text as the plain route
    assert stored(db) == []


def test_error_on_the_first_chunk_is_also_a_503(
    client, db, world, chat_session, fake_async_ollama, store_in_test_db
):
    fake = fake_async_ollama(script=[ConnectionError(LEAK)])
    r = post(client, world["alice"], chat_session)
    _assert_clean_503(r)
    assert stored(db) == [] and fake.closed


def test_chroma_failure_is_a_clean_503_and_never_reaches_the_model(
    client, db, world, chat_session, fake_async_ollama, store_in_test_db, monkeypatch
):
    def broken(q, s):
        raise RuntimeError(LEAK)

    monkeypatch.setattr(cc, "retrieve", broken)
    fake = fake_async_ollama(script=["x"])
    r = post(client, world["alice"], chat_session)
    _assert_clean_503(r)
    assert r.json()["detail"] == cc.SEARCH_UNAVAILABLE_DETAIL
    assert fake.kwargs == [] and stored(db) == []


# --- failures mid-stream: an in-band error event -----------------------------


def test_error_mid_stream_sends_a_generic_error_event_and_stores_nothing(
    client, db, world, chat_session, fake_async_ollama, store_in_test_db
):
    fake = fake_async_ollama(script=["Clus", "tering", httpx.ReadTimeout(LEAK), "never"])
    r = post(client, world["alice"], chat_session)

    assert r.status_code == 200  # headers were already sent
    evs = events(r)
    assert [e["type"] for e in evs] == ["citations", "token", "token", "error"]
    assert evs[-1] == {"type": "error", "detail": chat_stream.STREAM_ERROR_DETAIL}
    assert LEAK not in r.text and "Traceback" not in r.text and "ReadTimeout" not in r.text
    assert stored(db) == []  # neither the question nor the partial answer
    assert fake.closed and fake.consumed == 2  # stopped at the failure


def test_failure_while_storing_is_an_error_event_not_a_done(
    client, db, world, chat_session, fake_async_ollama, store_in_test_db, monkeypatch
):
    def broken(*a, **k):
        raise RuntimeError(LEAK)

    monkeypatch.setattr(cc, "persist_exchange", broken)
    fake_async_ollama(script=["fine answer"])
    r = post(client, world["alice"], chat_session)
    evs = events(r)
    assert evs[-1]["type"] == "error" and "done" not in [e["type"] for e in evs]
    assert LEAK not in r.text


# --- the client goes away ------------------------------------------------------


def _scope(path, cookie, body):
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "scheme": "http",
        "headers": [
            (b"content-type", b"application/json"), (b"host", b"test"),
            (b"cookie", cookie.encode()),
        ],
        "client": ("127.0.0.1", 5000), "server": ("test", 80),
    }


async def _drive(path, cookie, body, *, disconnect_after=None, probe=None):
    """Run the real app over raw ASGI so a client disconnect can be simulated.

    Starlette's TestClient buffers the whole response, so it cannot do this.
    `probe()` is evaluated the instant the app returns, while the event loop is
    still running: asyncio.run() closes every leftover async generator on the way
    out, which would make an unclosed Ollama stream look closed.
    """
    sent, disconnect, delivered = [], asyncio.Event(), False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        chunks = sum(1 for m in sent if m["type"] == "http.response.body" and m.get("body"))
        if disconnect_after is not None and chunks >= disconnect_after:
            disconnect.set()

    await app(_scope(path, cookie, body), receive, send)
    return sent, (probe() if probe else None)


def _cookie(user):
    return f"{settings.cookie_name}={create_access_token(user.id)}"


def test_client_disconnect_mid_answer_stops_generation_and_stores_nothing(
    client, db, world, chat_session, fake_async_ollama, store_in_test_db
):
    script = [f"tok{i} " for i in range(200)]
    fake = fake_async_ollama(script=script, delay=0.005)

    sent, closed_on_return = asyncio.run(_drive(
        url(chat_session), _cookie(world["alice"]),
        json.dumps({"content": "hi"}).encode(), disconnect_after=3,
        probe=lambda: fake.closed,
    ))

    assert closed_on_return, "the Ollama stream must be closed at once so Ollama stops generating"
    assert fake.consumed < len(script) // 2, "generation must stop, not run to the end"
    assert stored(db) == [], "nothing is stored for an abandoned answer"
    assert not any(b"done" in m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def test_closing_response_closes_the_generator_even_when_send_fails(db, world):
    """If the connection dies at a yield, the generator never sees the cancellation."""
    from .conftest import FakeAsyncOllama

    fake = FakeAsyncOllama(script=[f"t{i}" for i in range(50)])
    turn = cc.PreparedTurn(1, "q", [], [])

    async def go():
        stream = await fake.chat()
        first = await anext(stream)
        response = chat_stream.streaming_response(turn, stream, first)
        calls = 0

        async def send(message):
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise OSError("connection reset")

        async def receive():
            await asyncio.sleep(10)

        # Starlette runs the response in a task group, so the OSError arrives wrapped.
        with pytest.raises(ExceptionGroup) as exc:
            await response({"type": "http"}, receive, send)
        assert exc.group_contains(OSError)
        return fake.closed  # read now, before asyncio.run() closes stragglers

    closed_on_return = asyncio.run(go())
    assert closed_on_return, "the response must close the generator itself"
    assert fake.consumed < 50


def test_closing_the_event_generator_directly_releases_the_stream(
    db, world, store_in_test_db
):
    from .conftest import FakeAsyncOllama

    fake = FakeAsyncOllama(script=["a", "b", "c", "d"])
    turn = cc.PreparedTurn(1, "q", [], [])

    async def go():
        stream = await fake.chat()
        gen = chat_stream.stream_events(turn, stream, await anext(stream))
        await anext(gen)  # citations
        await anext(gen)  # first token
        await gen.aclose()
        return fake.closed  # read now, before asyncio.run() closes stragglers

    assert asyncio.run(go()), "closing the generator must close the Ollama stream"
    assert stored(db) == []


# --- it must not block other requests ------------------------------------------


def test_many_concurrent_streams_do_not_starve_other_requests(monkeypatch):
    """60 streams (more than the ~40 worker threads) and /health still answers at once.

    A sync generator would pin a worker thread per stream; this one is awaited on
    the event loop, so health stays prompt while every stream is mid-answer.
    """
    from .conftest import FakeAsyncOllama

    mini = FastAPI()
    fake = FakeAsyncOllama(script=[f"t{i} " for i in range(25)], delay=0.02)
    monkeypatch.setattr(chat_stream, "get_async_client", lambda timeout=None: fake)
    monkeypatch.setattr(cc, "persist_exchange", lambda *a, **k: ChatMessage(
        id=1, session_id=1, role=ROLE_ASSISTANT, content="x",
        created_at=__import__("datetime").datetime(2026, 1, 1),
    ))

    @mini.get("/health")
    def health():
        return {"status": "ok"}

    @mini.post("/stream")
    async def stream_route():
        turn = cc.PreparedTurn(1, "q", [], [])
        stream, first = await chat_stream.open_chat_stream(turn.messages)
        return chat_stream.streaming_response(turn, stream, first)

    async def go():
        import time

        transport = httpx.ASGITransport(app=mini)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            streams = [asyncio.create_task(c.post("/stream")) for _ in range(60)]
            await asyncio.sleep(0.15)  # let them all get going
            t0 = time.perf_counter()
            health = await c.get("/health")
            health_latency = time.perf_counter() - t0
            results = await asyncio.gather(*streams)
        return health, health_latency, results

    health, latency, results = asyncio.run(go())
    assert health.status_code == 200
    assert latency < 0.25, f"health took {latency:.2f}s while 60 streams were running"
    assert all(r.status_code == 200 for r in results)
