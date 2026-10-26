"""Opt-in tests against a REAL Postgres. Set EDUPATH_TEST_PG_CONTAINER to the name of a
THROWAWAY postgres container started like

    docker run -d --name edupath-pg-final -e POSTGRES_USER=edupath -e POSTGRES_PASSWORD=anything \\
        -p 127.0.0.1:55432:5432 postgres:16

Each test creates its own scratch database inside it and drops it afterwards. Never point this
at a container that holds real data.
"""

import json
import os
import re
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONTAINER = os.environ.get("EDUPATH_TEST_PG_CONTAINER")
needs_postgres = pytest.mark.skipif(
    not CONTAINER, reason="set EDUPATH_TEST_PG_CONTAINER to a THROWAWAY postgres container (see tests/pg_helpers.py)"
)


def _docker(*args, input=None) -> str:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", input=input)
    if proc.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {proc.stderr.strip()}")  # psql's own message, e.g. the violated constraint
    return proc.stdout


def psql(sql: str, database: str = "postgres", user: str | None = None) -> str:
    return _docker("exec", CONTAINER, "psql", "-U", user or _env()["POSTGRES_USER"], "-d", database, "-Atc", sql).strip()


def _env() -> dict:
    info = json.loads(_docker("inspect", CONTAINER))[0]
    return dict(e.split("=", 1) for e in info["Config"]["Env"] if "=" in e)


def base_url(database: str) -> str:
    """postgresql://user:pw@127.0.0.1:PORT/database for the throwaway container's published port."""
    env = _env()
    port_line = _docker("port", CONTAINER, "5432/tcp").splitlines()
    if not port_line:
        pytest.skip("the throwaway container must publish 5432 (-p 127.0.0.1:55432:5432)")
    port = re.search(r":(\d+)$", port_line[0].strip()).group(1)
    return f"postgresql://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}@127.0.0.1:{port}/{database}"


def new_database() -> str:
    name = "t_" + uuid.uuid4().hex[:10]
    psql(f"CREATE DATABASE {name}")
    return name


def drop_database(name: str) -> None:
    psql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def alembic_config(monkeypatch, url: str):
    from alembic.config import Config

    from app.config import settings

    monkeypatch.setattr(settings, "database_url", url)
    cfg = Config()  # no ini file: env.py would call logging.fileConfig and silence other tests' loggers
    cfg.set_main_option("script_location", str(ROOT / "app" / "db" / "migrations"))
    return cfg


def dump_data(database: str, tables: list[str]) -> str:
    """pg_dump --data-only of `tables`, without the random \\restrict token newer pg_dump prints."""
    args = ["exec", CONTAINER, "pg_dump", "-U", _env()["POSTGRES_USER"], "-d", database, "--data-only", "--inserts"]
    for t in tables:
        args += ["-t", t]
    return "\n".join(line for line in _docker(*args).splitlines() if not re.match(r"^\\(un)?restrict ", line))
