"""
Tests for short-lived evidence retention, the evidence endpoint, and photo-attached
server-side email dispatch.

Coverage:
- EvidenceStore TTL, eviction, draft attachment, delivery counters.
- POST /api/report registers a report_id and an evidence URL inside the letter.
- GET /api/evidence/{token} serves the exact sanitized JPEG bytes.
- PUBLIC_BASE_URL override for proxied deployments.
- POST /api/reports/{id}/email validation, 503 when SMTP is absent, and success path.
"""

from __future__ import annotations

import base64
import urllib.parse
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.evidence import EVIDENCE_STORE, EvidenceStore
from backend.main import app
from backend.vlm_router import VLMResult
from tests.test_privacy import generate_synthetic_pavement_image

client = TestClient(app)
SYNTHETIC_IMAGE = generate_synthetic_pavement_image(width=256, height=256)

POSITIVE_VLM = {
    "garbage_detected": True,
    "confidence": 0.96,
    "waste_types": ["plastic bags", "domestic refuse"],
    "severity": "high",
    "official_complaint_description": "A large pile of uncollected refuse is blocking the pavement.",
}

NEGATIVE_VLM = {
    "garbage_detected": False,
    "confidence": 0.99,
    "waste_types": [],
    "severity": "low",
    "official_complaint_description": "The pavement is clean and free of refuse.",
}


class FakeClock:
    """Deterministic clock for retention-window tests."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _clean_evidence_store():
    """Keep the process-wide store isolated between tests."""
    EVIDENCE_STORE.clear()
    yield
    EVIDENCE_STORE.clear()


def _submit_report(vlm_payload: dict | None = None) -> dict:
    """Submit a synthetic report with mocked VLM and geocoding."""
    payload = vlm_payload or POSITIVE_VLM
    with patch(
        "backend.main.analyze_with_vlm",
        return_value=VLMResult(dict(payload), provider="Google Gemini"),
    ), patch(
        "backend.authority_router.lookup_address",
        return_value="Sector 15, Chandigarh, 160015, India",
    ):
        response = client.post(
            "/api/report",
            files={"file": ("waste.jpg", SYNTHETIC_IMAGE, "image/jpeg")},
            data={"lat": "30.7520", "lon": "76.7750", "captured_at": "2026-10-27 10:00:00 UTC"},
        )
    assert response.status_code == 200, response.text
    return response.json()


# ============================================================================
# EvidenceStore unit behaviour
# ============================================================================

def test_store_create_and_get_returns_same_bytes():
    store = EvidenceStore(ttl_seconds=600, max_entries=5)
    record = store.create(b"\xff\xd8fake-jpeg")

    assert len(record.token) >= 32
    assert record.expires_at > record.created_at
    assert len(store) == 1

    fetched = store.get(record.token)
    assert fetched is record
    assert fetched.image_bytes == b"\xff\xd8fake-jpeg"
    assert fetched.expires_at_iso.endswith("Z")


def test_store_expires_records_after_ttl():
    clock = FakeClock()
    store = EvidenceStore(ttl_seconds=600, max_entries=5, clock=clock)
    record = store.create(b"jpeg")

    clock.advance(599)
    assert store.get(record.token) is not None

    clock.advance(2)
    assert store.get(record.token) is None
    assert len(store) == 0


def test_store_evicts_oldest_when_capacity_is_reached():
    store = EvidenceStore(ttl_seconds=600, max_entries=2)
    first = store.create(b"one")
    second = store.create(b"two")
    third = store.create(b"three")

    assert len(store) == 2
    assert store.get(first.token) is None
    assert store.get(second.token) is not None
    assert store.get(third.token) is not None


def test_store_can_attach_draft_and_track_deliveries():
    store = EvidenceStore()
    record = store.create(b"jpeg")

    assert store.set_draft(record.token, draft={"subject": "s", "body": "b"})
    assert store.set_draft("unknown-token", draft={}) is False

    stored = store.get(record.token)
    assert stored.draft == {"subject": "s", "body": "b"}
    assert store.record_delivery(record.token) == 1
    assert store.record_delivery(record.token) == 2
    assert store.record_delivery("unknown-token") is None


# ============================================================================
# Report pipeline + evidence endpoint
# ============================================================================

def test_report_response_contains_report_id_and_evidence_url():
    body = _submit_report()
    draft = body["draft"]

    assert body["report_id"]
    assert draft["evidence_url"].endswith(f"/api/evidence/{body['report_id']}")
    assert draft["evidence_url"] in draft["body"]
    assert urllib.parse.quote(draft["evidence_url"]) in draft["whatsapp_url"]
    assert draft["evidence_expires_at"].endswith("Z")
    assert "Photo Evidence" in draft["body"]


def test_evidence_endpoint_serves_exact_sanitized_jpeg():
    body = _submit_report()
    path = urllib.parse.urlparse(body["draft"]["evidence_url"]).path

    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "private" in response.headers["cache-control"]
    assert response.content == base64.b64decode(body["sanitized_image_b64"])
    assert response.content.startswith(b"\xff\xd8")


def test_evidence_endpoint_unknown_token_returns_404():
    response = client.get("/api/evidence/not-a-real-token")
    assert response.status_code == 404
    assert "expired" in response.json()["detail"].lower()


def test_evidence_endpoint_expired_token_returns_404(monkeypatch):
    body = _submit_report()
    record = EVIDENCE_STORE.get(body["report_id"])
    assert record is not None

    # Move the store's clock past the record's expiry without sleeping.
    monkeypatch.setattr(EVIDENCE_STORE, "_clock", lambda: record.expires_at + 1)
    path = urllib.parse.urlparse(body["draft"]["evidence_url"]).path
    response = client.get(path)

    assert response.status_code == 404


def test_public_base_url_override_is_used(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://civic.example/")
    body = _submit_report()

    expected_prefix = f"https://civic.example/api/evidence/{body['report_id']}"
    assert body["draft"]["evidence_url"] == expected_prefix


def test_negative_report_does_not_retain_evidence():
    body = _submit_report(NEGATIVE_VLM)

    assert body["garbage_detected"] is False
    assert "report_id" not in body
    assert "draft" not in body
    assert len(EVIDENCE_STORE) == 0


def test_frontend_exposes_evidence_and_photo_email_controls():
    response = client.get("/")

    assert response.status_code == 200
    assert "Evidence link" in response.text
    assert "Email + Attach Photo" in response.text


# ============================================================================
# Server-side email dispatch endpoint
# ============================================================================

def test_email_endpoint_returns_503_when_smtp_not_configured(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    body = _submit_report()

    response = client.post(f"/api/reports/{body['report_id']}/email", json={})

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_email_endpoint_sends_edited_body_with_attachment(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    body = _submit_report()
    delivery = {
        "recipients": ["sanitation.ward12@chd.nic.in"],
        "attachment_bytes": 1234,
        "message_bytes": 5678,
        "used_starttls": True,
        "used_ssl": False,
    }

    with patch("backend.main.send_complaint_email", return_value=delivery) as mock_send:
        response = client.post(
            f"/api/reports/{body['report_id']}/email",
            json={"body": "Edited complaint body with photos.", "subject": "Edited subject"},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "sent"
    assert payload["to"] == "sanitation.ward12@chd.nic.in"
    assert payload["subject"] == "Edited subject"
    assert payload["attachment_bytes"] == 1234
    assert payload["send_count"] == 1

    mock_send.assert_called_once()
    kwargs = mock_send.call_args.kwargs
    assert kwargs["body"] == "Edited complaint body with photos."
    assert kwargs["subject"] == "Edited subject"
    assert kwargs["image_bytes"] == base64.b64decode(body["sanitized_image_b64"])


def test_email_endpoint_uses_compiled_draft_when_no_override(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    body = _submit_report()
    delivery = {
        "recipients": [],
        "attachment_bytes": 1,
        "message_bytes": 2,
        "used_starttls": True,
        "used_ssl": False,
    }

    with patch("backend.main.send_complaint_email", return_value=delivery) as mock_send:
        response = client.post(f"/api/reports/{body['report_id']}/email", json={})

    assert response.status_code == 200, response.text
    kwargs = mock_send.call_args.kwargs
    assert kwargs["subject"] == body["draft"]["subject"]
    assert kwargs["body"] == body["draft"]["body"]
    assert body["draft"]["evidence_url"] in kwargs["body"]


def test_email_endpoint_enforces_send_limit(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    body = _submit_report()
    delivery = {
        "recipients": [],
        "attachment_bytes": 1,
        "message_bytes": 2,
        "used_starttls": True,
        "used_ssl": False,
    }

    with patch("backend.main.send_complaint_email", return_value=delivery):
        for _ in range(5):
            response = client.post(f"/api/reports/{body['report_id']}/email", json={})
            assert response.status_code == 200, response.text
        blocked = client.post(f"/api/reports/{body['report_id']}/email", json={})

    assert blocked.status_code == 429
    assert "Send limit" in blocked.json()["detail"]


def test_email_endpoint_rejects_unknown_report():
    response = client.post("/api/reports/unknown-report/email", json={})
    assert response.status_code == 404


def test_email_endpoint_rejects_oversized_body(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    body = _submit_report()

    response = client.post(
        f"/api/reports/{body['report_id']}/email",
        json={"body": "x" * 20_001},
    )
    assert response.status_code == 422
