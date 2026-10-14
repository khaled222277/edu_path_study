"""Application settings, loaded from environment / .env."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    # Absolute, so the CLI scripts under ingestion/ and scripts/ load the same
    # .env as the API no matter which directory they are invoked from.
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database ---
    database_url: str = (
        "postgresql://edupath:your_password_here@localhost:5432/edupath_study"
    )

    # --- Google OAuth2 ---
    google_client_id: str = ""
    google_client_secret: str = ""
    # Must match the redirect URI registered in the Google Cloud console.
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    # --- JWT / session ---
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # 7 days

    # Signs the short-lived Starlette session used for the OAuth state param.
    session_secret: str = "change-me-in-production-too"

    # --- Cookie ---
    cookie_name: str = "edupath_session"
    cookie_secure: bool = False  # True once served over HTTPS
    cookie_samesite: str = "lax"

    # --- Frontend ---
    frontend_url: str = "http://localhost:5173"


settings = Settings()
