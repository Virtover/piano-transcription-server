import sqlite3
import time

import pytest
import requests
from fastapi import HTTPException

from app import auth


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


def token_info(**overrides):
    info = {
        "aud": "test-client-id",
        "sub": "user-1",
        "email": "user@example.com",
        "email_verified": "true",
        "exp": str(int(time.time()) + 3600),
    }
    info.update(overrides)
    return {key: value for key, value in info.items() if value is not None}


@pytest.fixture
def google(api_context, monkeypatch):
    responses = {}
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        response = responses[url]
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(auth.requests, "get", fake_get)
    return responses, calls


def verify_status(token="token"):
    with pytest.raises(HTTPException) as error:
        auth.verify_google_access_token(token)
    return error.value.status_code, error.value.detail


def test_valid_token_is_cached(google):
    responses, calls = google
    responses[auth.TOKENINFO_URL] = FakeResponse(200, token_info())

    assert auth.verify_google_access_token("token")["sub"] == "user-1"
    assert auth.verify_google_access_token("token")["sub"] == "user-1"
    assert calls == [auth.TOKENINFO_URL]


def test_token_for_another_app_is_rejected(google):
    responses, _ = google
    responses[auth.TOKENINFO_URL] = FakeResponse(200, token_info(aud="someone-else"))

    assert verify_status() == (401, "Google authentication token was not issued for this app")


def test_expired_token_is_rejected(google):
    responses, _ = google
    responses[auth.TOKENINFO_URL] = FakeResponse(200, token_info(exp=str(int(time.time()) - 1)))

    assert verify_status() == (401, "Google authentication token has expired")


@pytest.mark.parametrize("exp", [None, "soon"])
def test_token_without_valid_expiry_is_rejected(google, exp):
    responses, _ = google
    responses[auth.TOKENINFO_URL] = FakeResponse(200, token_info(exp=exp))

    assert verify_status()[0] == 401


def test_unverified_email_is_rejected(google):
    responses, _ = google
    responses[auth.TOKENINFO_URL] = FakeResponse(200, token_info(email_verified="false"))

    assert verify_status() == (401, "Google authentication email is not verified")


def test_missing_subject_falls_back_to_userinfo(google):
    responses, calls = google
    responses[auth.TOKENINFO_URL] = FakeResponse(200, token_info(sub=None, email=None, email_verified=None))
    responses[auth.USERINFO_URL] = FakeResponse(
        200, {"sub": "from-userinfo", "email": "info@example.com", "email_verified": True}
    )

    info = auth.verify_google_access_token("token")

    assert (info["sub"], info["email"]) == ("from-userinfo", "info@example.com")
    assert calls == [auth.TOKENINFO_URL, auth.USERINFO_URL]


def test_userinfo_without_subject_is_rejected(google):
    responses, _ = google
    responses[auth.TOKENINFO_URL] = FakeResponse(200, token_info(sub=None))
    responses[auth.USERINFO_URL] = FakeResponse(200, {})

    assert verify_status() == (401, "Google authentication token has no subject")


@pytest.mark.parametrize(
    "response",
    [FakeResponse(500), requests.ConnectionError("offline")],
)
def test_google_outage_returns_503(google, response):
    responses, _ = google
    responses[auth.TOKENINFO_URL] = response

    assert verify_status()[0] == 503


def test_authentication_requires_configured_client_ids(google, api_context):
    _, settings, _ = api_context
    settings.google_oauth_client_ids = [" "]

    assert verify_status() == (503, "Google authentication is not configured")


def test_current_user_normalizes_email(google, api_context):
    client, settings, _ = api_context
    responses, _ = google
    settings.billing_provider = "google_play"
    settings.free_minutes = 1
    responses[auth.TOKENINFO_URL] = FakeResponse(200, token_info(email=" User@Example.COM "))

    response = client.get("/api/billing/balance", headers={"Authorization": "Bearer token"})

    assert response.status_code == 200
    assert response.json()["user_id"] == "user-1"
    with sqlite3.connect(settings.billing_database_path) as connection:
        assert connection.execute("SELECT email FROM users").fetchall() == [("user@example.com",)]


def test_token_without_email_is_rejected(google, api_context):
    client, settings, _ = api_context
    responses, _ = google
    settings.billing_provider = "google_play"
    responses[auth.TOKENINFO_URL] = FakeResponse(200, token_info(email=None))
    responses[auth.USERINFO_URL] = FakeResponse(200, {})

    response = client.get("/api/billing/balance", headers={"Authorization": "Bearer token"})

    assert response.status_code == 401
