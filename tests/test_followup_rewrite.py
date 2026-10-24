"""A follow-up is searched for by what it means; the answer prompt keeps the student's words."""

import httpx
import pytest

from app.config import Settings, settings
from app.controllers import chat_controller as cc
from app.models import ChatMessage, ChatSession
from app.models.chat import ROLE_ASSISTANT, ROLE_USER
from app.services import query_rewrite as qr
from app.services.retrieval import RetrievedChunk

from .conftest import FakeOllama, login

CHUNK = RetrievedChunk(
    text="K-means picks centroids.", source_filename="dm.pdf", page_number=4,
    content_type="text", lecture_id=1, subject_id=7, score=0.9,
)
PREV = "What is k-means clustering?"
FOLLOW = "Can you explain that again in simpler words?"
STANDALONE = "What is k-means clustering in simpler words?"


def reply(text):
    return {"message": {"content": text}}


@pytest.fixture(autouse=True)
def rewriting_on(monkeypatch):
    monkeypatch.setattr(settings, "chat_followup_rewrite", True)


@pytest.fixture()
def model(monkeypatch):
    """The rewrite goes through qr.get_client; script it separately from the answer call."""

    def install(*replies, error=None):
        fake = FakeOllama(responses=[reply(r) for r in replies] if replies else None,
                          default=error if error is not None else (None if replies else reply(STANDALONE)))
        monkeypatch.setattr(qr, "get_client", lambda timeout=None: fake)
        return fake

    return install


# --- the rewrite itself ----------------------------------------------------------------------


def test_a_follow_up_is_rewritten_with_the_previous_question(model):
    fake = model(STANDALONE)
    assert qr.retrieval_query(PREV, FOLLOW) == STANDALONE
    prompt = fake.kwargs[0]["messages"][0]["content"]
    assert PREV in prompt and FOLLOW in prompt


def test_the_call_is_cheap_deterministic_and_does_not_reload_the_model(model):
    fake = model(STANDALONE)
    qr.retrieval_query(PREV, FOLLOW)
    call = fake.kwargs[0]
    assert call["model"] == settings.chat_model
    assert call["options"]["temperature"] == 0 and call["options"]["num_predict"] <= 100
    assert "num_ctx" not in call["options"], "a different context size would make Ollama reload the model"
    assert call["keep_alive"] == qr.keep_alive()


def test_the_first_question_of_a_session_is_never_rewritten(model):
    fake = model(STANDALONE)
    assert qr.retrieval_query(None, FOLLOW) == FOLLOW
    assert qr.retrieval_query("", FOLLOW) == FOLLOW
    assert qr.retrieval_query("   ", FOLLOW) == FOLLOW
    assert fake.kwargs == []


def test_the_setting_turns_it_off(model, monkeypatch):
    fake = model(STANDALONE)
    monkeypatch.setattr(settings, "chat_followup_rewrite", False)
    assert qr.retrieval_query(PREV, FOLLOW) == FOLLOW and fake.kwargs == []


def test_a_standalone_question_comes_back_unchanged(model):
    q = "What is the Apriori principle?"
    model(q)
    assert qr.retrieval_query(PREV, q) == q


@pytest.mark.parametrize("exc", [ConnectionError("down"), httpx.ReadTimeout("slow"), RuntimeError("boom")])
def test_any_failure_falls_back_to_the_question_as_asked(model, exc):
    model(error=exc)
    assert qr.retrieval_query(PREV, FOLLOW) == FOLLOW


@pytest.mark.parametrize("junk", ["", "   \n  ", "x" * 500])
def test_unusable_output_falls_back(model, junk):
    model(junk)
    assert qr.retrieval_query(PREV, FOLLOW) == FOLLOW


def test_only_the_first_line_is_used_and_quotes_are_stripped(model):
    model('"What is k-means?"\nThis question asks about clustering.')
    assert qr.retrieval_query(PREV, FOLLOW) == "What is k-means?"


def test_arabic_goes_through_intact(model):
    ar = "ما هي عيوب خوارزمية k-means؟"
    model(ar)
    assert qr.retrieval_query("كيف تعمل خوارزمية k-means؟", "وما هي عيوبها؟") == ar


def test_the_rewrite_is_never_longer_than_the_limit():
    assert qr._usable("x" * qr.MAX_REWRITE_CHARS) is not None
    assert qr._usable("x" * (qr.MAX_REWRITE_CHARS + 1)) is None


# --- how the chat uses it -------------------------------------------------------------------------------


@pytest.fixture()
def session(db, world):
    s = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(s)
    db.commit()
    return s


def turn(db, session, role, content):
    db.add(ChatMessage(session_id=session.id, role=role, content=content))
    db.commit()


@pytest.fixture()
def searched(monkeypatch):
    """Record what retrieval is asked for."""
    seen = []
    monkeypatch.setattr(cc, "retrieve", lambda q, s: seen.append(q) or [CHUNK])
    return seen


def test_the_search_uses_the_rewrite_but_the_prompt_keeps_the_students_words(
    client, db, world, session, searched, model, fake_ollama
):
    turn(db, session, ROLE_USER, PREV)
    turn(db, session, ROLE_ASSISTANT, "K-means groups points.")
    model()  # answers every rewrite request with STANDALONE, however many are made
    answer = fake_ollama(default=reply("simpler"))
    login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": FOLLOW})

    assert searched == [STANDALONE]
    prompt = answer.kwargs[0]["messages"]
    assert STANDALONE not in str(prompt), "the rewrite never reaches the answer prompt"
    assert FOLLOW in prompt[-1]["content"] and prompt[-2]["content"] == FOLLOW


def test_the_first_turn_searches_with_the_question_and_makes_no_rewrite_call(
    client, world, session, searched, model, fake_ollama
):
    rewrite = model(STANDALONE)
    fake_ollama(default=reply("an answer"))
    login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": PREV})
    assert searched == [PREV] and rewrite.kwargs == []


def test_the_previous_question_is_the_last_one_asked_not_an_answer_or_the_current_one(
    client, db, world, session, searched, model, fake_ollama
):
    turn(db, session, ROLE_USER, "an older question")
    turn(db, session, ROLE_ASSISTANT, "an answer")
    turn(db, session, ROLE_USER, PREV)
    turn(db, session, ROLE_ASSISTANT, "another answer")
    rewrite = model(STANDALONE)
    fake_ollama(default=reply("x"))
    login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": FOLLOW})
    prompt = rewrite.kwargs[0]["messages"][0]["content"]
    assert f"Previous question: {PREV}\n" in prompt and "older question" not in prompt


def test_a_failing_rewrite_does_not_fail_the_turn(client, db, world, session, searched, model, fake_ollama):
    turn(db, session, ROLE_USER, PREV)
    turn(db, session, ROLE_ASSISTANT, "x")
    model(error=ConnectionError("down"))
    fake_ollama(default=reply("still answered"))
    r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": FOLLOW})
    assert r.status_code == 200 and r.json()["message"]["content"] == "still answered"
    assert searched == [FOLLOW]


def test_the_stream_route_searches_the_same_way(
    client, db, world, session, searched, model, fake_async_ollama, store_in_test_db
):
    turn(db, session, ROLE_USER, PREV)
    turn(db, session, ROLE_ASSISTANT, "x")
    model(STANDALONE)
    fake_async_ollama(script=["ok"])
    r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages/stream", json={"content": FOLLOW})
    assert r.status_code == 200 and searched == [STANDALONE]


def test_previous_user_question_helper():
    from types import SimpleNamespace as NS

    msgs = [NS(role="user", content="a"), NS(role="assistant", content="b"), NS(role="user", content="c")]
    assert cc.previous_user_question(NS(messages=msgs)) == "a"
    assert cc.previous_user_question(NS(messages=msgs[:1])) is None
    assert cc.previous_user_question(NS(messages=[])) is None
    assert cc.previous_user_question(NS(messages=[msgs[1], msgs[2]])) is None


# --- settings --------------------------------------------------------------------------------------------------


def test_default_is_on(monkeypatch):
    monkeypatch.delenv("CHAT_FOLLOWUP_REWRITE", raising=False)
    assert Settings(_env_file=None).chat_followup_rewrite is True
