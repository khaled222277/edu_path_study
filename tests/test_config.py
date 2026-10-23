"""Production startup validation."""

import pytest

from app.config import (
    DEFAULT_DB_PASSWORD,
    PLACEHOLDER_JWT_SECRET,
    PLACEHOLDER_SESSION_SECRET,
    Settings,
    settings,
)

GOOD = dict(
    environment="production",
    jwt_secret="a" * 40,
    session_secret="b" * 40,
    cookie_secure=True,
    database_url="postgresql://edupath:a-real-password@db:5432/edupath",
)


def make(**overrides) -> Settings:
    return Settings(_env_file=None, **{**GOOD, **overrides})


def test_development_defaults_start_fine():
    Settings(_env_file=None).validate_for_environment()


def test_good_production_settings_pass():
    make().validate_for_environment()


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"jwt_secret": "change-me-in-production"}, "JWT_SECRET"),
        ({"jwt_secret": "short"}, "JWT_SECRET must be at least"),
        ({"session_secret": "change-me-in-production-too"}, "SESSION_SECRET"),
        ({"session_secret": "short"}, "SESSION_SECRET must be at least"),
        ({"session_secret": "a" * 40}, "must be different"),
        ({"database_url": "postgresql://edupath:your_password_here@db/edupath"}, "database password"),
        ({"database_url": "postgresql://edupath@db/edupath"}, "database password"),
        ({"cookie_secure": False}, "COOKIE_SECURE"),
    ],
)
def test_unsafe_production_settings_are_refused(overrides, fragment):
    with pytest.raises(RuntimeError) as exc:
        make(**overrides).validate_for_environment()
    assert fragment in str(exc.value)


def test_all_problems_are_reported_at_once():
    with pytest.raises(RuntimeError) as exc:
        # conftest pins real-looking secrets in the environment, so spell out
        # the placeholder defaults a bare checkout would have.
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret=PLACEHOLDER_JWT_SECRET,
            session_secret=PLACEHOLDER_SESSION_SECRET,
            database_url=f"postgresql://edupath:{DEFAULT_DB_PASSWORD}@db/edupath",
            cookie_secure=False,
        ).validate_for_environment()
    message = str(exc.value)
    for fragment in ("JWT_SECRET", "SESSION_SECRET", "database password", "COOKIE_SECURE"):
        assert fragment in message


def test_environment_name_is_case_insensitive():
    with pytest.raises(RuntimeError):
        Settings(_env_file=None, environment=" Production ").validate_for_environment()


# --- tunable values (models, retrieval, chunking, Ollama host) ---------------


def bare(**overrides) -> Settings:
    """Settings with no .env file and nothing but the given overrides."""
    return Settings(_env_file=None, **overrides)


def test_tuning_defaults_are_the_values_that_used_to_be_hard_coded():
    s = bare()
    assert (s.chat_model, s.quiz_model) == ("llama3.1:8b", "llama3.1:8b")
    assert (s.vision_model, s.fallback_vision_model) == ("qwen3-vl:4b", "qwen3-vl:8b")
    assert (s.candidate_k, s.top_k) == (10, 3)
    assert s.rerank_min_confidence == 0.1
    assert (s.chunk_size, s.chunk_overlap) == (800, 100)
    assert s.ollama_base_url == ""


def test_tuning_values_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("CHAT_MODEL", "qwen3:8b")
    monkeypatch.setenv("TOP_K", "5")
    monkeypatch.setenv("CANDIDATE_K", "20")
    monkeypatch.setenv("RERANK_MIN_CONFIDENCE", "0.25")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    s = bare()
    assert (s.chat_model, s.top_k, s.candidate_k) == ("qwen3:8b", 5, 20)
    assert s.rerank_min_confidence == 0.25
    assert s.ollama_base_url == "http://host.docker.internal:11434"


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"top_k": 0}, "top_k"),
        ({"top_k": -1}, "top_k"),
        ({"candidate_k": 0}, "candidate_k"),
        ({"candidate_k": 2, "top_k": 3}, "CANDIDATE_K"),
        ({"rerank_min_confidence": -0.1}, "rerank_min_confidence"),
        ({"rerank_min_confidence": 1.5}, "rerank_min_confidence"),
        ({"chunk_size": 50}, "chunk_size"),
        ({"chunk_overlap": -1}, "chunk_overlap"),
        ({"chunk_size": 200, "chunk_overlap": 200}, "CHUNK_OVERLAP"),
        ({"chat_model": "  "}, "CHAT_MODEL"),
        ({"quiz_model": ""}, "QUIZ_MODEL"),
        ({"vision_model": ""}, "VISION_MODEL"),
    ],
)
def test_invalid_tuning_values_are_rejected_at_startup(overrides, fragment):
    with pytest.raises(ValueError) as exc:
        bare(**overrides)
    assert fragment in str(exc.value)


def test_boundary_values_are_accepted():
    bare(top_k=1, candidate_k=1)
    bare(rerank_min_confidence=0.0)
    bare(rerank_min_confidence=1.0)
    bare(chunk_size=100, chunk_overlap=99)
    bare(fallback_vision_model="")  # empty disables the fallback


def test_modules_read_their_tuning_values_from_settings():
    """The module constants are initialised from settings, not re-hard-coded."""
    from app.config import settings
    from app.controllers import chat_controller
    from app.services import quiz_service, retrieval
    from ingestion import chunking, images

    assert chat_controller.CHAT_MODEL == settings.chat_model
    assert quiz_service.QUIZ_MODEL == settings.quiz_model
    assert (retrieval.TOP_K, retrieval.CANDIDATE_K) == (settings.top_k, settings.candidate_k)
    assert retrieval.RERANK_MIN_CONFIDENCE == settings.rerank_min_confidence
    assert (chunking.CHUNK_SIZE, chunking.CHUNK_OVERLAP) == (
        settings.chunk_size, settings.chunk_overlap)
    assert images.VISION_MODEL == settings.vision_model
    assert images.FALLBACK_VISION_MODEL == settings.fallback_vision_model


# Captured at import time: the autouse offline guard replaces llm.get_client with
# a stub for every test, and this test needs the real one.
from app.services import llm as _llm  # noqa: E402

_real_get_client = _llm.get_client


def test_ollama_client_uses_the_configured_host_and_timeout(monkeypatch):
    seen = {}
    monkeypatch.setattr(_llm.ollama, "Client", lambda **kw: seen.update(kw) or object())
    _real_get_client.cache_clear()
    try:
        monkeypatch.setattr(settings, "ollama_base_url", "http://host.docker.internal:11434")
        _real_get_client()
        assert seen["host"] == "http://host.docker.internal:11434"
        assert seen["timeout"] == settings.ollama_timeout_seconds

        _real_get_client.cache_clear()
        monkeypatch.setattr(settings, "ollama_base_url", "")
        _real_get_client()
        assert seen["host"] is None  # library default / OLLAMA_HOST env
    finally:
        _real_get_client.cache_clear()
