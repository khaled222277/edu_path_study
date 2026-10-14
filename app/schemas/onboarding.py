from pydantic import BaseModel, Field


class OnboardingIn(BaseModel):
    university_id: int = Field(..., ge=1)
    year: int = Field(..., ge=1, le=7, description="Study year, 1-7")
