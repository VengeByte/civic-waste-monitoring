"""Tests for countrywide municipal lookup, ward precedence, and central fallback."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.authority_router import resolve_authority
from backend.main import app, compile_complaint_letter
from backend.municipal_directory import get_municipal_directory
from backend.vlm_router import VLMResult
from tests.test_privacy import generate_synthetic_pavement_image

client = TestClient(app)
IMAGE = generate_synthetic_pavement_image(256, 256)
POSITIVE_VLM = {
    "garbage_detected": True,
    "confidence": 0.96,
    "waste_types": ["plastic bags"],
    "severity": "high",
    "official_complaint_description": "Refuse is blocking the pavement.",
}


def _report(lat: float, lon: float) -> dict:
    with patch(
        "backend.main.analyze_with_vlm",
        return_value=VLMResult(dict(POSITIVE_VLM), provider="Google Gemini"),
    ), patch(
        "backend.authority_router.lookup_address",
        return_value="Test address, India",
    ):
        response = client.post(
            "/api/report",
            files={"file": ("waste.jpg", IMAGE, "image/jpeg")},
            data={"lat": str(lat), "lon": str(lon)},
        )
    assert response.status_code == 200, response.text
    return response.json()


def test_directory_loads_30_covered_corporations():
    directory = get_municipal_directory()
    assert directory.count == 30


def test_mumbai_resolves_to_bmc():
    directory = get_municipal_directory()
    match = directory.resolve(19.0760, 72.8777)
    assert match is not None
    assert match.name == "Brihanmumbai Municipal Corporation (BMC)"
    assert match.primary_email == "ccp@mcgm.gov.in"
    assert match.boundary_quality == "community"


def test_pune_and_pcmc_are_distinguished():
    directory = get_municipal_directory()
    pune = directory.resolve(18.5204, 73.8567)
    pcmc = directory.resolve(18.6298, 73.7997)
    assert pune is not None and "Pune Municipal Corporation" in pune.name
    assert pcmc is not None and "Pimpri Chinchwad" in pcmc.name


def test_chandigarh_ward_still_wins_inside_ward():
    async def _run():
        with patch("backend.authority_router.lookup_address", return_value="Sector 15"):
            return await resolve_authority(30.7520, 76.7750)

    result = asyncio.run(_run())
    assert result["match_type"] == "ward"
    assert result["ward_number"] == "12"


def test_chandigarh_municipal_match_outside_wards():
    async def _run():
        with patch("backend.authority_router.lookup_address", return_value="Chandigarh"):
            return await resolve_authority(30.7400, 76.7900)

    result = asyncio.run(_run())
    assert result["match_type"] == "municipal_polygon"
    assert result["municipality_name"] == "Chandigarh Municipal Corporation"
    assert result["sanitary_inspector_email"] == "mc.chandigarh@nic.in"


def test_uncovered_city_uses_central_fallback():
    async def _run():
        with patch("backend.authority_router.lookup_address", return_value="Surat"):
            return await resolve_authority(21.1702, 72.8311)

    result = asyncio.run(_run())
    assert result["match_type"] == "central_fallback"
    assert result["municipality_name"] == "Municipal Grievance Cell (Central)"
    assert result["sanitary_inspector_email"] == "grievance.central@chd.nic.in"


def test_municipal_complaint_uses_commissioner_not_ward():
    authority = {
        "municipality_name": "Brihanmumbai Municipal Corporation (BMC)",
        "ward_number": "N/A",
        "ward_name": "Brihanmumbai Municipal Corporation (BMC), Maharashtra",
        "sanitary_inspector_email": "ccp@mcgm.gov.in",
        "moh_email": "",
        "commissioner_email": "",
        "whatsapp_helpline": "",
        "website": "https://portal.mcgm.gov.in",
        "boundary_quality": "community",
        "match_type": "municipal_polygon",
        "formatted_address": "Mumbai",
    }
    subject, body, to_email, cc = compile_complaint_letter(
        authority, POSITIVE_VLM, 19.076, 72.8777
    )

    assert "Ward N/A" not in subject
    assert "Brihanmumbai" in subject
    assert to_email == "ccp@mcgm.gov.in"
    assert cc == []
    assert "The Municipal Commissioner" in body
    assert "Municipal Portal" in body
    assert "https://portal.mcgm.gov.in" in body


def test_central_complaint_explains_fallback():
    authority = {
        "municipality_name": "Municipal Grievance Cell (Central)",
        "ward_number": "Unmapped",
        "ward_name": "Central Grievance Zone",
        "sanitary_inspector_email": "grievance.central@chd.nic.in",
        "moh_email": "",
        "commissioner_email": "",
        "whatsapp_helpline": "",
        "match_type": "central_fallback",
        "formatted_address": "Surat",
    }
    subject, body, to_email, cc = compile_complaint_letter(
        authority, POSITIVE_VLM, 21.1702, 72.8311
    )

    assert "Central Grievance Cell" in subject
    assert to_email == "grievance.central@chd.nic.in"
    assert cc == []
    assert "could not be mapped to a specific municipal corporation" in body
    assert "Central municipal fallback" in body


def test_mumbai_report_routes_to_bmc_email():
    body = _report(19.0760, 72.8777)
    authority = body["authority"]
    draft = body["draft"]

    assert authority["match_type"] == "municipal_polygon"
    assert authority["sanitary_inspector_email"] == "ccp@mcgm.gov.in"
    assert draft["to"] == "ccp@mcgm.gov.in"
    assert "whatsapp_url" not in draft
    assert "mailto_url" not in draft
    assert "Brihanmumbai" in draft["subject"]
    assert "Ward N/A" not in draft["subject"]


def test_surat_report_routes_to_central_email():
    body = _report(21.1702, 72.8311)
    authority = body["authority"]
    draft = body["draft"]

    assert authority["match_type"] == "central_fallback"
    assert draft["to"] == "grievance.central@chd.nic.in"
    assert "could not be mapped to a specific municipal corporation" in draft["body"]
