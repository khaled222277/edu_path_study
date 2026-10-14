"""Seed the university / subject catalogue.

Idempotent — re-running it will not duplicate rows.

    python scripts/seed.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.models.subject import Subject  # noqa: E402
from app.models.university import University  # noqa: E402

UNIVERSITY = {"name": "Al-Baath University - Homs", "country": "Syria"}

# Placeholder year-5 IT subjects; the real catalogue arrives with ingestion.
SUBJECTS = [
    {"year": 5, "name": "Artificial Intelligence", "code": "IT501"},
    {"year": 5, "name": "Computer Networks", "code": "IT502"},
    {"year": 5, "name": "Software Engineering", "code": "IT503"},
]


def seed() -> None:
    db = SessionLocal()
    try:
        university = db.scalar(
            select(University).where(University.name == UNIVERSITY["name"])
        )
        if university is None:
            university = University(**UNIVERSITY)
            db.add(university)
            db.flush()
            print(f"+ university: {university.name}")
        else:
            print(f"= university already present: {university.name}")

        for spec in SUBJECTS:
            existing = db.scalar(
                select(Subject).where(
                    Subject.university_id == university.id,
                    Subject.code == spec["code"],
                )
            )
            if existing is None:
                db.add(Subject(university_id=university.id, **spec))
                print(f"  + subject: {spec['code']} {spec['name']}")
            else:
                print(f"  = subject already present: {spec['code']}")

        db.commit()
        print("Seed complete.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
