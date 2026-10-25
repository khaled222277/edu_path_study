"""The chunk-size experiment must never write to the real store."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_chunk_experiment", ROOT / "scripts" / "build_chunk_experiment.py")
bce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bce)


@pytest.fixture()
def real_store(tmp_path, monkeypatch):
    real = tmp_path / "chroma_db"
    real.mkdir()
    monkeypatch.setattr(bce.vector_store, "CHROMA_PATH", real)
    return real


def test_the_real_store_is_refused(real_store):
    with pytest.raises(SystemExit):
        bce.assert_safe_target(real_store)


def test_a_path_inside_the_real_store_is_refused(real_store):
    with pytest.raises(SystemExit):
        bce.assert_safe_target(real_store / "experiment")


def test_a_path_that_contains_the_real_store_is_refused(real_store):
    with pytest.raises(SystemExit):
        bce.assert_safe_target(real_store.parent)


def test_a_dotdot_route_to_the_real_store_is_refused(real_store):
    with pytest.raises(SystemExit):
        bce.assert_safe_target(real_store.parent / "elsewhere" / ".." / "chroma_db")


def test_a_sibling_directory_is_allowed(real_store):
    bce.assert_safe_target(real_store.parent / "chroma_experiments" / "c400_o50")


def test_the_default_target_is_never_the_real_store():
    assert bce.default_out(400, 50).name == "c400_o50"
    assert "chroma_experiments" in str(bce.default_out(400, 50))


def test_file_templates_drop_the_per_chunk_fields():
    metas = [
        {"source_filename": "a.pdf", "subject_id": 7, "lecture_id": 1, "page_number": 3, "content_type": "text"},
        {"source_filename": "a.pdf", "subject_id": 7, "lecture_id": 1, "page_number": 9, "content_type": "image_description", "image_path": "1/p.png"},
        {"source_filename": "b.pdf", "subject_id": 6, "lecture_id": 2, "page_number": 1, "content_type": "text"},
    ]
    t = bce.file_templates(metas)
    assert t["a.pdf"] == {"source_filename": "a.pdf", "subject_id": 7, "lecture_id": 1}
    assert set(t) == {"a.pdf", "b.pdf"}


def test_text_records_carry_the_lectures_metadata_and_the_new_pages():
    chunks = [SimpleNamespace(index=0, text="one", page_number=2), SimpleNamespace(index=1, text="two", page_number=3)]
    ids, docs, metas = bce.text_records("a.pdf", chunks, {"source_filename": "a.pdf", "subject_id": 7})
    assert ids == ["a.pdf:text:0", "a.pdf:text:1"] and docs == ["one", "two"]
    assert metas[1] == {"source_filename": "a.pdf", "subject_id": 7, "page_number": 3, "content_type": "text"}
