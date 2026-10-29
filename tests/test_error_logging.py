"""Failures are logged as class + code + location, never with the exception's message.

Every failure path that has an exception is triggered with a message stuffed with a student's
question, a prompt, lecture text, a bearer token and a cookie; none of it may reach the logs.
"""

import logging

import pytest

from app import log_safe
from app.config import settings
from app.controllers import chat_controller as cc
from app.controllers import quiz_controller as qc
from app.models import ChatSession
from app.services.llm import LLMUnavailableError
from app.services.retrieval import RetrievedChunk

from .conftest import login

SECRET = "STUDENT-QUESTION-xyzzy-about-adaboost PROMPT-TEXT-plugh LECTURE-TEXT-quux Bearer eyJhbGciOi.TOKEN.sig edupath_session=COOKIEVALUE"
SECRET_WORDS = ["xyzzy", "plugh", "quux", "eyJhbGciOi", "TOKEN", "COOKIEVALUE", "edupath_session=", "STUDENT-QUESTION"]
CHUNK = RetrievedChunk(text="t", source_filename="dm.pdf", page_number=4, content_type="text",
                       lecture_id=1, subject_id=7, score=0.9)


def leaked(caplog) -> list[str]:
    text = caplog.text + "\n".join(f"{r.getMessage()} {r.exc_text or ''} {r.exc_info or ''}" for r in caplog.records)
    return [w for w in SECRET_WORDS if w in text]


def boom(*a, **k):
    raise RuntimeError(SECRET)


@pytest.fixture()
def session(db, world):
    s = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(s)
    db.commit()
    return s


@pytest.fixture(autouse=True)
def default_is_safe():
    assert settings.log_tracebacks is False


# --- chat --------------------------------------------------------------------------------------------


def test_plain_chat_retrieval_failure(client, world, session, monkeypatch, caplog):
    monkeypatch.setattr(cc, "retrieve", boom)
    with caplog.at_level(logging.DEBUG):
        r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": SECRET})
    assert r.status_code == 503 and "lecture retrieval failed" in caplog.text and "RuntimeError" in caplog.text
    assert leaked(caplog) == [] and SECRET not in r.text


def test_plain_chat_model_failure(client, world, session, monkeypatch, caplog):
    monkeypatch.setattr(cc, "retrieve", lambda q, s: [CHUNK])
    monkeypatch.setattr(cc, "get_client", lambda: type("C", (), {"chat": boom})())
    with caplog.at_level(logging.DEBUG):
        r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": SECRET})
    assert r.status_code == 503 and "chat model call failed" in caplog.text
    assert leaked(caplog) == []


def test_stream_model_open_failure(client, world, session, monkeypatch, fake_async_ollama, caplog):
    monkeypatch.setattr(cc, "retrieve", lambda q, s: [CHUNK])
    fake_async_ollama(script=[RuntimeError(SECRET)])
    with caplog.at_level(logging.DEBUG):
        login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages/stream", json={"content": SECRET})
    assert "chat model call failed" in caplog.text or "chat stream failed" in caplog.text
    assert leaked(caplog) == []


def test_stream_failure_part_way(client, world, session, monkeypatch, fake_async_ollama, store_in_test_db, caplog):
    monkeypatch.setattr(cc, "retrieve", lambda q, s: [CHUNK])
    fake_async_ollama(script=["fine so far ", RuntimeError(SECRET)])
    with caplog.at_level(logging.DEBUG):
        r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages/stream", json={"content": SECRET})
    assert "chat stream failed" in caplog.text and '"type":"error"' in r.text
    assert leaked(caplog) == [] and SECRET not in r.text


def test_stream_failure_while_storing(client, world, session, monkeypatch, fake_async_ollama, store_in_test_db, caplog):
    monkeypatch.setattr(cc, "retrieve", lambda q, s: [CHUNK])
    monkeypatch.setattr(cc, "persist_exchange", boom)
    fake_async_ollama(script=["an answer"])
    with caplog.at_level(logging.DEBUG):
        login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages/stream", json={"content": "q?"})
    assert "chat stream failed" in caplog.text and leaked(caplog) == []


def test_follow_up_rewrite_failure(monkeypatch, caplog):
    from app.services import query_rewrite as qr

    monkeypatch.setattr(qr, "get_client", lambda timeout=None: type("C", (), {"chat": boom})())
    with caplog.at_level(logging.DEBUG):
        out = qr.rewrite_followup(SECRET, SECRET)
    assert out == SECRET, "it still falls back to the question as asked"
    assert "follow-up rewrite failed" in caplog.text and leaked(caplog) == []


# --- quiz --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["/quiz/start", "/quiz/start/stream"])
@pytest.mark.parametrize("error", [LLMUnavailableError(SECRET), RuntimeError(SECRET)])
def test_quiz_generation_failures(client, world, monkeypatch, caplog, route, error):
    def fail(*a, **k):
        raise error

    monkeypatch.setattr(qc, "generate_quiz", fail)
    monkeypatch.setattr(qc, "iter_quiz", fail)
    with caplog.at_level(logging.DEBUG):
        r = login(client, world["alice"]).post(route, json={"subject_id": world["subject"].id, "question_count": 2, "language": "en"})
    assert r.status_code == 503 and "quiz generation failed" in caplog.text
    assert leaked(caplog) == [] and SECRET not in r.text


def test_a_quiz_reply_that_cannot_be_parsed_does_not_log_the_reply():
    """The debug line for a malformed model reply used to print the JSON error, which quotes it."""
    from app.services import quiz_service as qs

    import inspect

    source = inspect.getsource(qs)
    assert 'log.debug("quiz generation attempt failed to parse [%s]", log_safe.describe(exc))' in source


# --- a scan that keeps it that way -------------------------------------------------------------------------


def test_no_log_call_in_app_or_ingestion_formats_an_exception_or_asks_for_a_traceback():
    """Source guard: log.<level>(..., exc) / exc_info=... / log.exception(...) are what leak."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    levels = {"debug", "info", "warning", "error", "critical", "exception", "log"}
    names = {"exc", "e", "err", "error", "ex"}
    bad = []
    for folder in ("app", "ingestion"):
        for path in (root / folder).rglob("*.py"):
            if "migrations" in path.parts or path.name == "log_safe.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in levels):
                    continue
                owner = node.func.value
                if not (isinstance(owner, ast.Name) and owner.id in {"log", "logger"}):
                    continue
                where = f"{path.relative_to(root)}:{node.lineno}"
                if node.func.attr == "exception" or any(k.arg == "exc_info" for k in node.keywords):
                    bad.append(f"{where} asks for a traceback")
                if any(isinstance(a, ast.Name) and a.id in names for a in node.args):
                    bad.append(f"{where} formats the exception itself")
    assert bad == [], "\n".join(bad)


# --- auth ---------------------------------------------------------------------------------------------


def test_google_sign_in_failure(client, monkeypatch, caplog):
    from app.routers import auth as auth_router

    async def fail(request):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(auth_router.oauth.google, "authorize_access_token", fail, raising=False)
    with caplog.at_level(logging.DEBUG):
        r = client.get("/auth/google/callback")
    assert r.status_code == 400 and "google sign-in failed" in caplog.text
    assert leaked(caplog) == [] and SECRET not in r.text


# --- background work ----------------------------------------------------------------------------------


def test_warm_up_failure(monkeypatch, caplog):
    import app.main as main

    monkeypatch.setattr(main.vector_store, "warm_up", boom)
    monkeypatch.setattr(main.retrieval, "get_ranker", boom)
    monkeypatch.setattr(main.llm, "warm_chat_model", boom)
    with caplog.at_level(logging.DEBUG):
        main.warm_up_models()
    assert "embedding model warm-up failed; the first request will retry" in caplog.text
    assert "model warm-up finished" in caplog.text and leaked(caplog) == []


def test_quiz_sweeper_failure(caplog):
    from datetime import timedelta

    from app.services.quiz_sweeper import QuizSweeper

    sweeper = QuizSweeper(timedelta(minutes=1), 0)
    sweeper._sweep = boom
    with caplog.at_level(logging.DEBUG):
        sweeper.run()
    assert "quiz sweep failed" in caplog.text and leaked(caplog) == []


def test_stale_chroma_warning(monkeypatch, caplog):
    from chromadb.errors import InternalError

    from app.services import vector_store as vs

    calls = []

    def op():
        calls.append(1)
        if len(calls) == 1:
            raise InternalError(SECRET)
        return "ok"

    monkeypatch.setattr(vs, "reset_cache", lambda **kw: None)
    with caplog.at_level(logging.DEBUG):
        assert vs.retry_if_stale(op) == "ok"
    assert "looks stale" in caplog.text and "InternalError" in caplog.text and leaked(caplog) == []


# --- the helper itself ---------------------------------------------------------------------------------


def test_describe_gives_class_and_code_never_the_message():
    class Resp(Exception):
        status_code = 503

    class WithResponse(Exception):
        response = type("R", (), {"status_code": 429})()

    class ReadTimeout(Exception):
        pass

    assert log_safe.describe(Resp(SECRET)) == "Resp (503)"
    assert log_safe.describe(WithResponse(SECRET)) == "WithResponse (429)"
    assert log_safe.describe(ReadTimeout(SECRET)) == "ReadTimeout (timeout)"
    assert log_safe.describe(TimeoutError(SECRET)) == "TimeoutError (timeout)"
    assert log_safe.describe(ValueError(SECRET)) == "ValueError"
    assert all(w not in log_safe.describe(ValueError(SECRET)) for w in SECRET_WORDS)


def test_where_names_file_line_and_function_only():
    def inner():
        raise ValueError(SECRET)  # xyzzy: a source line must not be logged

    try:
        inner()
    except ValueError as exc:
        loc = log_safe.where(exc)
    assert "test_error_logging.py:" in loc and "in inner" in loc and "in test_where_names" in loc
    assert all(w not in loc for w in SECRET_WORDS)
    assert log_safe.where(ValueError("never raised")) == "unknown location"


def test_with_log_tracebacks_on_the_full_traceback_returns(monkeypatch, caplog):
    monkeypatch.setattr(settings, "log_tracebacks", True)
    log = logging.getLogger("t")
    try:
        boom()
    except RuntimeError as exc:
        with caplog.at_level(logging.DEBUG):
            log_safe.failure(log, "something failed", exc)
    assert "xyzzy" in caplog.text, "the switch really switches (and is off by default)"
