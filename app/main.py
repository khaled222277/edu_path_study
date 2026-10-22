from fastapi import FastAPI
from slowapi.errors import RateLimitExceeded
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.rate_limit import limiter, rate_limit_exceeded_handler
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

app = FastAPI(title="EduPath Study V2 API", version="0.1.0")

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
