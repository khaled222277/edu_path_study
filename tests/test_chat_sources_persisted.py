"""Sources are stored with each assistant reply, so a reopened chat shows them again."""

import json

import pytest

from app.controllers import chat_controller as cc
from app.models import ChatMessage, ChatSession
from app.services.refusal import REFUSAL_SENTINEL
from app.services.retrieval import RetrievedChunk

from .conftest import login

C1 = RetrievedChunk(text="Support.", source_filename="dm.pdf", page_number=4, content_type="text",
                    lecture_id=1, subject_id=7, score=0.9)
C2 = RetrievedChunk(text="Figure of a tree.", source_filename="dm.pdf", page_number=7,
                    content_type="image_description", lecture_id=1, subject_id=7, score=0.8,
                    image_path="1/fig7.png")


@pytest.fixture(autouse=True)
def two_chunks(monkeypatch):
    monkeypatch.setattr(cc, "retrieve", lambda q, s: [C1, C2])


@pytest.fixture()
def session(db, world):
    s = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(s)
    db.commit()
    return s


def events(r):
    return [json.loads(x) for x in r.text.splitlines() if x.strip()]


def reload(client, world, session):
    return login(client, world["alice"]).get(f"/chat/sessions/{session.id}").json()["messages"]


def test_plain_reply_sources_are_returned_when_the_session_is_reopened(client, world, session, fake_ollama):
    fake_ollama(default={"message": {"content": "Support is a ratio [dm.pdf, page 4]."}})
    body = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": "q?"}).json()
    msgs = reload(client, world, session)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["citations"] == []
    assert msgs[1]["citations"] == body["citations"] and len(body["citations"]) == 2
    c = {x["page_number"]: x for x in msgs[1]["citations"]}
    assert c[7]["content_type"] == "image_description" and c[7]["image_url"], "the figure reference is kept"
    assert c[4]["image_url"] is None


def test_stream_stores_exactly_the_list_the_done_event_returned(client, world, session, fake_async_ollama, store_in_test_db):
    fake_async_ollama(script=["Support [dm.pdf, page 4] and [ghost.pdf, page 1]."])
    r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages/stream", json={"content": "q?"})
    done = events(r)[-1]
    assert done["type"] == "done"
    msgs = reload(client, world, session)
    assert msgs[-1]["citations"] == done["citations"] and len(done["citations"]) == 2
    assert done["message"]["citations"] == done["citations"], "the message in the done event carries them too"


def test_plain_and_stream_store_the_same_list(client, db, world, session, fake_ollama, fake_async_ollama, store_in_test_db):
    answer = "A [dm.pdf, page 4] B [x.pdf, page 1]."
    fake_ollama(default={"message": {"content": answer}})
    login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": "q?"})
    other = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(other)
    db.commit()
    fake_async_ollama(script=[answer])
    login(client, world["alice"]).post(f"/chat/sessions/{other.id}/messages/stream", json={"content": "q?"})
    db.expire_all()
    a = [m for m in db.get(ChatSession, session.id).messages if m.role == "assistant"][0]
    b = [m for m in db.get(ChatSession, other.id).messages if m.role == "assistant"][0]
    assert a.sources == b.sources and len(a.sources) == 2


def test_a_refusal_stores_no_sources_on_either_route(client, db, world, session, fake_ollama, fake_async_ollama, store_in_test_db):
    fake_ollama(default={"message": {"content": f"{REFUSAL_SENTINEL} Not covered."}})
    login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": "q?"})
    fake_async_ollama(script=[f"{REFUSAL_SENTINEL} Not covered."])
    login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages/stream", json={"content": "q2?"})
    assert [m["citations"] for m in reload(client, world, session) if m["role"] == "assistant"] == [[], []]


def test_replies_stored_before_the_column_existed_come_back_with_an_empty_list(client, db, world, session):
    db.add(ChatMessage(session_id=session.id, role="assistant", content="old"))  # sources left NULL
    db.commit()
    assert reload(client, world, session)[0]["citations"] == []


def test_another_students_session_still_cannot_be_read(client, world, session):
    r = login(client, world["bob"]).get(f"/chat/sessions/{session.id}")
    assert r.status_code in (403, 404)
