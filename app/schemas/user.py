from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


class UserOut(BaseModel):
    """Shape returned by GET /auth/me."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    name: str
    university_id: int | None
    year: int | None
    created_at: datetime
    onboarding_required: bool
