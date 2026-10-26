"""Reusable FastAPI dependencies for reading the current user off the cookie."""

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth.jwt import token_claims
from app.config import settings
from app.db.session import get_db
from app.models.revoked_token import RevokedToken
from app.models.user import User


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    """Resolve the signed-in user, or raise 401."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
    )

    token = request.cookies.get(settings.cookie_name)
    if not token:
        raise unauthorized

    claims = token_claims(token)
    if claims is None:
        raise unauthorized
    jti = claims.get("jti")
    if jti and db.get(RevokedToken, jti) is not None:
        raise unauthorized  # signed out

    user = db.get(User, int(claims["sub"]))
    if user is None:
        raise unauthorized

    return user
