"""Insert one Subject row.

    python add_subject.py --university-id 1 --year 5 --name "Networking" --code "NET501"
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.models.subject import Subject  # noqa: E402
from app.models.university import University  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Add a subject.")
    parser.add_argument("--university-id", type=int, required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--code", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db = SessionLocal()
    try:
        university = db.get(University, args.university_id)
        if university is None:
            print(f"University id={args.university_id} not found.")
            return 1

        existing = db.scalar(
            select(Subject).where(
                Subject.university_id == args.university_id,
                Subject.code == args.code,
            )
        )
        if existing is not None:
            print(
                f"Subject {args.code} already exists for {university.name} "
                f"(id={existing.id}, year {existing.year})."
            )
            return 0

        subject = Subject(
            university_id=args.university_id,
            year=args.year,
            name=args.name,
            code=args.code,
        )
        db.add(subject)
        db.commit()
        db.refresh(subject)

        print(
            f"Added subject id={subject.id}: {subject.code} {subject.name} "
            f"(year {subject.year}) at {university.name}"
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
