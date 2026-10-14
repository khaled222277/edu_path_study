"""Persistent ChromaDB collection holding the lecture corpus.

Lives in services/ rather than ingestion/ because Phase 3 retrieval needs the
exact same collection handle and embedding function.
"""

from pathlib import Path

import chromadb
from chromadb.api.models.Collection import Collection
from chromadb.utils import embedding_functions

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


def get_embedding_function(device: str = "cpu"):
    """SentenceTransformer embedding fn; normalized vectors to pair with cosine."""
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBEDDING_MODEL,
        device=device,
        normalize_embeddings=True,
    )


def get_client(persist_path: Path | None = None) -> chromadb.ClientAPI:
    path = persist_path or CHROMA_PATH
    path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(path))


def get_collection(
    persist_path: Path | None = None, device: str = "cpu"
) -> Collection:
    """The single collection every lecture chunk lands in."""
    return get_client(persist_path).get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=get_embedding_function(device),
        configuration={"hnsw": {"space": "cosine"}},
    )
