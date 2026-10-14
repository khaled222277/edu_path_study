from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class QuizStart(BaseModel):
    subject_id: int = Field(..., ge=1)
    question_count: int = Field(default=5, ge=1, le=20)
    # Omit to auto-detect from `prompt`, else default to English.
    language: str | None = Field(default=None, pattern="^(ar|en)$")
    prompt: str | None = Field(
        default=None,
        max_length=500,
        description="Optional free text; its language picks the quiz language",
    )


class QuizQuestionOut(BaseModel):
    """A question as the student sees it -- deliberately no correct answer."""

    id: int
    question_text: str
    options: list[str]
    source_citation: str


class QuizStartOut(BaseModel):
    attempt_id: int
    subject_id: int
    language: str
    total_questions: int
    questions: list[QuizQuestionOut]


class AnswerIn(BaseModel):
    item_id: int
    chosen_answer_index: int = Field(..., ge=0, le=3)


class AnswerOut(BaseModel):
    """Returned only after the student commits an answer."""

    item_id: int
    is_correct: bool
    correct_answer_index: int
    source_citation: str
    answered_count: int
    total_questions: int
    is_complete: bool


class QuizResultItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    question_text: str
    options: list[str]
    correct_answer_index: int
    chosen_answer_index: int | None
    is_correct: bool | None
    source_citation: str


class QuizResults(BaseModel):
    attempt_id: int
    subject_id: int
    language: str
    score: int
    total_questions: int
    created_at: datetime
    completed_at: datetime | None
    items: list[QuizResultItem]


class QuizHistoryEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    subject_id: int
    language: str
    score: int
    total_questions: int
    created_at: datetime
    completed_at: datetime | None
