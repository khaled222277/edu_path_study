from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.controllers import chat_controller
from app.config import settings
from app.db.session import get_db
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.chat import (
    AssistantReply,
    Citation,
    MessageCreate,
    MessageOut,
    SessionCreate,
    SessionDetail,
    SessionOut,
)

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post(
    "/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED
)
def start_session(
    payload: SessionCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Open a chat for one subject. 403 if it is outside the student's scope."""
    return chat_controller.create_session(db, user, payload.subject_id)


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """The student's sessions, most recent first."""
    return chat_controller.list_sessions(db, user)


@router.get("/sessions/{session_id}", response_model=SessionDetail)
def get_session(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return chat_controller.get_session_or_404(db, session_id, user)


@router.post("/sessions/{session_id}/messages", response_model=AssistantReply)
@limiter.limit(settings.rate_limit_chat)
def send_message(
    request: Request,
    session_id: int,
    payload: MessageCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ask a question and get a grounded answer with citations."""
    chat_session = chat_controller.get_session_or_404(db, session_id, user)
    reply, chunks = chat_controller.answer_question(db, chat_session, payload.content)

    return AssistantReply(
        message=MessageOut.model_validate(reply),
        citations=[
            Citation(
                source_filename=c.source_filename,
                page_number=c.page_number,
                content_type=c.content_type,
                score=c.score,
                image_url=c.image_url,
            )
            for c in chunks
        ],
    )
