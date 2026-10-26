"""Google OAuth2 sign-in routes. Thin — the work lives in the controllers."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.auth.oauth import oauth
from app.config import settings
from app.controllers import auth_controller
from app.rate_limit import client_ip, limiter
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import UserOut

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/google/login")
@limiter.limit(settings.rate_limit_auth, key_func=client_ip)
async def google_login(request: Request):
    """Kick off the OAuth dance by redirecting to Google's consent screen."""
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth is not configured. Set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET in your .env file.",
        )
    return await oauth.google.authorize_redirect(request, settings.google_redirect_uri)


@router.get("/google/callback")
@limiter.limit(settings.rate_limit_auth, key_func=client_ip)
async def google_callback(request: Request, db: Session = Depends(get_db)):
    """Exchange the code for tokens, upsert the user, and set the session cookie."""
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as exc:  # Authlib raises a variety of OAuth errors here.
        # Log the cause; the client only learns that sign-in failed.
        log.warning("google sign-in failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google sign-in failed. Please try again.",
        ) from exc

    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = await oauth.google.userinfo(token=token)

    user = auth_controller.get_or_create_user_from_google(db, dict(userinfo))

    # Land the browser back on the frontend; it will call /auth/me next.
    redirect = RedirectResponse(
        url=settings.frontend_url, status_code=status.HTTP_302_FOUND
    )
    auth_controller.set_session_cookie(redirect, user)
    return redirect


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> User:
    """Current user, including whether onboarding still needs to happen."""
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> None:
    """Sign out: the cookie is cleared AND the token itself stops working (denylisted until it would expire)."""
    auth_controller.revoke_token(db, request.cookies.get(settings.cookie_name))
    auth_controller.clear_session_cookie(response)
