"""Admin tool: export the questions students reported as bad, as CSV.

    python scripts/export_quiz_reports.py > quiz_reports.csv
    python scripts/export_quiz_reports.py --out quiz_reports.csv --reason not_answerable --since 2026-11-01

One row per report: when, the reason, the quiz language, the subject, the question with its four options
and which one is marked correct, whether and what the student answered, and the source page it was made
from. Who reported it is NOT exported (no user id, name or email). Reads the database in DATABASE_URL
(.env); writes nothing to it. Cells that start with = + - @ are prefixed with an apostrophe so a
spreadsheet does not run them as formulas.
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

from app.models import QuizAttempt, QuizItem, QuizItemReport, Subject  # noqa: E402
from app.models.quiz import REPORT_REASONS  # noqa: E402

COLUMNS = [
    "created_at", "reason", "language", "subject_code", "question", "option_a", "option_b", "option_c",
    "option_d", "marked_correct", "student_chose", "source", "item_id", "attempt_id",
]
LETTERS = "ABCD"


def safe_cell(value) -> str:
    """A CSV cell that a spreadsheet will treat as text, never as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def rows(db: Session, reason: str | None = None, since: datetime | None = None):
    query = (
        select(QuizItemReport, QuizItem, QuizAttempt, Subject)
        .join(QuizItem, QuizItem.id == QuizItemReport.item_id)
        .join(QuizAttempt, QuizAttempt.id == QuizItemReport.attempt_id)
        .join(Subject, Subject.id == QuizAttempt.subject_id)
        .order_by(QuizItemReport.created_at, QuizItemReport.id)
    )
    if reason:
        query = query.where(QuizItemReport.reason == reason)
    if since:
        query = query.where(QuizItemReport.created_at >= since)
    for report, item, attempt, subject in db.execute(query):
        options = list(item.options_json or [])[:4]
        options += [""] * (4 - len(options))
        chose = item.chosen_answer_index
        yield [
            report.created_at.isoformat(timespec="seconds") if report.created_at else "",
            report.reason or "",
            attempt.language,
            subject.code,
            item.question_text,
            *options,
            LETTERS[item.correct_answer_index] if 0 <= item.correct_answer_index < 4 else "",
            LETTERS[chose] if chose is not None and 0 <= chose < 4 else "",
            item.source_citation,
            item.id,
            attempt.id,
        ]


def write_csv(db: Session, out, reason: str | None = None, since: datetime | None = None) -> int:
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    n = 0
    for row in rows(db, reason, since):
        writer.writerow([safe_cell(c) for c in row])
        n += 1
    return n


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", help="file to write (default: standard output)")
    p.add_argument("--reason", choices=list(REPORT_REASONS), help="only this reason")
    p.add_argument("--since", help="only reports from this date on, YYYY-MM-DD")
    args = p.parse_args(argv)
    since = datetime.fromisoformat(args.since) if args.since else None

    from app.db.session import SessionLocal

    with SessionLocal() as db:
        if args.out:
            with open(args.out, "w", encoding="utf-8-sig", newline="") as fh:  # BOM: Excel reads Arabic
                n = write_csv(db, fh, args.reason, since)
        else:
            sys.stdout.reconfigure(encoding="utf-8")
            n = write_csv(db, sys.stdout, args.reason, since)
    print(f"{n} report(s) exported", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
