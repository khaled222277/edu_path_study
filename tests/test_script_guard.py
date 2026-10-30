"""Characters of the wrong script (CJK, Hangul, Cyrillic, U+FFFD...) never reach a student, on either route.

Measured: the chat model writes e.g. "(أو 関係)" or "(أو 関�function)" for "(or function)" in Arabic answers.
The filter works one character at a time, so what is streamed must equal what is stored and what the
done event carries, however the model's pieces are cut.
"""

import json
import logging

import pytest

from app.controllers import chat_controller as cc
from app.models import ChatMessage, ChatSession
from app.models.chat import ROLE_ASSISTANT
from app.services.retrieval import RetrievedChunk
from app.services.script_guard import ScriptFilter, foreign_count, is_foreign, strip_foreign

from .conftest import login

ARABIC = "التصنيف هو عملية إيجاد نموذج (أو وظيفة) يصف فئات البيانات. [dm.pdf, page 4]"
ENGLISH = "Classification finds a model (or function) that describes data classes. [dm.pdf, page 4]"
MIXED = "ما هو الـ clustering في data mining؟ الإجابة: k-means مع α = 0.5 و Σ x² ≤ ℝ و 𝑥 و é"


@pytest.mark.parametrize("text", [ARABIC, ENGLISH, MIXED, "Entropy = -Σ p_i log₂ p_i, β ≥ 0", "١٢٣ 123 ½ √2 ∞ → ≠ ∑ ∈ λ π", ""])
def test_allowed_text_is_untouched(text):
    assert strip_foreign(text) == (text, 0)


@pytest.mark.parametrize("ch", ["関", "係", "数", "한", "あ", "カ", "ж", "Я", "ש", "क", "ก", "�", "、", "。", "「", "ｱ"])
def test_each_disallowed_script_is_recognised(ch):
    assert is_foreign(ch)


def test_the_observed_leaks_are_taken_out_and_a_latin_term_after_them_is_kept():
    assert strip_foreign("نموذج (أو 関係) يصف") == ("نموذج (أو ) يصف", 2)
    assert strip_foreign("(أو 関�function) يصف") == ("(أو function) يصف", 2)
    assert strip_foreign("a 関係 b") == ("a b", 2), "no double space where a word was"
    assert strip_foreign("لا يوجد 数据 هنا") == ("لا يوجد هنا", 2)


def test_a_word_mixing_foreign_and_arabic_letters_is_dropped_whole_not_left_as_a_fragment():
    assert strip_foreign("من الأтрибут إلى قيمة") == ("من إلى قيمة", 9)
    assert strip_foreign("قيم الأтрибутات الأخرى") == ("قيم الأخرى", 11)
    assert strip_foreign("(الأ数) و") == ("() و", 4)


def test_english_is_not_held_back_but_an_arabic_word_waits_for_its_end():
    guard = ScriptFilter()
    assert guard.feed("Hello wor") == "Hello wor"
    assert guard.feed("ld ") == "ld "
    assert guard.feed("كلمة") == "", "might still get a foreign letter glued on"
    assert guard.feed("ٌ و") == "كلمةٌ ", "released at the space; the next word is held in turn"
    guard = ScriptFilter()
    assert guard.feed("نهاية") == ""
    assert guard.flush() == "نهاية", "the last word is released at the end"


def test_a_cut_anywhere_gives_the_same_text_as_the_whole():
    text = "نموذج (أو 関係) يصف �function و 数 b  c"
    whole = strip_foreign(text)[0]
    for size in (1, 2, 3, 5):
        guard = ScriptFilter()
        pieces = [text[i:i + size] for i in range(0, len(text), size)]
        assert "".join(guard.feed(p) for p in pieces) + guard.flush() == whole, size
        assert guard.dropped == foreign_count(text)


# --- both routes ---------------------------------------------------------------------------------

CHUNK = RetrievedChunk(text="Support.", source_filename="dm.pdf", page_number=4, content_type="text",
                       lecture_id=1, subject_id=7, score=0.9)
LEAKY = ["التصنيف هو عملية إيجاد نموذج (أو 関", "�function) يصف فئات. ", "数", "据 [dm.pdf, page 4]."]
CLEAN = "التصنيف هو عملية إيجاد نموذج (أو function) يصف فئات. [dm.pdf, page 4]."


@pytest.fixture(autouse=True)
def one_chunk(monkeypatch):
    monkeypatch.setattr(cc, "retrieve", lambda q, s: [CHUNK])


@pytest.fixture()
def session(db, world):
    s = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(s)
    db.commit()
    return s


def events(r):
    return [json.loads(x) for x in r.text.splitlines() if x.strip()]


def test_plain_route_stores_the_clean_text(client, world, session, fake_ollama):
    fake_ollama(default={"message": {"content": "".join(LEAKY)}})
    body = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": "ما هو التصنيف"}).json()
    assert body["message"]["content"] == CLEAN


def test_stream_route_shows_stores_and_reports_the_same_clean_text(
    client, db, world, session, fake_async_ollama, store_in_test_db
):
    fake_async_ollama(script=LEAKY)
    r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages/stream", json={"content": "ما هو التصنيف"})
    evs = events(r)
    shown = "".join(e["text"] for e in evs if e["type"] == "token")
    done = next(e for e in evs if e["type"] == "done")
    stored = [m for m in db.query(ChatMessage).filter_by(session_id=session.id) if m.role == ROLE_ASSISTANT][-1]
    assert shown == done["message"]["content"] == stored.content == CLEAN
    assert foreign_count(r.text) == 0, "nothing foreign on the wire at all, not even in an early event"


def test_the_log_gets_a_count_and_never_the_text(client, world, session, fake_ollama, caplog):
    fake_ollama(default={"message": {"content": "".join(LEAKY)}})
    with caplog.at_level(logging.INFO, logger="app.controllers.chat_controller"):
        login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": "ما هو التصنيف"})
    lines = [r.getMessage() for r in caplog.records if "foreign-script" in r.getMessage()]
    assert lines == ["dropped 4 foreign-script character(s) from a chat reply"]
    assert not any(ch in caplog.text for ch in "関数据�")


def test_a_clean_answer_logs_nothing_about_dropping(client, world, session, fake_ollama, caplog):
    fake_ollama(default={"message": {"content": ARABIC}})
    with caplog.at_level(logging.INFO, logger="app.controllers.chat_controller"):
        login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": "ما هو التصنيف"})
    assert "foreign-script" not in caplog.text


def test_english_and_mixed_answers_pass_through_both_routes(
    client, db, world, session, fake_ollama, fake_async_ollama, store_in_test_db
):
    fake_ollama(default={"message": {"content": MIXED}})
    body = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": "q"}).json()
    assert body["message"]["content"] == MIXED
    fake_async_ollama(script=[MIXED[:20], MIXED[20:]])
    r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages/stream", json={"content": "q"})
    assert next(e for e in events(r) if e["type"] == "done")["message"]["content"] == MIXED


def test_an_answer_that_ends_on_an_arabic_word_still_shows_and_stores_it(
    client, db, world, session, fake_async_ollama, store_in_test_db
):
    fake_async_ollama(script=["التصنيف هو ", "عملية"])
    r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages/stream", json={"content": "ما هو التصنيف"})
    evs = events(r)
    shown = "".join(e["text"] for e in evs if e["type"] == "token")
    done = next(e for e in evs if e["type"] == "done")
    assert shown == done["message"]["content"] == "التصنيف هو عملية"
