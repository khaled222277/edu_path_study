"""Serve the figures extracted from lecture PDFs, scoped to the student.

Deliberately not a StaticFiles mount. A figure is lecture material like any
other chunk, so it has to sit behind the same gate: signed in, and the lecture's
subject belongs to the student's own university and year. A raw static mount
would hand every ingested diagram to anyone who could guess a lecture id.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.controllers.chat_controller import assert_subject_in_scope
from app.db.session import get_db
from app.models.lecture import Lecture
from app.models.user import User
from app.services.lecture_images import ROUTE_PREFIX, resolve

router = APIRouter(prefix=ROUTE_PREFIX, tags=["lecture images"])


@router.get("/{lecture_id}/{filename}")
def get_lecture_image(
    lecture_id: int,
    filename: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    """One figure image, if this student is allowed to see the lecture it came from."""
    lecture = db.get(Lecture, lecture_id)
    if lecture is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Lecture not found"
        )

    # Raises 403 unless the lecture's subject is in the student's own university
    # and year -- the identical rule chat and quiz enforce.
    assert_subject_in_scope(db, lecture.subject_id, user)

    path = resolve(lecture_id, filename)
    if path is None:
        # Same 404 for "no such file" and "not a name we ever write", so a
        # probe cannot distinguish a missing figure from a rejected path.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Image not found"
        )

    return FileResponse(
        path,
        media_type="image/png",
        # private: the response is scoped to one student, so only their own
        # browser may cache it -- never a shared proxy.
        headers={"Cache-Control": "private, max-age=3600"},
    )
