"""Grounded question answering over one subject's lecture corpus."""

import logging
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import log_safe
from app.config import settings
from app.db.session import session_scope
from app.models.chat import ROLE_ASSISTANT, ROLE_USER, ChatMessage, ChatSession
from app.models.subject import Subject
from app.models.user import User
from app.schemas.chat import Citation
from app.services.llm import get_client, keep_alive
from app.services.query_rewrite import retrieval_query
from app.services.quiz_service import detect_language
from app.services.citations import Retrieved, find_citations, judge, validate_answer
from app.services.refusal import (
    DEFAULT_APOLOGY,
    LEGACY_REFUSAL_PHRASE,
    REFUSAL_SENTINEL,
    ParsedReply,
    _SENTINEL_RE,
    parse_reply,
)
from app.services.retrieval import RetrievedChunk, format_context, retrieve

log = logging.getLogger(__name__)

CHAT_MODEL = settings.chat_model

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

# Kept under its old name: a reply that still refuses in this English phrase is
# recognised too (see app/services/refusal.py).
NOT_FOUND_MARKER = LEGACY_REFUSAL_PHRASE

# Rule 3 in its two forms. The sentinel form is language-neutral: the model is asked
# for a fixed token and writes the refusal sentence itself, in the student's language.
# The legacy form (an English sentence, which an Arabic reply never matched) is kept so
# the two can be compared by scripts/eval_quality.py.
#
# The wording matters more than it looks: "in the student's language" made the model
# answer an English question in Arabic on every seed (language correct 55/60), while
# "written in the same language as the question" did not (60/60). See eval/README.md.
REFUSAL_RULE_SENTINEL = (
    "If the CONTEXT does not contain the answer, begin your reply with the exact token "
    f"{REFUSAL_SENTINEL}, followed by one short sentence saying so, written in the same "
    "language as the question. Do not guess, and do not offer what you know from "
    "elsewhere. Never write that token in any other case."
)
REFUSAL_RULE_LEGACY = (
    f'If the CONTEXT does not contain the answer, reply that it is "{NOT_FOUND_MARKER}" '
    "and say nothing further about the topic. Do not guess, and do not offer what you "
    "know from elsewhere."
)


def build_system_prompt(refusal_rule: str = REFUSAL_RULE_SENTINEL) -> str:
    return f"""\
You are a study assistant for university students. You answer strictly from the \
lecture material provided below.

RULES:
1. Use ONLY the CONTEXT to answer. Never use outside or general knowledge, even \
if you are confident it is correct.
2. Cite every claim with the source in square brackets exactly as given in the \
context, e.g. [lecture.pdf, page 12]. Put the citation right after the sentence \
it supports.
3. {refusal_rule}
4. Some context blocks are descriptions of figures, diagrams, or tables from the \
slides. Treat them as valid evidence and cite them the same way.
5. Answer in the SAME LANGUAGE the student used. If they ask in Arabic, answer \
in Arabic; if in English, answer in English. Keep technical terms in their \
original form where that is what the lecture uses.
6. Be concise and explain like a tutor: define the term, then give the detail \
the student asked for.
"""


SYSTEM_PROMPT = build_system_prompt()

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
    """Fetch a session; 404 if it does not exist OR belongs to someone else.

    The same answer for both, so ids cannot be probed to learn which sessions exist.
    """
    chat_session = db.get(ChatSession, session_id)
    if chat_session is None or chat_session.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found"
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


def previous_user_question(chat_session: ChatSession) -> str | None:
    """The student's question before the one being answered, if there was one.

    The question being answered has already been added to the session when this is
    called, so it is the last message and is skipped.
    """
    earlier = [m for m in chat_session.messages[:-1] if m.role == ROLE_USER]
    return earlier[-1].content if earlier else None


def _prepare(
    db: Session, chat_session: ChatSession, question: str
) -> tuple[list[RetrievedChunk], list[dict]]:
    """Everything before the model call: record the question, retrieve, build the prompt.

    Leaves the question flushed (not committed) in `db`. Shared by the plain and
    the streaming route so the two cannot drift apart.
    """
    db.add(ChatMessage(session_id=chat_session.id, role=ROLE_USER, content=question))
    db.flush()
    db.refresh(chat_session)

    query = question
    try:
        query = retrieval_query(previous_user_question(chat_session), question)
        chunks = retrieve(query, chat_session.subject_id)
    except Exception as exc:
        # Chroma unreadable, embedding model or reranker failed to load, etc.
        db.rollback()
        log_safe.failure(log, "lecture retrieval failed", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=SEARCH_UNAVAILABLE_DETAIL,
        ) from exc
    if settings.chat_trace_log:
        log.info(
            "chat-trace retrieve session=%s subject=%s prior_messages=%d rewritten=%s query_chars=%d chunks=%s%s",
            chat_session.id, chat_session.subject_id, len(chat_session.messages) - 1,
            query != question, len(query),
            [(c.source_filename, c.page_number) for c in chunks],
            f" question={question!r} query={query!r}" if settings.chat_trace_queries else "",
        )
    return chunks, _build_messages(chat_session, question, chunks)


def trace_reply(
    session_id: int, raw: str | None, parsed: ParsedReply, chunks: list[RetrievedChunk]
) -> None:
    """With CHAT_TRACE_LOG on, one line saying what became of the model's reply.

    Answers "why did this answer have no sources": a refusal (and whether the token was
    at the start or somewhere inside a long answer), citations taken out, or nothing
    retrieved to begin with.
    """
    if not settings.chat_trace_log:
        return
    text = raw or ""
    retrieved = Retrieved.from_chunks(chunks)
    cites = find_citations(text)
    token = _SENTINEL_RE.search(text)
    log.info(
        "chat-trace reply session=%s refused=%s sentinel_at=%s legacy_phrase=%s raw_chars=%d "
        "citations_written=%d citations_invalid=%d chunks=%d sources_shown=%d final_chars=%d",
        session_id, parsed.refused, token.start() if token else None,
        LEGACY_REFUSAL_PHRASE in text.lower(), len(text), len(cites),
        sum(1 for c in cites if not judge(c, retrieved).ok), len(chunks),
        len(visible_chunks(parsed.refused, chunks)), len(parsed.text),
    )


def finalize_answer(
    raw: str | None, question: str = "", chunks: list[RetrievedChunk] | None = None
) -> ParsedReply:
    """What is stored and shown for a model reply, and whether it was a refusal.

    The refusal token is removed from the text; `refused` says the material did not
    cover the question. `question` only picks the language of the stand-in text used
    when the model wrote nothing but the token, or nothing at all.

    With `chunks` -- the chunks actually put in this turn's prompt -- every inline
    citation is checked against them: ones naming a page that was not retrieved are
    removed and the rest are written in one canonical form (see app/services/citations.py).
    Without it the text is returned as the model wrote it, which is what the evaluation
    uses to measure how often the model cites wrongly.
    """
    language = detect_language(question) if question else "en"
    parsed = parse_reply(raw, language)
    if chunks is None:
        return parsed
    checked = validate_answer(parsed.text, Retrieved.from_chunks(chunks))
    if checked.removed:
        log.info("removed %s citation(s) not among the retrieved chunks: %s", len(checked.removed), checked.removed)
    # strip again: removing a citation at the end of a paragraph can leave blank lines behind
    if checked.text.strip():
        return ParsedReply(checked.text.strip(), parsed.refused)
    # The reply was nothing but citations that did not hold up: there is no answer left.
    return ParsedReply(DEFAULT_APOLOGY.get(language, DEFAULT_APOLOGY["en"]), True)


def visible_chunks(refused: bool, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """The sources worth showing under a reply.

    Sources under a refusal contradict it -- the chunks were retrieved but none of them
    answered the question.
    """
    return [] if refused else chunks


@dataclass
class PreparedTurn:
    """A chat turn that is ready for the model but has stored nothing yet."""

    session_id: int
    question: str
    chunks: list[RetrievedChunk]
    messages: list[dict]


def prepare_turn(db: Session, chat_session: ChatSession, question: str) -> PreparedTurn:
    """_prepare(), then undo the write: a streamed turn is stored only on success."""
    session_id = chat_session.id
    chunks, messages = _prepare(db, chat_session, question)
    db.rollback()
    return PreparedTurn(session_id, question, chunks, messages)


def citation_payload(chunks: list[RetrievedChunk]) -> list[dict]:
    """The sources list as sent to the client and as stored with the reply: one definition."""
    return [
        Citation(
            source_filename=c.source_filename,
            page_number=c.page_number,
            content_type=c.content_type,
            score=c.score,
            image_url=c.image_url,
        ).model_dump()
        for c in chunks
    ]


def persist_exchange(
    session_id: int, question: str, answer: str, sources: list[dict] | None = None
) -> ChatMessage:
    """Store a finished question + answer pair in one commit, in its own session."""
    with session_scope() as db:
        db.add(ChatMessage(session_id=session_id, role=ROLE_USER, content=question))
        reply = ChatMessage(
            session_id=session_id, role=ROLE_ASSISTANT, content=answer,
            sources=sources or [],
        )
        db.add(reply)
        db.commit()
        db.refresh(reply)
        reply.feedback_rows  # load it now: it cannot be loaded once detached
        db.expunge(reply)  # keep its loaded attributes readable after the session closes
        return reply


def answer_question(
    db: Session, chat_session: ChatSession, question: str, model: str = CHAT_MODEL
) -> tuple[ChatMessage, list[RetrievedChunk]]:
    """Persist the question, answer it from the corpus, persist the reply."""
    chunks, messages = _prepare(db, chat_session, question)

    try:
        response = get_client().chat(
            model=model,
            messages=messages,
            options=CHAT_OPTIONS,
            keep_alive=keep_alive(),
        )
    except Exception as exc:
        db.rollback()
        log_safe.failure(log, "chat model call failed", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MODEL_UNAVAILABLE_DETAIL,
        ) from exc

    raw = response.get("message", {}).get("content")
    parsed = finalize_answer(raw, question, chunks)
    trace_reply(chat_session.id, raw, parsed, chunks)

    shown = visible_chunks(parsed.refused, chunks)
    reply = ChatMessage(
        session_id=chat_session.id,
        role=ROLE_ASSISTANT,
        content=parsed.text,
        sources=citation_payload(shown),
    )
    db.add(reply)
    db.commit()
    db.refresh(reply)

    return reply, shown
