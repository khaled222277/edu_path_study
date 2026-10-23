"""EMBED_DEVICE: validated at startup, and a missing accelerator degrades to cpu."""

import builtins

import pytest
import torch

from app.config import Settings, settings
from app.services import vector_store


@pytest.fixture(autouse=True)
def forget_warnings():
    vector_store._warned_devices.clear()
    yield
    vector_store._warned_devices.clear()


# --- the setting ---------------------------------------------------------------


def test_default_is_cpu():
    assert Settings(_env_file=None).embed_device == "cpu"


@pytest.mark.parametrize("value", ["cpu", "mps", "cuda", "cuda:0", "cuda:1", "cuda:12"])
def test_valid_devices(value):
    assert Settings(_env_file=None, embed_device=value).embed_device == value


@pytest.mark.parametrize("value", ["", "gpu", "tpu", "CUDA", "cuda:", "cuda:x", "cuda:-1", "cuda 0", "cpu:0"])
def test_invalid_devices_are_rejected_at_startup(value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, embed_device=value)


def test_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("EMBED_DEVICE", "cuda:1")
    assert Settings(_env_file=None).embed_device == "cuda:1"


# --- falling back ---------------------------------------------------------------


def test_cpu_never_touches_torch(monkeypatch):
    real_import = builtins.__import__

    def no_torch(name, *a, **k):
        if name == "torch":
            raise AssertionError("cpu must not import torch")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_torch)
    assert vector_store.resolve_device("cpu") == "cpu"


def test_available_cuda_is_used(monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "device_count", lambda: 2)
    assert vector_store.resolve_device("cuda") == "cuda"
    assert vector_store.resolve_device("cuda:1") == "cuda:1"


def test_unavailable_cuda_falls_back_to_cpu_with_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with caplog.at_level("WARNING"):
        assert vector_store.resolve_device("cuda") == "cpu"
    assert "EMBED_DEVICE=cuda" in caplog.text and "using cpu" in caplog.text


def test_a_cuda_index_that_does_not_exist_falls_back(monkeypatch, caplog):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "device_count", lambda: 1)
    with caplog.at_level("WARNING"):
        assert vector_store.resolve_device("cuda:3") == "cpu"
    assert "1 CUDA device" in caplog.text
    assert vector_store.resolve_device("cuda:0") == "cuda:0"


def test_unavailable_mps_falls_back_to_cpu(monkeypatch, caplog):
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
    with caplog.at_level("WARNING"):
        assert vector_store.resolve_device("mps") == "cpu"
    assert "MPS is not available" in caplog.text


def test_missing_torch_falls_back_to_cpu(monkeypatch, caplog):
    real_import = builtins.__import__

    def broken(name, *a, **k):
        if name == "torch":
            raise ImportError("no torch")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", broken)
    with caplog.at_level("WARNING"):
        assert vector_store.resolve_device("cuda") == "cpu"
    assert "torch is not installed" in caplog.text


def test_the_fallback_warning_is_logged_once_not_per_request(monkeypatch, caplog):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with caplog.at_level("WARNING"):
        for _ in range(5):
            vector_store.resolve_device("cuda")
    assert caplog.text.count("EMBED_DEVICE=cuda") == 1


# --- it reaches the embedder ----------------------------------------------------


@pytest.fixture()
def built(monkeypatch):
    seen = []
    monkeypatch.setattr(
        vector_store.embedding_functions,
        "SentenceTransformerEmbeddingFunction",
        lambda **kw: seen.append(kw["device"]) or object(),
    )
    return seen


def test_embedder_uses_the_configured_device(monkeypatch, built):
    monkeypatch.setattr(settings, "embed_device", "cuda")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    vector_store.get_embedding_function()
    assert built == ["cuda"]


def test_embedder_falls_back_when_the_configured_device_is_absent(monkeypatch, built):
    monkeypatch.setattr(settings, "embed_device", "cuda")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    vector_store.get_embedding_function()
    assert built == ["cpu"]


def test_requests_for_an_absent_gpu_share_the_cpu_embedder(monkeypatch, built):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    a = vector_store.get_embedding_function("cuda")
    b = vector_store.get_embedding_function("cpu")
    c = vector_store.get_embedding_function()
    assert a is b is c and built == ["cpu"]  # one ~2 GB load, not two


def test_an_explicit_device_overrides_the_setting(monkeypatch, built):
    """The ingestion CLI's --device must still win over EMBED_DEVICE."""
    monkeypatch.setattr(settings, "embed_device", "cuda")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    vector_store.get_embedding_function("cpu")
    assert built == ["cpu"]


@pytest.mark.parametrize("gpu_present, expected", [(True, "cuda"), (False, "cpu")])
def test_get_collection_follows_the_setting(monkeypatch, tmp_path, gpu_present, expected):
    """With a GPU the setting is honoured; without one it degrades. Both differ from
    a hard-coded "cpu", which is what makes this test able to fail."""
    from .test_vector_store import TinyEF

    asked = []
    monkeypatch.setattr(vector_store, "CHROMA_PATH", tmp_path)
    monkeypatch.setattr(settings, "embed_device", "cuda")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: gpu_present)
    monkeypatch.setattr(torch.cuda, "device_count", lambda: 1)
    monkeypatch.setattr(
        vector_store, "get_embedding_function",
        lambda device=None: asked.append(device) or TinyEF(),
    )
    vector_store.get_collection()
    assert asked == [expected]
