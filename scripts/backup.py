"""Back up and restore the two things that cannot be rebuilt cheaply: Postgres and data/.

    python scripts/backup.py backup  [--out backups] [--pg-container edupath-postgres] [--include-lectures]
                                     [--keep 7] [--log-file backups/backup.log]
    python scripts/backup.py verify  backups/edupath-20261026-101500
    python scripts/backup.py restore backups/edupath-20261026-101500 --yes [--target-data-dir DIR]
                                     [--pg-container NAME] [--database NAME] [--force]

Scheduled use (Task Scheduler / cron, see scripts/schedule_backup.ps1 and docs/PILOT.md): `backup`
verifies what it just wrote, then keeps only the newest --keep backups (default 7; 0 = keep all),
appends one line per run to --log-file, and exits non-zero on any failure (2 = backup failed,
3 = the new backup did not verify). Old backups are only pruned after a successful, verified one, so
a failing job never eats the backups that still work.

A backup is a folder holding

    database.dump   pg_dump -Fc of the app database (users, chats, feedback, quizzes, ...)
    data.tar.gz     data/chroma_db and data/images (add data/lectures with --include-lectures)
    manifest.json   what was saved, sizes and SHA-256 of both files

Postgres is reached through `docker exec` into the compose container (default
edupath-postgres: no password or client install needed on the host). Without --pg-container
the host's own pg_dump / pg_restore are used, with DATABASE_URL from .env (the password goes
to them through PGPASSWORD, never on a command line or into the output).

restore never overwrites silently: it needs --yes, checks the checksums first, and moves an
existing, non-empty target data folder aside (data.before-restore-<time>) instead of deleting it.
The database restore uses --clean --if-exists, so it REPLACES the tables of the target
database: point --database at a scratch database to rehearse.

Backing up while ingestion is running can capture a half-written Chroma store; run it when
no ingestion is going on (the API only reads Chroma, so serving students is fine).
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
DUMP = "database.dump"
ARCHIVE = "data.tar.gz"
MANIFEST = "manifest.json"
DATA_PARTS = ("chroma_db", "images")
EXTRA_PARTS = ("lectures",)


class BackupError(Exception):
    """Something the operator must fix; the message says what."""


# --- helpers ----------------------------------------------------------------------------------


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def database_settings(database_url: str | None = None) -> dict:
    """user / password / host / port / name from DATABASE_URL (the password is not printed)."""
    if database_url is None:
        from app.config import settings

        database_url = settings.database_url
    u = urlparse(database_url)
    if not u.scheme.startswith("postgres"):
        raise BackupError("DATABASE_URL is not a Postgres URL; nothing to dump")
    return {
        "user": unquote(u.username or ""),
        "password": unquote(u.password or ""),
        "host": u.hostname or "localhost",
        "port": str(u.port or 5432),
        "name": (u.path or "/").lstrip("/"),
    }


def run(cmd: list[str], *, stdout=None, stdin=None, env=None) -> None:
    proc = subprocess.run(cmd, stdout=stdout, stdin=stdin, stderr=subprocess.PIPE, env=env)
    if proc.returncode != 0:
        # stderr of pg_dump / pg_restore / docker: never contains the password
        raise BackupError(f"{Path(cmd[0]).name} failed ({proc.returncode}): {proc.stderr.decode(errors='replace').strip()[:600]}")


def pg_commands(container: str | None, db: dict, database: str | None):
    """(dump_cmd, restore_cmd, env) for the docker-exec or the host-client route."""
    name = database or db["name"]
    if container:
        base = ["docker", "exec"]
        return (
            base + [container, "pg_dump", "-U", db["user"], "-d", name, "-Fc"],
            base + ["-i", container, "pg_restore", "-U", db["user"], "-d", name, "--clean", "--if-exists", "--no-owner"],
            None,
        )
    conn = ["-h", db["host"], "-p", db["port"], "-U", db["user"]]
    env = {**os.environ, "PGPASSWORD": db["password"]}
    return (
        ["pg_dump", *conn, "-d", name, "-Fc"],
        ["pg_restore", *conn, "-d", name, "--clean", "--if-exists", "--no-owner"],
        env,
    )


# --- backup ------------------------------------------------------------------------------------


def archive_data(data_dir: Path, archive: Path, include_lectures: bool = False) -> list[str]:
    parts = list(DATA_PARTS) + (list(EXTRA_PARTS) if include_lectures else [])
    present = [p for p in parts if (data_dir / p).is_dir()]
    if not present:
        raise BackupError(f"none of {', '.join(parts)} found in {data_dir}")
    with tarfile.open(archive, "w:gz") as tar:
        for part in present:
            tar.add(data_dir / part, arcname=part)
    return present


def make_backup(out_dir: Path, data_dir: Path, container: str | None, database: str | None = None,
                include_lectures: bool = False, database_url: str | None = None, now: datetime | None = None) -> Path:
    db = database_settings(database_url)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    folder = out_dir / f"edupath-{stamp}"
    try:
        folder.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise BackupError(f"{folder.name} already exists (two runs in the same second?)") from None
    try:
        dump_cmd, _, env = pg_commands(container, db, database)
        with open(folder / DUMP, "wb") as fh:
            run(dump_cmd, stdout=fh, env=env)
        if (folder / DUMP).stat().st_size == 0:
            raise BackupError("pg_dump produced an empty file")
        parts = archive_data(data_dir, folder / ARCHIVE, include_lectures)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)  # never leave a half backup that looks complete
        raise
    manifest = {
        "created": (now or datetime.now()).isoformat(timespec="seconds"),
        "database": database or db["name"],
        "data_parts": parts,
        "files": {n: {"bytes": (folder / n).stat().st_size, "sha256": sha256(folder / n)} for n in (DUMP, ARCHIVE)},
    }
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return folder


# --- retention and the run log -------------------------------------------------------------------


def backup_folders(out_dir: Path) -> list[Path]:
    """Folders in `out_dir` that look like backups made by this script (name and manifest), oldest first."""
    if not out_dir.is_dir():
        return []
    found = [p for p in out_dir.iterdir()
             if p.is_dir() and re.fullmatch(r"edupath-\d{8}-\d{6}", p.name) and (p / MANIFEST).is_file()]
    return sorted(found, key=lambda p: p.name)


def prune(out_dir: Path, keep: int, protect: Path | None = None) -> list[Path]:
    """Delete all but the newest `keep` backups (never `protect`, never anything that is not a backup)."""
    if keep <= 0:
        return []
    folders = backup_folders(out_dir)
    doomed = [p for p in folders[: max(0, len(folders) - keep)] if p != protect]
    for p in doomed:
        shutil.rmtree(p)
    return doomed


def log_run(log_file: Path | None, status: str, detail: str, now: datetime | None = None) -> str:
    line = f"{(now or datetime.now()).isoformat(timespec='seconds')} {status} {detail}"
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return line


# --- verify / restore ---------------------------------------------------------------------------


def verify_backup(folder: Path) -> dict:
    """Raise BackupError unless every file is present and matches the manifest."""
    mpath = folder / MANIFEST
    if not mpath.is_file():
        raise BackupError(f"{folder} has no {MANIFEST}: not a backup folder")
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    for name, info in manifest["files"].items():
        path = folder / name
        if not path.is_file():
            raise BackupError(f"{name} is missing from the backup")
        if path.stat().st_size != info["bytes"] or sha256(path) != info["sha256"]:
            raise BackupError(f"{name} does not match its checksum: the backup is damaged")
    return manifest


def _safe_members(tar: tarfile.TarFile, allowed: set[str]):
    for member in tar.getmembers():
        top = Path(member.name).parts[0] if Path(member.name).parts else ""
        if member.name.startswith(("/", "\\")) or ".." in Path(member.name).parts or top not in allowed:
            raise BackupError(f"unexpected path in the archive: {member.name!r}")
        if member.issym() or member.islnk() or member.isdev():
            raise BackupError(f"unexpected link or device in the archive: {member.name!r}")
        yield member


def restore_data(archive: Path, target: Path, force: bool = False, now: datetime | None = None) -> Path | None:
    """Unpack into `target`. Returns where an existing non-empty folder was moved, if it was."""
    moved = None
    if target.exists() and any(target.iterdir()):
        if not force:
            raise BackupError(f"{target} is not empty; use --force to move it aside and restore into a fresh folder")
        moved = target.with_name(f"{target.name}.before-restore-{(now or datetime.now()).strftime('%Y%m%d-%H%M%S')}")
        target.rename(moved)
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = list(_safe_members(tar, set(DATA_PARTS) | set(EXTRA_PARTS)))
        tar.extractall(target, members=members, filter="data")
    return moved


def restore_backup(folder: Path, target_data_dir: Path, container: str | None, database: str | None = None,
                   force: bool = False, database_url: str | None = None, skip_database: bool = False) -> dict:
    manifest = verify_backup(folder)
    moved = restore_data(folder / ARCHIVE, target_data_dir, force)
    if not skip_database:
        _, restore_cmd, env = pg_commands(container, database_settings(database_url), database)
        with open(folder / DUMP, "rb") as fh:
            run(restore_cmd, stdin=fh, env=env)
    return {"manifest": manifest, "moved_aside": moved}


# --- command line -------------------------------------------------------------------------------


def run_backup(args) -> int:
    """backup -> verify -> prune -> log. Exit 0 ok, 2 backup failed, 3 the new backup did not verify."""
    out = Path(args.out)
    log_file = Path(args.log_file) if args.log_file else out / "backup.log"
    try:
        folder = make_backup(out, Path(args.data_dir), args.pg_container or None, args.database, args.include_lectures)
    except BackupError as e:
        print(log_run(log_file, "FAILED", f"backup: {e}"), file=sys.stderr)
        return 2
    try:
        manifest = verify_backup(folder)
    except BackupError as e:
        print(log_run(log_file, "FAILED", f"verify {folder.name}: {e}"), file=sys.stderr)
        return 3
    pruned = prune(out, args.keep, protect=folder)
    sizes = ", ".join(f"{n} {i['bytes']:,} B" for n, i in manifest["files"].items())
    print(log_run(log_file, "OK", f"{folder.name} ({sizes}); verified; kept {len(backup_folders(out))}, pruned {len(pruned)}"))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup")
    b.add_argument("--out", default=str(ROOT / "backups"))
    b.add_argument("--data-dir", default=str(ROOT / "data"))
    b.add_argument("--pg-container", default="edupath-postgres", help="docker container running Postgres ('' = use host pg_dump)")
    b.add_argument("--database", help="database name (default: from DATABASE_URL)")
    b.add_argument("--include-lectures", action="store_true", help="also save data/lectures (the source PDFs)")
    b.add_argument("--keep", type=int, default=7, help="keep this many newest backups after a successful one (0 = keep all)")
    b.add_argument("--log-file", help="append one line per run here (default: <out>/backup.log)")

    v = sub.add_parser("verify")
    v.add_argument("folder")

    r = sub.add_parser("restore")
    r.add_argument("folder")
    r.add_argument("--target-data-dir", default=str(ROOT / "data"))
    r.add_argument("--pg-container", default="edupath-postgres")
    r.add_argument("--database", help="database to restore INTO (default: from DATABASE_URL); use a scratch one to rehearse")
    r.add_argument("--force", action="store_true", help="move a non-empty target data folder aside and restore into a fresh one")
    r.add_argument("--no-database", action="store_true", help="restore only data/")
    r.add_argument("--yes", action="store_true", help="really do it")
    args = p.parse_args(argv)

    try:
        if args.cmd == "backup":
            return run_backup(args)
        elif args.cmd == "verify":
            m = verify_backup(Path(args.folder))
            print(f"OK: {Path(args.folder).name} made {m['created']}, data: {', '.join(m['data_parts'])}")
        else:
            m = verify_backup(Path(args.folder))
            target = args.database or m["database"]
            print(f"backup made {m['created']}; will restore data/ into {args.target_data_dir}"
                  + ("" if args.no_database else f" and REPLACE the tables of database '{target}'"))
            if not args.yes:
                print("nothing done: add --yes to proceed")
                return 1
            res = restore_backup(Path(args.folder), Path(args.target_data_dir), args.pg_container or None,
                                 args.database, args.force, skip_database=args.no_database)
            if res["moved_aside"]:
                print(f"previous data folder kept at {res['moved_aside']}")
            print("restored")
    except BackupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
