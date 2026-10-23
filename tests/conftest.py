"""Shared fixtures. Everything is offline: SQLite in memory, Ollama and Chroma faked.

The environment is pinned *before* any `app` import, because `app.config.settings`
is built at import time and a developer's real .env must never leak into a test
run (real Google credentials, a real Postgres URL, rate limiting getting in the
way of unrelated tests).
"""

import os

os.environ.update(
    {
        "ENVIRONMENT": "development",
        "DATABASE_URL": "sqlite://",
        "JWT_SECRET": "test-jwt-secret-" + "x" * 32,
        "SESSION_SECRET": "test-session-secret-" + "y" * 32,
        "GOOGLE_CLIENT_ID": "",
        "GOOGLE_CLIENT_SECRET": "",
        "RATE_LIMIT_ENABLED": "false",
        "WARMUP_ON_STARTUP": "false",
        "COOKIE_SECURE": "false",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.auth.jwt import create_access_token  # noqa: E402
from app.config import settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Subject, University, User  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def world(db):
    """One university, year-5 subject + a year-4 subject, and two year-5 students."""
    uni = University(name="Test University", country="Egypt")
    db.add(uni)
    db.flush()
    subject = Subject(university_id=uni.id, year=5, name="Data Mining", code="DM501")
    other_year = Subject(university_id=uni.id, year=4, name="Networks", code="NET401")
    db.add_all([subject, other_year])
    alice = User(
        google_id="g-alice", email="a@example.com", name="Alice",
        university_id=uni.id, year=5,
    )
    bob = User(
        google_id="g-bob", email="b@example.com", name="Bob",
        university_id=uni.id, year=5,
    )
    db.add_all([alice, bob])
    db.commit()
    return {"uni": uni, "subject": subject, "other_year": other_year,
            "alice": alice, "bob": bob}


def login(client: TestClient, user: User) -> TestClient:
    """Return a client carrying `user`'s session cookie."""
    client.cookies.clear()
    client.cookies.set(settings.cookie_name, create_access_token(user.id))
    return client


# --- Ollama / Chroma fakes --------------------------------------------------


class FakeOllama:
    """Stands in for ollama.Client. `responses` are consumed in order; an
    Exception instance in the list is raised instead of returned."""

    def __init__(self, responses=None, default=None):
        self.responses = list(responses or [])
        self.default = default
        self.calls = 0

    def _next(self):
        self.calls += 1
        if self.responses:
            item = self.responses.pop(0)
        elif self.default is not None:
            item = self.default
        else:
            raise AssertionError("FakeOllama ran out of scripted responses")
        if isinstance(item, Exception):
            raise item
        return item

    def chat(self, **kwargs):
        return self._next()

    def generate(self, **kwargs):
        return self._next()


@pytest.fixture()
def fake_ollama(monkeypatch):
    """Install a FakeOllama everywhere the app builds an Ollama client."""

    def install(**kwargs) -> FakeOllama:
        fake = FakeOllama(**kwargs)
        monkeypatch.setattr("app.services.llm.get_client", lambda timeout=None: fake)
        monkeypatch.setattr("app.controllers.chat_controller.get_client", lambda: fake)
        monkeypatch.setattr("app.services.quiz_service.get_client", lambda: fake)
        return fake

    return install


@pytest.fixture(autouse=True)
def forbid_real_backends(monkeypatch):
    """Fail loudly if a test would reach real Chroma, the reranker or Ollama.

    Without this, a regression (say, a removed auth check) would let a request
    fall through to the real embedding model and silently try to download it.
    Tests that need these install their own fakes, which replace these guards.
    """

    def blocked(name):
        def _raise(*args, **kwargs):
            raise AssertionError(f"test attempted to use real {name}")

        return _raise

    for target, name in [
        ("app.services.retrieval.get_collection", "Chroma"),
        ("app.services.retrieval.get_ranker", "the reranker"),
        ("app.services.quiz_service.get_collection", "Chroma"),
        ("app.services.llm.get_client", "Ollama"),
        ("app.controllers.chat_controller.get_client", "Ollama"),
        ("app.services.quiz_service.get_client", "Ollama"),
    ]:
        monkeypatch.setattr(target, blocked(name))


@pytest.fixture(autouse=True)
def fresh_vector_store_caches():
    """Process-wide caches must not leak between tests."""
    from app.services import vector_store

    vector_store.reset_cache(drop_embedders=True)
    yield
    vector_store.reset_cache(drop_embedders=True)
