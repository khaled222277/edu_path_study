"""Persistent ChromaDB collection holding the lecture corpus.

Lives in services/ rather than ingestion/ because Phase 3 retrieval needs the
exact same collection handle and embedding function.

Caching. The embedder (bge-m3, ~2 GB, ~30 s to load) and the Chroma client are
built once per process and shared by every request thread. Chroma already keeps
the loaded model in a class-level dict, but that dict is not locked: two
requests arriving together on a cold process would each load the model. The lock
here is what makes the first load happen exactly once.

Freshness. Ingestion runs as a separate process and writes straight to the same
on-disk store. A Chroma client that was open before such a write cannot query
the new rows -- `query()` raises "Error finding id" -- and that holds for any
client in this process, cached or not, because Chroma shares one System per
path. `retry_if_stale` therefore resets the client and tries once more, so a
running API picks up a newly ingested lecture without a restart.
"""

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import chromadb
from chromadb.api.models.Collection import Collection
from chromadb.api.shared_system_client import SharedSystemClient
from chromadb.errors import InternalError
from chromadb.utils import embedding_functions

log = logging.getLogger(__name__)

T = TypeVar("T")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHROMA_PATH = PROJECT_ROOT / "data" / "chroma_db"

COLLECTION_NAME = "lecture_corpus"

# bge-m3 is multilingual (100+ languages, 8192 tokens), which is what the
# Arabic/English lecture corpus needs. Per the model card it takes NO
# instruction prefix -- unlike e5, queries and passages are both encoded raw:
# "the BGE-M3 model no longer requires adding instructions to the queries".
EMBEDDING_MODEL = "BAAI/bge-m3"

# Content-type tags stored on every chunk so retrieval can tell them apart.
CONTENT_TYPE_TEXT = "text"
CONTENT_TYPE_IMAGE = "image_description"

# RLock: get_collection() calls get_embedding_function() while holding it.
_lock = threading.RLock()
_embedding_functions: dict[str, object] = {}
_clients: dict[str, chromadb.ClientAPI] = {}
_collections: dict[tuple[str, str], Collection] = {}


def get_embedding_function(device: str = "cpu"):
    """SentenceTransformer embedding fn, built once per device per process.

    Normalized vectors, to pair with cosine.
    """
    with _lock:
        fn = _embedding_functions.get(device)
        if fn is None:
            log.info("loading embedding model %s on %s", EMBEDDING_MODEL, device)
            fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=EMBEDDING_MODEL,
                device=device,
                normalize_embeddings=True,
            )
            _embedding_functions[device] = fn
        return fn


def get_client(persist_path: Path | None = None) -> chromadb.ClientAPI:
    path = persist_path or CHROMA_PATH
    key = str(path)
    with _lock:
        client = _clients.get(key)
        if client is None:
            path.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=key)
            _clients[key] = client
        return client


def get_collection(
    persist_path: Path | None = None, device: str = "cpu"
) -> Collection:
    """The single collection every lecture chunk lands in."""
    key = (str(persist_path or CHROMA_PATH), device)
    with _lock:
        collection = _collections.get(key)
        if collection is None:
            collection = get_client(persist_path).get_or_create_collection(
                name=COLLECTION_NAME,
                embedding_function=get_embedding_function(device),
                configuration={"hnsw": {"space": "cosine"}},
            )
            _collections[key] = collection
        return collection


def reset_cache(*, drop_embedders: bool = False) -> None:
    """Forget the cached client and collection handles.

    Also clears Chroma's own per-path System cache -- without that, a new
    PersistentClient would hand back the same stale instance. The embedder is
    kept unless `drop_embedders`, because reloading it costs ~30 s and a stale
    index has nothing to do with it. Tests pass drop_embedders=True.
    """
    with _lock:
        _collections.clear()
        _clients.clear()
        if drop_embedders:
            _embedding_functions.clear()
        SharedSystemClient.clear_system_cache()


def retry_if_stale(operation: Callable[[], T]) -> T:
    """Run a Chroma operation; if the client is stale, reset it and retry once.

    `operation` must fetch its own collection (call get_collection() inside it)
    so the retry actually uses the fresh handle. Only Chroma's InternalError --
    what a client stale against another process's writes raises -- is retried;
    anything else propagates, and a second failure propagates too.
    """
    try:
        return operation()
    except InternalError as exc:
        log.warning("chroma client looks stale (%s); resetting and retrying once", exc)
        reset_cache()
        return operation()


def warm_up() -> None:
    """Load the embedder and open the collection ahead of the first request."""
    get_collection()
