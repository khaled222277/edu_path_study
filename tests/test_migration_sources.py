"""The chat_messages.sources migration: up and down on a database holding an old row."""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parent.parent
PREVIOUS, THIS = "13c704704310", "a1c5e7b9d201"


@pytest.fixture()
def old_database(tmp_path, monkeypatch):
    """A SQLite file shaped like the schema before this migration, with one stored reply."""
    from app.db.base import Base
    from app import models  # noqa: F401  (registers the tables)

    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("ALTER TABLE chat_messages DROP COLUMN sources"))
        c.execute(text("DROP TABLE message_feedback"))  # a later migration's table
        c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        c.execute(text(f"INSERT INTO alembic_version VALUES ('{PREVIOUS}')"))
        c.execute(text("INSERT INTO universities (id, name, country) VALUES (1, 'U', 'E')"))
        c.execute(text("INSERT INTO subjects (id, university_id, year, name, code) VALUES (1, 1, 5, 'S', 'S1')"))
        c.execute(text("INSERT INTO users (id, google_id, email, name) VALUES (1, 'g', 'a@example.com', 'A')"))
        c.execute(text("INSERT INTO chat_sessions (id, user_id, subject_id) VALUES (1, 1, 1)"))
        c.execute(text("INSERT INTO chat_messages (id, session_id, role, content) VALUES (1, 1, 'assistant', 'old answer')"))
    engine.dispose()
    # alembic/env.py reads settings.database_url, which is fixed at import: point it at the file
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", url)
    cfg = Config()  # no ini file: env.py would call logging.fileConfig and silence other tests' loggers
    cfg.set_main_option("script_location", str(ROOT / "app" / "db" / "migrations"))
    return url, cfg


def columns(url):
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("chat_messages")}
    finally:
        engine.dispose()


def test_upgrade_adds_a_nullable_column_and_keeps_the_old_reply(old_database):
    url, cfg = old_database
    assert "sources" not in columns(url)
    command.upgrade(cfg, THIS)
    assert "sources" in columns(url)
    engine = create_engine(url)
    with engine.connect() as c:
        row = c.execute(text("SELECT content, sources FROM chat_messages WHERE id = 1")).one()
    assert row.content == "old answer" and row.sources is None
    engine.dispose()


def test_an_old_reply_is_served_with_an_empty_sources_list(old_database):
    url, cfg = old_database
    command.upgrade(cfg, "head")  # the ORM reads the newest schema
    from sqlalchemy.orm import sessionmaker
    from app.models import ChatMessage
    from app.schemas.chat import MessageOut

    engine = create_engine(url)
    with sessionmaker(bind=engine)() as db:
        assert MessageOut.model_validate(db.get(ChatMessage, 1)).citations == []
    engine.dispose()


def test_downgrade_removes_the_column_and_keeps_the_rest(old_database):
    url, cfg = old_database
    command.upgrade(cfg, THIS)
    command.downgrade(cfg, PREVIOUS)
    assert "sources" not in columns(url)
    engine = create_engine(url)
    with engine.connect() as c:
        assert c.execute(text("SELECT content FROM chat_messages WHERE id = 1")).scalar() == "old answer"
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == PREVIOUS
    engine.dispose()


def test_the_migration_chain_has_a_single_head_and_this_migration_is_in_it():
    from alembic.script import ScriptDirectory

    cfg = Config()  # no ini file: env.py would call logging.fileConfig and silence other tests' loggers
    cfg.set_main_option("script_location", str(ROOT / "app" / "db" / "migrations"))
    scripts = ScriptDirectory.from_config(cfg)
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert THIS in [r.revision for r in scripts.walk_revisions()]
