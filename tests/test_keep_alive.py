"""Keeping the chat model resident: keep_alive on every call, and the warm-up."""

import pytest

from app import main
from app.config import Settings, settings
from app.services import llm as _llm
from app.services import quiz_service, retrieval, vector_store

from .conftest import FakeOllama, login
from .test_error_handling import chat_session  # noqa: F401  (fixture)

# The real functions, captured at import: the autouse offline guard replaces the
# module attributes with stubs for the duration of every test.
_real_warm_chat_model = _llm.warm_chat_model
_real_keep_alive = _llm.keep_alive


# --- the setting ------------------------------------------------------------


def test_default_is_thirty_minutes():
    assert Settings(_env_file=None).ollama_keep_alive == "30m"


@pytest.mark.parametrize("value", ["30m", "1h30m", "90s", "120", "-1", "0", "1.5h"])
def test_valid_keep_alive_values(value):
    assert Settings(_env_file=None, ollama_keep_alive=value).ollama_keep_alive == value


@pytest.mark.parametrize("value", ["", "abc", "m30", "--1", "30 m", "30x"])
def test_invalid_keep_alive_values_are_rejected(value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, ollama_keep_alive=value)


@pytest.mark.parametrize(
    "configured, sent",
    [("30m", "30m"), ("1h30m", "1h30m"), ("-1", -1.0), ("120", 120.0), ("0", 0.0)],
)
def test_numbers_are_sent_as_numbers_durations_as_strings(monkeypatch, configured, sent):
    """Ollama rejects the *string* "-1" (no unit) but accepts the number -1."""
    monkeypatch.setattr(settings, "ollama_keep_alive", configured)
    assert _real_keep_alive() == sent
    assert type(_real_keep_alive()) is type(sent)


# --- every call carries it --------------------------------------------------


def test_chat_call_passes_keep_alive(client, world, chat_session, fake_ollama, monkeypatch):
    monkeypatch.setattr("app.controllers.chat_controller.retrieve", lambda q, s: [])
    monkeypatch.setattr("app.controllers.chat_controller.keep_alive", lambda: "30m")
    fake = fake_ollama(default={"message": {"content": "hi"}})
    r = login(client, world["alice"]).post(
        f"/chat/sessions/{chat_session.id}/messages", json={"content": "hello"}
    )
    assert r.status_code == 200
    assert fake.kwargs[0]["keep_alive"] == "30m"


def test_quiz_call_passes_keep_alive(monkeypatch):
    fake = FakeOllama(default={"response": "[]"})
    monkeypatch.setattr(quiz_service, "get_client", lambda: fake)
    monkeypatch.setattr(quiz_service, "keep_alive", lambda: "30m")
    quiz_service._ask_model("prompt", "m")
    assert fake.kwargs[0]["keep_alive"] == "30m"


def test_chat_and_quiz_use_the_same_keep_alive_and_no_context_override(
    client, world, chat_session, fake_ollama, monkeypatch
):
    """No reload churn: same expiry from both paths, and no num_ctx on either.

    Ollama reloads a resident model when num_ctx changes between requests, and
    resets its expiry to whatever the latest request asked for.
    """
    from app.controllers import chat_controller

    monkeypatch.setattr(settings, "ollama_keep_alive", "45m")
    monkeypatch.setattr(chat_controller, "keep_alive", _real_keep_alive)
    monkeypatch.setattr(quiz_service, "keep_alive", _real_keep_alive)
    monkeypatch.setattr(chat_controller, "retrieve", lambda q, s: [])
    fake = fake_ollama(default={"message": {"content": "x"}, "response": "[]"})

    login(client, world["alice"]).post(
        f"/chat/sessions/{chat_session.id}/messages", json={"content": "hello"}
    )
    quiz_service._ask_model("p", "m")

    chat_call, quiz_call = fake.kwargs
    assert chat_call["keep_alive"] == quiz_call["keep_alive"] == "45m"
    assert "num_ctx" not in chat_call["options"]
    assert "num_ctx" not in quiz_call["options"]


# --- the warm-up request ----------------------------------------------------


def test_warm_chat_model_sends_a_one_token_request(monkeypatch):
    fake = FakeOllama(default={"message": {"content": "o"}})
    monkeypatch.setattr(_llm, "get_client", lambda timeout=None: fake)
    monkeypatch.setattr(settings, "ollama_keep_alive", "30m")
    monkeypatch.setattr(settings, "chat_model", "llama3.1:8b")

    _real_warm_chat_model()

    (call,) = fake.kwargs
    assert call["model"] == "llama3.1:8b"
    assert call["options"] == {"num_predict": 1}  # no num_ctx: would force a reload
    assert call["keep_alive"] == "30m"


# --- warm-up orchestration --------------------------------------------------


def _steps(monkeypatch, fail=()):
    ran = []

    def make(name):
        def step():
            ran.append(name)
            if name in fail:
                raise OSError(f"{name} unavailable")

        return step

    monkeypatch.setattr(vector_store, "warm_up", make("embedder"))
    monkeypatch.setattr(retrieval, "get_ranker", make("reranker"))
    monkeypatch.setattr(main.llm, "warm_chat_model", make("chat"))
    return ran


@pytest.mark.parametrize("failing", ["embedder", "reranker", "chat"])
def test_one_failed_step_does_not_stop_the_others(monkeypatch, caplog, failing):
    ran = _steps(monkeypatch, fail={failing})
    with caplog.at_level("WARNING"):
        main.warm_up_models()  # must not raise
    assert ran == ["embedder", "reranker", "chat"]
    assert caplog.text.count("warm-up failed") == 1


def test_ollama_down_at_boot_is_a_warning_not_a_crash(monkeypatch, caplog):
    _steps(monkeypatch)

    def refused():
        raise ConnectionError("refused")

    monkeypatch.setattr(main.llm, "warm_chat_model", refused)
    with caplog.at_level("WARNING"):
        main.warm_up_models()
    assert "chat model warm-up failed" in caplog.text


def test_differing_quiz_model_is_noted(monkeypatch, caplog):
    _steps(monkeypatch)
    monkeypatch.setattr(settings, "quiz_model", "other:7b")
    with caplog.at_level("INFO"):
        main.warm_up_models()
    assert "QUIZ_MODEL" in caplog.text and "other:7b" in caplog.text


def test_same_quiz_and_chat_model_is_not_noted(monkeypatch, caplog):
    _steps(monkeypatch)
    monkeypatch.setattr(settings, "quiz_model", settings.chat_model)
    with caplog.at_level("INFO"):
        main.warm_up_models()
    assert "QUIZ_MODEL" not in caplog.text
