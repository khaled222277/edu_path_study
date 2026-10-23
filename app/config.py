"""Application settings, loaded from environment / .env."""

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Development defaults. They exist so a fresh clone boots, and are refused in
# production by Settings.validate_for_environment().
DEFAULT_DB_PASSWORD = "your_password_here"
PLACEHOLDER_JWT_SECRET = "change-me-in-production"
PLACEHOLDER_SESSION_SECRET = "change-me-in-production-too"
MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    # Absolute, so the CLI scripts under ingestion/ and scripts/ load the same
    # .env as the API no matter which directory they are invoked from.
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # "production" turns on validate_for_environment() at startup.
    environment: str = "development"

    # --- Database ---
    database_url: str = (
        f"postgresql://edupath:{DEFAULT_DB_PASSWORD}@localhost:5432/edupath_study"
    )

    # Load the embedding model and reranker in the background at startup so the
    # first user request does not pay ~30 s for it. Failure is logged, not fatal.
    warmup_on_startup: bool = True

    # --- Models (Ollama tags) ---
    chat_model: str = "llama3.1:8b"
    quiz_model: str = "llama3.1:8b"
    # Ingestion only: reads slide figures. The fallback is tried when the primary
    # gives no verdict; set it to an empty string to disable the fallback.
    vision_model: str = "qwen3-vl:4b"
    fallback_vision_model: str = "qwen3-vl:8b"

    # --- Retrieval ---
    # Chunks pulled from Chroma, then reranked down to `top_k` for the prompt.
    candidate_k: int = Field(default=10, ge=1, le=200)
    top_k: int = Field(default=3, ge=1, le=50)
    # Below this best cross-encoder score the reranker is ignored and the vector
    # order is kept (it is unreliable on cross-lingual pairs).
    rerank_min_confidence: float = Field(default=0.1, ge=0.0, le=1.0)

    # --- Chunking (ingestion) ---
    # Changing either only affects lectures ingested AFTER the change; existing
    # chunks keep their old boundaries until the lecture is re-ingested.
    chunk_size: int = Field(default=800, ge=100, le=8000)
    chunk_overlap: int = Field(default=100, ge=0)

    # --- Ollama ---
    # How long Ollama keeps a model in memory after its last request. Its own
    # default is 5 minutes, so after a quiet spell the next student pays the model
    # load. A Go duration ("30m", "1h30m"), a number of seconds, or "-1" (forever).
    ollama_keep_alive: str = Field(
        default="30m", pattern=r"^(-1|\d+(\.\d+)?|(\d+(\.\d+)?(ns|us|µs|ms|s|m|h))+)$"
    )
    # Empty = the ollama library default, which honours the OLLAMA_HOST env var
    # (http://localhost:11434). Inside Docker point it at the host, e.g.
    # http://host.docker.internal:11434.
    ollama_base_url: str = ""
    # Per-request ceiling for the chat / quiz models (seconds). Without it a hung
    # Ollama would hold a request thread forever.
    ollama_timeout_seconds: float = 120.0
    # Offline ingestion talks to a slower vision model on whatever hardware the
    # admin has, so it gets a much longer ceiling.
    ollama_ingest_timeout_seconds: float = 600.0

    # --- Rate limiting (slowapi, in-memory, per process) ---
    rate_limit_enabled: bool = True
    rate_limit_auth: str = "10/minute"  # login + OAuth callback, per client IP
    rate_limit_chat: str = "20/minute"  # chat messages, per user
    rate_limit_quiz: str = "5/minute"  # quiz generation, per user

    # --- Google OAuth2 ---
    google_client_id: str = ""
    google_client_secret: str = ""
    # Must match the redirect URI registered in the Google Cloud console.
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    # --- JWT / session ---
    jwt_secret: str = PLACEHOLDER_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # 7 days

    # Signs the short-lived Starlette session used for the OAuth state param.
    session_secret: str = PLACEHOLDER_SESSION_SECRET

    # --- Cookie ---
    cookie_name: str = "edupath_session"
    cookie_secure: bool = False  # True once served over HTTPS
    cookie_samesite: str = "lax"

    # --- Frontend ---
    frontend_url: str = "http://localhost:5173"

    @model_validator(mode="after")
    def _check_relationships(self) -> "Settings":
        """Cross-field bounds that a single Field() constraint cannot express."""
        if self.candidate_k < self.top_k:
            raise ValueError(
                f"CANDIDATE_K ({self.candidate_k}) must be >= TOP_K ({self.top_k})"
            )
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                f"CHUNK_OVERLAP ({self.chunk_overlap}) must be smaller than "
                f"CHUNK_SIZE ({self.chunk_size})"
            )
        if not self.chat_model.strip() or not self.quiz_model.strip():
            raise ValueError("CHAT_MODEL and QUIZ_MODEL must not be empty")
        if not self.vision_model.strip():
            raise ValueError("VISION_MODEL must not be empty")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"

    def validate_for_environment(self) -> None:
        """Raise RuntimeError listing every unsafe setting when in production.

        A no-op in any other environment. Reports all problems at once so an
        operator fixes them in one pass instead of one restart per secret.
        """
        if not self.is_production:
            return

        problems: list[str] = []
        for name, value, placeholder in (
            ("JWT_SECRET", self.jwt_secret, PLACEHOLDER_JWT_SECRET),
            ("SESSION_SECRET", self.session_secret, PLACEHOLDER_SESSION_SECRET),
        ):
            if not value or value == placeholder or value.startswith("change-me"):
                problems.append(f"{name} is unset or still the placeholder")
            elif len(value) < MIN_SECRET_LENGTH:
                problems.append(f"{name} must be at least {MIN_SECRET_LENGTH} characters")
        if self.jwt_secret == self.session_secret:
            problems.append("JWT_SECRET and SESSION_SECRET must be different values")

        db_password = urlsplit(self.database_url).password
        if not db_password or db_password == DEFAULT_DB_PASSWORD:
            problems.append("DATABASE_URL has an empty or default database password")

        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true (serve over HTTPS)")

        if problems:
            raise RuntimeError(
                "Refusing to start with ENVIRONMENT=production:\n  - "
                + "\n  - ".join(problems)
            )


settings = Settings()
