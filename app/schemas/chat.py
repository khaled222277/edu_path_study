from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SessionCreate(BaseModel):
    subject_id: int = Field(..., ge=1)


class MessageCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)


class Citation(BaseModel):
    """Where a piece of the answer came from."""

    source_filename: str
    page_number: int
    content_type: str
    score: float
    # API path to the figure itself, on image_description citations whose image
    # was saved at ingest. None on text citations and on older figures.
    image_url: str | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    created_at: datetime
    # What the reply was grounded on, kept so a reopened chat shows its Sources again.
    # Always a list: empty for user messages, refusals and replies stored before this existed.
    citations: list[Citation] = []
    # The student's own rating of this reply: "up", "down" or null.
    feedback: str | None = None


class FeedbackIn(BaseModel):
    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=500)


class AssistantReply(BaseModel):
    """The assistant turn plus the chunks it was grounded on."""

    message: MessageOut
    citations: list[Citation]


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    subject_id: int
    created_at: datetime


class SessionDetail(SessionOut):
    messages: list[MessageOut]
