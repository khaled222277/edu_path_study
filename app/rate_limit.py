"""Request rate limiting (slowapi, in-memory).

Auth endpoints are limited per client IP, since the caller is not signed in yet.
Chat and quiz are limited per signed-in user, so one student on a shared
university network cannot exhaust the budget for the others; an unauthenticated
caller falls back to its IP and will be rejected by auth right after anyway.

Counters live in process memory, so with N workers the effective limit is N times
higher; swap `storage_uri` for Redis if the API is ever scaled out.
"""

import ipaddress

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.auth.jwt import decode_access_token
from app.config import settings


def client_ip(request: Request) -> str:
    """The caller's address: the socket peer, or with TRUSTED_PROXY the address the proxy saw.

    X-Forwarded-For is "client, proxy1, proxy2": each proxy appends the address it received
    the request from. Everything left of what our own proxies appended is client-supplied
    and spoofable, so the entry `trusted_proxy_hops` from the right is used. A missing,
    short or malformed header falls back to the socket peer.
    """
    peer = get_remote_address(request)
    if not settings.trusted_proxy:
        return peer
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if len(parts) < settings.trusted_proxy_hops:
        return peer
    candidate = parts[-settings.trusted_proxy_hops]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return peer


def user_or_ip_key(request: Request) -> str:
    token = request.cookies.get(settings.cookie_name)
    user_id = decode_access_token(token) if token else None
    return f"user:{user_id}" if user_id is not None else client_ip(request)


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
