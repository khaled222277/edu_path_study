"""Admin tool: delete ONE student's data (a data-deletion request), optionally exporting it first.

    python scripts/delete_student.py student@example.com                    # shows what would go, deletes nothing
    python scripts/delete_student.py student@example.com --export out.json  # also write their data to a file
    python scripts/delete_student.py student@example.com --yes              # really delete
    python scripts/delete_student.py student@example.com --yes --remove-from-allowlist

Deletes, for that account only: their chat sessions and messages, the ratings/comments on those
messages (and any ratings they gave), their quiz attempts and questions, and the account row.
Nothing else is touched. It does NOT reach: backups already taken (they still hold the data until
they rotate out: see docs/PILOT.md), the access log, or a session token already issued (it stops
working because the account is gone). Run against the database in DATABASE_URL.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import (  # noqa: E402
    AllowedEmail,
    ChatMessage,
    ChatSession,
    MessageFeedback,
    QuizAttempt,
    QuizItem,
    User,
)
from app.services.allowlist import normalize  # noqa: E402


def find_user(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.email) == normalize(email)))


def _ids(db: Session, stmt) -> list[int]:
    return list(db.scalars(stmt))


def summarize(db: Session, user: User) -> dict:
    sessions = _ids(db, select(ChatSession.id).where(ChatSession.user_id == user.id))
    messages = _ids(db, select(ChatMessage.id).where(ChatMessage.session_id.in_(sessions))) if sessions else []
    attempts = _ids(db, select(QuizAttempt.id).where(QuizAttempt.user_id == user.id))
    return {
        "chat_sessions": len(sessions),
        "chat_messages": len(messages),
        "feedback_rows": db.scalar(
            select(func.count()).select_from(MessageFeedback).where(
                (MessageFeedback.user_id == user.id) | (MessageFeedback.message_id.in_(messages or [0]))
            )
        ),
        "quiz_attempts": len(attempts),
        "quiz_items": db.scalar(select(func.count()).select_from(QuizItem).where(QuizItem.attempt_id.in_(attempts or [0]))),
    }


def export(db: Session, user: User) -> dict:
    sessions = list(db.scalars(select(ChatSession).where(ChatSession.user_id == user.id).order_by(ChatSession.id)))
    attempts = list(db.scalars(select(QuizAttempt).where(QuizAttempt.user_id == user.id).order_by(QuizAttempt.id)))
    return {
        "account": {"email": user.email, "name": user.name, "university_id": user.university_id, "year": user.year},
        "chat_sessions": [
            {"id": s.id, "subject_id": s.subject_id, "created_at": s.created_at.isoformat() if s.created_at else None,
             "messages": [{"role": m.role, "content": m.content, "sources": m.sources, "created_at": m.created_at.isoformat() if m.created_at else None,
                           "rating": m.feedback} for m in s.messages]}
            for s in sessions
        ],
        "quiz_attempts": [
            {"id": a.id, "subject_id": a.subject_id, "language": a.language, "score": a.score, "total_questions": a.total_questions,
             "items": [{"question": i.question_text, "options": i.options_json, "chosen": i.chosen_answer_index, "correct": i.correct_answer_index} for i in a.items]}
            for a in attempts
        ],
    }


def delete_student(db: Session, user: User, remove_from_allowlist: bool = False) -> dict:
    """Delete the account and everything that hangs off it, children first (works with or without FK cascades)."""
    counts = summarize(db, user)
    sessions = _ids(db, select(ChatSession.id).where(ChatSession.user_id == user.id))
    messages = _ids(db, select(ChatMessage.id).where(ChatMessage.session_id.in_(sessions))) if sessions else []
    attempts = _ids(db, select(QuizAttempt.id).where(QuizAttempt.user_id == user.id))
    email = user.email
    db.execute(delete(MessageFeedback).where((MessageFeedback.user_id == user.id) | (MessageFeedback.message_id.in_(messages or [0]))))
    if messages:
        db.execute(delete(ChatMessage).where(ChatMessage.id.in_(messages)))
    if sessions:
        db.execute(delete(ChatSession).where(ChatSession.id.in_(sessions)))
    if attempts:
        db.execute(delete(QuizItem).where(QuizItem.attempt_id.in_(attempts)))
        db.execute(delete(QuizAttempt).where(QuizAttempt.id.in_(attempts)))
    db.execute(delete(User).where(User.id == user.id))
    if remove_from_allowlist:
        db.execute(delete(AllowedEmail).where(AllowedEmail.email == normalize(email)))
    db.commit()
    return counts


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("email")
    p.add_argument("--yes", action="store_true", help="really delete (without it nothing is changed)")
    p.add_argument("--export", metavar="FILE", help="write the student's data to this JSON file first")
    p.add_argument("--remove-from-allowlist", action="store_true")
    args = p.parse_args(argv)

    from app.db.session import SessionLocal

    with SessionLocal() as db:
        user = find_user(db, args.email)
        if user is None:
            print("no account with that email", file=sys.stderr)
            return 1
        counts = summarize(db, user)
        print("account found; would delete: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        if args.export:
            Path(args.export).write_text(json.dumps(export(db, user), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"exported to {args.export}")
        if not args.yes:
            print("nothing deleted: add --yes to proceed")
            return 2
        delete_student(db, user, args.remove_from_allowlist)
        print("deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
