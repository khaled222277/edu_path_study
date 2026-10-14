from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.dashboard import (
    ActivityFeedOut,
    DashboardSummaryOut,
    SubjectScoreOut,
)
from app.services import dashboard_service

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummaryOut)
def dashboard_summary(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Score cards + overall readiness for the signed-in student.

    Deliberately takes no identifying parameter: the subject set and every
    aggregate derive from the session cookie, so there is nothing a caller could
    pass to reach another student's data.
    """
    summary = dashboard_service.build_summary(db, user)
    return DashboardSummaryOut(
        subjects=[SubjectScoreOut.model_validate(s) for s in summary.subjects],
        overall_readiness=summary.overall_readiness,
        subjects_with_attempts=summary.subjects_with_attempts,
        total_completed_attempts=summary.total_completed_attempts,
        total_in_progress_attempts=summary.total_in_progress_attempts,
    )


@router.get("/activity", response_model=ActivityFeedOut)
def dashboard_activity(
    limit: int = Query(default=dashboard_service.DEFAULT_ACTIVITY_LIMIT, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Recent chat sessions and quiz attempts, newest first."""
    events = dashboard_service.build_activity(db, user, limit)
    return ActivityFeedOut(events=events, limit=limit)
