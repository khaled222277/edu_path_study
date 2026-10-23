"""Engine + session factory."""

from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """A session that is not tied to a request.

    Needed by work that outlives the route function: FastAPI closes `get_db`'s
    session as soon as the route returns, which for a streamed response is before
    the body has been produced.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
