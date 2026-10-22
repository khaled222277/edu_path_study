"""Production startup validation."""

import pytest

from app.config import (
    DEFAULT_DB_PASSWORD,
    PLACEHOLDER_JWT_SECRET,
    PLACEHOLDER_SESSION_SECRET,
    Settings,
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
