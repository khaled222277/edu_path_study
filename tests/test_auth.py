"""Session-cookie authentication: valid, expired, tampered and missing tokens."""

from datetime import UTC, datetime, timedelta

import jwt

from app.auth.jwt import create_access_token, decode_access_token
from app.config import settings

from .conftest import login


def _token(user_id, secret=None, exp_delta=timedelta(hours=1), **extra):
    now = datetime.now(UTC)
    payload = {"sub": str(user_id), "iat": now, **extra}
    if exp_delta is not None:
        payload["exp"] = now + exp_delta
    return jwt.encode(payload, secret or settings.jwt_secret, algorithm="HS256")


def _me(client, token=None):
    client.cookies.clear()
    if token is not None:
        client.cookies.set(settings.cookie_name, token)
    return client.get("/auth/me")


def test_valid_cookie_returns_current_user(client, world):
    response = login(client, world["alice"]).get("/auth/me")
    assert response.status_code == 200
    assert response.json()["email"] == "a@example.com"


def test_missing_cookie_is_401(client, world):
    assert _me(client).status_code == 401


def test_expired_jwt_is_401(client, world):
    token = _token(world["alice"].id, exp_delta=timedelta(seconds=-5))
    assert _me(client, token).status_code == 401


def test_tampered_signature_is_401(client, world):
    token = create_access_token(world["alice"].id)
    head, body, sig = token.split(".")
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert _me(client, f"{head}.{body}.{flipped}").status_code == 401


def test_tampered_payload_is_401(client, world):
    """Swap the subject to Bob's id while keeping Alice's signature."""
    good = create_access_token(world["alice"].id).split(".")
    forged = _token(world["bob"].id).split(".")
    assert _me(client, f"{good[0]}.{forged[1]}.{good[2]}").status_code == 401


def test_token_signed_with_another_secret_is_401(client, world):
    token = _token(world["alice"].id, secret="some-other-secret-" + "z" * 32)
    assert _me(client, token).status_code == 401


def test_unsigned_alg_none_token_is_401(client, world):
    token = jwt.encode(
        {"sub": str(world["alice"].id), "exp": datetime.now(UTC) + timedelta(hours=1)},
        key=None, algorithm="none",
    )
    assert _me(client, token).status_code == 401


def test_token_without_expiry_is_rejected(client, world):
    assert decode_access_token(_token(world["alice"].id, exp_delta=None)) is None
    assert _me(client, _token(world["alice"].id, exp_delta=None)).status_code == 401


def test_garbage_cookie_is_401(client, world):
    assert _me(client, "not-a-jwt").status_code == 401


def test_token_for_deleted_user_is_401(client, world):
    assert _me(client, create_access_token(99999)).status_code == 401


def test_protected_routes_require_a_cookie(client, world):
    client.cookies.clear()
    for method, path in [
        ("get", "/chat/sessions"), ("get", "/quiz/history"),
        ("get", "/dashboard/summary"), ("get", "/subjects"),
    ]:
        assert getattr(client, method)(path).status_code == 401, path
