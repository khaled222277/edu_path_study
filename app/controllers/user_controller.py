"""Business logic for the user profile / onboarding."""

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.university import University
from app.models.user import User
from app.schemas.onboarding import OnboardingIn


def complete_onboarding(db: Session, user: User, payload: OnboardingIn) -> User:
    """Attach a university + study year to `user`."""
    university = db.get(University, payload.university_id)
    if university is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="University not found"
        )

    user.university_id = university.id
    user.year = payload.year
    db.commit()
    db.refresh(user)
    return user
