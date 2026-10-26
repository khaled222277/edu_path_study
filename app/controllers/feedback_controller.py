"""Thumbs up / down on assistant replies."""

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chat import ROLE_ASSISTANT, ChatMessage, ChatSession
from app.models.feedback import RATING_DOWN, RATING_UP, MessageFeedback
from app.models.user import User

NOT_FOUND = "Message not found"
RATINGS = {"up": RATING_UP, "down": RATING_DOWN}


def _own_reply(db: Session, message_id: int, user: User) -> ChatMessage:
    """An assistant reply in one of this student's own chats; 404 for anything else.

    404 rather than 403, so the existence of other students' messages is not revealed.
    """
    message = db.get(ChatMessage, message_id)
    session = db.get(ChatSession, message.session_id) if message else None
    if message is None or message.role != ROLE_ASSISTANT or session.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    return message


def _existing(db: Session, message_id: int, user_id: int) -> MessageFeedback | None:
    return db.scalar(
        select(MessageFeedback).where(
            MessageFeedback.message_id == message_id, MessageFeedback.user_id == user_id
        )
    )


def set_feedback(
    db: Session, user: User, message_id: int, rating: str, comment: str | None
) -> MessageFeedback:
    """Create or replace this student's feedback on a reply."""
    message = _own_reply(db, message_id, user)
    comment = (comment or "").strip() or None
    row = _existing(db, message.id, user.id)
    if row is None:
        row = MessageFeedback(
            message_id=message.id, user_id=user.id, rating=RATINGS[rating], comment=comment
        )
        db.add(row)
    else:
        row.rating, row.comment = RATINGS[rating], comment
    db.commit()
    db.refresh(row)
    return row


def clear_feedback(db: Session, user: User, message_id: int) -> None:
    message = _own_reply(db, message_id, user)
    row = _existing(db, message.id, user.id)
    if row is not None:
        db.delete(row)
        db.commit()
