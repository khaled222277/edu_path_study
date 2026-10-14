from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.controllers import university_controller
from app.db.session import get_db
from app.models.user import User
from app.schemas.university import SubjectOut, UniversityOut

router = APIRouter(tags=["universities"])


@router.get("/subjects", response_model=list[SubjectOut])
def my_subjects(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Subjects for the signed-in student's own university and year."""
    if user.university_id is None or user.year is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Complete onboarding first",
        )
    return university_controller.list_subjects(db, user.university_id, user.year)


@router.get("/universities", response_model=list[UniversityOut])
def list_universities(db: Session = Depends(get_db)):
    """Public — the onboarding form needs this before a user has a university."""
    return university_controller.list_universities(db)


@router.get(
    "/universities/{university_id}/subjects", response_model=list[SubjectOut]
)
def list_subjects(
    university_id: int,
    year: int | None = Query(default=None, ge=1, le=7),
    db: Session = Depends(get_db),
):
    return university_controller.list_subjects(db, university_id, year)
