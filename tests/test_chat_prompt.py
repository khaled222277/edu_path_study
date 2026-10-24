"""The question being answered is sent twice ON PURPOSE. Do not "fix" it without re-running the eval.

Both chat routes put the student's question in the history (the session already holds
it when the prompt is built) and again inside the context block. It looks like waste --
and removing it was tried -- but eval/README.md shows what the bare turn is for: it
anchors a follow-up to the previous answer and tells the model which language to reply
in. Without it, over 8 seeds each:

  * an Arabic out-of-scope question was answered in English 8 times out of 8 (0/8 before),
  * an English follow-up ("explain that again") lost the thread 4 times out of 8,
  * an Arabic follow-up lost it 2 times out of 8,

to save about 12 prompt tokens (1.5%). These tests pin the shape so a change has to be
deliberate: if you change it, run scripts/eval_chat_prompt.py first and update them.
"""

from types import SimpleNamespace

import pytest

from app.controllers import chat_controller as cc
from app.models import ChatMessage
from app.models.chat import ROLE_ASSISTANT, ROLE_USER
from app.services.retrieval import RetrievedChunk

from .conftest import login
from .test_error_handling import chat_session  # noqa: F401  (fixture)

CHUNK = RetrievedChunk(
    text="Clustering groups similar objects.", source_filename="dm.pdf", page_number=4,
    content_type="text", lecture_id=1, subject_id=7, score=0.9,
)
Q = "what is clustering?"


@pytest.fixture(autouse=True)
def retrieval_returns(monkeypatch):
    monkeypatch.setattr(cc, "retrieve", lambda q, s: [CHUNK])


def turn(db, session, role, content):
    db.add(ChatMessage(session_id=session.id, role=role, content=content))
    db.commit()


def prompt_of(fake):
    return fake.kwargs[0]["messages"]


def session_with(*pairs):
    return SimpleNamespace(messages=[SimpleNamespace(role=r, content=c) for r, c in pairs])


# --- through the real routes -------------------------------------------------------------


def test_the_question_is_a_bare_turn_and_again_inside_the_context_block(client, world, chat_session, fake_ollama):
    fake = fake_ollama(default={"message": {"content": "an answer"}})
    r = login(client, world["alice"]).post(f"/chat/sessions/{chat_session.id}/messages", json={"content": Q})
    assert r.status_code == 200
    messages = prompt_of(fake)
    assert [m["role"] for m in messages] == ["system", "user", "user"]
    assert messages[1]["content"] == Q, "the bare turn: anchor and language cue"
    assert "CONTEXT:" in messages[2]["content"] and Q in messages[2]["content"]


def test_a_follow_up_keeps_the_earlier_exchange_and_the_bare_question_after_it(
    client, db, world, chat_session, fake_ollama
):
    turn(db, chat_session, ROLE_USER, "what is k-means?")
    turn(db, chat_session, ROLE_ASSISTANT, "K-means groups points around centroids.")
    fake = fake_ollama(default={"message": {"content": "simpler"}})
    login(client, world["alice"]).post(
        f"/chat/sessions/{chat_session.id}/messages", json={"content": "explain that again"}
    )
    messages = prompt_of(fake)
    assert [(m["role"], m["content"]) for m in messages[1:-1]] == [
        ("user", "what is k-means?"),
        ("assistant", "K-means groups points around centroids."),
        ("user", "explain that again"),  # directly after the answer it refers to
    ]
    assert "CONTEXT:" in messages[-1]["content"] and "explain that again" in messages[-1]["content"]


def test_the_stream_route_builds_the_same_prompt(
    client, db, world, chat_session, fake_ollama, fake_async_ollama, store_in_test_db
):
    from app.models import ChatSession

    other = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(other)
    db.commit()
    for session in (chat_session, other):
        turn(db, session, ROLE_USER, "q1")
        turn(db, session, ROLE_ASSISTANT, "a1")
    plain = fake_ollama(default={"message": {"content": "x"}})
    streamed = fake_async_ollama(script=["x"])

    c = login(client, world["alice"])
    c.post(f"/chat/sessions/{chat_session.id}/messages", json={"content": "q2"})
    c.post(f"/chat/sessions/{other.id}/messages/stream", json={"content": "q2"})
    assert prompt_of(plain) == streamed.kwargs[0]["messages"]


# --- the builder itself ----------------------------------------------------------------------


def test_the_history_window_includes_the_current_question():
    n = cc.HISTORY_TURNS * 2
    pairs = [("user" if i % 2 == 0 else "assistant", f"m{i}") for i in range(n + 6)]
    history = cc._build_messages(session_with(*pairs, ("user", Q)), Q, [CHUNK])[1:-1]
    assert len(history) == n
    assert history[-1]["content"] == Q and history[0]["content"] == "m7"


def test_a_context_free_turn_is_built_the_same_way():
    messages = cc._build_messages(session_with(("user", Q)), Q, [])
    assert [m["role"] for m in messages] == ["system", "user", "user"]
    assert messages[1]["content"] == Q and "no relevant lecture material" in messages[2]["content"]


def test_an_empty_session_is_fine():
    assert [m["role"] for m in cc._build_messages(session_with(), Q, [CHUNK])] == ["system", "user"]
