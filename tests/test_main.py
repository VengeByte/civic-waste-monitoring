"""
Unit tests for FastAPI application in backend/main.py.

Run with:
    pytest tests/test_main.py -v
"""

import base64
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
import sys
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.main import app
from backend.vlm_router import VLMResult


class TestMainAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        # Sample minimal 10x10 white JPEG bytes for testing
        cls.sample_image_bytes = (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"
            b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
            b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
            b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342"
            b"\xff\xc0\x00\x0b\x08\x00\n\x00\n\x01\x01\x11\x00\xff\xc4\x00\x1f"
            b"\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08"
            b"\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
        )

    def test_health_check_endpoint(self):
        """GET /api/health returns status healthy."""
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["service"], "civic-garbage-api")

    def test_cors_headers(self):
        """Ensure CORS middleware headers are present."""
        response = self.client.options(
            "/api/report",
            headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(response.headers.get("access-control-allow-origin"), ["*", "http://localhost:3000"])
        self.assertEqual(response.headers.get("access-control-allow-credentials"), "true")

    def test_report_empty_file_returns_400(self):
        """POST /api/report with empty file returns HTTP 400."""
        files = {"file": ("empty.jpg", b"", "image/jpeg")}
        data = {"lat": "30.7520", "lon": "76.7750"}
        response = self.client.post("/api/report", files=files, data=data)
        self.assertEqual(response.status_code, 400)
        self.assertIn("empty", response.json()["detail"].lower())

    @patch("backend.main.analyze_with_vlm")
    @patch("backend.main.resolve_authority")
    @patch("backend.main.blur_pii_in_image")
    def test_report_positive_garbage_detection_full_pipeline(
        self, mock_blur, mock_resolve_authority, mock_analyze_vlm
    ):
        """POST /api/report with garbage detected compiles full handoff draft."""
        # 1. Mock PII blur
        mock_blur.return_value = (self.sample_image_bytes, True)

        # 2. Mock VLM result with garbage detected
        mock_vlm_dict = {
            "garbage_detected": True,
            "confidence": 0.96,
            "waste_types": ["plastic bottles", "household refuse"],
            "severity": "high",
            "official_complaint_description": "Massive pile of uncollected garbage blocking pedestrian walkway.",
        }
        mock_analyze_vlm.return_value = VLMResult(mock_vlm_dict, provider="Google Gemini")

        # 3. Mock Authority result
        mock_resolve_authority.return_value = {
            "municipality_name": "Municipal Corporation Chandigarh",
            "ward_number": "12",
            "ward_name": "Sector 15 & 16",
            "sanitary_inspector_email": "sanitation.ward12@chd.nic.in",
            "moh_email": "moh-mcc@chd.nic.in",
            "commissioner_email": "commissioner-mcc@chd.nic.in",
            "helpline": "919220592205",
            "ward_matched": True,
            "latitude": 30.7520,
            "longitude": 76.7750,
            "resolved_address": "Sector 15, Chandigarh, 160015, India",
            "formatted_address": "Sector 15, Chandigarh, 160015, India",
        }

        files = {"file": ("garbage.jpg", self.sample_image_bytes, "image/jpeg")}
        form_data = {
            "lat": "30.7520",
            "lon": "76.7750",
            "captured_at": "2026-10-25 12:00:00 UTC",
        }

        response = self.client.post("/api/report", files=files, data=form_data)
        self.assertEqual(response.status_code, 200)

        res_json = response.json()
        self.assertEqual(res_json["status"], "success")
        self.assertTrue(res_json["garbage_detected"])
        self.assertTrue(res_json["pii_blurred"])
        self.assertIn("vlm_result", res_json)
        self.assertIn("authority", res_json)
        self.assertIn("sanitized_image_b64", res_json)

        # Verify draft structure
        draft = res_json["draft"]
        self.assertEqual(draft["to"], "sanitation.ward12@chd.nic.in")
        self.assertIn("moh-mcc@chd.nic.in", draft["cc"])
        self.assertIn("commissioner-mcc@chd.nic.in", draft["cc"])
        self.assertIn("Ward 12", draft["subject"])
        self.assertIn("Massive pile of uncollected garbage", draft["body"])
        self.assertIn("https://www.google.com/maps?q=30.752,76.775", draft["body"])

        # Verify draft contains only clean, structured fields.
        self.assertEqual(set(draft.keys()), {"to", "cc", "subject", "body", "evidence_url", "evidence_expires_at"})

    @patch("backend.main.analyze_with_vlm")
    @patch("backend.main.blur_pii_in_image")
    def test_report_negative_garbage_detection_early_exit(
        self, mock_blur, mock_analyze_vlm
    ):
        """POST /api/report when no garbage is detected exits early with message."""
        mock_blur.return_value = (self.sample_image_bytes, False)

        mock_vlm_dict = {
            "garbage_detected": False,
            "confidence": 0.99,
            "waste_types": [],
            "severity": "low",
            "official_complaint_description": "Clean street with zero litter.",
        }
        mock_analyze_vlm.return_value = VLMResult(mock_vlm_dict, provider="Google Gemini")

        files = {"file": ("clean_street.jpg", self.sample_image_bytes, "image/jpeg")}
        form_data = {"lat": "30.7520", "lon": "76.7750"}

        response = self.client.post("/api/report", files=files, data=form_data)
        self.assertEqual(response.status_code, 200)

        res_json = response.json()
        self.assertEqual(res_json["status"], "success")
        self.assertFalse(res_json["garbage_detected"])
        self.assertFalse(res_json["pii_blurred"])
        self.assertEqual(res_json["message"], "No uncollected civic waste detected.")
        self.assertIn("sanitized_image_b64", res_json)
        self.assertNotIn("draft", res_json)


    def test_privacy_policy_endpoint(self):
        """GET /privacy returns 200 with HTML content containing Limited Use disclosure."""
        response = self.client.get("/privacy")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers.get("content-type", ""))
        self.assertIn("Privacy Policy", response.text)
        self.assertIn("Google API Services User Data Policy", response.text)


if __name__ == "__main__":
    unittest.main()
