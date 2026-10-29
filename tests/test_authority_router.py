"""
Unit tests for backend/authority_router.py municipal routing engine.

Run with:
    pytest tests/test_authority_router.py -v
    python -m unittest tests/test_authority_router.py
"""

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
import sys
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.authority_router import (
    DEFAULT_GEOJSON_PATH,
    haversine_distance_meters,
    is_duplicate_incident,
    load_ward_directory,
    lookup_address,
    resolve_authority,
)


class TestAuthorityRouter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.geojson_path = DEFAULT_GEOJSON_PATH

    def test_geojson_feature_collection_structure(self):
        """Verify ward_directory.geojson contains at least two wards with required properties."""
        self.assertTrue(self.geojson_path.is_file(), f"GeoJSON missing: {self.geojson_path}")

        with open(self.geojson_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data.get("type"), "FeatureCollection")
        features = data.get("features", [])
        self.assertGreaterEqual(len(features), 2, "Must contain at least 2 distinct ward polygons.")

        required_properties = {
            "municipality_name",
            "ward_number",
            "ward_name",
            "sanitary_inspector_email",
            "moh_email",
            "commissioner_email",
            "helpline",
        }

        for feature in features:
            self.assertEqual(feature.get("type"), "Feature")
            self.assertEqual(feature.get("geometry", {}).get("type"), "Polygon")
            props = feature.get("properties", {})
            for req in required_properties:
                self.assertIn(req, props, f"Missing property '{req}' in feature.")
                self.assertTrue(bool(props[req]), f"Empty property '{req}'.")

    def test_point_in_polygon_matching_ward_12(self):
        """Verify coordinate in Sector 15/16 resolves to Ward 12."""
        # Sector 15 & 16 center: ~30.752 N, 76.775 E
        lat, lon = 30.7520, 76.7750

        async def _run():
            # Mock Nominatim lookup to test containment in isolation
            with patch("backend.authority_router.lookup_address", return_value="Sector 15, Chandigarh, India"):
                result = await resolve_authority(lat, lon)
                self.assertTrue(result["ward_matched"])
                self.assertEqual(result["ward_number"], "12")
                self.assertEqual(result["ward_name"], "Sector 15 & 16")
                self.assertEqual(result["sanitary_inspector_email"], "sanitation.ward12@chd.nic.in")
                self.assertEqual(result["helpline"], "919220592205")
                self.assertEqual(result["resolved_address"], "Sector 15, Chandigarh, India")
                self.assertEqual(result["latitude"], lat)
                self.assertEqual(result["longitude"], lon)

        asyncio.run(_run())

    def test_point_in_polygon_matching_ward_13(self):
        """Verify coordinate in Sector 17/22 resolves to Ward 13."""
        # Sector 17 & 22 center: ~30.738 N, 76.775 E
        lat, lon = 30.7380, 76.7750

        async def _run():
            with patch("backend.authority_router.lookup_address", return_value="Sector 17 Plaza, Chandigarh"):
                result = await resolve_authority(lat, lon)
                self.assertTrue(result["ward_matched"])
                self.assertEqual(result["ward_number"], "13")
                self.assertEqual(result["ward_name"], "Sector 17 & 22")
                self.assertEqual(result["sanitary_inspector_email"], "sanitation.ward13@chd.nic.in")

        asyncio.run(_run())

    def test_unmapped_fallback_outside_polygons(self):
        """Verify a coordinate outside every covered municipality uses central fallback."""
        # Null Island is outside all 30 covered municipal polygons.
        lat, lon = 0.0, 0.0

        async def _run():
            with patch("backend.authority_router.lookup_address", return_value="Gulf of Guinea"):
                result = await resolve_authority(lat, lon)
                self.assertFalse(result["ward_matched"])
                self.assertFalse(result["municipal_matched"])
                self.assertEqual(result["match_type"], "central_fallback")
                self.assertEqual(result["municipality_name"], "Municipal Grievance Cell (Central)")
                self.assertEqual(result["ward_number"], "Unmapped")
                self.assertEqual(result["ward_name"], "Central Grievance Zone")
                self.assertEqual(result["sanitary_inspector_email"], "grievance.central@chd.nic.in")
                self.assertEqual(result["moh_email"], "")
                self.assertEqual(result["helpline"], "")
                self.assertEqual(result["resolved_address"], "Gulf of Guinea")

        asyncio.run(_run())

    def test_nominatim_address_lookup_success(self):
        """Verify lookup_address queries Nominatim with proper headers and extracts display_name."""
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers.get("User-Agent"), "GarbageCivicMonitor/2.0")
            self.assertIn("format=json", str(request.url))
            self.assertIn("lat=30.75", str(request.url))
            return httpx.Response(200, json={"display_name": "Sector 15, Chandigarh, 160015, India"})

        async def _run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                address = await lookup_address(30.7500, 76.7700, client=client)
                self.assertEqual(address, "Sector 15, Chandigarh, 160015, India")

        asyncio.run(_run())

    def test_nominatim_address_lookup_timeout_fallback(self):
        """Verify lookup_address safely falls back to formatted coordinates on network error."""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("Connection timed out after 5.0s")

        async def _run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                address = await lookup_address(30.7500, 76.7700, client=client)
                self.assertEqual(address, "Lat: 30.750000, Lon: 76.770000")

        asyncio.run(_run())

    def test_haversine_distance_and_duplicate_incident(self):
        """Test Haversine distance and 30-meter proximity grouping."""
        lat1, lon1 = 30.75200, 76.77500

        # Exact same point: 0 meters -> duplicate
        self.assertTrue(is_duplicate_incident(lat1, lon1, lat1, lon1, threshold_meters=30.0))
        self.assertEqual(haversine_distance_meters(lat1, lon1, lat1, lon1), 0.0)

        # 0.0001 deg latitude shift is ~11.12 meters -> duplicate (<= 30m)
        lat2_near = 30.75210
        dist_near = haversine_distance_meters(lat1, lon1, lat2_near, lon1)
        self.assertAlmostEqual(dist_near, 11.12, delta=0.5)
        self.assertTrue(is_duplicate_incident(lat1, lon1, lat2_near, lon1, threshold_meters=30.0))

        # 0.0005 deg latitude shift is ~55.60 meters -> NOT duplicate (> 30m)
        lat3_far = 30.75250
        dist_far = haversine_distance_meters(lat1, lon1, lat3_far, lon1)
        self.assertAlmostEqual(dist_far, 55.60, delta=0.5)
        self.assertFalse(is_duplicate_incident(lat1, lon1, lat3_far, lon1, threshold_meters=30.0))

        # Custom threshold check: 55.6m is duplicate under 60m threshold
        self.assertTrue(is_duplicate_incident(lat1, lon1, lat3_far, lon1, threshold_meters=60.0))


if __name__ == "__main__":
    unittest.main()
