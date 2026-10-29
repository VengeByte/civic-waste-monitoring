"""
Gmail OAuth and draft creation.

The browser cannot attach a file to Gmail's compose URL. To open Gmail with the
evidence photo already attached and still let the user choose the sender and press
Send, the backend creates a Gmail **draft** through the Gmail API.

Required environment variables:
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REDIRECT_URI   # must match the Google Cloud console exactly

OAuth scope: https://www.googleapis.com/auth/gmail.compose
This permits draft creation but not sending; the user presses Send in Gmail.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Optional
from urllib.parse import quote, urlencode

import httpx

from backend.env_loader import load_env_file

logger = logging.getLogger("backend.gmail_draft")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GMAIL_DRAFTS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"

GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GOOGLE_SCOPES = ("openid", "email", "profile", GMAIL_COMPOSE_SCOPE)
SESSION_COOKIE_NAME = "gmail_session"

OAUTH_STATE_TTL_SECONDS = 10 * 60
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60


class GmailOAuthError(RuntimeError):
    """Raised when Google OAuth or Gmail draft creation fails."""


@dataclass(frozen=True)
class GoogleOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...] = GOOGLE_SCOPES

    @classmethod
    def from_env(cls) -> Optional["GoogleOAuthConfig"]:
        load_env_file()
        client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
        redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", "").strip()
        if not client_id or not client_secret:
            return None
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "select_account consent",
            "state": state,
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


@dataclass
class GoogleSession:
    session_id: str
    access_token: str
    refresh_token: str
    expires_at: float
    email: str
    scope: str = ""
    updated_at: float = 0.0


@dataclass
class PendingAuth:
    state: str
    report_id: str
    redirect_uri: str
    created_at: float
    popup: bool = False


def _token_expiry(token_data: dict[str, Any]) -> float:
    try:
        expires_in = int(token_data.get("expires_in", 3600))
    except (TypeError, ValueError):
        expires_in = 3600
    return time.time() + max(60, expires_in)


async def exchange_code(
    config: GoogleOAuthConfig,
    code: str,
    redirect_uri: str,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """Exchange an OAuth authorization code for access/refresh tokens."""
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=30)
    try:
        response = await http_client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    finally:
        if owns_client:
            await http_client.aclose()

    if response.status_code != 200:
        raise GmailOAuthError(
            f"Google token exchange failed (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        )
    data = response.json()
    if not data.get("access_token"):
        raise GmailOAuthError("Google token response did not contain an access token.")
    return data


async def refresh_access_token(
    config: GoogleOAuthConfig,
    refresh_token: str,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """Refresh an expired Gmail access token."""
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=30)
    try:
        response = await http_client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    finally:
        if owns_client:
            await http_client.aclose()

    if response.status_code != 200:
        if "invalid_grant" in response.text:
            raise GmailOAuthError("invalid_grant: Google revoked or expired the refresh token.")
        raise GmailOAuthError(
            f"Google token refresh failed (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        )
    data = response.json()
    if not data.get("access_token"):
        raise GmailOAuthError("Google refresh response did not contain an access token.")
    return data


async def fetch_userinfo(
    access_token: str,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """Read the authenticated Google account's email/profile."""
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=30)
    try:
        response = await http_client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    finally:
        if owns_client:
            await http_client.aclose()

    if response.status_code != 200:
        raise GmailOAuthError(
            f"Google userinfo failed (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        )
    data = response.json()
    if not data.get("email"):
        raise GmailOAuthError("Google userinfo did not contain an email address.")
    return data


def build_raw_message(message: EmailMessage) -> str:
    """Encode an EmailMessage as Gmail's base64url ``raw`` value."""
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")


async def create_gmail_draft(
    access_token: str,
    raw_message: str,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """Create a Gmail draft containing the full MIME message."""
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=45)
    try:
        response = await http_client.post(
            GMAIL_DRAFTS_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"message": {"raw": raw_message}},
        )
    finally:
        if owns_client:
            await http_client.aclose()

    if response.status_code not in (200, 201):
        raise GmailOAuthError(
            f"Gmail draft creation failed (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        )
    data = response.json()
    if not data.get("id"):
        raise GmailOAuthError("Gmail draft response did not contain a draft id.")
    return data


async def ensure_active_session(
    session: GoogleSession,
    config: GoogleOAuthConfig,
    client: Optional[httpx.AsyncClient] = None,
) -> GoogleSession:
    """
    Ensure the access token is valid, refreshing automatically if close to expiration.
    On invalid_grant or revoked token, evicts the session from store and raises GmailOAuthError.
    """
    if session.expires_at > time.time() + 60:
        return session
    if not session.refresh_token:
        GMAIL_SESSIONS.delete(session.session_id)
        raise GmailOAuthError("invalid_grant: No refresh token present.")
    try:
        refreshed = await refresh_access_token(config, session.refresh_token, client=client)
        updated = GMAIL_SESSIONS.update_tokens(session.session_id, refreshed)
        if updated is None:
            raise GmailOAuthError("Session was deleted during token refresh.")
        return updated
    except GmailOAuthError:
        GMAIL_SESSIONS.delete(session.session_id)
        raise


def build_draft_url(email: str, draft_id: str = "", message_id: str = "") -> str:
    """
    Return the Gmail deep link targeting the authorized account's draft.

    Format:
        https://mail.google.com/mail/u/?authuser=<urlencoded-email>#drafts?compose=<draftId>
    """
    compose_id = draft_id or message_id
    encoded_email = quote(email, safe="")
    if compose_id:
        encoded_compose = quote(compose_id, safe="")
        return f"https://mail.google.com/mail/u/?authuser={encoded_email}#drafts?compose={encoded_compose}"
    return f"https://mail.google.com/mail/u/?authuser={encoded_email}#drafts"


class GoogleSessionStore:
    """In-memory OAuth sessions keyed by an HttpOnly browser cookie."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, GoogleSession] = {}

    def create(self, token_data: dict[str, Any], email: str) -> GoogleSession:
        now = time.time()
        session = GoogleSession(
            session_id=secrets.token_urlsafe(32),
            access_token=str(token_data["access_token"]),
            refresh_token=str(token_data.get("refresh_token", "")),
            expires_at=_token_expiry(token_data),
            email=email,
            scope=str(token_data.get("scope", "")),
            updated_at=now,
        )
        with self._lock:
            self._purge_locked(now)
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: Optional[str]) -> Optional[GoogleSession]:
        if not session_id:
            return None
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if now - session.updated_at > SESSION_TTL_SECONDS:
                del self._sessions[session_id]
                return None
            return session

    def update_tokens(self, session_id: str, token_data: dict[str, Any]) -> Optional[GoogleSession]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session.access_token = str(token_data["access_token"])
            if token_data.get("refresh_token"):
                session.refresh_token = str(token_data["refresh_token"])
            session.expires_at = _token_expiry(token_data)
            session.updated_at = time.time()
            return session

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def clear(self) -> None:
        """Clear all sessions. Intended for tests."""
        with self._lock:
            self._sessions.clear()

    def _purge_locked(self, now: float) -> None:
        expired = [
            sid
            for sid, session in self._sessions.items()
            if now - session.updated_at > SESSION_TTL_SECONDS
        ]
        for sid in expired:
            del self._sessions[sid]


class OAuthStateStore:
    """Short-lived CSRF state values for the OAuth redirect."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, PendingAuth] = {}

    def create(
        self,
        report_id: str = "",
        redirect_uri: str = "",
        popup: bool = False,
    ) -> str:
        if isinstance(report_id, bool):
            popup = report_id
            report_id = ""
        state = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            self._states[state] = PendingAuth(
                state=state,
                report_id=report_id,
                redirect_uri=redirect_uri,
                created_at=now,
                popup=popup,
            )
        return state

    def consume(self, state: Optional[str]) -> Optional[PendingAuth]:
        if not state:
            return None
        now = time.time()
        with self._lock:
            pending = self._states.pop(state, None)
            if pending is None:
                return None
            if now - pending.created_at > OAUTH_STATE_TTL_SECONDS:
                return None
            return pending

    def clear(self) -> None:
        """Clear pending OAuth states. Intended for tests."""
        with self._lock:
            self._states.clear()

    def _purge_locked(self, now: float) -> None:
        expired = [
            state
            for state, pending in self._states.items()
            if now - pending.created_at > OAUTH_STATE_TTL_SECONDS
        ]
        for state in expired:
            del self._states[state]


GMAIL_SESSIONS = GoogleSessionStore()
GMAIL_STATES = OAuthStateStore()
