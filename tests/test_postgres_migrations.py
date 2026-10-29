"""The migrations on a real Postgres, starting from a database holding realistic OLD data.

Opt-in (see tests/pg_helpers.py). SQLite cannot show what Postgres does with types, defaults,
foreign keys and cascades.
"""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from .pg_helpers import ROOT, alembic_config, base_url, drop_database, dump_data, needs_postgres, new_database, psql

pytestmark = needs_postgres

OLD = "13c704704310"
OLD_TABLES = ["universities", "subjects", "users", "chat_sessions", "quiz_attempts", "quiz_items"]


@pytest.fixture()
def old_db(monkeypatch):
    """A scratch database built by `alembic upgrade 13c7...` and filled with old-schema rows."""
    name = new_database()
    url = base_url(name)
    cfg = alembic_config(monkeypatch, url)
    command.upgrade(cfg, OLD)
    sql = (ROOT / "scripts" / "seed_old_schema_rows.sql").read_text(encoding="utf-8")
    from .pg_helpers import CONTAINER, _docker, _env

    _docker("exec", "-i", CONTAINER, "psql", "-U", _env()["POSTGRES_USER"], "-d", name, "-v", "ON_ERROR_STOP=1", "-q", input=sql)
    try:
        yield name, url, cfg
    finally:
        drop_database(name)


def messages(name):
    return psql("select id||'|'||content from chat_messages order by id", name)


def test_upgrade_keeps_every_old_row_and_old_replies_read_with_no_sources(old_db):
    name, url, cfg = old_db
    before, msgs = dump_data(name, OLD_TABLES), messages(name)
    command.upgrade(cfg, "head")
    assert dump_data(name, OLD_TABLES) == before, "old tables are byte-identical after the upgrade"
    assert messages(name) == msgs, "including the Arabic text"
    assert psql("select count(*), count(sources) from chat_messages", name) == "6|0"

    from app.models import ChatSession
    from app.schemas.chat import SessionDetail

    engine = create_engine(url)
    with sessionmaker(bind=engine)() as db:
        detail = SessionDetail.model_validate(db.get(ChatSession, 2))
    engine.dispose()
    assert "التنقيب" in detail.messages[1].content
    assert [m.citations for m in detail.messages] == [[], []] and [m.feedback for m in detail.messages] == [None, None]


def test_new_structures_have_the_intended_types_defaults_and_indexes(old_db):
    name, url, cfg = old_db
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    insp = inspect(engine)
    cols = {t: {c["name"]: c for c in insp.get_columns(t)} for t in ("chat_messages", "message_feedback", "revoked_tokens")}
    assert str(cols["chat_messages"]["sources"]["type"]).upper().startswith("JSON") and cols["chat_messages"]["sources"]["nullable"]
    fb = cols["message_feedback"]
    assert str(fb["rating"]["type"]).upper() == "SMALLINT" and not fb["rating"]["nullable"]
    assert fb["comment"]["type"].length == 500 and fb["comment"]["nullable"]
    assert "now()" in str(fb["created_at"]["default"]).lower() and "now()" in str(fb["updated_at"]["default"]).lower()
    assert cols["revoked_tokens"]["jti"]["type"].length == 64
    names = {i["name"] for i in insp.get_indexes("message_feedback")} | {i["name"] for i in insp.get_indexes("revoked_tokens")}
    assert {"ix_message_feedback_message_id", "ix_revoked_tokens_expires_at"} <= names
    assert any(u["name"] == "uq_feedback_message_user" for u in insp.get_unique_constraints("message_feedback"))
    fks = {(fk["referred_table"], fk["options"].get("ondelete")) for fk in insp.get_foreign_keys("message_feedback")}
    assert fks == {("chat_messages", "CASCADE"), ("users", "CASCADE")}
    engine.dispose()


def test_the_database_enforces_one_rating_per_student_per_reply_and_cascades(old_db):
    name, url, cfg = old_db
    command.upgrade(cfg, "head")
    psql("insert into message_feedback (message_id, user_id, rating) values (2, 1, 1)", name)
    with pytest.raises(Exception, match="uq_feedback_message_user|unique"):
        psql("insert into message_feedback (message_id, user_id, rating) values (2, 1, -1)", name)
    psql("delete from chat_sessions where id = 1", name)
    assert psql("select count(*) from message_feedback", name) == "0", "deleting a chat deletes its ratings"


def test_downgrade_drops_exactly_the_new_structures_and_upgrade_again_loses_nothing_old(old_db):
    name, url, cfg = old_db
    before, msgs = dump_data(name, OLD_TABLES), messages(name)
    command.upgrade(cfg, "head")
    # fill the new structures, then downgrade: THIS is what is lost
    psql("""update chat_messages set sources = '[{"source_filename":"a.pdf","page_number":7}]' where id = 2""", name)
    psql("insert into message_feedback (message_id, user_id, rating, comment) values (2, 1, -1, 'x')", name)
    psql("insert into revoked_tokens (jti, expires_at) values ('j', now() + interval '1 day')", name)
    command.downgrade(cfg, OLD)
    assert psql("select count(*) from information_schema.tables where table_name in ('message_feedback','revoked_tokens','allowed_emails')", name) == "0"
    assert psql("select count(*) from information_schema.columns where table_name='chat_messages' and column_name='sources'", name) == "0"
    assert psql("select version_num from alembic_version", name) == OLD
    assert dump_data(name, OLD_TABLES) == before and messages(name) == msgs, "messages, users, quizzes survive the downgrade"
    command.upgrade(cfg, "head")
    assert dump_data(name, OLD_TABLES) == before and messages(name) == msgs
    assert psql("select count(*), count(sources) from chat_messages", name) == "6|0", "the dropped sources do not come back"
    assert psql("select count(*) from message_feedback", name) == "0"


def test_allowlist_table_on_postgres_stores_one_row_per_address_and_the_script_works(old_db):
    name, url, cfg = old_db
    command.upgrade(cfg, "head")
    assert psql("select data_type||' '||character_maximum_length||' '||is_nullable from information_schema.columns where table_name='allowed_emails' and column_name='email'", name) == "character varying 320 NO"
    assert psql("select count(*) from pg_indexes where tablename='allowed_emails' and indexdef like '%UNIQUE%'", name) == "1"
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import manage_allowlist

    engine = create_engine(url)
    with sessionmaker(bind=engine)() as db:
        assert manage_allowlist.add(db, ["A@Example.com", "a@example.com", "طالب@example.com"])[0] == ["a@example.com", "طالب@example.com"]
        assert manage_allowlist.listing(db) == ["a@example.com", "طالب@example.com"]
    engine.dispose()
    assert psql("select count(*) from allowed_emails", name) == "2"
