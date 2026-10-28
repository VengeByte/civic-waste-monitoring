"""
Test runner for backend/vlm_router.py using pytest or standard asyncio.

Runs and displays formatted metric cards for four checks:
1. Test 1: Schema & Positive Garbage Detection
2. Test 2: Negative Detection (Clean Scene)
3. Test 3: Provider Failover Simulation
4. Test 4: All-Provider Failure Handling

Execution:
    pytest tests/test_vlm.py -v
    python tests/test_vlm.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from unittest.mock import patch

import httpx
import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.vlm_config import VLMModelConfig
from backend.vlm_router import (
    DEFAULT_PROVIDERS,
    AllVLMProvidersFailedError,
    VLMProviderConfig,
    VLMResult,
    analyze_with_vlm,
)
from tests.test_privacy import generate_synthetic_pavement_image


@dataclass
class MetricCard:
    test_name: str
    provider_used: str
    latency_ms: float
    status: str
    details: dict[str, Any]
    error_message: Optional[str] = None


# Sample Mock Payloads conforming strictly to system prompt schema
MOCK_POSITIVE_GARBAGE_PAYLOAD = {
    "choices": [
        {
            "message": {
                "content": json.dumps({
                    "garbage_detected": True,
                    "confidence": 0.95,
                    "waste_types": ["plastic bags", "domestic waste", "discarded cardboard"],
                    "severity": "medium",
                    "official_complaint_description": (
                        "A substantial accumulation of discarded plastic bags and domestic refuse is obstructing the "
                        "pedestrian walkway. Immediate municipal sanitation dispatch is requested for clearance and sanitization."
                    ),
                })
            }
        }
    ]
}

MOCK_NEGATIVE_CLEAN_PAYLOAD = {
    "choices": [
        {
            "message": {
                "content": json.dumps({
                    "garbage_detected": False,
                    "confidence": 0.99,
                    "waste_types": [],
                    "severity": "low",
                    "official_complaint_description": (
                        "The inspected pavement and street surface is clean and devoid of refuse. No municipal sanitation violation observed."
                    ),
                })
            }
        }
    ]
}


def load_or_create_garbage_image() -> bytes:
    """Load a sample street garbage image or return synthetic image bytes."""
    candidate_paths = [
        PROJECT_ROOT / "tests" / "sample_car.jpg",
        PROJECT_ROOT / "tests" / "output_car_blurred.jpg",
    ]
    for p in candidate_paths:
        if p.is_file():
            with open(p, "rb") as f:
                return f.read()

    # Fallback to generating a textured image
    return generate_synthetic_pavement_image(512, 512)


def load_or_create_clean_pavement_image() -> bytes:
    """Return synthetic neutral pavement/clean floor image bytes."""
    return generate_synthetic_pavement_image(512, 512)


# ============================================================================
# Core Test Implementations (Compatible with pytest and standard asyncio)
# ============================================================================

@pytest.mark.asyncio
async def test_schema_and_positive_garbage_detection() -> MetricCard:
    """
    Test 1: Schema & Positive Garbage Detection
    - Loads an image representing street garbage.
    - Calls analyze_with_vlm(image_bytes).
    - Asserts garbage_detected == True, confidence >= 0.5, waste_types non-empty,
      severity in ['low', 'medium', 'high'], official_complaint_description >= 15 chars.
    - Asserts provider_used is populated.
    """
    image_bytes = load_or_create_garbage_image()

    async def mock_post(self, url, *args, **kwargs):
        return httpx.Response(200, json=MOCK_POSITIVE_GARBAGE_PAYLOAD)

    start_time = time.perf_counter()
    with patch.object(httpx.AsyncClient, "post", new=mock_post):
        result = await analyze_with_vlm(image_bytes)
    latency_ms = (time.perf_counter() - start_time) * 1000.0

    # Schema & Positive Assertions
    assert result["garbage_detected"] is True, f"Expected garbage_detected=True, got {result.get('garbage_detected')}"
    assert result["confidence"] >= 0.5, f"Expected confidence >= 0.5, got {result.get('confidence')}"
    assert isinstance(result["waste_types"], list) and len(result["waste_types"]) > 0, "waste_types must be a non-empty list"
    assert result["severity"] in ["low", "medium", "high"], f"Invalid severity: {result.get('severity')}"
    assert isinstance(result["official_complaint_description"], str), "Description must be a string"
    assert len(result["official_complaint_description"]) >= 15, "Description must have at least 15 characters"

    # Provider used assertion
    provider_used = result.get("provider_used") or result.get("provider") or getattr(result, "provider_used", None)
    assert provider_used is not None and len(str(provider_used).strip()) > 0, "provider_used must be populated"

    return MetricCard(
        test_name="Test 1: Schema & Positive Garbage Detection",
        provider_used=str(provider_used),
        latency_ms=latency_ms,
        status="PASSED",
        details={
            "garbage_detected": result["garbage_detected"],
            "confidence": result["confidence"],
            "waste_types": result["waste_types"],
            "severity": result["severity"],
            "description_length": len(result["official_complaint_description"]),
        },
    )


@pytest.mark.asyncio
async def test_negative_detection_clean_scene() -> MetricCard:
    """
    Test 2: Negative Detection (Clean Scene)
    - Passes a synthetic neutral pavement/clean floor image.
    - Calls analyze_with_vlm(image_bytes).
    - Asserts garbage_detected == False.
    """
    clean_bytes = load_or_create_clean_pavement_image()

    async def mock_post(self, url, *args, **kwargs):
        return httpx.Response(200, json=MOCK_NEGATIVE_CLEAN_PAYLOAD)

    start_time = time.perf_counter()
    with patch.object(httpx.AsyncClient, "post", new=mock_post):
        result = await analyze_with_vlm(clean_bytes)
    latency_ms = (time.perf_counter() - start_time) * 1000.0

    # Negative Assertions
    assert result["garbage_detected"] is False, f"Expected garbage_detected=False on clean scene, got {result.get('garbage_detected')}"
    provider_used = result.get("provider_used") or result.get("provider") or getattr(result, "provider_used", None)

    return MetricCard(
        test_name="Test 2: Negative Detection (Clean Scene)",
        provider_used=str(provider_used),
        latency_ms=latency_ms,
        status="PASSED",
        details={
            "garbage_detected": result["garbage_detected"],
            "confidence": result["confidence"],
            "waste_types": result["waste_types"],
            "severity": result["severity"],
        },
    )


@pytest.mark.asyncio
async def test_provider_failover_simulation() -> MetricCard:
    """
    Test 3: Provider Failover Simulation
    - Temporarily patch or monkey-patch the primary provider's endpoint or API key to an invalid value
      (simulating a 401, 429, or network drop).
    - Calls analyze_with_vlm(image_bytes).
    - Asserts that the function does not crash, successfully catches the error, and completes
      the request using the secondary or tertiary provider (verifying provider_used changed to backup).
    """
    image_bytes = load_or_create_garbage_image()

    attempted_urls = []

    async def mock_post(self, url, *args, **kwargs):
        url_str = str(url)
        attempted_urls.append(url_str)
        # Primary provider (Gemini or invalid endpoint) fails with HTTP 429 Rate Limit
        if "generativelanguage" in url_str or "invalid" in url_str:
            return httpx.Response(429, json={"error": {"message": "Resource rate limit exceeded."}})
        # Secondary provider (OpenRouter) takes over and returns 200
        return httpx.Response(200, json=MOCK_POSITIVE_GARBAGE_PAYLOAD)

    start_time = time.perf_counter()
    # Temporarily monkey-patch primary provider's endpoint to simulate failure
    modified_primary = VLMProviderConfig(
        name="Google Gemini",
        endpoint="https://generativelanguage.googleapis.com/v1beta/openai/invalid_drop",
        api_key_env="GEMINI_API_KEY",
        models=(VLMModelConfig(id="gemini-3.5-flash"),),
        timeout_seconds=10.0,
        max_retries=0,
    )
    patched_providers = (modified_primary, DEFAULT_PROVIDERS[1])

    with patch.object(httpx.AsyncClient, "post", new=mock_post):
        result = await analyze_with_vlm(image_bytes, providers=patched_providers)
    latency_ms = (time.perf_counter() - start_time) * 1000.0

    provider_used = result.get("provider_used") or result.get("provider") or getattr(result, "provider_used", None)
    primary_name = modified_primary.name

    # Assertions
    assert provider_used != primary_name, f"Expected provider_used to change to backup, got {provider_used}"
    assert provider_used == "OpenRouter", f"Expected backup provider OpenRouter, got {provider_used}"
    assert result["garbage_detected"] is True

    return MetricCard(
        test_name="Test 3: Provider Failover Simulation",
        provider_used=str(provider_used),
        latency_ms=latency_ms,
        status="PASSED",
        details={
            "simulated_error": "Primary HTTP 429 Rate Limit",
            "primary_attempted": primary_name,
            "backup_resolved": provider_used,
            "total_attempts": len(attempted_urls),
        },
    )


@pytest.mark.asyncio
async def test_all_provider_failure_handling() -> MetricCard:
    """
    Test 4: All-Provider Failure Handling
    - Temporarily disable or invalidate all API keys/endpoints.
    - Asserts that analyze_with_vlm raises a clean RuntimeError rather than unhandled JSON or HTTP crash errors.
    """
    image_bytes = load_or_create_garbage_image()

    async def mock_post(self, url, *args, **kwargs):
        # All providers return server errors HTTP 500
        return httpx.Response(500, text="Internal Server Error")

    start_time = time.perf_counter()
    raised_clean_runtime_error = False
    caught_exception_name = ""

    with patch.object(httpx.AsyncClient, "post", new=mock_post):
        try:
            await analyze_with_vlm(image_bytes)
        except RuntimeError as exc:
            # AllVLMProvidersFailedError is a subclass of RuntimeError
            raised_clean_runtime_error = True
            caught_exception_name = type(exc).__name__
        except Exception as exc:
            caught_exception_name = f"UNEXPECTED: {type(exc).__name__}: {exc}"

    latency_ms = (time.perf_counter() - start_time) * 1000.0

    assert raised_clean_runtime_error, f"Expected clean RuntimeError, but caught: {caught_exception_name}"

    return MetricCard(
        test_name="Test 4: All-Provider Failure Handling",
        provider_used="None (All Failed)",
        latency_ms=latency_ms,
        status="PASSED",
        details={
            "expected_exception": "RuntimeError",
            "caught_exception": caught_exception_name,
            "cleanly_handled": True,
        },
    )


# ============================================================================
# Visual CLI Summary Formatter
# ============================================================================

def display_metric_card(card: MetricCard) -> None:
    """Print an individual test metric card."""
    border = "-" * 78
    print(f"\n{border}")
    status_tag = f"[{card.status}]"
    print(f" {card.test_name.upper():<64} {status_tag}")
    print(border)
    print(f"  * Provider Used : {card.provider_used}")
    print(f"  * Latency       : {card.latency_ms:.2f} ms")
    if card.error_message:
        print(f"  * Error         : {card.error_message}")
    print("  * Metric Details:")
    for k, v in card.details.items():
        print(f"      - {k:<28}: {v}")
    print(border)


def display_visual_summary_block(cards: list[MetricCard]) -> None:
    """
    Print the final visual summary table displaying:
    - Test Name
    - Provider Used
    - Latency (ms)
    - Passed/Failed Status
    """
    col_test = 44
    col_prov = 20
    col_lat = 14
    col_stat = 10

    total_width = col_test + col_prov + col_lat + col_stat + 9

    header_bar = "=" * total_width
    sub_bar = "-" * total_width

    print("\n" + header_bar)
    title = "VLM ROUTER TEST RUNNER: EXECUTIVE SUMMARY"
    print(f"{title:^{total_width}}")
    print(header_bar)
    header_line = (
        f" {'Test Name':<{col_test}} | "
        f"{'Provider Used':<{col_prov}} | "
        f"{'Latency (ms)':<{col_lat}} | "
        f"{'Status':<{col_stat}}"
    )
    print(header_line)
    print(sub_bar)

    all_passed = True
    total_latency = 0.0

    for card in cards:
        lat_str = f"{card.latency_ms:.1f} ms"
        status_str = card.status
        if card.status != "PASSED":
            all_passed = False

        total_latency += card.latency_ms
        row = (
            f" {card.test_name:<{col_test}} | "
            f"{card.provider_used:<{col_prov}} | "
            f"{lat_str:<{col_lat}} | "
            f"{status_str:<{col_stat}}"
        )
        print(row)

    print(header_bar)
    summary_verdict = "ALL 4 VLM CHECKS PASSED [SUCCESS]" if all_passed else "SOME CHECKS FAILED [FAILURE]"
    print(f" Verdict: {summary_verdict} (Total Latency: {total_latency:.1f} ms)")
    print(header_bar + "\n")


# ============================================================================
# Runner
# ============================================================================

async def run_all_checks() -> int:
    """Execute all 4 tests in order and render the formatted metric cards and summary block."""
    tests = [
        ("Test 1", test_schema_and_positive_garbage_detection),
        ("Test 2", test_negative_detection_clean_scene),
        ("Test 3", test_provider_failover_simulation),
        ("Test 4", test_all_provider_failure_handling),
    ]

    cards: list[MetricCard] = []
    print("\n>>> Starting VLM Router Multi-Provider Test Suite...")

    for label, test_fn in tests:
        try:
            card = await test_fn()
            cards.append(card)
            display_metric_card(card)
        except Exception as exc:
            fail_card = MetricCard(
                test_name=f"{label} (Execution Error)",
                provider_used="None",
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
    return asyncio.run(run_all_checks())


if __name__ == "__main__":
    sys.exit(main())
