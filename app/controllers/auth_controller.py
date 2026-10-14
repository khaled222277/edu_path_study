"""Business logic for sign-in / sign-up via Google."""

from fastapi import HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.jwt import create_access_token
from app.config import settings
from app.models.user import User


def get_or_create_user_from_google(db: Session, userinfo: dict) -> User:
    """Look the user up by their Google subject id, creating the row on first login."""
    google_id = userinfo.get("sub")
    email = userinfo.get("email")
    if not google_id or not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google did not return an id and email for this account",
        )

    user = db.scalar(select(User).where(User.google_id == google_id))
    if user is None:
        user = User(
            google_id=google_id,
            email=email,
            name=userinfo.get("name") or email.split("@")[0],
        )
        db.add(user)
    else:
        # Keep the profile fresh in case it changed on Google's side.
        user.email = email
        if userinfo.get("name"):
            user.name = userinfo["name"]

    db.commit()
    db.refresh(user)
    return user


def set_session_cookie(response: Response, user: User) -> None:
    response.set_cookie(
        key=settings.cookie_name,
        value=create_access_token(user.id),
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        max_age=settings.jwt_expire_minutes * 60,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.cookie_name,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )
