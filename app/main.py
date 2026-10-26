import logging
import threading
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from slowapi.errors import RateLimitExceeded
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import log_redaction
from app.config import settings
from app.rate_limit import limiter, rate_limit_exceeded_handler
from app.services import llm, retrieval, vector_store
from app.services.quiz_sweeper import QuizSweeper
from app.routers import (
    auth,
    chat,
    dashboard,
    health,
    lecture_images,
    onboarding,
    quiz,
    universities,
)

# Fail fast on placeholder secrets / insecure cookies before serving anything.
settings.validate_for_environment()

log = logging.getLogger(__name__)
log_redaction.install()


def warm_up_models() -> None:
    """Load bge-m3, the reranker and the chat model before the first student.

    Never raises, and each step stands alone: a failure (no network for the first
    download, Ollama not started yet) is logged and the others still run. The API
    starts regardless and the first real request retries whatever failed, since
    none of the loaders cache a failure.
    """
    steps = (
        ("embedding model", vector_store.warm_up),
        ("reranker", retrieval.get_ranker),
        ("chat model", llm.warm_chat_model),
    )
    for name, step in steps:
        try:
            step()
        except Exception:
            log.warning("%s warm-up failed; the first request will retry", name, exc_info=True)
    if settings.quiz_model != settings.chat_model:
        log.info(
            "QUIZ_MODEL (%s) differs from CHAT_MODEL (%s): it loads on first use, "
            "and on a small GPU may evict the chat model",
            settings.quiz_model, settings.chat_model,
        )
    log.info("model warm-up finished")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.warmup_on_startup:
        # A thread, not a blocking call: the server accepts connections at once,
        # and a request that lands mid-load waits on the same lock instead of
        # loading the model a second time.
        threading.Thread(target=warm_up_models, name="model-warmup", daemon=True).start()
    sweeper = None
    if settings.quiz_sweep_enabled:
        sweeper = QuizSweeper(
            timedelta(minutes=settings.quiz_stale_after_minutes),
            settings.quiz_sweep_interval_minutes,
        )
        sweeper.start()
    try:
        yield
    finally:
        if sweeper is not None:
            sweeper.stop()


app = FastAPI(title="EduPath Study V2 API", version="0.1.0", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# Authlib stores the OAuth state/nonce in this signed cookie session.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=settings.cookie_secure,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    # Exactly what the frontend uses (see frontend/src/api.js), not "*", since
    # credentials are allowed.
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(onboarding.router)
app.include_router(universities.router)
app.include_router(chat.router)
app.include_router(quiz.router)
app.include_router(dashboard.router)
app.include_router(lecture_images.router)
