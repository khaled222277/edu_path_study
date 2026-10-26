"""The CI workflow runs where it should. (GitHub itself is the only real check; this guards the config.)"""

from pathlib import Path

import pytest

import yaml  # pulled in by chromadb/uvicorn; a missing one should fail, not skip

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def ci():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_runs_on_pushes_to_any_branch_pull_requests_and_by_hand(ci):
    triggers = ci.get("on", ci.get(True))  # YAML 1.1 reads a bare `on` as True
    assert set(triggers) == {"push", "pull_request", "workflow_dispatch"}
    assert not (triggers["push"] or {}).get("branches"), "a branch filter would skip feature branches"


def test_python_job_uses_the_project_python_and_both_requirement_files(ci):
    steps = ci["jobs"]["test"]["steps"]
    setup = next(s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python"))
    assert setup["with"]["python-version"] == "3.13" and setup["with"]["cache"] == "pip"
    text = "\n".join(s.get("run", "") for s in steps)
    assert "requirements.txt" in text and "requirements-dev.txt" in text and "pytest" in text


def test_frontend_job_installs_tests_and_builds_with_an_npm_cache(ci):
    steps = ci["jobs"]["frontend"]["steps"]
    setup = next(s for s in steps if str(s.get("uses", "")).startswith("actions/setup-node"))
    assert setup["with"]["cache"] == "npm"
    runs = [s.get("run") for s in steps]
    assert runs.index("npm ci") < runs.index("npm test") < runs.index("npm run build")


def test_no_job_needs_a_secret_or_a_service(ci):
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text
    assert not any("services" in job for job in ci["jobs"].values())


def test_actions_are_on_the_node24_majors(ci):
    """checkout / setup-python / setup-node v7 declare `using: node24` (checked against their action.yml)."""
    uses = [s["uses"] for job in ci["jobs"].values() for s in job["steps"] if "uses" in s]
    assert uses and all(u.endswith("@v7") for u in uses), uses
