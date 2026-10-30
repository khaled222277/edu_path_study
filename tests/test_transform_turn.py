"""Turns that work on the previous answer ("say that in Arabic", "I don't get it") skip retrieval.

Searching the lectures with such a message returned a logo and a DNA figure (35 cases out of 35 in
the measured sweep) and the model answered those. These tests pin: what counts as such a turn (and
that a real question never does), that nothing is searched, that the previous answer is the only
material, that the sources are the previous turn's, and that both routes behave the same.
"""

import json

import pytest

from app.controllers import chat_controller as cc
from app.models import ChatMessage, ChatSession
from app.models.chat import ROLE_ASSISTANT, ROLE_USER
from app.services.retrieval import RetrievedChunk
from app.services.transform_intent import find_transform_request

from .conftest import login

PREVIOUS_ANSWER = "Classification predicts a class label. [dm.pdf, page 4]"
PREVIOUS_SOURCES = [
    {"source_filename": "dm.pdf", "page_number": 4, "content_type": "text", "score": 0.9, "image_url": None},
]
FIGURE = RetrievedChunk(text="This is a logo. No Arabic text is present.", source_filename="logo.pdf",
                        page_number=2, content_type="image_description", lecture_id=1, subject_id=7, score=0.99)


# --- what counts as a transform request ---------------------------------------------------------

TRANSFORMS = [
    ("okay i can't get it could repeat all that in arabic", "ar"),
    ("say that in Arabic", "ar"),
    ("repeat all that", "en"),
    ("I don't get it", "en"),
    ("I didn't understand", "en"),
    ("this is confusing", "en"),
    ("give an example", "en"),
    ("simplify it please", "en"),
    ("Can you explain that again in simpler words?", "en"),
    ("translate this to english", "en"),
    ("translate it from arabic to english", "en"),
    ("summarize that in 3 bullet points", "en"),
    ("ترجم ذلك للعربية", "ar"),
    ("أعد كل ذلك بالعربي", "ar"),
    ("مش فاهم", "ar"),
    ("لم أفهم", "ar"),
    ("ممكن تبسطها", "ar"),
    ("اشرح ذلك مرة أخرى", "ar"),
    ("اعطني مثال", "ar"),
    ("كرر الكلام ده بالانجليزي", "en"),
]

REAL_QUESTIONS = [
    "what is translation in NLP",
    "explain overfitting again",
    "give an example of entropy",
    "what is classification",
    "tell me more",
    "give me another example",
    "how does k-means work",
    "what are its limitations",
    "explain that in more detail",
    "what does it mean",
    "ok thanks",
    "translate the part about decision trees into arabic",
    "حسنا ما هو التصنيف",
    "اشرح التجميع",
    "ما هو الترجمة في معالجة اللغة",
    "ما هي عيوبها؟",
]


@pytest.mark.parametrize("question,language", TRANSFORMS)
def test_a_request_about_the_previous_answer_is_recognised_with_its_language(question, language):
    from app.services.quiz_service import detect_language

    found = find_transform_request(question, detect_language(question))
    assert found is not None, question
    assert found.language == language, question


@pytest.mark.parametrize("question", REAL_QUESTIONS)
def test_a_real_question_is_never_taken_for_one(question):
    assert find_transform_request(question, "en") is None, question


def test_one_word_outside_the_request_vocabulary_makes_it_an_ordinary_question():
    assert find_transform_request("repeat all that", "en")
    assert find_transform_request("repeat all that about entropy", "en") is None


def test_filler_alone_is_not_a_request():
    assert find_transform_request("ok thanks", "en") is None
    assert find_transform_request("", "en") is None


# --- through the routes ----------------------------------------------------------------------------


@pytest.fixture()
def session(db, world):
    s = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(s)
    db.commit()
    return s


@pytest.fixture()
def searched(monkeypatch):
    """Records every retrieval and every rewrite; both would return the logo."""
    calls = {"retrieve": [], "rewrite": []}

    def retrieve(query, subject_id):
        calls["retrieve"].append(query)
        return [FIGURE]

    monkeypatch.setattr(cc, "retrieve", retrieve)
    monkeypatch.setattr(cc, "retrieval_query", lambda previous, question: calls["rewrite"].append(question) or question)
    return calls


def add_exchange(db, session, question="what is classification?", answer=PREVIOUS_ANSWER, sources=PREVIOUS_SOURCES):
    db.add(ChatMessage(session_id=session.id, role=ROLE_USER, content=question))
    db.add(ChatMessage(session_id=session.id, role=ROLE_ASSISTANT, content=answer, sources=sources))
    db.commit()


def events(r):
    return [json.loads(x) for x in r.text.splitlines() if x.strip()]


def test_a_transform_turn_searches_nothing_and_uses_the_previous_answer_as_the_only_material(
    client, db, world, session, fake_ollama, searched
):
    add_exchange(db, session)
    fake = fake_ollama(default={"message": {"content": "التصنيف يتنبأ بفئة. [dm.pdf, page 4]"}})
    r = login(client, world["alice"]).post(
        f"/chat/sessions/{session.id}/messages", json={"content": "okay i can't get it could repeat all that in arabic"}
    )
    assert r.status_code == 200
    assert searched == {"retrieve": [], "rewrite": []}, "the lectures are not searched, nor is the question rewritten"
    messages = fake.kwargs[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"], "its own prompt, no replayed history"
    assert "PREVIOUS ANSWER:\n" + PREVIOUS_ANSWER in messages[1]["content"]
    assert "Arabic" in messages[0]["content"] and "Write your reply in Arabic." in messages[1]["content"]
    assert "CONTEXT:" not in messages[1]["content"], "no lecture context in this prompt"
    assert "logo" not in json.dumps(messages), "nothing retrieved reached the model"
    body = r.json()
    assert [(c["source_filename"], c["page_number"]) for c in body["citations"]] == [("dm.pdf", 4)]
    assert body["message"]["content"] == "التصنيف يتنبأ بفئة. [dm.pdf, page 4]"


def test_the_language_asked_for_wins_over_the_language_it_was_asked_in(
    client, db, world, session, fake_ollama, searched
):
    add_exchange(db, session)
    fake = fake_ollama(default={"message": {"content": "ok"}})
    login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": "ترجم ذلك للانجليزي"})
    assert "Write your reply in English." in fake.kwargs[0]["messages"][1]["content"]


def test_a_citation_the_previous_turn_did_not_have_is_taken_out(client, db, world, session, fake_ollama, searched):
    add_exchange(db, session)
    fake_ollama(default={"message": {"content": "Same thing [dm.pdf, page 4]. And more [other.pdf, page 9]."}})
    r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": "repeat all that"})
    text = r.json()["message"]["content"]
    assert "[dm.pdf, page 4]" in text
    assert "other.pdf" not in text


def test_with_no_sources_on_the_previous_answer_there_are_none_and_no_citation_survives(
    client, db, world, session, fake_ollama, searched
):
    add_exchange(db, session, sources=[])
    fake_ollama(default={"message": {"content": "Same thing [dm.pdf, page 4]."}})
    body = login(client, world["alice"]).post(
        f"/chat/sessions/{session.id}/messages", json={"content": "say that again"}
    ).json()
    assert body["citations"] == []
    assert "[" not in body["message"]["content"]


def test_a_real_question_after_an_answer_is_still_searched(client, db, world, session, fake_ollama, searched):
    add_exchange(db, session)
    fake = fake_ollama(default={"message": {"content": "NLP translation maps text. [logo.pdf, page 2]"}})
    login(client, world["alice"]).post(
        f"/chat/sessions/{session.id}/messages", json={"content": "what is translation in NLP"}
    )
    assert searched["retrieve"] == ["what is translation in NLP"]
    assert "CONTEXT:" in fake.kwargs[0]["messages"][-1]["content"], "the ordinary, pinned prompt"


def test_the_first_message_of_a_chat_is_always_an_ordinary_question(client, world, session, fake_ollama, searched):
    fake = fake_ollama(default={"message": {"content": "x"}})
    login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": "repeat all that"})
    assert searched["retrieve"] == ["repeat all that"], "no previous answer to work on, so it is searched"
    assert "CONTEXT:" in fake.kwargs[0]["messages"][-1]["content"]


def test_the_stream_route_gives_the_same_prompt_and_the_same_sources(
    client, db, world, session, fake_ollama, fake_async_ollama, store_in_test_db, searched
):
    other = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(other)
    db.commit()
    add_exchange(db, session)
    add_exchange(db, other)
    plain = fake_ollama(default={"message": {"content": "x"}})
    streamed = fake_async_ollama(script=["Same ", "thing [dm.pdf, ", "page 4]. And [other.pdf, page 9]."])
    c = login(client, world["alice"])
    c.post(f"/chat/sessions/{session.id}/messages", json={"content": "repeat all that"})
    r = c.post(f"/chat/sessions/{other.id}/messages/stream", json={"content": "repeat all that"})

    assert plain.kwargs[0]["messages"] == streamed.kwargs[0]["messages"]
    assert searched == {"retrieve": [], "rewrite": []}
    evs = events(r)
    first = next(e for e in evs if e["type"] == "citations")
    done = next(e for e in evs if e["type"] == "done")
    assert [(x["source_filename"], x["page_number"]) for x in first["citations"]] == [("dm.pdf", 4)]
    assert done["citations"] == first["citations"]
    shown = "".join(e["text"] for e in evs if e["type"] == "token")
    assert shown == done["message"]["content"] == "Same thing [dm.pdf, page 4]. And."
    stored = [m for m in db.query(ChatMessage).filter_by(session_id=other.id) if m.role == ROLE_ASSISTANT][-1]
    assert stored.sources == done["citations"], "the reply is stored with the previous turn's sources"
