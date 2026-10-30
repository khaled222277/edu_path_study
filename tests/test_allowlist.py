"""The pilot sign-in allowlist: OAuth callback, ongoing sessions, the admin script, the migration."""

import logging
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.config import settings
from app.models import AllowedEmail, User
from app.routers import auth as auth_router
from app.services import allowlist

from .conftest import login

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import manage_allowlist  # noqa: E402


@pytest.fixture()
def google(monkeypatch):
    """Make the OAuth callback believe Google returned `profile`."""
    holder = {"profile": {}}

    async def fake_token(request):
        return {"userinfo": holder["profile"]}

    monkeypatch.setattr(auth_router.oauth.google, "authorize_access_token", fake_token, raising=False)
    return holder


def callback(client):
    client.cookies.clear()
    return client.get("/auth/google/callback", follow_redirects=False)


def listed(db, *emails):
    for e in emails:
        db.add(AllowedEmail(email=e))
    db.commit()


PROFILE = {"sub": "g-new", "email": "New.Student@Example.com", "name": "New Student", "email_verified": True}


# --- allowlist OFF (the default): nothing changes ---------------------------------------------------


def test_off_by_default_anyone_can_sign_in_and_a_row_is_created(client, db, google):
    assert settings.allowlist_enabled is False
    google["profile"] = PROFILE
    r = callback(client)
    assert r.status_code == 302 and r.headers["location"].rstrip("/") == settings.frontend_url.rstrip("/")
    assert settings.cookie_name in r.headers.get("set-cookie", "")
    assert db.query(User).filter_by(google_id="g-new").count() == 1


# --- allowlist ON -------------------------------------------------------------------------------------


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setattr(settings, "allowlist_enabled", True)


def test_a_listed_account_signs_in_whatever_the_case(client, db, google, on):
    listed(db, "new.student@example.com")
    google["profile"] = PROFILE  # Google returned mixed case
    r = callback(client)
    assert "signin=not_allowed" not in r.headers["location"] and settings.cookie_name in r.headers["set-cookie"]
    assert db.query(User).filter_by(google_id="g-new").count() == 1


def test_an_unlisted_account_is_refused_with_no_row_and_no_cookie(client, db, google, on):
    listed(db, "someone.else@example.com")
    google["profile"] = PROFILE
    r = callback(client)
    assert r.status_code == 302 and r.headers["location"].endswith("/?signin=not_allowed")
    assert "set-cookie" not in r.headers
    assert db.query(User).filter_by(google_id="g-new").count() == 0, "no account is created"


def test_the_refusal_is_identical_for_every_unlisted_account_and_says_nothing_about_the_list(client, db, google, on):
    listed(db, "someone.else@example.com")
    outs = []
    for sub, mail in (("g1", "a@example.com"), ("g2", "someone.else.almost@example.com"), ("g3", "")):
        google["profile"] = {"sub": sub, "email": mail, "name": "X"}
        r = callback(client)
        outs.append((r.status_code, r.headers["location"], r.text))
    assert len(set(outs)) == 1
    assert "example.com" not in outs[0][1] + outs[0][2] and "someone" not in outs[0][1] + outs[0][2]


def test_an_unverified_google_email_is_refused_even_if_listed(client, db, google, on):
    listed(db, "new.student@example.com")
    google["profile"] = {**PROFILE, "email_verified": False}
    assert callback(client).headers["location"].endswith("signin=not_allowed")
    assert db.query(User).count() == 0


def test_the_refusal_is_logged_with_a_hash_not_the_address(client, db, google, on, caplog):
    google["profile"] = PROFILE
    with caplog.at_level(logging.INFO):
        callback(client)
    text_ = caplog.text
    assert "refused" in text_ and allowlist.log_ref(PROFILE["email"]) in text_
    assert "new.student" not in text_.lower() and "example.com" not in text_.lower()


def test_an_existing_student_who_is_removed_is_signed_out_at_the_next_request(client, db, world, on):
    listed(db, "a@example.com")
    c = login(client, world["alice"])
    assert c.get("/auth/me").status_code == 200
    db.query(AllowedEmail).delete()
    db.commit()
    assert c.get("/auth/me").status_code == 401
    assert c.get("/chat/sessions").status_code == 401


def test_with_the_list_off_a_student_who_is_not_listed_keeps_working(client, db, world):
    assert login(client, world["alice"]).get("/auth/me").status_code == 200


def test_matching_helpers():
    assert allowlist.normalize("  A.B@Example.COM ") == "a.b@example.com" and allowlist.normalize(None) == ""
    assert allowlist.looks_like_email("a@b.co") and not allowlist.looks_like_email("a@b") and not allowlist.looks_like_email("a b@c.com")
    assert not allowlist.looks_like_email("@b.com") and not allowlist.looks_like_email("a@@b.com")
    assert allowlist.log_ref("A@B.com") == allowlist.log_ref(" a@b.COM ") and len(allowlist.log_ref("a@b.com")) == 10


def test_is_allowed_is_true_when_off_and_false_for_blank_when_on(db, monkeypatch):
    assert allowlist.is_allowed(db, "anyone@example.com") is True
    monkeypatch.setattr(settings, "allowlist_enabled", True)
    assert allowlist.is_allowed(db, None) is False and allowlist.is_allowed(db, "") is False


# --- the admin script -------------------------------------------------------------------------------------


def test_script_add_remove_list_are_case_insensitive_and_idempotent(db):
    assert manage_allowlist.add(db, ["A@Example.com", "b@example.com", "a@example.com"]) == (["a@example.com", "b@example.com"], ["a@example.com"], [])
    assert manage_allowlist.add(db, ["A@EXAMPLE.COM"]) == ([], ["a@example.com"], [])
    assert manage_allowlist.listing(db) == ["a@example.com", "b@example.com"]
    assert manage_allowlist.remove(db, ["B@example.com", "ghost@example.com"]) == (["b@example.com"], ["ghost@example.com"])
    assert manage_allowlist.listing(db) == ["a@example.com"]


def test_script_rejects_things_that_are_not_email_addresses(db):
    added, _, rejected = manage_allowlist.add(db, ["fine@example.com", "not an email", "x@y", ""])
    assert added == ["fine@example.com"] and rejected == ["not an email", "x@y", ""]


def test_script_import_skips_blank_lines_comments_and_a_bom(tmp_path, db):
    f = tmp_path / "emails.txt"
    f.write_text("﻿# pilot group 1\nOne@Example.com\n\n  two@example.com   # TA\n", encoding="utf-8")
    emails = manage_allowlist.read_emails(f)
    assert emails == ["One@Example.com", "two@example.com"]
    assert manage_allowlist.add(db, emails)[0] == ["one@example.com", "two@example.com"]


def test_script_command_line(tmp_path, db, monkeypatch, capsys):
    import app.db.session as dbs

    monkeypatch.setattr(dbs, "SessionLocal", lambda: db)
    assert manage_allowlist.main(["add", "a@example.com"]) == 0
    assert manage_allowlist.main(["list"]) == 0 and "a@example.com" in capsys.readouterr().out
    assert manage_allowlist.main(["add", "bogus"]) == 1, "a skipped entry is a non-zero exit"


# --- the migration ------------------------------------------------------------------------------------------


def test_migration_goes_up_and_down(tmp_path, monkeypatch):
    from app import models  # noqa: F401
    from app.db.base import Base

    url = f"sqlite:///{tmp_path / 'a.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("DROP TABLE quiz_item_reports"))  # created by a later migration
        c.execute(text("DROP TABLE allowed_emails"))
        c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        c.execute(text("INSERT INTO alembic_version VALUES ('c3e8a1d94f52')"))
    engine.dispose()
    monkeypatch.setattr(settings, "database_url", url)
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "app" / "db" / "migrations"))
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    assert "allowed_emails" in inspect(engine).get_table_names()
    engine.dispose()
    command.downgrade(cfg, "c3e8a1d94f52")
    engine = create_engine(url)
    assert "allowed_emails" not in inspect(engine).get_table_names() and "revoked_tokens" in inspect(engine).get_table_names()
    engine.dispose()
