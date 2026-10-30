"""scripts/delete_student.py: removes one student's data and nothing else."""

import json
import sys
from pathlib import Path

import pytest

from app.models import AllowedEmail, ChatMessage, ChatSession, MessageFeedback, QuizAttempt, QuizItem, QuizItemReport, User

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import delete_student  # noqa: E402


def fill(db, world, user):
    s = ChatSession(user_id=user.id, subject_id=world["subject"].id)
    db.add(s)
    db.flush()
    q = ChatMessage(session_id=s.id, role="user", content="سؤال خاص")
    a = ChatMessage(session_id=s.id, role="assistant", content="answer", sources=[{"source_filename": "x.pdf", "page_number": 1}])
    db.add_all([q, a])
    db.flush()
    db.add(MessageFeedback(message_id=a.id, user_id=user.id, rating=-1, comment="no"))
    att = QuizAttempt(user_id=user.id, subject_id=world["subject"].id, score=1, total_questions=1, language="en")
    db.add(att)
    db.flush()
    item = QuizItem(attempt_id=att.id, question_text="q", options_json=["a", "b"], correct_answer_index=0, chosen_answer_index=0, is_correct=True, source_citation="x.pdf, page 1")
    db.add(item)
    db.flush()
    db.add(QuizItemReport(attempt_id=att.id, item_id=item.id, reason="unclear"))
    db.commit()
    return s, a


def counts(db):
    return {m.__name__: db.query(m).count() for m in (User, ChatSession, ChatMessage, MessageFeedback, QuizAttempt, QuizItem, QuizItemReport)}


def test_one_student_is_deleted_completely_and_the_other_is_untouched(db, world):
    fill(db, world, world["alice"])
    fill(db, world, world["bob"])
    before = counts(db)
    done = delete_student.delete_student(db, delete_student.find_user(db, "A@Example.com"))
    assert done == {"chat_sessions": 1, "chat_messages": 2, "feedback_rows": 1, "quiz_attempts": 1, "quiz_items": 1, "quiz_item_reports": 1}
    after = counts(db)
    assert after == {"User": before["User"] - 1, "ChatSession": 1, "ChatMessage": 2, "MessageFeedback": 1, "QuizAttempt": 1, "QuizItem": 1, "QuizItemReport": 1}
    assert db.query(User).filter_by(email="b@example.com").one()
    assert {s.user_id for s in db.query(ChatSession)} == {world["bob"].id}
    assert {f.user_id for f in db.query(MessageFeedback)} == {world["bob"].id}


def test_ratings_other_people_gave_on_the_students_answers_go_too(db, world):
    s, a = fill(db, world, world["alice"])
    db.add(MessageFeedback(message_id=a.id, user_id=world["bob"].id, rating=1))
    db.commit()
    delete_student.delete_student(db, world["alice"])
    assert db.query(MessageFeedback).count() == 0


def test_a_student_with_no_data_is_just_the_account(db, world):
    assert delete_student.delete_student(db, world["bob"])["chat_sessions"] == 0
    assert db.query(User).filter_by(email="b@example.com").count() == 0


def test_allowlist_entry_removed_only_when_asked(db, world):
    db.add_all([AllowedEmail(email="a@example.com"), AllowedEmail(email="b@example.com")])
    db.commit()
    delete_student.delete_student(db, world["alice"], remove_from_allowlist=False)
    assert db.query(AllowedEmail).count() == 2
    delete_student.delete_student(db, world["bob"], remove_from_allowlist=True)
    assert [r.email for r in db.query(AllowedEmail)] == ["a@example.com"]


def test_lookup_is_case_insensitive_and_unknown_is_none(db, world):
    assert delete_student.find_user(db, "  A@EXAMPLE.COM ").id == world["alice"].id
    assert delete_student.find_user(db, "nobody@example.com") is None


def test_the_deleted_students_token_stops_working(client, db, world):
    from .conftest import login

    c = login(client, world["alice"])
    assert c.get("/auth/me").status_code == 200
    delete_student.delete_student(db, world["alice"])
    assert c.get("/auth/me").status_code == 401


def test_export_holds_the_students_data_and_only_theirs(db, world):
    fill(db, world, world["alice"])
    fill(db, world, world["bob"])
    data = delete_student.export(db, world["alice"])
    assert data["account"]["email"] == "a@example.com"
    assert len(data["chat_sessions"]) == 1 and len(data["quiz_attempts"]) == 1, "only their own"
    msgs = data["chat_sessions"][0]["messages"]
    assert [m["content"] for m in msgs] == ["سؤال خاص", "answer"] and msgs[1]["rating"] == "down"
    assert len(data["quiz_attempts"]) == 1 and data["quiz_attempts"][0]["items"][0]["question"] == "q"
    assert "b@example.com" not in json.dumps(data, ensure_ascii=False)


def test_command_line_needs_yes_and_can_export_first(db, world, monkeypatch, tmp_path, capsys):
    import app.db.session as dbs

    fill(db, world, world["alice"])
    monkeypatch.setattr(dbs, "SessionLocal", lambda: db)
    out = tmp_path / "e.json"
    assert delete_student.main(["a@example.com", "--export", str(out)]) == 2
    assert db.query(User).filter_by(email="a@example.com").count() == 1, "nothing deleted without --yes"
    assert "add --yes" in capsys.readouterr().out and json.loads(out.read_text(encoding="utf-8"))["account"]["email"] == "a@example.com"
    assert delete_student.main(["a@example.com", "--yes"]) == 0
    assert db.query(User).filter_by(email="a@example.com").count() == 0
    assert delete_student.main(["a@example.com", "--yes"]) == 1, "already gone"
