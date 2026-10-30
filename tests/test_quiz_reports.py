"""A student can flag a quiz question as bad; the reports can be exported without saying who."""

import csv
import io
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.models import QuizAttempt, QuizItem, QuizItemReport

from .conftest import login

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import export_quiz_reports  # noqa: E402


@pytest.fixture()
def quiz(db, world):
    """Alice's attempt with two items, and Bob's attempt with one."""
    a = QuizAttempt(user_id=world["alice"].id, subject_id=world["subject"].id, score=0, total_questions=2, language="ar")
    b = QuizAttempt(user_id=world["bob"].id, subject_id=world["subject"].id, score=0, total_questions=1, language="en")
    db.add_all([a, b])
    db.flush()
    items = [
        QuizItem(attempt_id=a.id, question_text="هذا الفكرة هي", options_json=["1", "2", "3", "4"], correct_answer_index=2,
                 source_citation="kdd.pdf, page 3"),
        QuizItem(attempt_id=a.id, question_text="=SUM(A1)", options_json=["a", "b", "c", "d"], correct_answer_index=0,
                 chosen_answer_index=1, is_correct=False, source_citation="kdd.pdf, page 4"),
        QuizItem(attempt_id=b.id, question_text="Bob's question", options_json=["a", "b", "c", "d"], correct_answer_index=1,
                 source_citation="kdd.pdf, page 5"),
    ]
    db.add_all(items)
    db.commit()
    return {"alice_attempt": a, "bob_attempt": b, "i1": items[0], "i2": items[1], "bobs": items[2]}


def put(client, user, world, attempt, item, body=None):
    return login(client, world[user]).put(f"/quiz/{attempt.id}/items/{item.id}/report", json=body if body is not None else {})


def test_a_student_can_report_a_question_before_answering_it(client, db, world, quiz):
    r = put(client, "alice", world, quiz["alice_attempt"], quiz["i1"], {"reason": "not_answerable"})
    assert r.status_code == 204 and r.content == b""
    row = db.query(QuizItemReport).one()
    assert (row.attempt_id, row.item_id, row.reason) == (quiz["alice_attempt"].id, quiz["i1"].id, "not_answerable")


def test_and_after_answering_it(client, db, world, quiz):
    assert quiz["i2"].chosen_answer_index is not None
    assert put(client, "alice", world, quiz["alice_attempt"], quiz["i2"], {"reason": "wrong_answer"}).status_code == 204


def test_the_reason_is_optional(client, db, world, quiz):
    assert put(client, "alice", world, quiz["alice_attempt"], quiz["i1"]).status_code == 204
    assert db.query(QuizItemReport).one().reason is None


@pytest.mark.parametrize("reason", ["not_answerable", "wrong_answer", "unclear", "other"])
def test_each_listed_reason_is_accepted(client, world, quiz, reason):
    assert put(client, "alice", world, quiz["alice_attempt"], quiz["i1"], {"reason": reason}).status_code == 204


def test_a_reason_outside_the_list_or_free_text_is_refused(client, db, world, quiz):
    assert put(client, "alice", world, quiz["alice_attempt"], quiz["i1"], {"reason": "my phone number is 555"}).status_code == 422
    assert db.query(QuizItemReport).count() == 0


def test_reporting_again_replaces_the_reason_and_keeps_one_row(client, db, world, quiz):
    put(client, "alice", world, quiz["alice_attempt"], quiz["i1"], {"reason": "unclear"})
    put(client, "alice", world, quiz["alice_attempt"], quiz["i1"], {"reason": "wrong_answer"})
    assert [r.reason for r in db.query(QuizItemReport)] == ["wrong_answer"]


def test_a_report_can_be_withdrawn(client, db, world, quiz):
    put(client, "alice", world, quiz["alice_attempt"], quiz["i1"], {"reason": "unclear"})
    r = login(client, world["alice"]).delete(f"/quiz/{quiz['alice_attempt'].id}/items/{quiz['i1'].id}/report")
    assert r.status_code == 204 and db.query(QuizItemReport).count() == 0


def test_another_students_attempt_is_a_404_for_report_and_withdraw(client, db, world, quiz):
    r = put(client, "bob", world, quiz["alice_attempt"], quiz["i1"], {"reason": "unclear"})
    assert r.status_code == 404
    assert r.json() == login(client, world["bob"]).put("/quiz/999999/items/1/report", json={}).json(), "same answer as a missing attempt"
    assert login(client, world["bob"]).delete(f"/quiz/{quiz['alice_attempt'].id}/items/{quiz['i1'].id}/report").status_code == 404
    assert db.query(QuizItemReport).count() == 0


def test_an_item_of_another_attempt_is_a_404_even_in_your_own_attempt(client, db, world, quiz):
    assert put(client, "alice", world, quiz["alice_attempt"], quiz["bobs"], {"reason": "unclear"}).status_code == 404
    assert put(client, "alice", world, quiz["alice_attempt"], type("I", (), {"id": 999999})(), {}).status_code == 404
    assert db.query(QuizItemReport).count() == 0


def test_reporting_needs_a_login(client, quiz):
    client.cookies.clear()
    assert client.put(f"/quiz/{quiz['alice_attempt'].id}/items/{quiz['i1'].id}/report", json={}).status_code == 401


def test_a_report_does_not_change_the_quiz_or_what_the_student_sees(client, db, world, quiz):
    before = login(client, world["alice"]).get("/quiz/history").json()
    put(client, "alice", world, quiz["alice_attempt"], quiz["i1"], {"reason": "unclear"})
    assert login(client, world["alice"]).get("/quiz/history").json() == before
    db.refresh(quiz["i1"])
    assert quiz["i1"].chosen_answer_index is None


# --- export -------------------------------------------------------------------------------------


def exported(db, **kw):
    out = io.StringIO()
    n = export_quiz_reports.write_csv(db, out, **kw)
    return n, list(csv.DictReader(io.StringIO(out.getvalue())))


def test_the_export_has_the_question_and_no_student_identity(client, db, world, quiz):
    put(client, "alice", world, quiz["alice_attempt"], quiz["i1"], {"reason": "not_answerable"})
    put(client, "alice", world, quiz["alice_attempt"], quiz["i2"], {"reason": "wrong_answer"})
    n, rows = exported(db)
    assert n == 2
    first = rows[0]
    assert first["question"] == "هذا الفكرة هي" and first["reason"] == "not_answerable" and first["language"] == "ar"
    assert [first[f"option_{c}"] for c in "abcd"] == ["1", "2", "3", "4"]
    assert first["marked_correct"] == "C" and first["student_chose"] == "" and first["source"] == "kdd.pdf, page 3"
    assert rows[1]["student_chose"] == "B" and rows[1]["marked_correct"] == "A"
    everything = "".join(str(v) for r in rows for v in r.values()) + ",".join(export_quiz_reports.COLUMNS)
    for secret in ("a@example.com", "Alice", "g-alice", "user_id", "email"):
        assert secret not in everything


def test_a_cell_that_would_run_as_a_formula_is_neutralised(client, db, world, quiz):
    put(client, "alice", world, quiz["alice_attempt"], quiz["i2"], {})
    _, rows = exported(db)
    assert rows[0]["question"] == "'=SUM(A1)"


def test_the_export_can_be_filtered_by_reason(client, db, world, quiz):
    put(client, "alice", world, quiz["alice_attempt"], quiz["i1"], {"reason": "unclear"})
    put(client, "alice", world, quiz["alice_attempt"], quiz["i2"], {"reason": "other"})
    assert exported(db, reason="unclear")[0] == 1
    assert exported(db, reason="wrong_answer")[0] == 0


# --- the data goes with the quiz and with the student -------------------------------------------


def test_deleting_a_student_deletes_their_reports(client, db, world, quiz):
    import delete_student

    put(client, "alice", world, quiz["alice_attempt"], quiz["i1"], {"reason": "unclear"})
    put(client, "bob", world, quiz["bob_attempt"], quiz["bobs"], {"reason": "other"})
    done = delete_student.delete_student(db, delete_student.find_user(db, "a@example.com"))
    assert done["quiz_item_reports"] == 1
    assert [r.item_id for r in db.query(QuizItemReport)] == [quiz["bobs"].id]


# --- the migration ----------------------------------------------------------------------------------

PREVIOUS, THIS = "d4f9b2c7e613", "e5a7c9b13d40"


@pytest.fixture()
def old_database(tmp_path, monkeypatch):
    from app import models  # noqa: F401
    from app.db.base import Base

    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("DROP TABLE quiz_item_reports"))
        c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        c.execute(text(f"INSERT INTO alembic_version VALUES ('{PREVIOUS}')"))
        c.execute(text("INSERT INTO universities (id, name, country) VALUES (1, 'U', 'E')"))
        c.execute(text("INSERT INTO subjects (id, university_id, year, name, code) VALUES (1, 1, 5, 'S', 'S1')"))
        c.execute(text("INSERT INTO users (id, google_id, email, name) VALUES (1, 'g', 'a@example.com', 'A')"))
        c.execute(text("INSERT INTO quiz_attempts (id, user_id, subject_id, score, total_questions, language) VALUES (1, 1, 1, 0, 1, 'en')"))
        c.execute(text("INSERT INTO quiz_items (id, attempt_id, question_text, options_json, correct_answer_index, source_citation) "
                       "VALUES (1, 1, 'old question', '[\"a\",\"b\",\"c\",\"d\"]', 0, 'x.pdf, page 1')"))
    engine.dispose()
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", url)
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "app" / "db" / "migrations"))
    return url, cfg


def tables(url):
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_adds_the_table_and_keeps_old_quizzes(old_database):
    url, cfg = old_database
    assert "quiz_item_reports" not in tables(url)
    command.upgrade(cfg, THIS)
    assert "quiz_item_reports" in tables(url)
    engine = create_engine(url)
    with engine.begin() as c:
        assert c.execute(text("SELECT question_text FROM quiz_items WHERE id = 1")).scalar() == "old question"
        c.execute(text("INSERT INTO quiz_item_reports (attempt_id, item_id, reason) VALUES (1, 1, 'unclear')"))
        assert c.execute(text("SELECT reason FROM quiz_item_reports")).scalar() == "unclear"
    engine.dispose()


def test_one_report_per_item_is_enforced_by_the_database(old_database):
    url, cfg = old_database
    command.upgrade(cfg, THIS)
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO quiz_item_reports (attempt_id, item_id) VALUES (1, 1)"))
    with pytest.raises(Exception):
        with engine.begin() as c:
            c.execute(text("INSERT INTO quiz_item_reports (attempt_id, item_id) VALUES (1, 1)"))
    engine.dispose()


def test_downgrade_removes_only_the_table(old_database):
    url, cfg = old_database
    command.upgrade(cfg, THIS)
    command.downgrade(cfg, PREVIOUS)
    assert "quiz_item_reports" not in tables(url)
    engine = create_engine(url)
    with engine.connect() as c:
        assert c.execute(text("SELECT question_text FROM quiz_items WHERE id = 1")).scalar() == "old question"
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == PREVIOUS
    engine.dispose()


def test_there_is_still_a_single_migration_head_and_it_is_this_one():
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "app" / "db" / "migrations"))
    assert ScriptDirectory.from_config(cfg).get_heads() == [THIS]
