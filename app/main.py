import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from slowapi.errors import RateLimitExceeded
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.rate_limit import limiter, rate_limit_exceeded_handler
from app.services import retrieval, vector_store
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


def warm_up_models() -> None:
    """Load bge-m3 and the reranker now rather than on the first chat message.

    Never raises: if Ollama-independent loading fails (no network for the first
    download, a corrupt cache), the API still starts and the first request
    retries the load -- neither loader caches a failure.
    """
    try:
        vector_store.warm_up()
        retrieval.get_ranker()
        log.info("model warm-up complete")
    except Exception:
        log.warning("model warm-up failed; the first request will retry", exc_info=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.warmup_on_startup:
        # A thread, not a blocking call: the server accepts connections at once,
        # and a request that lands mid-load waits on the same lock instead of
        # loading the model a second time.
        threading.Thread(target=warm_up_models, name="model-warmup", daemon=True).start()
    yield


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
