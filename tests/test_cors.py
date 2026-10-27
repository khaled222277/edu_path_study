"""Browser calls the frontend makes must pass CORS preflight (found by driving real Chrome)."""

import pytest

from app.config import settings


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_every_method_the_frontend_uses_is_allowed_from_the_frontend_origin(client, method):
    r = client.options(
        "/chat/messages/1/feedback",
        headers={"Origin": settings.frontend_url, "Access-Control-Request-Method": method,
                 "Access-Control-Request-Headers": "content-type"},
    )
    assert r.status_code == 200, r.text
    assert method in r.headers["access-control-allow-methods"]
    assert r.headers["access-control-allow-credentials"] == "true"


def test_another_origin_is_not_allowed(client):
    r = client.options("/chat/messages/1/feedback",
                       headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "PUT"})
    assert "access-control-allow-origin" not in r.headers
