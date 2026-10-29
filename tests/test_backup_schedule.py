"""Scheduled use of scripts/backup.py: verify, retention, the run log, exit codes, the Windows task script."""

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import backup  # noqa: E402

FAKE_DB = {"user": "u", "password": "p", "host": "h", "port": "5432", "name": "d"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "chroma_db").mkdir(parents=True)
    (data / "chroma_db" / "f").write_bytes(b"v")
    monkeypatch.setattr(backup, "database_settings", lambda url=None: FAKE_DB)

    def fake_run(cmd, *, stdout=None, stdin=None, env=None):
        if stdout is not None:
            stdout.write(b"PGDMP")

    monkeypatch.setattr(backup, "run", fake_run)
    return tmp_path, data


def old_backup(out: Path, stamp: str) -> Path:
    folder = out / f"edupath-{stamp}"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({"files": {}}))
    return folder


def args(tmp_path, data, **kw):
    ns = dict(out=str(tmp_path / "out"), data_dir=str(data), pg_container="c", database=None,
              include_lectures=False, keep=7, log_file=None)
    ns.update(kw)
    return type("A", (), ns)()


def test_a_run_backs_up_verifies_logs_and_exits_zero(env, capsys):
    tmp, data = env
    assert backup.run_backup(args(tmp, data)) == 0
    log = (tmp / "out" / "backup.log").read_text(encoding="utf-8").splitlines()
    assert len(log) == 1 and " OK edupath-" in log[0] and "verified" in log[0] and "kept 1, pruned 0" in log[0]
    assert "OK" in capsys.readouterr().out


def test_only_the_newest_keep_backups_survive(env):
    tmp, data = env
    out = tmp / "out"
    old = [old_backup(out, f"2026010{d}-020000") for d in range(1, 6)]
    assert backup.run_backup(args(tmp, data, keep=3)) == 0
    names = sorted(p.name for p in backup.backup_folders(out))
    assert len(names) == 3 and names[:2] == ["edupath-20260104-020000", "edupath-20260105-020000"], names
    assert not old[0].exists() and not old[1].exists() and not old[2].exists() and old[3].exists() and old[4].exists()
    assert "pruned 3" in (out / "backup.log").read_text()


def test_keep_zero_keeps_everything(env):
    tmp, data = env
    out = tmp / "out"
    for d in range(1, 4):
        old_backup(out, f"2026010{d}-020000")
    assert backup.run_backup(args(tmp, data, keep=0)) == 0
    assert len(backup.backup_folders(out)) == 4


def test_prune_only_touches_backup_folders(tmp_path):
    out = tmp_path
    newest = old_backup(out, "20260105-020000")
    (out / "notes").mkdir()
    (out / "notes" / "important.txt").write_text("x")
    (out / "backup.log").write_text("log")
    fake = out / "edupath-20260101-020000"  # right name, no manifest: not a backup this script made
    fake.mkdir()
    (fake / "mine.txt").write_text("not yours")
    old_backup(out, "20260102-020000")
    other = out / "edupath-manual-copy"  # has a manifest, but its name is not one this script generates
    other.mkdir()
    (other / "manifest.json").write_text("{}")
    backup.prune(out, keep=1)
    assert other.exists()
    assert newest.exists() and (out / "notes" / "important.txt").exists() and (out / "backup.log").exists()
    assert (fake / "mine.txt").exists(), "a folder without a manifest is left alone"
    assert not (out / "edupath-20260102-020000").exists()


def test_prune_never_deletes_the_backup_just_made(tmp_path):
    new = old_backup(tmp_path, "20250101-000000")  # sorts as the OLDEST by name (a clock that went back)
    old_backup(tmp_path, "20260102-020000")
    old_backup(tmp_path, "20260103-020000")
    backup.prune(tmp_path, keep=1, protect=new)
    assert new.exists()


def test_a_failing_dump_exits_2_logs_it_and_prunes_nothing(env, monkeypatch, capsys):
    tmp, data = env
    out = tmp / "out"
    olds = [old_backup(out, f"2026010{d}-020000") for d in range(1, 4)]

    def broken(cmd, **kw):
        raise backup.BackupError("pg_dump failed (1): connection refused")

    monkeypatch.setattr(backup, "run", broken)
    assert backup.run_backup(args(tmp, data, keep=1)) == 2
    assert all(p.exists() for p in olds), "a failing job must not eat the backups that still work"
    assert "FAILED backup: pg_dump failed" in (out / "backup.log").read_text()
    assert "FAILED" in capsys.readouterr().err
    assert len(backup.backup_folders(out)) == 3, "and no half backup is left behind"


def test_a_backup_that_does_not_verify_exits_3_and_prunes_nothing(env, monkeypatch):
    tmp, data = env
    out = tmp / "out"
    olds = [old_backup(out, f"2026010{d}-020000") for d in range(1, 4)]

    def bad_verify(folder):
        raise backup.BackupError("database.dump does not match its checksum: the backup is damaged")

    monkeypatch.setattr(backup, "verify_backup", bad_verify)
    assert backup.run_backup(args(tmp, data, keep=1)) == 3
    assert all(p.exists() for p in olds)
    assert "FAILED verify" in (out / "backup.log").read_text()


def test_a_missing_data_folder_is_a_failure_exit(env):
    tmp, _ = env
    assert backup.run_backup(args(tmp, tmp / "nothing")) == 2


def test_two_runs_in_the_same_second_are_a_clean_failure_not_a_crash(env, monkeypatch):
    tmp, data = env

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 26, 2, 0, 0)

    monkeypatch.setattr(backup, "datetime", Frozen)
    assert backup.run_backup(args(tmp, data)) == 0
    assert backup.run_backup(args(tmp, data)) == 2


def test_the_command_line_defaults_to_keeping_seven_and_passes_the_exit_code_through(env, monkeypatch):
    tmp, data = env
    assert backup.main(["backup", "--out", str(tmp / "out"), "--data-dir", str(data)]) == 0
    assert backup.main(["backup", "--out", str(tmp / "out2"), "--data-dir", str(tmp / "missing")]) == 2
    seen = {}
    monkeypatch.setattr(backup, "run_backup", lambda a: seen.setdefault("keep", a.keep) and 0)
    backup.main(["backup", "--out", str(tmp / "o3"), "--data-dir", str(data)])
    assert seen["keep"] == 7


def test_log_lines_accumulate_one_per_run(tmp_path):
    f = tmp_path / "logs" / "b.log"
    backup.log_run(f, "OK", "one", datetime(2026, 1, 1, 2, 0, 0))
    backup.log_run(f, "FAILED", "two", datetime(2026, 1, 2, 2, 0, 0))
    assert f.read_text().splitlines() == ["2026-01-01T02:00:00 OK one", "2026-01-02T02:00:00 FAILED two"]


# --- the Windows task script -------------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell"), reason="Windows PowerShell only")
class TestScheduleScript:
    PS = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "schedule_backup.ps1")]

    def run(self, *extra):
        return subprocess.run([*self.PS, *extra], capture_output=True, text=True, encoding="utf-8")

    def test_default_run_registers_nothing_and_shows_the_command(self):
        r = self.run()
        assert r.returncode == 0 and "nothing registered" in r.stdout
        assert "backup.py" in r.stdout and "backup --keep 7" in r.stdout and "daily at 02:00" in r.stdout

    def test_register_with_dry_run_registers_nothing(self):
        r = self.run("-Register", "-DryRun", "-At", "03:15", "-Keep", "14")
        assert r.returncode == 0 and "nothing registered" in r.stdout and "03:15" in r.stdout and "--keep 14" in r.stdout

    def test_unregister_dry_run_removes_nothing(self):
        r = self.run("-Unregister", "-DryRun")
        assert r.returncode == 0 and "would remove" in r.stdout

    def test_a_bad_time_is_refused(self):
        r = self.run("-At", "25:99")
        assert r.returncode != 0 and "HH:MM" in (r.stdout + r.stderr)
