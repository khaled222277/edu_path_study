"""A throwaway world to drive the real chat routes against, without touching real data.

    from scripts.chat_sandbox import Sandbox
    box = Sandbox(chroma_copy="...", db_file="...")     # BEFORE importing anything else from app
    sid = box.new_session()
    out = box.stream(sid, "what is classification", seed=42)

What is real: the FastAPI app and its chat routes, the controller, retrieval (bge-m3, FlashRank), the
Ollama chat model. What is not: the database is a throwaway SQLite file, and the vector store is a
COPY of data/chroma_db that the caller made. Both are refused if they point into the real data/
folder or at anything but SQLite, so a wrong argument cannot reach real data.

It never configures, loads, unloads or restarts anything in Ollama: it only sends chat requests.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL_DATA = (ROOT / "data").resolve()


class Sandbox:
    def __init__(self, chroma_copy: str, db_file: str, subject_id: int = 7) -> None:
        chroma, db = Path(chroma_copy).resolve(), Path(db_file).resolve()
        for p in (chroma, db):
            if REAL_DATA == p or REAL_DATA in p.parents:
                raise SystemExit(f"refusing to use {p}: it is inside the real data/ folder")
        if not chroma.exists():
            raise SystemExit(f"{chroma} does not exist: copy data/chroma_db there first")
        if db.exists():
            db.unlink()
        # Pinned before `app` is imported, exactly as tests/conftest.py does.
        os.environ.update({
            "DATABASE_URL": f"sqlite:///{db.as_posix()}",
            "ENVIRONMENT": "development", "RATE_LIMIT_ENABLED": "false", "WARMUP_ON_STARTUP": "false",
            "QUIZ_SWEEP_ENABLED": "false", "ALLOWLIST_ENABLED": "false", "COOKIE_SECURE": "false",
            "JWT_SECRET": "sandbox-jwt-" + "x" * 32, "SESSION_SECRET": "sandbox-session-" + "y" * 32,
            "GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": "",
        })
        sys.path.insert(0, str(ROOT))
        from fastapi.testclient import TestClient

        from app.auth.jwt import create_access_token
        from app.config import settings
        from app.db.base import Base
        from app.db.session import SessionLocal, engine
        from app.main import app
        from app.models import Subject, University, User
        from app.services import vector_store

        assert engine.url.get_backend_name() == "sqlite", "sandbox must run on SQLite"
        vector_store.CHROMA_PATH = chroma
        vector_store.reset_cache()
        Base.metadata.create_all(engine)
        with SessionLocal() as db_session:
            uni = University(name="Sandbox University", country="Egypt")
            db_session.add(uni)
            db_session.flush()
            db_session.add(Subject(id=subject_id, university_id=uni.id, year=5, name="KDD", code="KDD"))
            user = User(google_id="g-sandbox", email="sandbox@example.com", name="Sandbox",
                        university_id=uni.id, year=5)
            db_session.add(user)
            db_session.commit()
            self.user_id = user.id
        self.subject_id = subject_id
        self.settings = settings
        self.SessionLocal = SessionLocal
        self._client_cm = TestClient(app)
        self.client = self._client_cm.__enter__()
        self.client.cookies.set(settings.cookie_name, create_access_token(self.user_id))

    def close(self) -> None:
        self._client_cm.__exit__(None, None, None)

    def new_session(self) -> int:
        r = self.client.post("/chat/sessions", json={"subject_id": self.subject_id})
        r.raise_for_status()
        return r.json()["id"]

    def stream(self, session_id: int, question: str) -> dict:
        """One turn through the real streaming route. Returns what the browser would have seen."""
        r = self.client.post(f"/chat/sessions/{session_id}/messages/stream", json={"content": question})
        r.raise_for_status()
        events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
        streamed = "".join(e["text"] for e in events if e["type"] == "token")
        done = next((e for e in events if e["type"] == "done"), None)
        return {
            "streamed": streamed,
            "stored": done["message"]["content"] if done else None,
            "citations": done["citations"] if done else None,
            "early_citations": next((e["citations"] for e in events if e["type"] == "citations"), None),
            "errored": any(e["type"] == "error" for e in events),
        }

    def plain(self, session_id: int, question: str) -> dict:
        """One turn through the plain route (answer_question)."""
        r = self.client.post(f"/chat/sessions/{session_id}/messages", json={"content": question})
        r.raise_for_status()
        body = r.json()
        return {"stored": body["message"]["content"], "citations": body["citations"]}

    def seed_exchange(self, session_id: int, question: str, answer: str, sources: list[dict]) -> None:
        """Put a finished question + answer (with the sources it was shown with) into a session.

        Used to replay a conversation whose first turn was already answered for real, so several
        second turns can follow the very same first turn without paying for it again.
        """
        from app.models.chat import ROLE_ASSISTANT, ROLE_USER, ChatMessage

        with self.SessionLocal() as db_session:
            db_session.add(ChatMessage(session_id=session_id, role=ROLE_USER, content=question))
            db_session.add(ChatMessage(session_id=session_id, role=ROLE_ASSISTANT, content=answer, sources=sources))
            db_session.commit()
