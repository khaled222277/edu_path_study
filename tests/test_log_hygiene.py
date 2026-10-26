"""What the app writes to its logs: no credentials, no student text unless CHAT_TRACE_QUERIES."""

import logging

import pytest

from app import log_redaction
from app.config import settings
from app.controllers import chat_controller as cc
from app.models import ChatSession
from app.services.retrieval import RetrievedChunk

from .conftest import login

CHUNK = RetrievedChunk(text="t", source_filename="dm.pdf", page_number=4, content_type="text",
                       lecture_id=1, subject_id=7, score=0.9)
QUESTION = "my very private question about adaboost"


def test_oauth_codes_and_tokens_are_blanked_in_access_log_lines():
    line = "/auth/google/callback?state=abc123&code=4/0AeanS-secret&scope=email"
    out = log_redaction.redact(line)
    assert "secret" not in out and "abc123" not in out
    assert out == "/auth/google/callback?state=[redacted]&code=[redacted]&scope=email"
    assert log_redaction.redact("/chat/sessions?limit=5") == "/chat/sessions?limit=5"
    assert "[redacted]" in log_redaction.redact("/x?ACCESS_TOKEN=zzz")


def test_the_filter_rewrites_uvicorn_access_arguments():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "GET", "/auth/google/callback?code=SECRETCODE", "1.1", 302), None)
    assert log_redaction.RedactQueryFilter().filter(record)
    assert "SECRETCODE" not in record.getMessage() and "[redacted]" in record.getMessage()
    assert record.args[4] == 302, "non-string arguments are untouched"


def test_the_filter_is_installed_once_on_the_access_logger():
    import app.main  # noqa: F401

    filters = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, log_redaction.RedactQueryFilter)]
    assert len(filters) == 1


@pytest.fixture()
def session(db, world):
    s = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(s)
    db.commit()
    return s


def test_a_full_chat_turn_logs_neither_the_question_nor_credentials(client, world, session, fake_ollama, monkeypatch, caplog):
    monkeypatch.setattr(cc, "retrieve", lambda q, s: [CHUNK])
    monkeypatch.setattr(settings, "chat_followup_rewrite", True)
    from app.auth.jwt import create_access_token

    token = create_access_token(world["alice"].id)
    fake_ollama(default={"message": {"content": "answer [dm.pdf, page 4] [ghost.pdf, page 2]"}})
    c = login(client, world["alice"])
    with caplog.at_level(logging.DEBUG):
        c.post(f"/chat/sessions/{session.id}/messages", json={"content": "first"})
        c.post(f"/chat/sessions/{session.id}/messages", json={"content": QUESTION})  # a follow-up: rewrite runs
    text = caplog.text
    assert "adaboost" not in text.lower() and QUESTION not in text
    assert token not in text and settings.jwt_secret not in text and settings.session_secret not in text
    assert "cookie" not in text.lower()


def test_with_trace_queries_on_the_question_may_appear(client, world, session, fake_ollama, monkeypatch, caplog):
    monkeypatch.setattr(cc, "retrieve", lambda q, s: [CHUNK])
    monkeypatch.setattr(settings, "chat_trace_log", True)
    monkeypatch.setattr(settings, "chat_trace_queries", True)
    fake_ollama(default={"message": {"content": "ok"}})
    with caplog.at_level(logging.INFO):
        login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": QUESTION})
    assert QUESTION in caplog.text
