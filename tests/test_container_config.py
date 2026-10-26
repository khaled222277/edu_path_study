"""The container setup keeps the properties that were checked by hand (see README, "Backend in Docker")."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
API = COMPOSE["services"]["api"]


def test_the_model_cache_is_on_a_volume_the_app_user_can_write():
    assert re.search(r"^ENV HF_HOME=/models/huggingface$", DOCKERFILE, re.M)
    assert "/models/huggingface" in DOCKERFILE and re.search(r"chown -R appuser /app /models", DOCKERFILE)
    assert "edupath_models:/models" in API["volumes"]
    assert "edupath_models" in COMPOSE["volumes"]


def test_the_cache_env_comes_after_the_pip_layers_so_changing_it_does_not_rebuild_torch():
    assert DOCKERFILE.index("ENV HF_HOME") > DOCKERFILE.index("pip install -r requirements.txt")


def test_the_container_reaches_the_host_ollama_and_the_compose_postgres():
    assert API["environment"]["OLLAMA_BASE_URL"].startswith("${OLLAMA_BASE_URL_DOCKER:-http://host.docker.internal:11434")
    assert "host.docker.internal:host-gateway" in API["extra_hosts"]
    assert "@postgres:5432/" in API["environment"]["DATABASE_URL"], ".env's localhost would be the container itself"
    assert API["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_the_data_directory_is_mounted_not_baked_in_and_the_app_runs_as_non_root():
    assert any(v.endswith(":/app/data") for v in API["volumes"])
    assert re.search(r"^USER appuser$", DOCKERFILE, re.M)


def test_the_base_image_matches_ci_and_the_container_never_downloads_models():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    version = re.search(r"^FROM python:(\d+\.\d+)-slim", DOCKERFILE, re.M).group(1)
    assert f'python-version: "{version}"' in ci, "the image and CI must run the same Python"
    assert API["environment"]["HF_HUB_OFFLINE"] == "${HF_HUB_OFFLINE:-1}"


def test_the_model_volume_is_owned_by_compose_not_external():
    assert COMPOSE["volumes"]["edupath_models"] in (None, {}), "external: true would need a manual `docker volume create`"


def test_the_api_service_stays_out_of_a_plain_compose_up():
    assert API["profiles"] == ["app"]
