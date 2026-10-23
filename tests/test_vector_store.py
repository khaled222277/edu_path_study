"""Caching, thread-safety and cross-process freshness of the Chroma handles."""

import subprocess
import sys
import textwrap
import threading
import time

import pytest
from chromadb import EmbeddingFunction
from chromadb.errors import InternalError
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.services import retrieval, vector_store


class TinyEF(EmbeddingFunction):
    """Deterministic 3-d embedder, so real Chroma runs offline in milliseconds."""

    def __init__(self):
        pass

    def __call__(self, input):
        return [[float(len(t) % 7), 1.0, 0.5] for t in input]

    @staticmethod
    def name():
        return "tiny_test_ef"

    def get_config(self):
        return {}

    @staticmethod
    def build_from_config(config):
        return TinyEF()

    def default_space(self):
        return "cosine"


@pytest.fixture()
def tiny_store(tmp_path, monkeypatch):
    """vector_store pointed at a throwaway directory with the tiny embedder."""
    monkeypatch.setattr(vector_store, "CHROMA_PATH", tmp_path)
    monkeypatch.setattr(vector_store, "get_embedding_function", lambda device="cpu": TinyEF())
    return tmp_path


# --- embedder: built once, even under a stampede ---------------------------


def test_embedder_is_built_once_under_concurrent_first_requests(monkeypatch):
    built = []

    def slow_factory(**kwargs):
        built.append(kwargs)
        time.sleep(0.2)  # wide enough that unlocked callers would all enter
        return object()

    monkeypatch.setattr(
        vector_store.embedding_functions,
        "SentenceTransformerEmbeddingFunction",
        slow_factory,
    )

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(vector_store.get_embedding_function()))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(built) == 1
    assert len({id(r) for r in results}) == 1  # everyone got the same instance


def test_embedder_is_cached_per_device(monkeypatch):
    # Pretend both devices exist: this is about cache keys, not this machine's GPU.
    monkeypatch.setattr(vector_store, "resolve_device", lambda requested: requested)
    monkeypatch.setattr(
        vector_store.embedding_functions,
        "SentenceTransformerEmbeddingFunction",
        lambda **kw: object(),
    )
    cpu = vector_store.get_embedding_function("cpu")
    assert vector_store.get_embedding_function("cpu") is cpu
    assert vector_store.get_embedding_function("cuda") is not cpu


def test_failed_embedder_load_is_not_cached(monkeypatch):
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("model download failed")
        return object()

    monkeypatch.setattr(
        vector_store.embedding_functions, "SentenceTransformerEmbeddingFunction", flaky
    )
    with pytest.raises(OSError):
        vector_store.get_embedding_function()
    assert vector_store.get_embedding_function() is not None  # retried, not poisoned


# --- collection / client cache ---------------------------------------------


def test_collection_handle_is_cached_until_reset(tiny_store):
    first = vector_store.get_collection()
    assert vector_store.get_collection() is first
    vector_store.reset_cache()
    assert vector_store.get_collection() is not first


def test_reset_keeps_the_expensive_embedder(monkeypatch):
    monkeypatch.setattr(
        vector_store.embedding_functions,
        "SentenceTransformerEmbeddingFunction",
        lambda **kw: object(),
    )
    embedder = vector_store.get_embedding_function()
    vector_store.reset_cache()
    assert vector_store.get_embedding_function() is embedder
    vector_store.reset_cache(drop_embedders=True)
    assert vector_store.get_embedding_function() is not embedder


# --- retry_if_stale ---------------------------------------------------------


def test_retry_if_stale_resets_and_retries_once(monkeypatch):
    resets = []
    monkeypatch.setattr(vector_store, "reset_cache", lambda **kw: resets.append(1))
    calls = []

    def op():
        calls.append(1)
        if len(calls) == 1:
            raise InternalError("Error finding id")
        return "ok"

    assert vector_store.retry_if_stale(op) == "ok"
    assert len(calls) == 2 and resets == [1]


def test_retry_if_stale_gives_up_after_one_retry(monkeypatch):
    monkeypatch.setattr(vector_store, "reset_cache", lambda **kw: None)
    calls = []

    def op():
        calls.append(1)
        raise InternalError("still broken")

    with pytest.raises(InternalError):
        vector_store.retry_if_stale(op)
    assert len(calls) == 2


def test_retry_if_stale_ignores_unrelated_errors(monkeypatch):
    resets = []
    monkeypatch.setattr(vector_store, "reset_cache", lambda **kw: resets.append(1))

    def op():
        raise ValueError("a bug, not staleness")

    with pytest.raises(ValueError):
        vector_store.retry_if_stale(op)
    assert resets == []


def test_retrieve_recovers_from_a_stale_client(monkeypatch):
    """retrieve() must survive one stale-client error without surfacing it."""
    from .test_retrieval import FakeCollection, FakeRanker

    attempts = []

    class StaleOnce(FakeCollection):
        def query(self, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise InternalError("Error finding id")
            return super().query(**kwargs)

    monkeypatch.setattr(vector_store, "reset_cache", lambda **kw: None)
    monkeypatch.setattr(retrieval, "get_collection", lambda: StaleOnce())
    monkeypatch.setattr(retrieval, "get_ranker", lambda: FakeRanker({0: 0.9}))

    chunks = retrieval.retrieve("q", subject_id=7)
    assert len(attempts) == 2 and chunks


# --- the real thing: a second process writes, the API process must see it ---


def test_new_rows_from_another_process_are_visible_after_stale_retry(tiny_store):
    """A held handle breaks after an ingestion process writes; retry_if_stale heals it."""
    collection = vector_store.get_collection()
    collection.add(ids=["a"], documents=["first"], metadatas=[{"subject_id": 1}])

    def query():
        return vector_store.get_collection().query(
            query_texts=["x"], n_results=5, where={"subject_id": 1}
        )["ids"][0]

    assert query() == ["a"]

    writer = textwrap.dedent(
        f"""
        import chromadb
        from chromadb import EmbeddingFunction

        class EF(EmbeddingFunction):
            def __init__(self): pass
            def __call__(self, input): return [[float(len(t) % 7), 1.0, 0.5] for t in input]
            @staticmethod
            def name(): return "tiny_test_ef"
            def get_config(self): return {{}}
            @staticmethod
            def build_from_config(c): return EF()
            def default_space(self): return "cosine"

        col = chromadb.PersistentClient(path=r"{tiny_store}").get_or_create_collection(
            "{vector_store.COLLECTION_NAME}", embedding_function=EF())
        col.add(ids=["b"], documents=["added by ingestion"], metadatas=[{{"subject_id": 1}}])
        """
    )
    done = subprocess.run([sys.executable, "-c", writer], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr

    # Without the retry the held client cannot read the new row...
    with pytest.raises(InternalError):
        query()
    # ...with it, the same call just works and returns both rows.
    assert sorted(vector_store.retry_if_stale(query)) == ["a", "b"]


# --- startup warm-up --------------------------------------------------------


def test_warm_up_failure_is_not_fatal(monkeypatch, caplog):
    def boom():
        raise OSError("no network for the model download")

    monkeypatch.setattr(vector_store, "warm_up", boom)
    monkeypatch.setattr(retrieval, "get_ranker", lambda: None)
    monkeypatch.setattr(main.llm, "warm_chat_model", lambda: None)
    with caplog.at_level("WARNING"):
        main.warm_up_models()  # must not raise
    assert "embedding model warm-up failed; the first request will retry" in caplog.text


def test_warm_up_loads_embedder_reranker_and_chat_model(monkeypatch):
    loaded = []
    monkeypatch.setattr(vector_store, "warm_up", lambda: loaded.append("embedder"))
    monkeypatch.setattr(retrieval, "get_ranker", lambda: loaded.append("reranker"))
    monkeypatch.setattr(main.llm, "warm_chat_model", lambda: loaded.append("chat"))
    main.warm_up_models()
    assert loaded == ["embedder", "reranker", "chat"]


@pytest.mark.parametrize("enabled, expected_runs", [(True, 1), (False, 0)])
def test_startup_runs_warm_up_only_when_enabled(monkeypatch, enabled, expected_runs):
    ran = threading.Event()
    count = []
    monkeypatch.setattr(settings, "warmup_on_startup", enabled)
    monkeypatch.setattr(main, "warm_up_models", lambda: (count.append(1), ran.set()))

    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200  # not blocked by warm-up
        ran.wait(timeout=2)

    assert len(count) == expected_runs
