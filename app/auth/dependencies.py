"""Reusable FastAPI dependencies for reading the current user off the cookie."""

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth.jwt import decode_access_token
from app.config import settings
from app.db.session import get_db
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

    user_id = decode_access_token(token)
    if user_id is None:
        raise unauthorized

    user = db.get(User, user_id)
    if user is None:
        raise unauthorized

    return user
