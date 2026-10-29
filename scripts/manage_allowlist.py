"""Admin tool: who may sign in during the pilot (the allowed_emails table).

    python scripts/manage_allowlist.py add student@example.com other@example.com
    python scripts/manage_allowlist.py remove student@example.com
    python scripts/manage_allowlist.py list
    python scripts/manage_allowlist.py import emails.txt      # one address per line; blank lines and # comments ignored

Addresses are stored lower-case, so matching is case-insensitive. The list only matters while
ALLOWLIST_ENABLED=true. Removing an address also stops that student's existing session at their
next request (the setting being on). Reads DATABASE_URL from .env.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models.allowed_email import AllowedEmail  # noqa: E402
from app.services.allowlist import looks_like_email, normalize  # noqa: E402


def add(db: Session, emails: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(added, already_there, rejected) -- `rejected` are strings that are not email addresses."""
    added, existing, rejected = [], [], []
    for raw in emails:
        email = normalize(raw)
        if not looks_like_email(email):
            rejected.append(raw)
        elif db.get(AllowedEmail, email) is not None or email in added:
            existing.append(email)
        else:
            db.add(AllowedEmail(email=email))
            added.append(email)
    db.commit()
    return added, existing, rejected


def remove(db: Session, emails: list[str]) -> tuple[list[str], list[str]]:
    removed, missing = [], []
    for raw in emails:
        row = db.get(AllowedEmail, normalize(raw))
        if row is None:
            missing.append(normalize(raw))
        else:
            db.delete(row)
            removed.append(row.email)
    db.commit()
    return removed, missing


def listing(db: Session) -> list[str]:
    return list(db.scalars(select(AllowedEmail.email).order_by(AllowedEmail.email)))


def read_emails(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("add").add_argument("emails", nargs="+")
    sub.add_parser("remove").add_argument("emails", nargs="+")
    sub.add_parser("list")
    sub.add_parser("import").add_argument("file")
    args = p.parse_args(argv)

    from app.db.session import SessionLocal

    with SessionLocal() as db:
        if args.cmd == "list":
            rows = listing(db)
            print("\n".join(rows) if rows else "(empty)", file=sys.stdout)
            print(f"{len(rows)} address(es)", file=sys.stderr)
            return 0
        if args.cmd == "remove":
            removed, missing = remove(db, args.emails)
            print(f"removed {len(removed)}; not on the list: {len(missing)}")
            return 0
        emails = read_emails(Path(args.file)) if args.cmd == "import" else args.emails
        added, existing, rejected = add(db, emails)
        print(f"added {len(added)}; already listed {len(existing)}; not an email address {len(rejected)}")
        for r in rejected:
            print(f"  skipped: {r!r}", file=sys.stderr)
        return 1 if rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
