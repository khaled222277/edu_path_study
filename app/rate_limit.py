"""Request rate limiting (slowapi, in-memory).

Auth endpoints are limited per client IP, since the caller is not signed in yet.
Chat and quiz are limited per signed-in user, so one student on a shared
university network cannot exhaust the budget for the others; an unauthenticated
caller falls back to its IP and will be rejected by auth right after anyway.

Counters live in process memory, so with N workers the effective limit is N times
higher; swap `storage_uri` for Redis if the API is ever scaled out.
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.auth.jwt import decode_access_token
from app.config import settings


def user_or_ip_key(request: Request) -> str:
    token = request.cookies.get(settings.cookie_name)
    user_id = decode_access_token(token) if token else None
    return f"user:{user_id}" if user_id is not None else get_remote_address(request)


limiter = Limiter(
    key_func=user_or_ip_key,
    enabled=settings.rate_limit_enabled,
)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    """429 in FastAPI's usual {"detail": ...} shape, without echoing the limit."""
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests. Please slow down and try again shortly."},
        headers={"Retry-After": "60"},
    )
