"""scripts/backup.py: archive, checksum, safe restore. Postgres is faked here; the real round
trip against a throwaway container is the opt-in test at the bottom."""

import io
import json
import os
import sys
import tarfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import backup  # noqa: E402

URL = "postgresql://edupath:s3cret%21@localhost:5432/edupath_study"
NOW = datetime(2026, 10, 26, 10, 15, 0)


@pytest.fixture()
def data_dir(tmp_path):
    d = tmp_path / "data"
    (d / "chroma_db").mkdir(parents=True)
    (d / "chroma_db" / "chroma.sqlite3").write_bytes(b"vectors")
    (d / "images" / "1").mkdir(parents=True)
    (d / "images" / "1" / "fig.png").write_bytes(b"png")
    (d / "lectures").mkdir()
    (d / "lectures" / "a.pdf").write_bytes(b"pdf")
    (d / "flashrank").mkdir()
    (d / "flashrank" / "model.onnx").write_bytes(b"cache")
    return d


@pytest.fixture()
def fake_pg(monkeypatch):
    calls = []

    def fake_run(cmd, *, stdout=None, stdin=None, env=None):
        calls.append({"cmd": cmd, "env": env, "stdin": stdin.read() if stdin else None})
        if stdout is not None:
            stdout.write(b"PGDMP-fake-dump")

    monkeypatch.setattr(backup, "run", fake_run)
    return calls


def test_a_backup_holds_the_dump_the_archive_and_a_manifest(tmp_path, data_dir, fake_pg):
    folder = backup.make_backup(tmp_path / "out", data_dir, "edupath-postgres", database_url=URL, now=NOW)
    assert folder.name == "edupath-20261026-101500"
    assert sorted(p.name for p in folder.iterdir()) == ["data.tar.gz", "database.dump", "manifest.json"]
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["data_parts"] == ["chroma_db", "images"]
    assert backup.verify_backup(folder)["database"] == "edupath_study"
    with tarfile.open(folder / "data.tar.gz") as tar:
        names = tar.getnames()
    assert "chroma_db/chroma.sqlite3" in names and "images/1/fig.png" in names
    assert not any(n.startswith(("lectures", "flashrank")) for n in names), "source PDFs and model caches are not included by default"


def test_lectures_are_included_only_when_asked(tmp_path, data_dir, fake_pg):
    folder = backup.make_backup(tmp_path / "out", data_dir, "c", database_url=URL, include_lectures=True, now=NOW)
    with tarfile.open(folder / "data.tar.gz") as tar:
        assert "lectures/a.pdf" in tar.getnames()


def test_the_dump_goes_through_docker_exec_and_the_password_is_never_on_a_command_line(tmp_path, data_dir, fake_pg):
    backup.make_backup(tmp_path / "out", data_dir, "edupath-postgres", database_url=URL, now=NOW)
    cmd = fake_pg[0]["cmd"]
    assert cmd[:3] == ["docker", "exec", "edupath-postgres"] and "pg_dump" in cmd and "-Fc" in cmd
    assert "s3cret!" not in " ".join(cmd) and "s3cret%21" not in " ".join(cmd)
    assert fake_pg[0]["env"] is None


def test_the_host_client_route_passes_the_password_by_environment_only(tmp_path, data_dir, fake_pg):
    backup.make_backup(tmp_path / "out", data_dir, None, database_url=URL, now=NOW)
    call = fake_pg[0]
    assert call["cmd"][0] == "pg_dump" and "s3cret!" not in " ".join(call["cmd"])
    assert call["env"]["PGPASSWORD"] == "s3cret!", "decoded from the URL"


def test_a_failed_dump_leaves_no_folder_that_looks_like_a_backup(tmp_path, data_dir, monkeypatch):
    def boom(cmd, **kw):
        raise backup.BackupError("pg_dump failed (1): no such container")

    monkeypatch.setattr(backup, "run", boom)
    with pytest.raises(backup.BackupError, match="no such container"):
        backup.make_backup(tmp_path / "out", data_dir, "x", database_url=URL, now=NOW)
    assert list((tmp_path / "out").iterdir()) == []


def test_an_empty_dump_is_an_error(tmp_path, data_dir, monkeypatch):
    monkeypatch.setattr(backup, "run", lambda cmd, **kw: None)
    with pytest.raises(backup.BackupError, match="empty"):
        backup.make_backup(tmp_path / "out", data_dir, "x", database_url=URL, now=NOW)


def test_a_missing_data_folder_is_an_error_not_an_empty_archive(tmp_path, fake_pg):
    with pytest.raises(backup.BackupError, match="none of"):
        backup.make_backup(tmp_path / "out", tmp_path / "nothing", "x", database_url=URL, now=NOW)


def test_a_non_postgres_url_is_refused():
    with pytest.raises(backup.BackupError, match="not a Postgres URL"):
        backup.database_settings("sqlite:///x.db")


def test_verify_detects_a_damaged_or_missing_file(tmp_path, data_dir, fake_pg):
    folder = backup.make_backup(tmp_path / "out", data_dir, "c", database_url=URL, now=NOW)
    (folder / "database.dump").write_bytes(b"PGDMP-tampered!")
    with pytest.raises(backup.BackupError, match="checksum"):
        backup.verify_backup(folder)
    (folder / "database.dump").unlink()
    with pytest.raises(backup.BackupError, match="missing"):
        backup.verify_backup(folder)
    with pytest.raises(backup.BackupError, match="not a backup folder"):
        backup.verify_backup(tmp_path)


def test_restore_puts_the_data_back_and_feeds_the_dump_to_pg_restore(tmp_path, data_dir, fake_pg):
    folder = backup.make_backup(tmp_path / "out", data_dir, "edupath-postgres", database_url=URL, now=NOW)
    target = tmp_path / "restored"
    res = backup.restore_backup(folder, target, "edupath-postgres", database="scratch", database_url=URL)
    assert (target / "chroma_db" / "chroma.sqlite3").read_bytes() == b"vectors"
    assert (target / "images" / "1" / "fig.png").read_bytes() == b"png"
    assert res["moved_aside"] is None
    cmd = fake_pg[-1]["cmd"]
    assert "pg_restore" in cmd and cmd[cmd.index("-d") + 1] == "scratch" and "--clean" in cmd
    assert fake_pg[-1]["stdin"] == b"PGDMP-fake-dump"


def test_restore_refuses_a_non_empty_target_unless_forced_and_then_keeps_the_old_one(tmp_path, data_dir, fake_pg):
    folder = backup.make_backup(tmp_path / "out", data_dir, "c", database_url=URL, now=NOW)
    target = tmp_path / "live"
    target.mkdir()
    (target / "precious.txt").write_text("keep me")
    with pytest.raises(backup.BackupError, match="not empty"):
        backup.restore_backup(folder, target, "c", database_url=URL, skip_database=True)
    assert (target / "precious.txt").read_text() == "keep me", "nothing touched"
    res = backup.restore_backup(folder, target, "c", database_url=URL, skip_database=True, force=True)
    assert (res["moved_aside"] / "precious.txt").read_text() == "keep me"
    assert (target / "chroma_db").is_dir() and not (target / "precious.txt").exists()


def test_a_damaged_backup_is_refused_before_anything_is_restored(tmp_path, data_dir, fake_pg):
    folder = backup.make_backup(tmp_path / "out", data_dir, "c", database_url=URL, now=NOW)
    (folder / "data.tar.gz").write_bytes(b"garbage")
    target = tmp_path / "t"
    calls_before = len(fake_pg)
    with pytest.raises(backup.BackupError):
        backup.restore_backup(folder, target, "c", database_url=URL)
    assert not target.exists() and len(fake_pg) == calls_before, "neither data nor database was touched"


def test_a_hostile_archive_cannot_write_outside_the_target(tmp_path):
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo("../outside.txt")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bad"))
    with pytest.raises(backup.BackupError, match="unexpected path"):
        backup.restore_data(evil, tmp_path / "t")
    assert not (tmp_path / "outside.txt").exists()

    odd = tmp_path / "odd.tar.gz"
    with tarfile.open(odd, "w:gz") as tar:
        info = tarfile.TarInfo("secrets/x")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(backup.BackupError, match="unexpected path"):
        backup.restore_data(odd, tmp_path / "t2")

    link = tmp_path / "link.tar.gz"
    with tarfile.open(link, "w:gz") as tar:
        info = tarfile.TarInfo("images/l")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(backup.BackupError, match="link"):
        backup.restore_data(link, tmp_path / "t3")


def test_the_command_line_needs_yes_to_restore(tmp_path, data_dir, fake_pg, capsys):
    folder = backup.make_backup(tmp_path / "out", data_dir, "c", database_url=URL, now=NOW)
    rc = backup.main(["restore", str(folder), "--target-data-dir", str(tmp_path / "t"), "--no-database"])
    assert rc == 1 and not (tmp_path / "t").exists()
    assert "add --yes" in capsys.readouterr().out
    rc = backup.main(["restore", str(folder), "--target-data-dir", str(tmp_path / "t"), "--no-database", "--yes"])
    assert rc == 0 and (tmp_path / "t" / "chroma_db").is_dir()


# --- real Postgres, opt-in -----------------------------------------------------------------------


@pytest.mark.skipif(not os.environ.get("EDUPATH_TEST_PG_CONTAINER"), reason="set EDUPATH_TEST_PG_CONTAINER to a THROWAWAY postgres container")
def test_round_trip_through_a_real_throwaway_postgres(tmp_path, data_dir):
    import subprocess

    c = os.environ["EDUPATH_TEST_PG_CONTAINER"]
    psql = lambda sql, db="postgres": subprocess.run(["docker", "exec", c, "psql", "-U", "edupath", "-d", db, "-Atc", sql], capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()
    psql("DROP DATABASE IF EXISTS src_db")
    psql("DROP DATABASE IF EXISTS scratch_db")
    psql("CREATE DATABASE src_db")
    psql("CREATE TABLE t(id int, v text); INSERT INTO t VALUES (1,'مرحبا'),(2,'two')", "src_db")
    url = "postgresql://edupath:x@localhost:5432/src_db"
    folder = backup.make_backup(tmp_path / "out", data_dir, c, database_url=url, now=NOW)
    psql("CREATE DATABASE scratch_db")
    backup.restore_backup(folder, tmp_path / "restored", c, database="scratch_db", database_url=url)
    assert psql("SELECT count(*) FROM t", "scratch_db") == "2"
    assert psql("SELECT v FROM t WHERE id=1", "scratch_db") == "مرحبا"
    assert (tmp_path / "restored" / "images" / "1" / "fig.png").read_bytes() == b"png"
    psql("DROP DATABASE src_db")
    psql("DROP DATABASE scratch_db")
