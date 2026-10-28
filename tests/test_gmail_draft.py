"""Tests for Gmail OAuth, draft MIME creation, and draft endpoints."""

from __future__ import annotations

import base64
import email
import json
from email import policy
from unittest.mock import AsyncMock, patch

import asyncio
import httpx
import pytest
from fastapi.testclient import TestClient

from backend.email_dispatch import build_email_message
from backend.evidence import EVIDENCE_STORE
from backend.gmail_draft import (
    GMAIL_SESSIONS,
    GMAIL_STATES,
    GmailOAuthError,
    GoogleOAuthConfig,
    SESSION_COOKIE_NAME,
    build_draft_url,
    build_raw_message,
    create_gmail_draft,
)
from backend.main import app

client = TestClient(app)

GOOGLE_ENV = (
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REDIRECT_URI",
)


@pytest.fixture(autouse=True)
def _clear_gmail_state(monkeypatch):
    GMAIL_SESSIONS.clear()
    GMAIL_STATES.clear()
    EVIDENCE_STORE.clear()
    for name in GOOGLE_ENV:
        monkeypatch.delenv(name, raising=False)
    client.cookies.clear()
    yield
    GMAIL_SESSIONS.clear()
    GMAIL_STATES.clear()
    EVIDENCE_STORE.clear()
    client.cookies.clear()


def _config() -> GoogleOAuthConfig:
    return GoogleOAuthConfig(
        client_id="client-id",
        client_secret="client-secret",
        redirect_uri="http://testserver/auth/google/callback",
    )


def _session():
    return GMAIL_SESSIONS.create(
        {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        },
        "tester@gmail.com",
    )


def _draft_record():
    return EVIDENCE_STORE.create(
        b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9",
        draft={
            "to": "sanitation@city.gov",
            "cc": [],
            "subject": "Waste clearance",
            "body": "Please clear the refuse.",
        },
    )


# ============================================================================
# OAuth/config helpers
# ============================================================================

def test_config_from_env_and_auth_url(monkeypatch):
    assert GoogleOAuthConfig.from_env() is None
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://example.test/auth/google/callback")

    config = GoogleOAuthConfig.from_env()
    assert config is not None
    url = config.authorization_url("state-123", config.redirect_uri)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "gmail.compose" in url
    assert "prompt=select_account+consent" in url
    assert "state=state-123" in url


def test_draft_url_uses_authuser():
    url = build_draft_url("a.b+test@gmail.com")
    assert url.startswith("https://mail.google.com/mail/?authuser=")
    assert "a.b%2Btest%40gmail.com" in url
    assert url.endswith("#drafts")


def test_build_raw_message_contains_jpeg():
    message = build_email_message(
        sender="tester@gmail.com",
        to="sanitation@city.gov",
        cc=[],
        subject="Waste clearance",
        body="Please clear the refuse.",
        image_bytes=b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9",
        filename="evidence.jpg",
    )
    raw = build_raw_message(message)
    padded = raw + "=" * (-len(raw) % 4)
    decoded = email.message_from_bytes(
        base64.urlsafe_b64decode(padded), policy=policy.default
    )
    attachments = list(decoded.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_content_type() == "image/jpeg"
    assert attachments[0].get_payload(decode=True) == b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


def test_create_gmail_draft_posts_raw_message():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "draft-1", "message": {"id": "msg-1"}})

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await create_gmail_draft("access-token", "cmF3", client=http)

    result = asyncio.run(_run())
    assert result["id"] == "draft-1"
    assert captured["auth"] == "Bearer access-token"
    assert captured["body"]["message"]["raw"] == "cmF3"


def test_create_gmail_draft_raises_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(GmailOAuthError):
                await create_gmail_draft("expired", "cmF3", client=http)

    asyncio.run(_run())


# ============================================================================
# Endpoints
# ============================================================================

def test_google_status_unconfigured(monkeypatch):
    response = client.get("/auth/google/status")
    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "connected": False,
        "email": None,
    }


def test_google_start_returns_503_when_unconfigured():
    response = client.get("/auth/google/start")
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_google_start_redirects_to_google():
    with patch("backend.main.GoogleOAuthConfig.from_env", return_value=_config()):
        response = client.get("/auth/google/start?popup=1", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://accounts.google.com/")
    assert "gmail.compose" in location


def test_gmail_draft_requires_connection():
    record = _draft_record()
    with patch("backend.main.GoogleOAuthConfig.from_env", return_value=_config()):
        response = client.post(f"/api/reports/{record.token}/gmail-draft", json={})
    assert response.status_code == 401
    assert "Connect a Gmail account" in response.json()["detail"]


def test_gmail_draft_creates_draft_with_cookie():
    record = _draft_record()
    session = _session()
    client.cookies.set(SESSION_COOKIE_NAME, session.session_id)

    with patch("backend.main.GoogleOAuthConfig.from_env", return_value=_config()), \
         patch(
             "backend.main.create_gmail_draft",
             new=AsyncMock(return_value={"id": "draft-9", "message": {"id": "msg-9"}}),
         ) as mock_create:
        response = client.post(
            f"/api/reports/{record.token}/gmail-draft",
            json={"body": "Edited body", "subject": "Edited subject"},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["draft_id"] == "draft-9"
    assert payload["account_email"] == "tester@gmail.com"
    assert "authuser=tester%40gmail.com" in payload["draft_url"]
    assert mock_create.await_count == 1


def test_gmail_callback_stores_session_and_notifies_popup():
    state = GMAIL_STATES.create(True, "http://testserver/auth/google/callback")
    with patch("backend.main.GoogleOAuthConfig.from_env", return_value=_config()), \
         patch(
             "backend.main.exchange_code",
             new=AsyncMock(return_value={
                 "access_token": "access",
                 "refresh_token": "refresh",
                 "expires_in": 3600,
             }),
         ), \
         patch(
             "backend.main.fetch_userinfo",
             new=AsyncMock(return_value={"email": "tester@gmail.com"}),
         ):
        response = client.get(f"/auth/google/callback?code=abc&state={state}")

    assert response.status_code == 200
    assert "gmail-connected" in response.text
    assert SESSION_COOKIE_NAME in response.cookies
    assert len(GMAIL_SESSIONS._sessions) == 1
