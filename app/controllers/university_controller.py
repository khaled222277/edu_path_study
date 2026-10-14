"""Read-only queries over the university / subject catalogue."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.subject import Subject
from app.models.university import University


def list_universities(db: Session) -> list[University]:
    return list(db.scalars(select(University).order_by(University.name)))


def list_subjects(
    db: Session, university_id: int, year: int | None = None
) -> list[Subject]:
    stmt = select(Subject).where(Subject.university_id == university_id)
    if year is not None:
        stmt = stmt.where(Subject.year == year)
    return list(db.scalars(stmt.order_by(Subject.year, Subject.name)))
