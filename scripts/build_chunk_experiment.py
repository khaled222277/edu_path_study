"""Rebuild the lecture chunks at a different size into a SEPARATE Chroma store, for evaluation only.

    python scripts/build_chunk_experiment.py --chunk-size 400 --chunk-overlap 50
    python scripts/eval_quality.py retrieval --chroma-path data/chroma_experiments/c400_o50 ...

The real collection is only READ (to copy each lecture's metadata and its figure
descriptions). Nothing is written to it, to Postgres, or to data/images, and the script
refuses to write anywhere near the real store. Text is re-extracted from the PDFs in
data/lectures and re-chunked with the same functions ingestion uses; figure-description
chunks are copied unchanged, since they do not depend on the chunk size.

Re-run with the shipped size (800/100) first and compare the chunk counts it prints with
the real collection's: if they match, the rebuild is faithful.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import chromadb  # noqa: E402

from app.services import vector_store  # noqa: E402

EXPERIMENTS = ROOT / "data" / "chroma_experiments"


def default_out(size: int, overlap: int) -> Path:
    return EXPERIMENTS / f"c{size}_o{overlap}"


def assert_safe_target(out: Path) -> None:
    """Refuse to write into the real store (or anywhere that contains or sits inside it)."""
    real = vector_store.CHROMA_PATH.resolve()
    target = out.resolve()
    if target == real or real in target.parents or target in real.parents:
        raise SystemExit(f"refusing to write to {target}: it is, contains or sits inside the real store {real}")


def file_templates(metadatas: list[dict]) -> dict[str, dict]:
    """source_filename -> the metadata every chunk of that lecture shares."""
    templates: dict[str, dict] = {}
    for m in metadatas:
        templates.setdefault(
            m["source_filename"],
            {k: v for k, v in m.items() if k not in ("page_number", "content_type", "image_path")},
        )
    return templates


def text_records(filename: str, chunks, template: dict) -> tuple[list[str], list[str], list[dict]]:
    ids = [f"{filename}:text:{c.index}" for c in chunks]
    docs = [c.text for c in chunks]
    metas = [{**template, "page_number": c.page_number, "content_type": vector_store.CONTENT_TYPE_TEXT} for c in chunks]
    return ids, docs, metas


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--chunk-size", type=int, required=True)
    p.add_argument("--chunk-overlap", type=int, required=True)
    p.add_argument("--out", type=Path)
    p.add_argument("--force", action="store_true", help="overwrite an existing experiment store")
    args = p.parse_args()
    if args.chunk_overlap >= args.chunk_size:
        raise SystemExit("overlap must be smaller than the chunk size")

    out = args.out or default_out(args.chunk_size, args.chunk_overlap)
    assert_safe_target(out)
    if out.exists() and any(out.iterdir()) and not args.force:
        raise SystemExit(f"{out} already exists; pass --force to rebuild it")

    from ingestion.chunking import chunk_document
    from ingestion.extract_text import extract_pdf_pages
    from ingestion.ingest_lecture import compose_document

    real = vector_store.get_collection().get(include=["documents", "metadatas"])
    templates = file_templates(real["metadatas"])
    real_counts = Counter(m["source_filename"] for m, d in zip(real["metadatas"], real["documents"]) if m["content_type"] == "text")

    out.mkdir(parents=True, exist_ok=True)
    store = chromadb.PersistentClient(path=str(out))
    try:
        store.delete_collection(vector_store.COLLECTION_NAME)
    except Exception:
        pass
    collection = store.create_collection(
        name=vector_store.COLLECTION_NAME,
        embedding_function=vector_store.get_embedding_function(),
        configuration={"hnsw": {"space": "cosine"}},
    )

    ids, docs, metas = [], [], []
    for pdf in sorted((ROOT / "data" / "lectures").rglob("*.pdf")):
        template = templates.get(pdf.name)
        if template is None:
            print(f"  skip {pdf.name}: not in the real collection")
            continue
        chunks, _ = chunk_document(compose_document(extract_pdf_pages(str(pdf))), args.chunk_size, args.chunk_overlap)
        i, d, m = text_records(pdf.name, chunks, template)
        ids += i; docs += d; metas += m
        print(f"  {pdf.name}: {len(chunks)} text chunks (real collection: {real_counts[pdf.name]})", flush=True)

    # Figure descriptions are independent of the chunk size: copy them as they are.
    for rid, doc, meta in zip(real["ids"], real["documents"], real["metadatas"]):
        if meta["content_type"] != vector_store.CONTENT_TYPE_TEXT:
            ids.append(rid); docs.append(doc); metas.append(meta)

    for start in range(0, len(ids), 64):
        collection.upsert(ids=ids[start:start + 64], documents=docs[start:start + 64], metadatas=metas[start:start + 64])
    print(f"\nwrote {len(ids)} records to {out}  (chunk size {args.chunk_size}, overlap {args.chunk_overlap})")
    print(f"text chunks: {sum(1 for m in metas if m['content_type'] == 'text')} (real collection: {sum(real_counts.values())})")


if __name__ == "__main__":
    main()
