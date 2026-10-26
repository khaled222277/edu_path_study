from fastapi import APIRouter, Depends, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.controllers import chat_controller, chat_stream, feedback_controller
from app.config import settings
from app.db.session import get_db
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.chat import (
    AssistantReply,
    FeedbackIn,
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


@router.put("/messages/{message_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
def rate_message(
    message_id: int,
    payload: FeedbackIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Thumbs up / down (and an optional short comment) on one of the student's own replies."""
    feedback_controller.set_feedback(db, user, message_id, payload.rating, payload.comment)


@router.delete("/messages/{message_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
def unrate_message(
    message_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    feedback_controller.clear_feedback(db, user, message_id)


@router.post("/sessions/{session_id}/messages", response_model=AssistantReply)
@limiter.shared_limit(settings.rate_limit_chat, scope="chat")
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
        citations=chat_controller.citation_payload(chunks),
    )


@router.post("/sessions/{session_id}/messages/stream")
@limiter.shared_limit(settings.rate_limit_chat, scope="chat")
async def stream_message(
    request: Request,
    session_id: int,
    payload: MessageCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Same turn as `send_message`, streamed as newline-delimited JSON events.

    Events: `citations`, then `token`s, then `done` (or `error`). See
    app/controllers/chat_stream.py. Failures before the first token are the same
    HTTP errors as the plain route; the exchange is stored only once the answer
    is complete.

    `async def` on purpose: the stream is awaited on the event loop rather than
    held in a worker thread. The blocking steps -- ownership check, vector search,
    database writes -- are pushed to the threadpool explicitly.
    """
    chat_session = await run_in_threadpool(
        chat_controller.get_session_or_404, db, session_id, user
    )
    turn = await run_in_threadpool(
        chat_controller.prepare_turn, db, chat_session, payload.content
    )
    stream, first_part = await chat_stream.open_chat_stream(turn.messages)
    return chat_stream.streaming_response(turn, stream, first_part)
