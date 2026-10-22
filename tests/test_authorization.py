"""A student can only reach their own data, and only subjects in their own scope."""

import pytest

from app.models import ChatMessage, ChatSession, Lecture, QuizAttempt, QuizItem
from app.models.chat import ROLE_USER

from .conftest import login


@pytest.fixture()
def alice_data(db, world):
    """A chat session and a finished quiz attempt belonging to Alice."""
    session = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(session)
    db.flush()
    db.add(ChatMessage(session_id=session.id, role=ROLE_USER, content="secret question"))

    attempt = QuizAttempt(
        user_id=world["alice"].id, subject_id=world["subject"].id,
        score=0, total_questions=1, language="en",
    )
    db.add(attempt)
    db.flush()
    item = QuizItem(
        attempt_id=attempt.id, question_text="Q?", options_json=["a", "b", "c", "d"],
        correct_answer_index=2, source_citation="f.pdf, page 1",
    )
    db.add(item)
    db.commit()
    return {"session": session, "attempt": attempt, "item": item}


# --- chat -------------------------------------------------------------------


def test_owner_can_read_own_chat_session(client, world, alice_data):
    r = login(client, world["alice"]).get(f"/chat/sessions/{alice_data['session'].id}")
    assert r.status_code == 200
    assert r.json()["messages"][0]["content"] == "secret question"


def test_other_student_cannot_read_chat_session(client, world, alice_data):
    r = login(client, world["bob"]).get(f"/chat/sessions/{alice_data['session'].id}")
    assert r.status_code == 403
    assert "secret question" not in r.text


def test_other_student_cannot_post_to_chat_session(client, world, alice_data, fake_ollama):
    fake = fake_ollama()
    r = login(client, world["bob"]).post(
        f"/chat/sessions/{alice_data['session'].id}/messages", json={"content": "hi"}
    )
    assert r.status_code == 403
    assert fake.calls == 0  # rejected before any model work


def test_chat_listing_only_contains_own_sessions(client, world, alice_data):
    r = login(client, world["bob"]).get("/chat/sessions")
    assert r.status_code == 200 and r.json() == []
    r = login(client, world["alice"]).get("/chat/sessions")
    assert [s["id"] for s in r.json()] == [alice_data["session"].id]


def test_missing_chat_session_is_404(client, world):
    assert login(client, world["alice"]).get("/chat/sessions/9999").status_code == 404


def test_cannot_open_chat_for_subject_outside_own_year(client, world):
    r = login(client, world["alice"]).post(
        "/chat/sessions", json={"subject_id": world["other_year"].id}
    )
    assert r.status_code == 403


def test_cannot_open_chat_before_onboarding(client, db, world):
    world["bob"].university_id = None
    world["bob"].year = None
    db.commit()
    r = login(client, world["bob"]).post(
        "/chat/sessions", json={"subject_id": world["subject"].id}
    )
    assert r.status_code == 403


# --- quiz -------------------------------------------------------------------


def test_owner_can_complete_and_read_own_quiz(client, world, alice_data):
    c = login(client, world["alice"])
    attempt_id = alice_data["attempt"].id
    assert c.post(f"/quiz/{attempt_id}/complete").status_code == 200
    r = c.get(f"/quiz/{attempt_id}/results")
    assert r.status_code == 200
    assert r.json()["items"][0]["correct_answer_index"] == 2


def test_other_student_cannot_read_quiz_results(client, db, world, alice_data):
    from datetime import UTC, datetime

    alice_data["attempt"].completed_at = datetime.now(UTC)
    db.commit()
    r = login(client, world["bob"]).get(f"/quiz/{alice_data['attempt'].id}/results")
    assert r.status_code == 403
    assert "correct_answer_index" not in r.text


def test_other_student_cannot_answer_or_complete_quiz(client, db, world, alice_data):
    c = login(client, world["bob"])
    attempt_id = alice_data["attempt"].id
    r = c.post(
        f"/quiz/{attempt_id}/answer",
        json={"item_id": alice_data["item"].id, "chosen_answer_index": 2},
    )
    assert r.status_code == 403
    assert c.post(f"/quiz/{attempt_id}/complete").status_code == 403

    db.refresh(alice_data["item"])
    assert alice_data["item"].chosen_answer_index is None  # untouched


def test_quiz_history_only_contains_own_attempts(client, world, alice_data):
    assert login(client, world["bob"]).get("/quiz/history").json() == []
    ids = [a["id"] for a in login(client, world["alice"]).get("/quiz/history").json()]
    assert ids == [alice_data["attempt"].id]


def test_cannot_start_quiz_for_subject_outside_own_year(client, world, fake_ollama):
    fake = fake_ollama()
    r = login(client, world["alice"]).post(
        "/quiz/start", json={"subject_id": world["other_year"].id}
    )
    assert r.status_code == 403
    assert fake.calls == 0


# --- dashboard / images -----------------------------------------------------


def test_dashboard_activity_excludes_other_students(client, world, alice_data):
    bob_feed = login(client, world["bob"]).get("/dashboard/activity").json()
    assert bob_feed["events"] == []
    alice_feed = login(client, world["alice"]).get("/dashboard/activity").json()
    assert len(alice_feed["events"]) == 2


def test_dashboard_summary_counts_only_own_attempts(client, db, world, alice_data):
    from datetime import UTC, datetime

    alice_data["attempt"].completed_at = datetime.now(UTC)
    db.commit()
    bob = login(client, world["bob"]).get("/dashboard/summary").json()
    alice = login(client, world["alice"]).get("/dashboard/summary").json()
    assert bob["total_completed_attempts"] == 0
    assert alice["total_completed_attempts"] == 1


def test_lecture_image_requires_subject_in_scope(client, db, world):
    lecture = Lecture(
        subject_id=world["other_year"].id, filename="n.pdf", content_hash="h" * 64
    )
    db.add(lecture)
    db.commit()
    r = login(client, world["alice"]).get(f"/lectures/images/{lecture.id}/page_1_0.png")
    assert r.status_code == 403
