"""Grounded question answering over one subject's lecture corpus."""

import logging

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chat import ROLE_ASSISTANT, ROLE_USER, ChatMessage, ChatSession
from app.models.subject import Subject
from app.models.user import User
from app.services.llm import get_client
from app.services.retrieval import RetrievedChunk, format_context, retrieve

log = logging.getLogger(__name__)

CHAT_MODEL = "llama3.1:8b"

# How many prior turns to replay. Enough for follow-ups ("explain that again")
# without letting old turns crowd out the retrieved context.
HISTORY_TURNS = 6

CHAT_OPTIONS = {"temperature": 0.2, "num_predict": 800}

# Client-facing 503 messages. Deliberately generic: the real cause is logged
# server-side, never echoed back.
MODEL_UNAVAILABLE_DETAIL = (
    "The study assistant is temporarily unavailable. Please try again shortly."
)
SEARCH_UNAVAILABLE_DETAIL = (
    "Lecture search is temporarily unavailable. Please try again shortly."
)

NOT_FOUND_MARKER = "not found in the lecture material"

SYSTEM_PROMPT = f"""\
You are a study assistant for university students. You answer strictly from the \
lecture material provided below.

RULES:
1. Use ONLY the CONTEXT to answer. Never use outside or general knowledge, even \
if you are confident it is correct.
2. Cite every claim with the source in square brackets exactly as given in the \
context, e.g. [lecture.pdf, page 12]. Put the citation right after the sentence \
it supports.
3. If the CONTEXT does not contain the answer, reply that it is \
"{NOT_FOUND_MARKER}" and say nothing further about the topic. Do not guess, and \
do not offer what you know from elsewhere.
4. Some context blocks are descriptions of figures, diagrams, or tables from the \
slides. Treat them as valid evidence and cite them the same way.
5. Answer in the SAME LANGUAGE the student used. If they ask in Arabic, answer \
in Arabic; if in English, answer in English. Keep technical terms in their \
original form where that is what the lecture uses.
6. Be concise and explain like a tutor: define the term, then give the detail \
the student asked for.
"""

USER_TEMPLATE = """\
CONTEXT:
{context}

STUDENT QUESTION:
{question}"""

NO_CONTEXT_TEMPLATE = """\
CONTEXT:
(no relevant lecture material was found)

STUDENT QUESTION:
{question}"""


def get_session_or_404(db: Session, session_id: int, user: User) -> ChatSession:
    """Fetch a session, 404 if missing and 403 if it belongs to someone else."""
    chat_session = db.get(ChatSession, session_id)
    if chat_session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found"
        )
    if chat_session.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This chat session belongs to another student",
        )
    return chat_session


def assert_subject_in_scope(db: Session, subject_id: int, user: User) -> Subject:
    """A student may only study subjects from their own university and year."""
    if user.university_id is None or user.year is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Complete onboarding before starting a chat",
        )

    subject = db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Subject not found"
        )

    if subject.university_id != user.university_id or subject.year != user.year:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This subject is not part of your university and year",
        )
    return subject


def create_session(db: Session, user: User, subject_id: int) -> ChatSession:
    assert_subject_in_scope(db, subject_id, user)
    chat_session = ChatSession(user_id=user.id, subject_id=subject_id)
    db.add(chat_session)
    db.commit()
    db.refresh(chat_session)
    return chat_session


def list_sessions(db: Session, user: User) -> list[ChatSession]:
    return list(
        db.scalars(
            select(ChatSession)
            .where(ChatSession.user_id == user.id)
            .order_by(ChatSession.created_at.desc(), ChatSession.id.desc())
        )
    )


def _history_messages(chat_session: ChatSession) -> list[dict]:
    """Prior turns in Ollama's message format, oldest first."""
    recent = chat_session.messages[-HISTORY_TURNS * 2 :]
    return [{"role": m.role, "content": m.content} for m in recent]


def _build_messages(
    chat_session: ChatSession, question: str, chunks: list[RetrievedChunk]
) -> list[dict]:
    body = (
        USER_TEMPLATE.format(context=format_context(chunks), question=question)
        if chunks
        else NO_CONTEXT_TEMPLATE.format(question=question)
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *_history_messages(chat_session),
        {"role": "user", "content": body},
    ]


def answer_question(
    db: Session, chat_session: ChatSession, question: str, model: str = CHAT_MODEL
) -> tuple[ChatMessage, list[RetrievedChunk]]:
    """Persist the question, answer it from the corpus, persist the reply."""
    db.add(ChatMessage(session_id=chat_session.id, role=ROLE_USER, content=question))
    db.flush()
    db.refresh(chat_session)

    try:
        chunks = retrieve(question, chat_session.subject_id)
    except Exception as exc:
        # Chroma unreadable, embedding model or reranker failed to load, etc.
        db.rollback()
        log.exception("lecture retrieval failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=SEARCH_UNAVAILABLE_DETAIL,
        ) from exc
    messages = _build_messages(chat_session, question, chunks)

    try:
        response = get_client().chat(
            model=model, messages=messages, options=CHAT_OPTIONS
        )
    except Exception as exc:
        db.rollback()
        log.exception("chat model call failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MODEL_UNAVAILABLE_DETAIL,
        ) from exc

    answer = (response.get("message", {}).get("content") or "").strip()
    if not answer:
        answer = f"Sorry — I could not produce an answer. This may be {NOT_FOUND_MARKER}."

    reply = ChatMessage(
        session_id=chat_session.id, role=ROLE_ASSISTANT, content=answer
    )
    db.add(reply)
    db.commit()
    db.refresh(reply)

    # Showing sources under "not found in the lecture material" contradicts the
    # answer -- the chunks were retrieved but none of them answered the question.
    if NOT_FOUND_MARKER.lower() in answer.lower():
        return reply, []

    return reply, chunks
