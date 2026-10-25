"""CHAT_TRACE_LOG: off by default, one line per stage when on, never the question text unless asked."""

import logging

import pytest

from app.config import settings
from app.controllers import chat_controller as cc
from app.models import ChatSession
from app.services.refusal import REFUSAL_SENTINEL
from app.services.retrieval import RetrievedChunk

from .conftest import login

C1 = RetrievedChunk(text="AdaBoost reweights.", source_filename="dm.pdf", page_number=4,
                    content_type="text", lecture_id=1, subject_id=7, score=0.9)
SECRET_WORDS = "explain adaboost please"


@pytest.fixture(autouse=True)
def one_chunk(monkeypatch):
    monkeypatch.setattr(cc, "retrieve", lambda q, s: [C1])


@pytest.fixture()
def session(db, world):
    s = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(s)
    db.commit()
    return s


def lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("chat-trace")]


def ask(client, world, session, fake_ollama, answer):
    fake_ollama(default={"message": {"content": answer}})
    return login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": SECRET_WORDS})


def test_off_by_default_nothing_is_logged(client, world, session, fake_ollama, caplog):
    assert settings.chat_trace_log is False
    with caplog.at_level(logging.INFO):
        ask(client, world, session, fake_ollama, "AdaBoost reweights [dm.pdf, page 4].")
    assert lines(caplog) == []


def test_on_it_logs_the_retrieval_and_the_reply_without_the_question(client, world, session, fake_ollama, caplog, monkeypatch):
    monkeypatch.setattr(settings, "chat_trace_log", True)
    with caplog.at_level(logging.INFO):
        ask(client, world, session, fake_ollama, "AdaBoost reweights [dm.pdf, page 4] and [dm.pdf, page 99].")
    got = lines(caplog)
    assert len(got) == 2
    assert "chunks=[('dm.pdf', 4)]" in got[0] and "rewritten=False" in got[0]
    assert "citations_written=2" in got[1] and "citations_invalid=1" in got[1] and "sources_shown=1" in got[1]
    assert "refused=False" in got[1]
    assert "adaboost" not in " ".join(got).lower(), "the question must not be logged"


def test_the_question_is_logged_only_when_asked_for(client, world, session, fake_ollama, caplog, monkeypatch):
    monkeypatch.setattr(settings, "chat_trace_log", True)
    monkeypatch.setattr(settings, "chat_trace_queries", True)
    with caplog.at_level(logging.INFO):
        ask(client, world, session, fake_ollama, "ok")
    assert SECRET_WORDS in lines(caplog)[0]


def test_a_refusal_token_inside_a_long_answer_is_visible_in_the_trace(client, world, session, fake_ollama, caplog, monkeypatch):
    """The suspected way an explained answer ends up with no sources: the token is not at the start."""
    monkeypatch.setattr(settings, "chat_trace_log", True)
    answer = "AdaBoost reweights the data and combines weak learners over many rounds. " + REFUSAL_SENTINEL + " The formula is missing."
    with caplog.at_level(logging.INFO):
        r = ask(client, world, session, fake_ollama, answer)
    reply = lines(caplog)[1]
    assert "refused=True" in reply and f"sentinel_at={answer.index(REFUSAL_SENTINEL)}" in reply and "sources_shown=0" in reply
    assert r.json()["citations"] == []


def test_the_stream_route_logs_too(client, world, session, fake_async_ollama, store_in_test_db, caplog, monkeypatch):
    monkeypatch.setattr(settings, "chat_trace_log", True)
    fake_async_ollama(script=["AdaBoost [dm.pdf, ", "page 4]."])
    with caplog.at_level(logging.INFO):
        login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages/stream", json={"content": SECRET_WORDS})
    got = lines(caplog)
    assert len(got) == 2 and "sources_shown=1" in got[1] and "citations_written=1" in got[1]
