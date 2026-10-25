# Backend API only (the React app is built/served separately).
# Ingestion needs Tesseract and is an offline admin task, so it is not in here.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=10

WORKDIR /app

# CPU-only torch first: the default Linux wheel drags in ~2.5 GB of CUDA libs
# that sentence-transformers does not need here. requirements.txt then sees
# torch as already satisfied.
#
# The pip cache is a BuildKit cache mount (kept out of the image), so a build
# that dies mid-download on a slow link resumes from the wheels it already has
# instead of starting over.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY alembic.ini .
COPY app ./app
COPY ingestion ./ingestion
COPY scripts ./scripts

# HF_HOME is where sentence-transformers keeps the bge-m3 weights (~2 GB). It lives on
# its own volume (/models) so a recreated container does not download them again.
# (Set after the pip steps so changing it does not invalidate the cached layers.)
ENV HF_HOME=/models/huggingface

# data/ (Chroma store, figures, reranker cache) is mounted, never baked in.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data /models/huggingface \
    && chown -R appuser /app /models
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

# Run `alembic upgrade head` as a separate one-off step before first start.
# Set ENVIRONMENT=production to enforce the secret / cookie checks at startup.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
