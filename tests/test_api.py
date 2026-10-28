"""
End-to-end integration test runner for backend/main.py using fastapi.testclient.TestClient.

Tests:
1. Test 1: Full Positive Pipeline & Handoff Compilation (Ward 12 resolution, dispatch URLs)
2. Test 2: Negative Detection Short-Circuit (garbage_detected=False, draft omitted)
3. Test 3: URL Encoding & Escaping Validation (&, ?, =, %, spaces, newlines properly escaped)
4. Test 4: Input Validation (Empty File -> HTTP 400 or 422)

Executive Summary Table:
- Test Name
- Endpoint
- Latency
- Status

Execution:
    pytest tests/test_api.py -v
    python tests/test_api.py
"""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.main import app
from backend.privacy import blur_pii_in_image
from backend.vlm_router import VLMResult
from tests.test_privacy import generate_synthetic_pavement_image


@dataclass
class APIMetricCard:
    test_name: str
    endpoint: str
    latency_ms: float
    status: str
    details: dict[str, Any]
    error_message: Optional[str] = None


# Shared TestClient instance
client = TestClient(app)

# Generate valid JPEG test image bytes
SYNTHETIC_TEST_IMAGE_BYTES = generate_synthetic_pavement_image(width=256, height=256)


# ============================================================================
# Core Integration Tests (Compatible with pytest and standard runner)
# ============================================================================

def test_full_positive_pipeline_and_handoff_compilation() -> APIMetricCard:
    """
    Test 1: Full Positive Pipeline & Handoff Compilation
    - Posts a synthetic test image along with lat=30.7500, lon=76.7700 (inside Ward 12).
    - Mocks the VLM to return positive garbage detection.
    - Asserts status code 200.
    - Asserts garbage_detected == True.
    - Asserts mailto_url contains mailto:sanitation.ward12@chd.nic.in.
    - Asserts gmail_web_url starts with https://mail.google.com/mail/?view=cm.
    - Asserts whatsapp_url contains sanitized phone number and encoded body.
    """
    lat, lon = 30.7500, 76.7700
    mock_vlm_dict = {
        "garbage_detected": True,
        "confidence": 0.96,
        "waste_types": ["plastic bags", "domestic refuse", "cardboard"],
        "severity": "high",
        "official_complaint_description": (
            "A heavy accumulation of uncollected domestic waste and discarded packaging is obstructing "
            "the pedestrian pavement. Immediate municipal sanitation dispatch is requested."
        ),
    }

    start_time = time.perf_counter()
    with patch("backend.main.analyze_with_vlm", return_value=VLMResult(mock_vlm_dict, provider="Google Gemini")), \
         patch("backend.authority_router.lookup_address", return_value="Sector 15, Chandigarh, 160015, India"):

        files = {"file": ("test_garbage.jpg", SYNTHETIC_TEST_IMAGE_BYTES, "image/jpeg")}
        form_data = {
            "lat": str(lat),
            "lon": str(lon),
            "captured_at": "2026-10-25 12:00:00 UTC",
        }
        response = client.post("/api/report", files=files, data=form_data)

    latency_ms = (time.perf_counter() - start_time) * 1000.0

    # Assertions
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    body = response.json()
    assert body["status"] == "success"
    assert body["garbage_detected"] is True, f"Expected garbage_detected=True, got {body.get('garbage_detected')}"

    # Verify Ward 12 was resolved
    authority = body.get("authority", {})
    assert authority.get("ward_number") == "12", f"Expected Ward 12, got {authority.get('ward_number')}"

    # Verify draft & dispatch URLs
    draft = body.get("draft", {})
    assert draft, "Draft object must be present in positive detection response"
    assert "mailto:sanitation.ward12@chd.nic.in" in draft["mailto_url"], (
        f"mailto_url missing target email: {draft.get('mailto_url')}"
    )
    assert draft["gmail_web_url"].startswith("https://mail.google.com/mail/?view=cm"), (
        f"gmail_web_url does not start with expected prefix: {draft.get('gmail_web_url')}"
    )

    clean_helpline = "919220592205"
    assert clean_helpline in draft["whatsapp_url"], (
        f"whatsapp_url must contain sanitized helpline {clean_helpline}: {draft.get('whatsapp_url')}"
    )
    assert "text=" in draft["whatsapp_url"], "whatsapp_url must contain encoded text parameter"

    return APIMetricCard(
        test_name="Test 1: Full Positive Pipeline",
        endpoint="POST /api/report",
        latency_ms=latency_ms,
        status="PASSED",
        details={
            "http_status": response.status_code,
            "garbage_detected": body["garbage_detected"],
            "ward_resolved": f"Ward {authority.get('ward_number')} ({authority.get('ward_name')})",
            "to_recipient": draft["to"],
            "mailto_url_prefix": draft["mailto_url"][:45] + "...",
            "gmail_web_url_prefix": draft["gmail_web_url"][:45] + "...",
            "whatsapp_url_prefix": draft["whatsapp_url"][:45] + "...",
        },
    )


def test_negative_detection_short_circuit() -> APIMetricCard:
    """
    Test 2: Negative Detection Short-Circuit
    - Posts an image where VLM returns garbage_detected == False.
    - Asserts status code 200.
    - Asserts garbage_detected == False.
    - Asserts draft is omitted or None (preventing complaint generation on clean areas).
    """
    lat, lon = 30.7500, 76.7700
    mock_clean_vlm = {
        "garbage_detected": False,
        "confidence": 0.99,
        "waste_types": [],
        "severity": "low",
        "official_complaint_description": "Clean municipal pavement with no discarded waste.",
    }

    start_time = time.perf_counter()
    with patch("backend.main.analyze_with_vlm", return_value=VLMResult(mock_clean_vlm, provider="Google Gemini")):
        files = {"file": ("clean_street.jpg", SYNTHETIC_TEST_IMAGE_BYTES, "image/jpeg")}
        form_data = {"lat": str(lat), "lon": str(lon)}
        response = client.post("/api/report", files=files, data=form_data)

    latency_ms = (time.perf_counter() - start_time) * 1000.0

    # Assertions
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    body = response.json()
    assert body["status"] == "success"
    assert body["garbage_detected"] is False, f"Expected garbage_detected=False, got {body.get('garbage_detected')}"
    assert "draft" not in body or body["draft"] is None, "Draft must be omitted or None when no garbage is detected"
    assert body.get("message") == "No uncollected civic waste detected."

    return APIMetricCard(
        test_name="Test 2: Negative Short-Circuit",
        endpoint="POST /api/report",
        latency_ms=latency_ms,
        status="PASSED",
        details={
            "http_status": response.status_code,
            "garbage_detected": body["garbage_detected"],
            "message": body.get("message"),
            "draft_omitted": "draft" not in body or body.get("draft") is None,
        },
    )


def test_url_encoding_and_escaping_validation() -> APIMetricCard:
    """
    Test 3: URL Encoding & Escaping Validation
    - Verifies that spaces, newlines, and special characters (&, ?, =, %) in generated
      mailto_url, gmail_web_url, and whatsapp_url are properly percent-encoded and do
      not break URL structure.
    """
    lat, lon = 30.7500, 76.7700
    # Description containing problematic characters (&, ?, =, %, newlines, spaces)
    special_desc = (
        "Hazardous refuse: 50% broken glass & toxic chemical containers?\n"
        "Urgent action = mandatory.\n"
        "Check area 100% clean."
    )
    mock_vlm_special = {
        "garbage_detected": True,
        "confidence": 0.92,
        "waste_types": ["glass & bottles", "50% plastic"],
        "severity": "high",
        "official_complaint_description": special_desc,
    }

    start_time = time.perf_counter()
    with patch("backend.main.analyze_with_vlm", return_value=VLMResult(mock_vlm_special, provider="Google Gemini")), \
         patch("backend.authority_router.lookup_address", return_value="Sector 15 & 16 Plaza, Chandigarh"):

        files = {"file": ("special_chars.jpg", SYNTHETIC_TEST_IMAGE_BYTES, "image/jpeg")}
        form_data = {"lat": str(lat), "lon": str(lon)}
        response = client.post("/api/report", files=files, data=form_data)

    latency_ms = (time.perf_counter() - start_time) * 1000.0

    assert response.status_code == 200
    draft = response.json()["draft"]

    mailto_url = draft["mailto_url"]
    gmail_web_url = draft["gmail_web_url"]
    whatsapp_url = draft["whatsapp_url"]

    # 1. Ensure NO raw unescaped spaces or newlines exist in the URL strings
    for url_name, url_str in [("mailto", mailto_url), ("gmail", gmail_web_url), ("whatsapp", whatsapp_url)]:
        assert " " not in url_str, f"Unescaped raw space found in {url_name} URL"
        assert "\n" not in url_str, f"Unescaped newline found in {url_name} URL"
        assert "\r" not in url_str, f"Unescaped carriage return found in {url_name} URL"

    # 2. Parse Gmail URL structure via urllib.parse to verify query integrity
    parsed_gmail = urllib.parse.urlparse(gmail_web_url)
    assert parsed_gmail.scheme == "https"
    assert parsed_gmail.netloc == "mail.google.com"
    gmail_qs = urllib.parse.parse_qs(parsed_gmail.query)

    assert gmail_qs.get("view") == ["cm"]
    assert gmail_qs.get("fs") == ["1"]
    assert "to" in gmail_qs and len(gmail_qs["to"][0]) > 0
    assert "su" in gmail_qs
    # Assert body successfully roundtrips through URL encoding without losing special characters
    recovered_body = gmail_qs["body"][0]
    assert special_desc in recovered_body, "Special characters (&, ?, =, %, newlines) failed to roundtrip in Gmail body"

    # 3. Parse WhatsApp URL structure
    parsed_wa = urllib.parse.urlparse(whatsapp_url)
    assert parsed_wa.scheme == "https"
    assert parsed_wa.netloc == "wa.me"
    wa_qs = urllib.parse.parse_qs(parsed_wa.query)
    assert "text" in wa_qs
    assert len(wa_qs["text"][0]) > 0

    return APIMetricCard(
        test_name="Test 3: URL Encoding & Escaping",
        endpoint="POST /api/report",
        latency_ms=latency_ms,
        status="PASSED",
        details={
            "raw_newlines_detected": False,
            "raw_spaces_detected": False,
            "special_chars_verified": ["&", "?", "=", "%", "\\n"],
            "gmail_query_params": list(gmail_qs.keys()),
            "whatsapp_param_verified": "text",
        },
    )


def test_input_validation_empty_file() -> APIMetricCard:
    """
    Test 4: Input Validation (Empty File)
    - Sends an empty payload and asserts that HTTP 400 or 422 is returned.
    """
    start_time = time.perf_counter()

    # 1. Send empty byte payload
    files = {"file": ("empty.jpg", b"", "image/jpeg")}
    form_data = {"lat": "30.7500", "lon": "76.7700"}
    response = client.post("/api/report", files=files, data=form_data)

    latency_ms = (time.perf_counter() - start_time) * 1000.0

    # Assert HTTP 400 or 422 returned
    assert response.status_code in (400, 422), (
        f"Expected HTTP 400 or 422 for empty file, got {response.status_code}: {response.text}"
    )

    return APIMetricCard(
        test_name="Test 4: Input Validation (Empty File)",
        endpoint="POST /api/report",
        latency_ms=latency_ms,
        status="PASSED",
        details={
            "http_status_returned": response.status_code,
            "expected_statuses": [400, 422],
            "error_detail": response.json().get("detail"),
        },
    )


# ============================================================================
# Executive Summary Formatter
# ============================================================================

def display_metric_card(card: APIMetricCard) -> None:
    """Print an individual test metric card."""
    border = "-" * 82
    print(f"\n{border}")
    status_tag = f"[{card.status}]"
    print(f" {card.test_name.upper():<68} {status_tag}")
    print(border)
    print(f"  * Endpoint      : {card.endpoint}")
    print(f"  * Latency       : {card.latency_ms:.2f} ms")
    if card.error_message:
        print(f"  * Error         : {card.error_message}")
    print("  * Metric Details:")
    for k, v in card.details.items():
        print(f"      - {k:<26}: {v}")
    print(border)


def display_visual_summary_block(cards: list[APIMetricCard]) -> None:
    """
    Print the final visual summary table displaying:
    - Test Name
    - Endpoint
    - Latency
    - Status
    """
    col_test = 42
    col_endp = 20
    col_lat = 14
    col_stat = 10

    total_width = col_test + col_endp + col_lat + col_stat + 9

    header_bar = "=" * total_width
    sub_bar = "-" * total_width

    print("\n" + header_bar)
    title = "API INTEGRATION TEST RUNNER: EXECUTIVE SUMMARY"
    print(f"{title:^{total_width}}")
    print(header_bar)
    header_line = (
        f" {'Test Name':<{col_test}} | "
        f"{'Endpoint':<{col_endp}} | "
        f"{'Latency':<{col_lat}} | "
        f"{'Status':<{col_stat}}"
    )
    print(header_line)
    print(sub_bar)

    all_passed = True
    total_latency = 0.0

    for card in cards:
        lat_str = f"{card.latency_ms:.2f} ms"
        status_str = card.status
        if card.status != "PASSED":
            all_passed = False

        total_latency += card.latency_ms
        row = (
            f" {card.test_name:<{col_test}} | "
            f"{card.endpoint:<{col_endp}} | "
            f"{lat_str:<{col_lat}} | "
            f"{status_str:<{col_stat}}"
        )
        print(row)

    print(header_bar)
    summary_verdict = (
        "ALL 4 API CHECKS PASSED [SUCCESS]"
        if all_passed
        else "SOME CHECKS FAILED [FAILURE]"
    )
    print(f" Verdict: {summary_verdict} (Total Latency: {total_latency:.2f} ms)")
    print(header_bar + "\n")


# ============================================================================
# Main Runner
# ============================================================================

def run_all_checks() -> int:
    """Run all 4 integration tests and render formatted metric cards & summary block."""
    tests = [
        ("Test 1", test_full_positive_pipeline_and_handoff_compilation),
        ("Test 2", test_negative_detection_short_circuit),
        ("Test 3", test_url_encoding_and_escaping_validation),
        ("Test 4", test_input_validation_empty_file),
    ]

    cards: list[APIMetricCard] = []
    print("\n>>> Starting End-to-End API Integration Test Suite...")

    for label, test_fn in tests:
        try:
            card = test_fn()
            cards.append(card)
            display_metric_card(card)
        except Exception as exc:
            fail_card = APIMetricCard(
                test_name=f"{label} (Execution Error)",
                endpoint="POST /api/report",
                latency_ms=0.0,
                status="FAILED",
                details={"exception": type(exc).__name__},
                error_message=str(exc),
            )
            cards.append(fail_card)
            display_metric_card(fail_card)

    display_visual_summary_block(cards)

    all_passed = all(c.status == "PASSED" for c in cards)
    return 0 if all_passed else 1


def main() -> int:
    return run_all_checks()


if __name__ == "__main__":
    sys.exit(main())
