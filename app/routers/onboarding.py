from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.controllers import user_controller
from app.db.session import get_db
from app.models.user import User
from app.schemas.onboarding import OnboardingIn
from app.schemas.user import UserOut

router = APIRouter(tags=["onboarding"])


@router.post("/onboarding", response_model=UserOut)
def submit_onboarding(
    payload: OnboardingIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    return user_controller.complete_onboarding(db, user, payload)
