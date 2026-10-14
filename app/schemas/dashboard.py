from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SubjectScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    subject_id: int
    subject_code: str
    subject_name: str
    completed_attempts: int
    in_progress_attempts: int
    # null (not 0) when the student has never finished a quiz for this subject.
    average_score: float | None
    best_score: float | None
    last_attempt_at: datetime | None
    trend: list[float]
    has_attempts: bool


class DashboardSummaryOut(BaseModel):
    subjects: list[SubjectScoreOut]
    overall_readiness: float | None
    subjects_with_attempts: int
    total_completed_attempts: int
    total_in_progress_attempts: int


class ActivityEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: str
    subject_id: int
    subject_code: str
    subject_name: str
    occurred_at: datetime
    reference_id: int
    score: int | None
    total_questions: int | None


class ActivityFeedOut(BaseModel):
    events: list[ActivityEventOut]
    limit: int
