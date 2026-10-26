"""Logout invalidates the token (not just the browser's cookie); login limits honour TRUSTED_PROXY."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt as pyjwt
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.auth.jwt import create_access_token, token_claims
from app.config import settings
from app.models import RevokedToken

from .conftest import login

ROOT = Path(__file__).resolve().parent.parent


def test_tokens_carry_a_unique_id():
    a, b = token_claims(create_access_token(1)), token_claims(create_access_token(1))
    assert a["jti"] and a["jti"] != b["jti"]


def test_a_token_stops_working_after_logout_even_if_someone_kept_it(client, db, world):
    c = login(client, world["alice"])
    stolen = c.cookies.get(settings.cookie_name)
    assert c.get("/auth/me").status_code == 200
    assert c.post("/auth/logout").status_code == 204
    assert db.query(RevokedToken).count() == 1
    client.cookies.clear()
    client.cookies.set(settings.cookie_name, stolen)
    assert client.get("/auth/me").status_code == 401
    assert client.get("/chat/sessions").status_code == 401


def test_logout_only_ends_that_token_other_sessions_of_the_same_student_survive(client, db, world):
    first = create_access_token(world["alice"].id)
    second = create_access_token(world["alice"].id)
    client.cookies.set(settings.cookie_name, first)
    client.post("/auth/logout")
    client.cookies.clear()
    client.cookies.set(settings.cookie_name, second)
    assert client.get("/auth/me").status_code == 200


def test_logout_without_a_cookie_or_with_garbage_is_still_204(client, db):
    client.cookies.clear()
    assert client.post("/auth/logout").status_code == 204
    client.cookies.set(settings.cookie_name, "not-a-token")
    assert client.post("/auth/logout").status_code == 204
    assert db.query(RevokedToken).count() == 0


def test_logging_out_twice_does_not_duplicate_the_entry(client, db, world):
    c = login(client, world["alice"])
    c.post("/auth/logout")
    c.post("/auth/logout")
    assert db.query(RevokedToken).count() == 1


def test_the_denylist_keeps_each_entry_only_until_its_token_would_have_expired(client, db, world):
    old = RevokedToken(jti="old", expires_at=datetime.now(UTC) - timedelta(days=1))
    db.add(old)
    db.commit()
    c = login(client, world["alice"])
    c.post("/auth/logout")
    jtis = {r.jti for r in db.query(RevokedToken)}
    assert "old" not in jtis and len(jtis) == 1
    kept = db.query(RevokedToken).one()
    assert kept.expires_at.replace(tzinfo=UTC) > datetime.now(UTC) + timedelta(days=6)


def test_tokens_issued_before_this_change_have_no_id_and_still_work_until_they_expire(client, world):
    now = datetime.now(UTC)
    legacy = pyjwt.encode({"sub": str(world["alice"].id), "iat": now, "exp": now + timedelta(hours=1)},
                          settings.jwt_secret, algorithm=settings.jwt_algorithm)
    client.cookies.set(settings.cookie_name, legacy)
    assert client.get("/auth/me").status_code == 200


def test_login_routes_are_limited_per_client_ip_as_seen_by_the_trusted_proxy():
    """The two login routes must use client_ip, or TRUSTED_PROXY would never reach them."""
    from app.rate_limit import client_ip
    from app.routers import auth

    keys = {r.endpoint.__name__: r for r in auth.router.routes}
    import inspect as _inspect

    source = _inspect.getsource(auth)
    assert source.count("key_func=client_ip") == 2 and "get_remote_address" not in source
    assert keys["google_login"] and keys["google_callback"] and client_ip


def test_revoked_tokens_migration_goes_up_and_down(tmp_path, monkeypatch):
    from app import models  # noqa: F401
    from app.db.base import Base

    url = f"sqlite:///{tmp_path / 'r.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("DROP TABLE revoked_tokens"))
        c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        c.execute(text("INSERT INTO alembic_version VALUES ('b7d2f4a6c308')"))
    engine.dispose()
    monkeypatch.setattr(settings, "database_url", url)
    cfg = Config()  # no ini file: env.py would call logging.fileConfig and silence other tests' loggers
    cfg.set_main_option("script_location", str(ROOT / "app" / "db" / "migrations"))
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    assert "revoked_tokens" in inspect(engine).get_table_names()
    engine.dispose()
    command.downgrade(cfg, "b7d2f4a6c308")
    engine = create_engine(url)
    assert "revoked_tokens" not in inspect(engine).get_table_names()
    assert "message_feedback" in inspect(engine).get_table_names()
    engine.dispose()
