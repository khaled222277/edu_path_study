"""Failure paths return clean, non-leaky errors -- never a stack trace or raw cause."""

import httpx
import pytest

from app.models import ChatMessage, ChatSession
from app.rate_limit import limiter
from app.services import quiz_service as qs

from .conftest import login

LEAK = "SECRET-INTERNAL-HOSTNAME:11434"


@pytest.fixture()
def chat_session(db, world):
    session = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(session)
    db.commit()
    return session


def _assert_clean(response, status):
    assert response.status_code == status
    body = response.text
    assert LEAK not in body
    assert "Traceback" not in body
    assert list(response.json()) == ["detail"]


# --- chat -------------------------------------------------------------------


def test_chat_ollama_down_returns_503(client, db, world, chat_session, fake_ollama, monkeypatch):
    monkeypatch.setattr("app.controllers.chat_controller.retrieve", lambda q, s: [])
    fake_ollama(default=ConnectionError(LEAK))
    r = login(client, world["alice"]).post(
        f"/chat/sessions/{chat_session.id}/messages", json={"content": "hello"}
    )
    _assert_clean(r, 503)
    # The unanswered question is rolled back, not left dangling in the history.
    assert db.query(ChatMessage).count() == 0


def test_chat_ollama_timeout_returns_503(client, world, chat_session, fake_ollama, monkeypatch):
    monkeypatch.setattr("app.controllers.chat_controller.retrieve", lambda q, s: [])
    fake_ollama(default=httpx.ReadTimeout(LEAK))
    r = login(client, world["alice"]).post(
        f"/chat/sessions/{chat_session.id}/messages", json={"content": "hello"}
    )
    _assert_clean(r, 503)


def test_chat_retrieval_failure_returns_503(client, world, chat_session, fake_ollama, monkeypatch):
    def broken(query, subject_id):
        raise RuntimeError(LEAK)

    monkeypatch.setattr("app.controllers.chat_controller.retrieve", broken)
    fake = fake_ollama()
    r = login(client, world["alice"]).post(
        f"/chat/sessions/{chat_session.id}/messages", json={"content": "hello"}
    )
    _assert_clean(r, 503)
    assert fake.calls == 0


def test_chat_with_no_retrieved_chunks_still_answers(client, world, chat_session, fake_ollama, monkeypatch):
    monkeypatch.setattr("app.controllers.chat_controller.retrieve", lambda q, s: [])
    fake_ollama(default={"message": {"content": "That is not found in the lecture material."}})
    r = login(client, world["alice"]).post(
        f"/chat/sessions/{chat_session.id}/messages", json={"content": "hello"}
    )
    assert r.status_code == 200
    assert r.json()["citations"] == []


# --- quiz -------------------------------------------------------------------

_CHUNK = qs.SourceChunk(
    text="x" * 300, source_filename="dm.pdf", page_number=1,
    content_type="text", lecture_id=1,
)


def _start(client, world):
    return login(client, world["alice"]).post(
        "/quiz/start", json={"subject_id": world["subject"].id, "question_count": 2}
    )


def test_quiz_ollama_down_returns_503_fast(client, world, fake_ollama, monkeypatch):
    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: [_CHUNK] * 4)
    fake = fake_ollama(default=ConnectionError(LEAK))
    _assert_clean(_start(client, world), 503)
    assert fake.calls == 1  # aborted at once, not 12 retries x 4 chunks


def test_quiz_retries_exhausted_returns_503(client, world, fake_ollama, monkeypatch):
    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: [_CHUNK])
    fake_ollama(default={"response": "I cannot comply"})
    _assert_clean(_start(client, world), 503)


def test_quiz_chroma_failure_returns_503(client, world, fake_ollama, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError(LEAK)

    monkeypatch.setattr(qs, "sample_chunks", broken)
    fake_ollama()
    _assert_clean(_start(client, world), 503)


def test_quiz_with_no_lecture_material_is_422(client, world, fake_ollama, monkeypatch):
    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: [])
    fake_ollama()
    r = _start(client, world)
    assert r.status_code == 422
    assert "lecture material" in r.json()["detail"]


# --- rate limiting ----------------------------------------------------------


@pytest.fixture()
def rate_limited():
    limiter.reset()
    limiter.enabled = True
    yield
    limiter.enabled = False
    limiter.reset()


def test_auth_endpoint_is_rate_limited_per_ip(client, rate_limited):
    codes = [client.get("/auth/google/login").status_code for _ in range(12)]
    # Google is unconfigured in tests -> 503 until the limiter (10/min) trips.
    assert codes[:10] == [503] * 10
    assert codes[10:] == [429, 429]
    r = client.get("/auth/google/login")
    assert r.json() == {"detail": "Too many requests. Please slow down and try again shortly."}
    assert "retry-after" in r.headers


def test_quiz_start_is_rate_limited_per_user(client, world, rate_limited, fake_ollama, monkeypatch):
    monkeypatch.setattr(qs, "sample_chunks", lambda *a, **k: [])
    fake_ollama()
    alice = [_start(client, world).status_code for _ in range(6)]
    assert alice[:5] == [422] * 5 and alice[5] == 429
    # A different student has their own budget.
    assert login(client, world["bob"]).post(
        "/quiz/start", json={"subject_id": world["subject"].id}
    ).status_code == 422
