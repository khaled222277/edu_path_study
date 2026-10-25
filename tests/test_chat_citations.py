"""Both chat routes check citations against the chunks retrieved for the turn."""

import json

import pytest

from app.controllers import chat_controller as cc
from app.models import ChatMessage, ChatSession
from app.services.refusal import DEFAULT_APOLOGY, REFUSAL_SENTINEL
from app.services.retrieval import RetrievedChunk

from .conftest import login

C1 = RetrievedChunk(text="Support is a ratio.", source_filename="dm.pdf", page_number=4,
                    content_type="text", lecture_id=1, subject_id=7, score=0.9)
C2 = RetrievedChunk(text="Confidence is another.", source_filename="dm.pdf", page_number=7,
                    content_type="text", lecture_id=1, subject_id=7, score=0.8)
RETRIEVED_SOURCES = [("dm.pdf", 4), ("dm.pdf", 7)]


@pytest.fixture(autouse=True)
def two_chunks(monkeypatch):
    monkeypatch.setattr(cc, "retrieve", lambda q, s: [C1, C2])


@pytest.fixture()
def session(db, world):
    s = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(s)
    db.commit()
    return s


def stored(db):
    db.expire_all()
    return [m.content for m in db.query(ChatMessage).order_by(ChatMessage.id).all()]


def events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def sources(citations):
    return [(c["source_filename"], c["page_number"]) for c in citations]


def ask(client, world, session, fake_ollama, answer, question="what is support?"):
    fake_ollama(default={"message": {"content": answer}})
    return login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": question})


def ask_stream(client, world, session, fake_async_ollama, pieces, question="what is support?"):
    fake_async_ollama(script=pieces)
    return login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages/stream", json={"content": question})


# --- plain route -------------------------------------------------------------------------------


def test_plain_an_invented_page_is_removed_from_the_reply_and_from_storage(client, db, world, session, fake_ollama):
    r = ask(client, world, session, fake_ollama, "Support is a ratio [dm.pdf, page 99]. It is useful [dm.pdf, page 4].")
    body = r.json()
    assert body["message"]["content"] == "Support is a ratio. It is useful [dm.pdf, page 4]."
    assert stored(db)[-1] == body["message"]["content"], "what is stored is what the student saw"


def test_plain_a_made_up_file_and_an_arabic_book_title_are_removed(client, world, session, fake_ollama):
    r = ask(client, world, session, fake_ollama, "الدعم نسبة [كتاب دروس كيد، صفحة 12] وهي مفيدة [other.pdf, page 4].", "ما هو الدعم؟")
    assert r.json()["message"]["content"] == "الدعم نسبة وهي مفيدة."


def test_plain_arabic_indic_page_numbers_are_checked_and_normalised(client, world, session, fake_ollama):
    r = ask(client, world, session, fake_ollama, "الدعم نسبة [dm.pdf, صفحة ٤] ثم [dm.pdf, صفحة ٩٩].", "ما هو الدعم؟")
    assert r.json()["message"]["content"] == "الدعم نسبة [dm.pdf, page 4] ثم."


def test_plain_several_pages_in_one_bracket_keep_only_the_retrieved_ones(client, world, session, fake_ollama):
    r = ask(client, world, session, fake_ollama, "Both [dm.pdf, page 4, 7, 50].")
    assert r.json()["message"]["content"] == "Both [dm.pdf, page 4] [dm.pdf, page 7]."


def test_plain_the_structured_sources_are_exactly_the_retrieved_chunks(client, world, session, fake_ollama):
    r = ask(client, world, session, fake_ollama, "Answer [dm.pdf, page 99] [ghost.pdf, page 1].")
    cites = r.json()["citations"]
    assert sources(cites) == RETRIEVED_SOURCES, "never a source that was not retrieved, whatever the model wrote"
    assert "ghost" not in json.dumps(r.json())


def test_plain_valid_citations_are_untouched(client, world, session, fake_ollama):
    text = "Support is a ratio [dm.pdf, page 4]. Confidence differs [dm.pdf, page 7]."
    assert ask(client, world, session, fake_ollama, text).json()["message"]["content"] == text


def test_plain_text_with_no_citations_is_untouched(client, world, session, fake_ollama):
    text = "Support is a ratio."
    assert ask(client, world, session, fake_ollama, text).json()["message"]["content"] == text


def test_plain_an_answer_that_was_only_an_invalid_citation_becomes_the_apology(client, db, world, session, fake_ollama):
    r = ask(client, world, session, fake_ollama, "[ghost.pdf, page 1]")
    body = r.json()
    assert body["message"]["content"] == DEFAULT_APOLOGY["en"] and body["citations"] == []
    assert stored(db)[-1] == DEFAULT_APOLOGY["en"], "never an empty message in the history"


def test_plain_the_apology_is_in_the_questions_language(client, world, session, fake_ollama):
    r = ask(client, world, session, fake_ollama, "[ghost.pdf, صفحة 1]", "ما هو الدعم؟")
    assert r.json()["message"]["content"] == DEFAULT_APOLOGY["ar"]


def test_plain_a_refusal_with_a_citation_still_hides_the_sources(client, world, session, fake_ollama):
    r = ask(client, world, session, fake_ollama, f"{REFUSAL_SENTINEL} Not covered [ghost.pdf, page 2].")
    body = r.json()
    assert body["message"]["content"] == "Not covered." and body["citations"] == []


def test_plain_brackets_that_are_not_citations_survive(client, world, session, fake_ollama):
    text = "The range [0.0, 1.0] is used [dm.pdf, page 4]."
    assert ask(client, world, session, fake_ollama, text).json()["message"]["content"] == text


def test_plain_the_next_turn_replays_the_checked_text_not_the_raw_one(client, world, session, fake_ollama):
    fake = fake_ollama(default={"message": {"content": "Answer [dm.pdf, page 99] here."}})
    c = login(client, world["alice"])
    c.post(f"/chat/sessions/{session.id}/messages", json={"content": "q1?"})
    c.post(f"/chat/sessions/{session.id}/messages", json={"content": "q2?"})
    replayed = [m["content"] for m in fake.kwargs[1]["messages"] if m["role"] == "assistant"]
    assert replayed == ["Answer here."], "an invented citation must not be fed back to the model as precedent"


# --- streaming route -----------------------------------------------------------------------------


def streamed_text(evs):
    return "".join(e["text"] for e in evs if e["type"] == "token")


def test_stream_an_invalid_citation_never_reaches_the_browser(client, world, session, fake_async_ollama, store_in_test_db):
    r = ask_stream(client, world, session, fake_async_ollama, ["Support is ", "a ratio [dm.pdf, pa", "ge 99]. Then [dm.", "pdf, page 4] ok."])
    evs = events(r)
    for e in evs:
        if e["type"] == "token":
            assert "99" not in e["text"] and "ghost" not in e["text"]
    assert streamed_text(evs) == "Support is a ratio. Then [dm.pdf, page 4] ok."


def test_stream_no_partial_bracket_is_ever_shown_while_it_is_being_judged(client, world, session, fake_async_ollama, store_in_test_db):
    text = "Answer [dm.pdf, page 99] end"
    r = ask_stream(client, world, session, fake_async_ollama, list(text))
    shown = ""
    for e in events(r):
        if e["type"] == "token":
            shown += e["text"]
            assert "[" not in shown, f"a bracket appeared mid-stream: {shown!r}"
    assert shown == "Answer end"


def test_stream_the_final_event_matches_what_was_streamed(client, world, session, fake_async_ollama, store_in_test_db):
    cases = [
        ["Support [dm.pdf, page 4] and [dm.pdf, page 99] done."],
        ["Both [dm.pdf, page 4, 7, 50]."],
        ["الدعم [dm.pdf, صفحة ٤] ثم [dm.pdf, صفحة ٩٩]."],
        list("Support [dm.pdf, page 4, 99] x [ghost.pdf] y."),
        ["plain text, no citations"],
    ]
    for pieces in cases:
        r = ask_stream(client, world, session, fake_async_ollama, pieces, "ما هو الدعم؟" if "الدعم" in pieces[0] else "what?")
        evs = events(r)
        done = evs[-1]
        assert done["type"] == "done"
        assert streamed_text(evs).strip() == done["message"]["content"], (pieces, streamed_text(evs), done["message"]["content"])


def test_stream_arabic_digits_are_already_canonical_on_the_wire(client, world, session, fake_async_ollama, store_in_test_db):
    r = ask_stream(client, world, session, fake_async_ollama, ["الدعم [dm.pdf, صفحة ", "٤] نسبة."], "ما هو الدعم؟")
    assert streamed_text(events(r)) == "الدعم [dm.pdf, page 4] نسبة."


def test_stream_stored_text_equals_the_final_event(client, db, world, session, fake_async_ollama, store_in_test_db):
    r = ask_stream(client, world, session, fake_async_ollama, ["A [dm.pdf, page 99] B [dm.pdf, page 7] C."])
    done = events(r)[-1]
    assert stored(db)[-1] == done["message"]["content"] == "A B [dm.pdf, page 7] C."


def test_stream_structured_sources_contain_only_retrieved_chunks(client, world, session, fake_async_ollama, store_in_test_db):
    evs = events(ask_stream(client, world, session, fake_async_ollama, ["x [ghost.pdf, page 1] y [dm.pdf, page 99]"]))
    first, done = evs[0], evs[-1]
    assert first["type"] == "citations" and sources(first["citations"]) == RETRIEVED_SOURCES
    assert sources(done["citations"]) == RETRIEVED_SOURCES
    assert "ghost" not in json.dumps(evs)


def test_stream_a_refusal_hides_sources_and_the_token_and_bad_citations(client, world, session, fake_async_ollama, store_in_test_db):
    r = ask_stream(client, world, session, fake_async_ollama, ["<<NOT_IN_", "MATERIAL>> Not covered [ghost.pdf, page 2]."])
    evs = events(r)
    assert streamed_text(evs) == "Not covered." == evs[-1]["message"]["content"]
    assert evs[-1]["citations"] == [] and "NOT_IN" not in r.text and "ghost" not in "".join(e.get("text", "") for e in evs)


def test_stream_only_an_invalid_citation_shows_the_apology(client, db, world, session, fake_async_ollama, store_in_test_db):
    evs = events(ask_stream(client, world, session, fake_async_ollama, ["[ghost.pdf, ", "page 1]"]))
    assert [e["type"] for e in evs] == ["citations", "token", "done"]
    assert evs[1]["text"] == DEFAULT_APOLOGY["en"] == evs[-1]["message"]["content"] == stored(db)[-1]


def test_stream_a_bracket_still_open_at_the_end_is_released_as_text(client, world, session, fake_async_ollama, store_in_test_db):
    evs = events(ask_stream(client, world, session, fake_async_ollama, ["See [dm.pdf, page"]))
    assert streamed_text(evs) == "See [dm.pdf, page" == evs[-1]["message"]["content"]


def test_stream_mid_stream_failure_still_shows_nothing_invalid(client, db, world, session, fake_async_ollama, store_in_test_db):
    r = ask_stream(client, world, session, fake_async_ollama, ["Fine [dm.pdf, page 99] ", RuntimeError("boom")])
    evs = events(r)
    assert evs[-1]["type"] == "error" and "99" not in streamed_text(evs) and stored(db) == []


# --- the two routes agree ------------------------------------------------------------------------------


def test_plain_and_stream_produce_the_same_text(client, world, session, db, fake_ollama, fake_async_ollama, store_in_test_db):
    answer = "A [dm.pdf, page 4, 99] B [x.pdf, page 1] C [dm.pdf, صفحة ٧] D."
    plain = ask(client, world, session, fake_ollama, answer).json()["message"]["content"]
    other = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(other)
    db.commit()
    fake_async_ollama(script=[answer])
    r = login(client, world["alice"]).post(f"/chat/sessions/{other.id}/messages/stream", json={"content": "what is support?"})
    assert events(r)[-1]["message"]["content"] == plain
