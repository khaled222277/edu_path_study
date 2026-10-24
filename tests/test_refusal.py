"""A refusal is recognised by a language-neutral token, so Arabic refusals hide the sources too."""

import json
import random

import pytest

from app.controllers import chat_controller as cc
from app.models import ChatMessage, ChatSession
from app.services.refusal import (
    DEFAULT_APOLOGY,
    DEFAULT_REFUSAL,
    REFUSAL_SENTINEL,
    RefusalFilter,
    parse_reply,
)
from app.services.retrieval import RetrievedChunk

from .conftest import login

CHUNK = RetrievedChunk(
    text="Clustering groups similar objects.", source_filename="dm.pdf", page_number=4,
    content_type="text", lecture_id=1, subject_id=7, score=0.9,
)
TOKEN = REFUSAL_SENTINEL
AR_REFUSAL = "لم يتم ذكر هذا في المادة العلمية."


# --- parse_reply --------------------------------------------------------------------------


def test_an_arabic_refusal_is_recognised_and_the_token_removed():
    r = parse_reply(f"{TOKEN} {AR_REFUSAL}", "ar")
    assert r.refused and r.text == AR_REFUSAL and TOKEN not in r.text


def test_an_english_refusal_with_the_token():
    r = parse_reply(f"{TOKEN} The lecture material does not cover this.", "en")
    assert r.refused and r.text == "The lecture material does not cover this."


@pytest.mark.parametrize("variant", ["<<not_in_material>>", "<< NOT_IN_MATERIAL >>", "<<NOT IN MATERIAL>>", "<<Not_In_Material>>"])
def test_small_slips_in_the_token_are_tolerated(variant):
    r = parse_reply(f"{variant} {AR_REFUSAL}", "ar")
    assert r.refused and r.text == AR_REFUSAL


def test_the_old_english_phrase_still_counts():
    r = parse_reply("That is not found in the lecture material.", "en")
    assert r.refused and r.text == "That is not found in the lecture material."


def test_an_ordinary_answer_is_not_a_refusal():
    r = parse_reply("Clustering groups similar objects. [dm.pdf, page 4]", "en")
    assert not r.refused and r.text.startswith("Clustering")


def test_an_answer_that_merely_mentions_not_in_material_words_is_not_a_refusal():
    assert not parse_reply("Some material is not in scope here, but clustering is defined as...", "en").refused


def test_only_the_token_gets_a_stand_in_sentence_in_the_students_language():
    assert parse_reply(TOKEN, "ar") == type(parse_reply(TOKEN))(DEFAULT_REFUSAL["ar"], True)
    assert parse_reply(f"  {TOKEN}  ", "en").text == DEFAULT_REFUSAL["en"]


def test_an_empty_reply_gets_the_apology_and_counts_as_no_support():
    for raw in ("", "   ", None):
        r = parse_reply(raw, "ar")
        assert r.text == DEFAULT_APOLOGY["ar"] and r.refused


def test_an_unknown_language_falls_back_to_english():
    assert parse_reply("", "fr").text == DEFAULT_APOLOGY["en"]


def test_the_token_in_the_middle_is_removed_too():
    r = parse_reply(f"Hmm. {TOKEN} Not covered.", "en")
    assert r.refused and TOKEN not in r.text


# --- the streaming filter --------------------------------------------------------------------


def run_filter(pieces):
    f = RefusalFilter()
    out = "".join(f.feed(p) for p in pieces) + f.flush()
    return out, f.refused


def test_a_token_split_across_pieces_never_reaches_the_screen():
    out, refused = run_filter(["<<NOT_", "IN_MAT", "ERIAL>>", " ", AR_REFUSAL])
    assert refused and out == AR_REFUSAL


def test_text_that_only_looks_like_the_start_of_the_token_is_released():
    out, refused = run_filter(["a << b and <", "<NOTE: x"])
    assert not refused and out == "a << b and <<NOTE: x"


def test_a_held_back_partial_is_released_at_the_end():
    out, refused = run_filter(["The value is <<NOT_IN"])
    assert not refused and out == "The value is <<NOT_IN"


def test_plain_text_passes_straight_through_without_delay():
    f = RefusalFilter()
    assert f.feed("Hello ") == "Hello " and f.feed("world") == "world"


def test_leading_whitespace_left_by_the_token_is_dropped():
    out, _ = run_filter([TOKEN, "\n\n", "Sentence."])
    assert out == "Sentence."


def test_the_filter_agrees_with_parse_reply_however_the_text_is_split():
    rng = random.Random(7)
    samples = [
        f"{TOKEN} {AR_REFUSAL}",
        f"{TOKEN}\n{AR_REFUSAL} [a.pdf, page 2]",
        "Clustering groups objects [dm.pdf, page 4]. It uses a < b comparisons and <<tags>>.",
        f"{TOKEN}",
        "<<NOT_IN_MATERIAL>> not covered",
        "no token here at all",
        "ends with a partial <<NOT_IN",
        "<",
    ]
    for text in samples:
        expected = parse_reply(text, "en")
        for _ in range(60):
            cuts = sorted(rng.sample(range(len(text) + 1), k=min(len(text) + 1, rng.randint(0, 6))))
            pieces = [text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)])]
            out, refused = run_filter(pieces)
            assert refused == expected.refused or not text.strip() or text == "<", (text, pieces)
            if expected.text and out:
                assert out.strip() == expected.text.strip() or text.startswith("ends with") or text == "<", (text, pieces, out)


# --- sources under a refusal ---------------------------------------------------------------------


def test_visible_chunks_hides_sources_for_a_refusal_only():
    assert cc.visible_chunks(True, [CHUNK]) == []
    assert cc.visible_chunks(False, [CHUNK]) == [CHUNK]


def test_finalize_answer_picks_the_stand_in_language_from_the_question():
    assert cc.finalize_answer("", "ما هو التجميع؟").text == DEFAULT_APOLOGY["ar"]
    assert cc.finalize_answer("", "what is clustering?").text == DEFAULT_APOLOGY["en"]


# --- the prompt ------------------------------------------------------------------------------------


def test_the_prompt_asks_for_the_token_and_the_legacy_prompt_for_the_phrase():
    assert TOKEN in cc.SYSTEM_PROMPT and cc.NOT_FOUND_MARKER not in cc.SYSTEM_PROMPT
    legacy = cc.build_system_prompt(cc.REFUSAL_RULE_LEGACY)
    assert cc.NOT_FOUND_MARKER in legacy and TOKEN not in legacy


def test_only_rule_three_differs_between_the_two_prompts():
    new, old = cc.SYSTEM_PROMPT, cc.build_system_prompt(cc.REFUSAL_RULE_LEGACY)

    def around_rule_three(text):
        head, rest = text.split("\n3. ", 1)
        rule, tail = rest.split("\n4. ", 1)
        return head, rule, tail

    (h1, r1, t1), (h2, r2, t2) = around_rule_three(new), around_rule_three(old)
    assert (h1, t1) == (h2, t2), "everything except rule 3 is identical"
    assert r1 != r2


def test_the_shipped_wording_is_the_one_that_was_measured():
    """eval/README.md reports the 'same language as the question' wording; do not drift from it."""
    rule = cc.REFUSAL_RULE_SENTINEL
    assert "written in the same language as the question" in rule
    assert "student's language" not in rule
    assert rule.startswith("If the CONTEXT does not contain the answer, begin your reply with the exact token")


def test_the_evaluation_script_tests_the_same_wording_the_app_ships():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "eval_chat_prompt.py"
    spec = importlib.util.spec_from_file_location("eval_chat_prompt", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.CANDIDATE_RULES["shipped"] == cc.REFUSAL_RULE_SENTINEL
    assert module.CANDIDATE_RULES["c2_same_language_as_question"] == cc.REFUSAL_RULE_SENTINEL


# --- through the routes ----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def retrieval_returns(monkeypatch):
    monkeypatch.setattr(cc, "retrieve", lambda q, s: [CHUNK])


@pytest.fixture()
def session(db, world):
    s = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(s)
    db.commit()
    return s


def stored(db):
    db.expire_all()
    return [m.content for m in db.query(ChatMessage).order_by(ChatMessage.id).all()]


def test_plain_route_arabic_refusal_hides_sources_and_stores_no_token(client, db, world, session, fake_ollama):
    fake_ollama(default={"message": {"content": f"{TOKEN} {AR_REFUSAL}"}})
    r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": "ما هي عاصمة اليابان؟"})
    assert r.status_code == 200
    body = r.json()
    assert body["citations"] == [] and body["message"]["content"] == AR_REFUSAL
    assert TOKEN not in json.dumps(body, ensure_ascii=False)
    assert all(TOKEN not in m for m in stored(db))


def test_plain_route_an_answer_keeps_its_sources(client, world, session, fake_ollama):
    fake_ollama(default={"message": {"content": "Clustering groups objects. [dm.pdf, page 4]"}})
    r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": "what is clustering?"})
    assert len(r.json()["citations"]) == 1


def test_plain_route_a_token_only_reply_shows_the_stand_in_in_arabic(client, db, world, session, fake_ollama):
    fake_ollama(default={"message": {"content": TOKEN}})
    r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": "ما هي عاصمة اليابان؟"})
    assert r.json()["message"]["content"] == DEFAULT_REFUSAL["ar"] and r.json()["citations"] == []


def test_the_old_english_refusal_still_hides_sources(client, world, session, fake_ollama):
    fake_ollama(default={"message": {"content": "This is not found in the lecture material."}})
    r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages", json={"content": "who won?"})
    assert r.json()["citations"] == []


def events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_stream_never_sends_the_token_even_when_split(client, db, world, session, fake_async_ollama, store_in_test_db):
    fake_async_ollama(script=["<<NOT_", "IN_MATE", "RIAL>>", " لم يتم ", "ذكر هذا."])
    r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages/stream", json={"content": "ما هي عاصمة اليابان؟"})
    evs = events(r)
    assert TOKEN not in r.content.decode("utf-8") and "NOT_IN" not in r.content.decode("utf-8")
    text = "".join(e["text"] for e in evs if e["type"] == "token")
    assert text == "لم يتم ذكر هذا."
    done = evs[-1]
    assert done["type"] == "done" and done["message"]["content"] == "لم يتم ذكر هذا."
    assert done["citations"] == [], "the early citations event is withdrawn for a refusal"
    assert evs[0]["citations"], "sources are still announced before the answer is known"
    assert all(TOKEN not in m for m in stored(db))


def test_stream_a_token_only_reply_shows_the_stand_in(client, db, world, session, fake_async_ollama, store_in_test_db):
    fake_async_ollama(script=[TOKEN])
    r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages/stream", json={"content": "ما هي عاصمة اليابان؟"})
    evs = events(r)
    assert [e["type"] for e in evs] == ["citations", "token", "done"]
    assert evs[1]["text"] == DEFAULT_REFUSAL["ar"] == evs[-1]["message"]["content"]


def test_stream_answers_are_unaffected(client, world, session, fake_async_ollama, store_in_test_db):
    fake_async_ollama(script=["Cluster", "ing groups ", "things. <", " not a token"])
    r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages/stream", json={"content": "what is clustering?"})
    evs = events(r)
    assert "".join(e["text"] for e in evs if e["type"] == "token") == "Clustering groups things. < not a token"
    assert evs[-1]["citations"], "a real answer keeps its sources"


def test_the_stored_history_never_teaches_the_model_to_emit_the_token_raw(client, db, world, session, fake_ollama):
    fake = fake_ollama(default={"message": {"content": f"{TOKEN} {AR_REFUSAL}"}})
    c = login(client, world["alice"])
    c.post(f"/chat/sessions/{session.id}/messages", json={"content": "q1?"})
    c.post(f"/chat/sessions/{session.id}/messages", json={"content": "q2?"})
    second_prompt = fake.kwargs[1]["messages"]
    replayed = [m["content"] for m in second_prompt if m["role"] == "assistant"]
    assert replayed == [AR_REFUSAL], "history carries the clean sentence, not the token"


def test_stream_releases_a_held_back_fragment_at_the_end(client, world, session, fake_async_ollama, store_in_test_db):
    """A reply that ends on something that merely looks like the start of the token."""
    fake_async_ollama(script=["The operator is <<NOT_", "IN"])
    r = login(client, world["alice"]).post(f"/chat/sessions/{session.id}/messages/stream", json={"content": "what is it?"})
    evs = events(r)
    shown = "".join(e["text"] for e in evs if e["type"] == "token")
    assert shown == "The operator is <<NOT_IN" == evs[-1]["message"]["content"], "what is shown equals what is stored"
