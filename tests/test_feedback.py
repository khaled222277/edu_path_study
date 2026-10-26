"""Thumbs up / down on assistant replies: API, privacy of the export, migration."""

import csv
import io
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.models import ChatMessage, ChatSession, MessageFeedback

from .conftest import login

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import export_feedback  # noqa: E402


@pytest.fixture()
def reply(db, world):
    s = ChatSession(user_id=world["alice"].id, subject_id=world["subject"].id)
    db.add(s)
    db.flush()
    db.add(ChatMessage(session_id=s.id, role="user", content="what is support?"))
    m = ChatMessage(session_id=s.id, role="assistant", content="Support is a ratio.",
                    sources=[{"source_filename": "dm.pdf", "page_number": 4, "content_type": "text", "score": 0.9, "image_url": None}])
    db.add(m)
    db.commit()
    return m


def rate(client, user, message_id, rating="up", comment=None):
    body = {"rating": rating}
    if comment is not None:
        body["comment"] = comment
    return login(client, user).put(f"/chat/messages/{message_id}/feedback", json=body)


def test_a_student_can_rate_their_own_reply_and_see_it_when_the_chat_is_reopened(client, db, world, reply):
    assert rate(client, world["alice"], reply.id, "down", "  wrong page  ").status_code == 204
    row = db.query(MessageFeedback).one()
    assert (row.rating, row.comment, row.user_id) == (-1, "wrong page", world["alice"].id)
    msgs = login(client, world["alice"]).get(f"/chat/sessions/{reply.session_id}").json()["messages"]
    assert [m["feedback"] for m in msgs] == [None, "down"]


def test_rating_again_replaces_it_and_clearing_removes_it(client, db, world, reply):
    rate(client, world["alice"], reply.id, "down", "bad")
    rate(client, world["alice"], reply.id, "up")
    row = db.query(MessageFeedback).one()
    assert (row.rating, row.comment) == (1, None)
    assert login(client, world["alice"]).delete(f"/chat/messages/{reply.id}/feedback").status_code == 204
    assert db.query(MessageFeedback).count() == 0
    assert login(client, world["alice"]).delete(f"/chat/messages/{reply.id}/feedback").status_code == 204, "idempotent"


def test_another_students_reply_looks_like_it_does_not_exist(client, db, world, reply):
    assert rate(client, world["bob"], reply.id).status_code == 404
    assert login(client, world["bob"]).delete(f"/chat/messages/{reply.id}/feedback").status_code == 404
    assert db.query(MessageFeedback).count() == 0


def test_only_assistant_replies_can_be_rated_and_a_missing_one_is_404(client, world, db, reply):
    question = db.query(ChatMessage).filter_by(role="user").one()
    assert rate(client, world["alice"], question.id).status_code == 404
    assert rate(client, world["alice"], 99999).status_code == 404


def test_bad_input_is_rejected(client, world, reply):
    assert rate(client, world["alice"], reply.id, "meh").status_code == 422
    assert rate(client, world["alice"], reply.id, "up", "x" * 501).status_code == 422
    c = login(client, world["alice"])
    assert c.put(f"/chat/messages/{reply.id}/feedback", json={}).status_code == 422


def test_signed_out_is_401(client, reply):
    client.cookies.clear()
    assert client.put(f"/chat/messages/{reply.id}/feedback", json={"rating": "up"}).status_code == 401


def test_deleting_the_chat_deletes_its_feedback(client, db, world, reply):
    rate(client, world["alice"], reply.id)
    db.delete(db.get(ChatSession, reply.session_id))
    db.commit()
    assert db.query(MessageFeedback).count() == 0


# --- the export ---------------------------------------------------------------------------------


def export(db, **kw):
    out = io.StringIO()
    n = export_feedback.write_csv(db, out, **kw)
    return n, list(csv.DictReader(io.StringIO(out.getvalue())))


def test_export_has_the_question_answer_rating_and_no_personal_data(client, db, world, reply):
    rate(client, world["alice"], reply.id, "down", "wrong page")
    n, rows = export(db)
    assert n == 1
    r = rows[0]
    assert (r["rating"], r["comment"], r["subject_code"]) == ("down", "wrong page", "DM501")
    assert (r["question"], r["answer"], r["sources_shown"], r["message_id"]) == ("what is support?", "Support is a ratio.", "1", str(reply.id))
    blob = io.StringIO()
    export_feedback.write_csv(db, blob)
    text_ = blob.getvalue()
    for private in ("a@example.com", "Alice", "g-alice"):
        assert private not in text_
    assert list(rows[0].keys()) == export_feedback.COLUMNS
    assert None not in rows[0], "no unnamed extra column"
    raw = list(csv.reader(io.StringIO(text_)))
    assert {len(r) for r in raw} == {len(export_feedback.COLUMNS)}, "every row has exactly the documented columns"


def test_export_filters_by_rating(client, db, world, reply):
    rate(client, world["alice"], reply.id, "up")
    assert export(db, rating="down")[0] == 0
    assert export(db, rating="up")[0] == 1


def test_export_neutralises_spreadsheet_formulas(client, db, world, reply):
    rate(client, world["alice"], reply.id, "down", "=HYPERLINK(\"http://evil\")")
    _, rows = export(db)
    assert rows[0]["comment"].startswith("'=")
    assert export_feedback.safe_cell("@x") == "'@x" and export_feedback.safe_cell("ok") == "ok"
    assert export_feedback.safe_cell(None) == "" and export_feedback.safe_cell(0) == "0"


def test_export_keeps_arabic_intact(client, db, world, reply):
    reply.content = "الدعم نسبة [dm.pdf, page 4]"
    db.commit()
    rate(client, world["alice"], reply.id, "up", "ممتاز")
    _, rows = export(db)
    assert rows[0]["answer"] == "الدعم نسبة [dm.pdf, page 4]" and rows[0]["comment"] == "ممتاز"


# --- the migration ------------------------------------------------------------------------------


def test_feedback_migration_goes_up_and_down(tmp_path, monkeypatch):
    from app import models  # noqa: F401
    from app.config import settings
    from app.db.base import Base

    url = f"sqlite:///{tmp_path / 'm.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("DROP TABLE message_feedback"))
        c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        c.execute(text("INSERT INTO alembic_version VALUES ('a1c5e7b9d201')"))
    engine.dispose()
    monkeypatch.setattr(settings, "database_url", url)
    cfg = Config()  # no ini file: env.py would call logging.fileConfig and silence other tests' loggers
    cfg.set_main_option("script_location", str(ROOT / "app" / "db" / "migrations"))

    command.upgrade(cfg, "head")
    engine = create_engine(url)
    assert "message_feedback" in inspect(engine).get_table_names()
    engine.dispose()
    command.downgrade(cfg, "a1c5e7b9d201")
    engine = create_engine(url)
    assert "message_feedback" not in inspect(engine).get_table_names()
    assert "chat_messages" in inspect(engine).get_table_names(), "only the new table is removed"
    engine.dispose()
