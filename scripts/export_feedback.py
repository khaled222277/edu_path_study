"""Admin tool: export the students' thumbs up / down on answers as CSV.

    python scripts/export_feedback.py > feedback.csv
    python scripts/export_feedback.py --out feedback.csv --rating down --since 2026-11-01

One row per rating: when, rating, comment, subject, the question and the answer that was
rated, and how many sources the answer showed. Who gave the rating is NOT exported (no
user id, name or email). Reads the database in DATABASE_URL (.env); writes nothing to it.
Cells that start with = + - @ are prefixed with an apostrophe so a spreadsheet does not
run them as formulas.
"""

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import ChatMessage, ChatSession, MessageFeedback, Subject  # noqa: E402

COLUMNS = ["created_at", "rating", "comment", "subject_code", "question", "answer", "sources_shown", "message_id"]


def safe_cell(value) -> str:
    """A CSV cell that a spreadsheet will treat as text, never as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def rows(db: Session, rating: str | None = None, since: datetime | None = None):
    query = (
        select(MessageFeedback, ChatMessage, Subject)
        .join(ChatMessage, ChatMessage.id == MessageFeedback.message_id)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .join(Subject, Subject.id == ChatSession.subject_id)
        .order_by(MessageFeedback.created_at, MessageFeedback.id)
    )
    if rating:
        query = query.where(MessageFeedback.rating == (1 if rating == "up" else -1))
    if since:
        query = query.where(MessageFeedback.created_at >= since)
    for fb, message, subject in db.execute(query):
        question = db.scalar(
            select(ChatMessage.content)
            .where(
                ChatMessage.session_id == message.session_id,
                ChatMessage.id < message.id,
                ChatMessage.role == "user",
            )
            .order_by(ChatMessage.id.desc())
            .limit(1)
        )
        yield [
            fb.created_at.isoformat(timespec="seconds") if fb.created_at else "",
            "up" if fb.rating > 0 else "down",
            fb.comment,
            subject.code,
            question,
            message.content,
            len(message.sources or []),
            message.id,
        ]


def write_csv(db: Session, out, rating: str | None = None, since: datetime | None = None) -> int:
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    n = 0
    for row in rows(db, rating, since):
        writer.writerow([safe_cell(c) for c in row])
        n += 1
    return n


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", help="file to write (default: standard output)")
    p.add_argument("--rating", choices=["up", "down"], help="only this rating")
    p.add_argument("--since", help="only ratings from this date on, YYYY-MM-DD")
    args = p.parse_args(argv)
    since = datetime.fromisoformat(args.since) if args.since else None

    from app.db.session import SessionLocal

    with SessionLocal() as db:
        if args.out:
            with open(args.out, "w", encoding="utf-8-sig", newline="") as fh:  # BOM: Excel reads Arabic
                n = write_csv(db, fh, args.rating, since)
        else:
            sys.stdout.reconfigure(encoding="utf-8")
            n = write_csv(db, sys.stdout, args.rating, since)
    print(f"{n} rating(s) exported", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
