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

from backend.authority_router import CENTRAL_GRIEVANCE_EMAIL_DEFAULT
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
    ensure_active_session,
    refresh_access_token,
)
from backend.main import app

client = TestClient(app)

GOOGLE_ENV = (
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REDIRECT_URI",
    "CENTRAL_GRIEVANCE_EMAIL",
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


def _draft_record(to: str = "sanitation@city.gov", subject: str = "Waste clearance"):
    return EVIDENCE_STORE.create(
        b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9",
        draft={
            "to": to,
            "cc": [],
            "subject": subject,
            "body": "Please clear the refuse.",
        },
    )


# ============================================================================
# OAuth & MIME unit tests
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
    assert "client_id=id" in url
    assert "gmail.compose" in url
    assert "access_type=offline" in url
    assert "prompt=select_account+consent" in url
    assert "state=state-123" in url


def test_draft_url_uses_authuser_and_compose():
    url = build_draft_url("a.b+test@gmail.com", draft_id="draft-123")
    assert url.startswith("https://mail.google.com/mail/u/?authuser=")
    assert "a.b%2Btest%40gmail.com" in url
    assert "#drafts?compose=draft-123" in url


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


def test_refresh_access_token_raises_on_invalid_grant():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token revoked"})

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(GmailOAuthError) as exc_info:
                await refresh_access_token(_config(), "bad-refresh", client=http)
            assert "invalid_grant" in str(exc_info.value)

    asyncio.run(_run())


# ============================================================================
# Endpoint Tests
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
    assert "Gmail draft integration is not configured. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REDIRECT_URI." in response.json()["detail"]


def test_google_start_returns_400_when_report_id_missing():
    with patch("backend.main.GoogleOAuthConfig.from_env", return_value=_config()):
        response = client.get("/auth/google/start")
    assert response.status_code == 400
    assert "report_id is required" in response.json()["detail"]


def test_google_start_returns_410_when_report_expired():
    with patch("backend.main.GoogleOAuthConfig.from_env", return_value=_config()):
        response = client.get("/auth/google/start?report_id=expired-or-missing-id")
    assert response.status_code == 410
    assert "report expired — please submit again" in response.json()["detail"]


def test_google_start_redirects_to_google():
    record = _draft_record()
    with patch("backend.main.GoogleOAuthConfig.from_env", return_value=_config()):
        response = client.get(f"/auth/google/start?report_id={record.token}", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=client-id" in location
    assert "gmail.compose" in location
    assert "access_type=offline" in location
    assert "prompt=select_account+consent" in location
    assert "state=" in location


def test_google_callback_invalid_state_returns_400():
    with patch("backend.main.GoogleOAuthConfig.from_env", return_value=_config()):
        response = client.get("/auth/google/callback?code=abc&state=invalid-state")
    assert response.status_code == 400
    assert "Invalid or expired OAuth state" in response.json()["detail"]


def test_google_callback_report_expired_returns_410():
    state = GMAIL_STATES.create(report_id="expired-report-token", redirect_uri="http://testserver/auth/google/callback")
    with patch("backend.main.GoogleOAuthConfig.from_env", return_value=_config()):
        response = client.get(f"/auth/google/callback?code=abc&state={state}")
    assert response.status_code == 410
    assert "report expired — please submit again" in response.json()["detail"]


def test_google_callback_creates_draft_and_redirects_to_gmail():
    record = _draft_record(to="sanitation@city.gov", subject="Refuse pile Ward 12")
    state = GMAIL_STATES.create(report_id=record.token, redirect_uri="http://testserver/auth/google/callback")

    with patch("backend.main.GoogleOAuthConfig.from_env", return_value=_config()), \
         patch(
             "backend.main.exchange_code",
             new=AsyncMock(return_value={
                 "access_token": "access-99",
                 "refresh_token": "refresh-99",
                 "expires_in": 3600,
             }),
         ), \
         patch(
             "backend.main.fetch_userinfo",
             new=AsyncMock(return_value={"email": "officer.citizen@gmail.com"}),
         ), \
         patch(
             "backend.main.create_gmail_draft",
             new=AsyncMock(return_value={"id": "draft-abc", "message": {"id": "msg-xyz"}}),
         ) as mock_create_draft:

        response = client.get(f"/auth/google/callback?code=code-123&state={state}", follow_redirects=False)

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://mail.google.com/mail/u/?authuser=")
    assert "officer.citizen%40gmail.com" in location
    assert "#drafts?compose=draft-abc" in location

    assert SESSION_COOKIE_NAME in response.cookies
    assert len(GMAIL_SESSIONS._sessions) == 1
    stored_session = list(GMAIL_SESSIONS._sessions.values())[0]
    assert stored_session.email == "officer.citizen@gmail.com"

    # Verify users.drafts.create was called exactly once with the expected raw MIME
    assert mock_create_draft.await_count == 1
    call_args = mock_create_draft.call_args.args
    assert call_args[0] == "access-99"
    raw_message = call_args[1]
    padded = raw_message + "=" * (-len(raw_message) % 4)
    decoded = email.message_from_bytes(base64.urlsafe_b64decode(padded), policy=policy.default)

    assert decoded["To"] == "sanitation@city.gov"
    assert decoded["Subject"] == "Refuse pile Ward 12"
    assert decoded["From"] == "officer.citizen@gmail.com"
    attachments = list(decoded.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_content_type() == "image/jpeg"
    assert attachments[0].get_payload(decode=True) == b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


def test_resolver_fallback_report_creates_draft_addressed_to_central_email(monkeypatch):
    monkeypatch.setenv("CENTRAL_GRIEVANCE_EMAIL", "central.test@custom.gov.in")
    record = _draft_record(to="", subject="Unmapped incident")
    state = GMAIL_STATES.create(report_id=record.token, redirect_uri="http://testserver/auth/google/callback")

    with patch("backend.main.GoogleOAuthConfig.from_env", return_value=_config()), \
         patch(
             "backend.main.exchange_code",
             new=AsyncMock(return_value={
                 "access_token": "token-central",
                 "expires_in": 3600,
             }),
         ), \
         patch(
             "backend.main.fetch_userinfo",
             new=AsyncMock(return_value={"email": "citizen@gmail.com"}),
         ), \
         patch(
             "backend.main.create_gmail_draft",
             new=AsyncMock(return_value={"id": "draft-central", "message": {"id": "msg-central"}}),
         ) as mock_create_draft:

        response = client.get(f"/auth/google/callback?code=code-xyz&state={state}", follow_redirects=False)

    assert response.status_code == 302
    assert mock_create_draft.await_count == 1
    raw_message = mock_create_draft.call_args.args[1]
    padded = raw_message + "=" * (-len(raw_message) % 4)
    decoded = email.message_from_bytes(base64.urlsafe_b64decode(padded), policy=policy.default)
    assert decoded["To"] == "central.test@custom.gov.in"


def test_update_report_draft_patch():
    record = _draft_record(to="sanitation@city.gov", subject="Initial subject")
    response = client.patch(
        f"/api/reports/{record.token}/draft",
        json={"body": "Updated edited complaint body", "subject": "Updated Subject"},
    )
    assert response.status_code == 200
    updated_draft = record.draft
    assert updated_draft["body"] == "Updated edited complaint body"
    assert updated_draft["subject"] == "Updated Subject"

    # Expired report returns 410
    expired_res = client.patch(
        "/api/reports/nonexistent-token/draft",
        json={"body": "Hello"},
    )
    assert expired_res.status_code == 410
