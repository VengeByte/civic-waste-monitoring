"""
Automated test runner for backend/authority_router.py using pytest or standard asyncio.

Verifies:
1. Test 1: In-Bounds Ward Resolution (exact GeoJSON property matching)
2. Test 2: Out-of-Bounds Fallback (safe central fallback profile without exceptions)
3. Test 3: Nominatim Reverse Geocoding (formatted_address populated with valid string)
4. Test 4: Proximity Deduplication (Haversine 15m -> True, 100m -> False)

Outputs an executive summary table with:
- Test Name
- Resolved Ward / Distance
- Latency
- Status

Execution:
    pytest tests/test_router.py -v
    python tests/test_router.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import httpx
import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.authority_router import (
    CENTRAL_FALLBACK_AUTHORITY,
    DEFAULT_GEOJSON_PATH,
    haversine_distance_meters,
    is_duplicate_incident,
    load_ward_directory,
    lookup_address,
    resolve_authority,
)


@dataclass
class RouterMetricCard:
    test_name: str
    resolved_info: str
    latency_ms: float
    status: str
    details: dict[str, Any]
    error_message: Optional[str] = None


# ============================================================================
# Core Test Functions (Compatible with pytest and standard asyncio)
# ============================================================================

@pytest.mark.asyncio
async def test_in_bounds_ward_resolution() -> RouterMetricCard:
    """
    Test 1: In-Bounds Ward Resolution
    - Passes coordinates known to lie inside one of the GeoJSON polygons (Sector 15/16).
    - Asserts that resolved ward_number, sanitary_inspector_email, and municipality_name
      match the GeoJSON properties exactly.
    """
    # Load GeoJSON to obtain ground-truth properties for Ward 12
    with open(DEFAULT_GEOJSON_PATH, "r", encoding="utf-8") as f:
        geojson_data = json.load(f)

    target_feature = next(
        f for f in geojson_data["features"] if f["properties"].get("ward_number") == "12"
    )
    expected_props = target_feature["properties"]

    # Coordinates centered inside Ward 12: Sector 15 & 16
    lat, lon = 30.7520, 76.7750

    start_time = time.perf_counter()
    with patch("backend.authority_router.lookup_address", return_value="Sector 15, Chandigarh, India"):
        result = await resolve_authority(lat, lon)
    latency_ms = (time.perf_counter() - start_time) * 1000.0

    # Assertions
    assert result["ward_matched"] is True, "Expected ward_matched=True for in-bounds coordinates"
    assert result["ward_number"] == expected_props["ward_number"], (
        f"Ward number mismatch: {result.get('ward_number')} != {expected_props['ward_number']}"
    )
    assert result["sanitary_inspector_email"] == expected_props["sanitary_inspector_email"], (
        f"Email mismatch: {result.get('sanitary_inspector_email')} != {expected_props['sanitary_inspector_email']}"
    )
    assert result["municipality_name"] == expected_props["municipality_name"], (
        f"Municipality mismatch: {result.get('municipality_name')} != {expected_props['municipality_name']}"
    )

    resolved_desc = f"Ward {result['ward_number']} ({result['ward_name']})"

    return RouterMetricCard(
        test_name="Test 1: In-Bounds Ward Resolution",
        resolved_info=resolved_desc,
        latency_ms=latency_ms,
        status="PASSED",
        details={
            "ward_number": result["ward_number"],
            "ward_name": result["ward_name"],
            "sanitary_inspector_email": result["sanitary_inspector_email"],
            "municipality_name": result["municipality_name"],
            "whatsapp_helpline": result["whatsapp_helpline"],
        },
    )


@pytest.mark.asyncio
async def test_out_of_bounds_fallback() -> RouterMetricCard:
    """
    Test 2: Out-of-Bounds Fallback
    - Passes coordinates well outside all polygons (e.g., oceanic Gulf of Guinea / 0.0, 0.0).
    - Asserts that the function gracefully returns the central fallback profile without
      throwing an unhandled exception.
    """
    # Coordinates in the ocean (Null Island / Gulf of Guinea)
    oceanic_lat, oceanic_lon = 0.0, 0.0

    start_time = time.perf_counter()
    with patch("backend.authority_router.lookup_address", return_value="Gulf of Guinea, Atlantic Ocean"):
        result = await resolve_authority(oceanic_lat, oceanic_lon)
    latency_ms = (time.perf_counter() - start_time) * 1000.0

    # Assertions for central fallback profile
    assert result["ward_matched"] is False, "Expected ward_matched=False for out-of-bounds coordinates"
    assert result["municipality_name"] == "Municipal Grievance Cell (Central)", (
        f"Expected central fallback municipality, got {result.get('municipality_name')}"
    )
    assert result["ward_number"] == "Unmapped", f"Expected ward_number='Unmapped', got {result.get('ward_number')}"
    assert result["sanitary_inspector_email"] == CENTRAL_FALLBACK_AUTHORITY["sanitary_inspector_email"]
    assert result["moh_email"] == CENTRAL_FALLBACK_AUTHORITY["moh_email"]
    assert result["commissioner_email"] == CENTRAL_FALLBACK_AUTHORITY["commissioner_email"]
    assert result["whatsapp_helpline"] == CENTRAL_FALLBACK_AUTHORITY["whatsapp_helpline"]

    resolved_desc = "Unmapped (Central Fallback)"

    return RouterMetricCard(
        test_name="Test 2: Out-of-Bounds Fallback",
        resolved_info=resolved_desc,
        latency_ms=latency_ms,
        status="PASSED",
        details={
            "municipality_name": result["municipality_name"],
            "ward_number": result["ward_number"],
            "ward_matched": result["ward_matched"],
            "fallback_email": result["sanitary_inspector_email"],
        },
    )


@pytest.mark.asyncio
async def test_nominatim_reverse_geocoding() -> RouterMetricCard:
    """
    Test 3: Nominatim Reverse Geocoding
    - Verifies that formatted_address is populated with a valid non-empty string.
    """
    lat, lon = 30.7520, 76.7750
    mocked_address = "Sector 15-A, Chandigarh, 160015, Punjab & Haryana, India"

    def mock_nominatim_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("User-Agent") == "GarbageCivicMonitor/2.0"
        return httpx.Response(200, json={"display_name": mocked_address})

    start_time = time.perf_counter()
    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_nominatim_handler)) as client:
        result = await resolve_authority(lat, lon, client=client)
    latency_ms = (time.perf_counter() - start_time) * 1000.0

    # Assertions
    assert "formatted_address" in result, "formatted_address key must be present in result"
    formatted_addr = result["formatted_address"]
    assert isinstance(formatted_addr, str), f"formatted_address must be a string, got {type(formatted_addr)}"
    assert len(formatted_addr.strip()) > 0, "formatted_address must not be empty"
    assert formatted_addr == mocked_address, f"Address mismatch: '{formatted_addr}' != '{mocked_address}'"

    # Display short snippet in table
    display_info = (formatted_addr[:28] + "...") if len(formatted_addr) > 31 else formatted_addr

    return RouterMetricCard(
        test_name="Test 3: Nominatim Reverse Geocoding",
        resolved_info=display_info,
        latency_ms=latency_ms,
        status="PASSED",
        details={
            "formatted_address": formatted_addr,
            "address_length": len(formatted_addr),
            "user_agent_verified": "GarbageCivicMonitor/2.0",
        },
    )


def test_proximity_deduplication_haversine() -> RouterMetricCard:
    """
    Test 4: Proximity Deduplication (Haversine)
    - Tests two coordinates 15 meters apart (assert True).
    - Tests two coordinates 100 meters apart (assert False).
    """
    lat1, lon1 = 30.752000, 76.775000

    # 1 degree latitude = 111,139 meters
    lat_15m = lat1 + (15.0 / 111139.0)
    lat_100m = lat1 + (100.0 / 111139.0)

    start_time = time.perf_counter()
    dist_15m = haversine_distance_meters(lat1, lon1, lat_15m, lon1)
    is_dup_15m = is_duplicate_incident(lat1, lon1, lat_15m, lon1, threshold_meters=30.0)

    dist_100m = haversine_distance_meters(lat1, lon1, lat_100m, lon1)
    is_dup_100m = is_duplicate_incident(lat1, lon1, lat_100m, lon1, threshold_meters=30.0)
    latency_ms = (time.perf_counter() - start_time) * 1000.0

    # Assertions
    assert is_dup_15m is True, f"Coordinates {dist_15m:.1f}m apart must be flagged as duplicates (<= 30m)"
    assert is_dup_100m is False, f"Coordinates {dist_100m:.1f}m apart must NOT be flagged as duplicates (> 30m)"

    resolved_desc = f"{dist_15m:.1f}m (True) / {dist_100m:.1f}m (False)"

    return RouterMetricCard(
        test_name="Test 4: Proximity Deduplication",
        resolved_info=resolved_desc,
        latency_ms=latency_ms,
        status="PASSED",
        details={
            "calculated_15m_dist": f"{dist_15m:.2f} meters",
            "is_duplicate_15m": is_dup_15m,
            "calculated_100m_dist": f"{dist_100m:.2f} meters",
            "is_duplicate_100m": is_dup_100m,
            "threshold_meters": 30.0,
        },
    )


# ============================================================================
# Visual CLI Formatter and Summary Block
# ============================================================================

def display_metric_card(card: RouterMetricCard) -> None:
    """Print an individual test metric card."""
    border = "-" * 82
    print(f"\n{border}")
    status_tag = f"[{card.status}]"
    print(f" {card.test_name.upper():<68} {status_tag}")
    print(border)
    print(f"  * Resolved Info : {card.resolved_info}")
    print(f"  * Latency       : {card.latency_ms:.2f} ms")
    if card.error_message:
        print(f"  * Error         : {card.error_message}")
    print("  * Metric Details:")
    for k, v in card.details.items():
        print(f"      - {k:<26}: {v}")
    print(border)


def display_visual_summary_block(cards: list[RouterMetricCard]) -> None:
    """
    Print the final visual summary table displaying:
    - Test Name
    - Resolved Ward / Distance
    - Latency
    - Status
    """
    col_test = 37
    col_info = 33
    col_lat = 14
    col_stat = 10

    total_width = col_test + col_info + col_lat + col_stat + 9

    header_bar = "=" * total_width
    sub_bar = "-" * total_width

    print("\n" + header_bar)
    title = "AUTHORITY ROUTER TEST RUNNER: EXECUTIVE SUMMARY"
    print(f"{title:^{total_width}}")
    print(header_bar)
    header_line = (
        f" {'Test Name':<{col_test}} | "
        f"{'Resolved Ward / Distance':<{col_info}} | "
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
            f"{card.resolved_info:<{col_info}} | "
            f"{lat_str:<{col_lat}} | "
            f"{status_str:<{col_stat}}"
        )
        print(row)

    print(header_bar)
    summary_verdict = (
        "ALL 4 AUTHORITY ROUTER CHECKS PASSED [SUCCESS]"
        if all_passed
        else "SOME CHECKS FAILED [FAILURE]"
    )
    print(f" Verdict: {summary_verdict} (Total Latency: {total_latency:.2f} ms)")
    print(header_bar + "\n")


# ============================================================================
# Main Runner
# ============================================================================

async def run_all_checks() -> int:
    """Run all 4 authority router tests and render formatted metric cards & summary block."""
    tests = [
        ("Test 1", test_in_bounds_ward_resolution),
        ("Test 2", test_out_of_bounds_fallback),
        ("Test 3", test_nominatim_reverse_geocoding),
        ("Test 4", test_proximity_deduplication_haversine),
    ]

    cards: list[RouterMetricCard] = []
    print("\n>>> Starting Municipal Authority Router Test Suite...")

    for label, test_fn in tests:
        try:
            if inspect.iscoroutinefunction(test_fn):
                card = await test_fn()
            else:
                card = test_fn()
            cards.append(card)
            display_metric_card(card)
        except Exception as exc:
            fail_card = RouterMetricCard(
                test_name=f"{label} (Execution Error)",
                resolved_info="N/A",
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
