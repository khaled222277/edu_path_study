"""Rate-limit keys: the socket peer by default, X-Forwarded-For only behind a trusted proxy."""

import pytest
from starlette.requests import Request

from app.config import settings
from app.rate_limit import client_ip, user_or_ip_key


def request(peer="10.0.0.5", xff=None, cookie=None):
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    if cookie is not None:
        headers.append((b"cookie", f"{settings.cookie_name}={cookie}".encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 1234), "query_string": b""})


@pytest.fixture()
def trusted(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy", True)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)


def test_default_off_the_header_is_ignored(monkeypatch):
    assert settings.trusted_proxy is False
    assert client_ip(request(xff="203.0.113.9")) == "10.0.0.5"
    assert user_or_ip_key(request(xff="203.0.113.9")) == "10.0.0.5"


def test_default_off_a_spoofed_header_cannot_change_the_key():
    assert user_or_ip_key(request(xff="1.1.1.1")) == user_or_ip_key(request(xff="2.2.2.2"))


def test_on_the_address_the_proxy_appended_is_used(trusted):
    assert client_ip(request(xff="203.0.113.9")) == "203.0.113.9"


def test_on_a_client_supplied_prefix_is_not_trusted(trusted):
    # the client sent "6.6.6.6"; our proxy appended the real peer 203.0.113.9
    assert client_ip(request(xff="6.6.6.6, 203.0.113.9")) == "203.0.113.9"


def test_on_two_proxies_the_second_from_the_right_is_the_client(trusted, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 2)
    assert client_ip(request(xff="6.6.6.6, 203.0.113.9, 10.1.1.1")) == "203.0.113.9"


def test_on_but_no_header_or_too_short_falls_back_to_the_peer(trusted, monkeypatch):
    assert client_ip(request()) == "10.0.0.5"
    monkeypatch.setattr(settings, "trusted_proxy_hops", 2)
    assert client_ip(request(xff="203.0.113.9")) == "10.0.0.5"


def test_on_a_malformed_entry_falls_back_to_the_peer(trusted):
    assert client_ip(request(xff="not-an-ip")) == "10.0.0.5"
    assert client_ip(request(xff="<script>")) == "10.0.0.5"


def test_on_ipv6_is_normalised(trusted):
    assert client_ip(request(xff="2001:DB8::1")) == "2001:db8::1"


def test_two_students_behind_the_proxy_get_separate_counters_only_when_trusted(monkeypatch):
    a, b = request(xff="203.0.113.1"), request(xff="203.0.113.2")
    assert user_or_ip_key(a) == user_or_ip_key(b), "off: everyone is the proxy"
    monkeypatch.setattr(settings, "trusted_proxy", True)
    assert user_or_ip_key(a) != user_or_ip_key(b)


def test_a_signed_in_user_is_keyed_by_user_not_address(trusted):
    from app.auth.jwt import create_access_token

    assert user_or_ip_key(request(xff="203.0.113.9", cookie=create_access_token(7))) == "user:7"
