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

    # Repair quizzes a crash left half-written (see app/services/quiz_sweeper.py): their
    # total says "10 questions" but fewer were stored. Runs at startup and then every
    # SWEEP_INTERVAL minutes (0 = only at startup). Only attempts older than STALE_AFTER
    # are touched, so a quiz still being written is never interfered with.
    quiz_sweep_enabled: bool = True
    quiz_stale_after_minutes: int = Field(default=60, ge=5, le=10080)
    quiz_sweep_interval_minutes: int = Field(default=30, ge=0, le=10080)

    # --- Models (Ollama tags) ---
    chat_model: str = "llama3.1:8b"
    quiz_model: str = "llama3.1:8b"
    # Ingestion only: reads slide figures. The fallback is tried when the primary
    # gives no verdict; set it to an empty string to disable the fallback.
    vision_model: str = "qwen3-vl:4b"
    fallback_vision_model: str = "qwen3-vl:8b"

    # --- Quiz generation ---
    # Sampling temperature for quiz calls only. Retries need some variation, so
    # this should not be 0.
    quiz_temperature: float = Field(default=0.5, ge=0.0, le=2.0)
    # Model calls per chunk under the full rules, then (Arabic only) under the
    # relaxed rules. When the strict budget runs out the relaxed phase accepts
    # Latin script in Arabic questions, so lowering it can lower quality.
    quiz_strict_attempts: int = Field(default=8, ge=1, le=20)
    quiz_relaxed_attempts: int = Field(default=4, ge=0, le=20)
    # Model calls to spend on a chunk before moving on to the next one. A chunk
    # that has failed twice is a worse bet than a fresh one (measured: 46% then 32%
    # per call, against 68% for a fresh chunk), so the first pass gives up early.
    # Chunks given up on are revisited -- with their remaining strict attempts, then
    # the relaxed rules -- only if the quiz is still short, so no rule is loosened.
    # 0 turns this off: each chunk gets its full budget in turn.
    quiz_first_pass_attempts: int = Field(default=2, ge=0, le=20)
    # Chunks generated at the same time. Only useful if Ollama itself runs requests
    # in parallel (OLLAMA_NUM_PARALLEL > 1, which is not the case by default on small
    # GPUs): with Ollama serving one request at a time, extra callers just queue and
    # nothing gets faster. Never more are in flight than questions still needed.
    quiz_concurrency: int = Field(default=1, ge=1, le=8)

    # --- Retrieval ---
    # Which cross-encoder reorders the vector candidates. "flashrank" is the small ONNX
    # model the app has always used; "bge-m3" is BAAI/bge-reranker-v2-m3, multilingual
    # (measured: ~0.6 GB more RAM than FlashRank on CPU and ~8x slower per query: 1.8 s vs
    # 0.2 s for 10 candidates). See eval/README.md for the measurements.
    reranker: str = Field(default="flashrank", pattern=r"^(flashrank|bge-m3)$")
    # Where the bge-m3 reranker runs. It shares the GPU with Ollama if set to cuda, which
    # can crowd the chat model out of VRAM, so the default is cpu.
    reranker_device: str = Field(default="cpu", pattern=r"^(cpu|mps|cuda(:\d+)?)$")
    # Search for what a follow-up ("explain that again") means, using the model to turn it
    # into a standalone question first. Costs one short model call (~0.35 s) per turn that
    # has a previous question; falls back to the question as asked if it fails.
    chat_followup_rewrite: bool = True
    # One INFO line per chat turn for retrieval and one for the reply (what was retrieved,
    # whether the reply was a refusal, how many citations were taken out, how many sources
    # were shown), to find out why a particular answer had no sources. Off by default. The
    # question text is never logged unless CHAT_TRACE_QUERIES is also on.
    chat_trace_log: bool = False
    chat_trace_queries: bool = False
    # Chunks pulled from Chroma, then reranked down to `top_k` for the prompt.
    candidate_k: int = Field(default=10, ge=1, le=200)
    top_k: int = Field(default=3, ge=1, le=50)
    # Below this best cross-encoder score the reranker is ignored and the vector
    # order is kept (it is unreliable on cross-lingual pairs).
    rerank_min_confidence: float = Field(default=0.1, ge=0.0, le=1.0)

    # Where the query embedder (bge-m3) runs: "cpu", "mps", "cuda" or "cuda:N".
    # If the requested accelerator is not available the app falls back to cpu with
    # a warning instead of failing to start. Vectors are the same either way, so
    # this never needs a re-ingest.
    embed_device: str = Field(default="cpu", pattern=r"^(cpu|mps|cuda(:\d+)?)$")

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
    # Behind a reverse proxy every request arrives from the proxy's address, so the per-IP
    # limits (login) would all share one counter. Turn this on ONLY when a proxy you control
    # sets X-Forwarded-For and the API is not reachable except through it: otherwise anyone
    # can send the header and pick their own "IP". Off by default.
    trusted_proxy: bool = False
    # Pilot access control: only emails in the allowed_emails table (scripts/manage_allowlist.py)
    # may sign in, and an already-signed-in student who is removed from it is signed out at their
    # next request. OFF by default so existing behaviour is unchanged; ON for the pilot.
    allowlist_enabled: bool = False
    # How many proxies in front of the API append to X-Forwarded-For. The address read is
    # the one that many entries from the right (the left side is client-supplied).
    trusted_proxy_hops: int = Field(default=1, ge=1, le=5)
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
