"""
Municipal Authority Routing and Spatial Deduplication Engine.

This module provides geographic authority resolution for municipal civic reporting:
1. Performs asynchronous reverse geocoding via OpenStreetMap Nominatim.
2. Evaluates point-in-polygon containment using Shapely against municipal ward GeoJSON boundaries.
3. Routes reports to designated sanitary inspectors, medical officers of health (MOH), and commissioners.
4. Provides spatial deduplication using the Haversine formula to detect duplicate incidents within 30 meters.
"""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from shapely.geometry import Point, shape

from backend.env_loader import load_env_file
from backend.municipal_directory import get_municipal_directory

# Configure logger
logger = logging.getLogger("backend.authority_router")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [authority_router]: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Default paths and API constants
DEFAULT_GEOJSON_PATH = Path(__file__).resolve().parent / "data" / "ward_directory.geojson"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_USER_AGENT = "GarbageCivicMonitor/2.0"
NOMINATIM_TIMEOUT_SECONDS = 5.0

# Central fallback authority when report coordinates fall outside all covered municipalities
CENTRAL_GRIEVANCE_EMAIL_DEFAULT = "grievance.central@chd.nic.in"


def central_grievance_email() -> str:
    """Resolve the central fallback email, allowing a deployment override."""
    load_env_file()
    return os.getenv("CENTRAL_GRIEVANCE_EMAIL", CENTRAL_GRIEVANCE_EMAIL_DEFAULT).strip()


CENTRAL_FALLBACK_AUTHORITY: dict[str, Any] = {
    "municipality_name": "Municipal Grievance Cell (Central)",
    "ward_number": "Unmapped",
    "ward_name": "Central Grievance Zone",
    "sanitary_inspector_email": "grievance.central@chd.nic.in",
    "moh_email": "",
    "commissioner_email": "",
    "whatsapp_helpline": "",
    "ward_matched": False,
}

# In-memory cached ward boundaries: list of (shapely_geometry, properties_dict)
_cached_wards: Optional[List[Tuple[Any, Dict[str, Any]]]] = None


def load_ward_directory(geojson_path: Optional[Path | str] = None) -> List[Tuple[Any, Dict[str, Any]]]:
    """
    Load and parse municipal ward polygons from a GeoJSON FeatureCollection.

    Args:
        geojson_path: Optional path to GeoJSON file. Defaults to backend/data/ward_directory.geojson.

    Returns:
        List of tuples: [(shapely_geometry, properties_dict), ...]

    Raises:
        FileNotFoundError: If the specified GeoJSON file does not exist.
        ValueError: If GeoJSON structure is invalid.
    """
    global _cached_wards

    path = Path(geojson_path) if geojson_path else DEFAULT_GEOJSON_PATH
    if not path.is_file():
        raise FileNotFoundError(f"Ward directory GeoJSON file not found at: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if data.get("type") != "FeatureCollection" or "features" not in data:
        raise ValueError("Invalid GeoJSON: Expected FeatureCollection with 'features' list.")

    wards: list[tuple[Any, dict[str, Any]]] = []
    for feature in data["features"]:
        geom = shape(feature["geometry"])
        props = feature.get("properties", {})
        wards.append((geom, props))

    _cached_wards = wards
    logger.info("Loaded %d municipal ward polygons from %s", len(wards), path.name)
    return _cached_wards


def get_cached_wards() -> List[Tuple[Any, Dict[str, Any]]]:
    """Retrieve preloaded ward polygons or load from default path."""
    global _cached_wards
    if _cached_wards is None:
        _cached_wards = load_ward_directory()
    return _cached_wards


async def lookup_address(
    lat: float,
    lon: float,
    client: Optional[httpx.AsyncClient] = None,
    timeout: float = NOMINATIM_TIMEOUT_SECONDS,
) -> str:
    """
    Asynchronously query OpenStreetMap Nominatim for human-readable reverse geocoded address.
    Falls back to formatted coordinates if Nominatim fails or times out.

    Args:
        lat: Latitude of location.
        lon: Longitude of location.
        client: Optional httpx.AsyncClient.
        timeout: Network timeout in seconds (default: 5.0s).

    Returns:
        Reverse geocoded display address or formatted coordinate fallback string.
    """
    fallback_address = f"Lat: {lat:.6f}, Lon: {lon:.6f}"
    url = f"{NOMINATIM_REVERSE_URL}?format=json&lat={lat}&lon={lon}&zoom=18"
    headers = {"User-Agent": NOMINATIM_USER_AGENT}

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=timeout)

    try:
        response = await http_client.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            display_name = data.get("display_name")
            if display_name and isinstance(display_name, str):
                return display_name.strip()
            return fallback_address
        else:
            logger.warning(
                "Nominatim reverse lookup returned HTTP %d. Falling back to coordinates.",
                response.status_code,
            )
            return fallback_address
    except Exception as exc:
        logger.warning("Nominatim reverse lookup failed or timed out (%s). Using coordinate fallback.", exc)
        return fallback_address
    finally:
        if owns_client:
            await http_client.aclose()


async def resolve_authority(
    lat: float,
    lon: float,
    client: Optional[httpx.AsyncClient] = None,
    geojson_path: Optional[Path | str] = None,
) -> dict[str, Any]:
    """
    Resolve municipal authority contacts and human-readable address for given coordinates.

    1. Reverse geocodes coordinates via OpenStreetMap Nominatim with a 5.0s timeout.
    2. Performs point-in-polygon containment against Chandigarh ward polygons.
    3. If no ward matches, checks the 30 covered countrywide municipal polygons.
    4. If no municipal corporation matches, returns the central grievance fallback so the
       complaint is still emailed to the central municipal address.

    Args:
        lat: Latitude (float).
        lon: Longitude (float).
        client: Optional httpx.AsyncClient.
        geojson_path: Optional custom GeoJSON path for ward directory.

    Returns:
        dict: Complete municipal authority details and resolved street address.
    """
    # 1. Reverse address lookup with 5.0s timeout and coordinate fallback
    resolved_address = await lookup_address(lat=lat, lon=lon, client=client)

    # 2. Point-in-polygon evaluation using Shapely (lon first as per GeoJSON specification)
    point = Point(float(lon), float(lat))
    wards = load_ward_directory(geojson_path) if geojson_path else get_cached_wards()

    matched_properties: Optional[dict[str, Any]] = None
    for geom, props in wards:
        if geom.contains(point):
            matched_properties = props
            break

    # 3. Matched Ward Result
    if matched_properties is not None:
        logger.info(
            "Point (%.5f, %.5f) matched Ward %s (%s).",
            lat,
            lon,
            matched_properties.get("ward_number"),
            matched_properties.get("ward_name"),
        )
        return {
            "municipality_name": matched_properties.get("municipality_name", "Municipal Corporation"),
            "ward_number": matched_properties.get("ward_number", "Unknown"),
            "ward_name": matched_properties.get("ward_name", "Unknown"),
            "sanitary_inspector_email": matched_properties.get("sanitary_inspector_email", ""),
            "moh_email": matched_properties.get("moh_email", ""),
            "commissioner_email": matched_properties.get("commissioner_email", ""),
            "whatsapp_helpline": matched_properties.get("whatsapp_helpline", ""),
            "helpline": matched_properties.get("whatsapp_helpline", ""),
            "ward_matched": True,
            "municipal_matched": False,
            "match_type": "ward",
            "boundary_quality": "ward",
            "confidence": "high",
            "latitude": float(lat),
            "longitude": float(lon),
            "resolved_address": resolved_address,
            "formatted_address": resolved_address,
        }

    # 3b. Countrywide municipal corporation lookup (30 covered polygons)
    municipal_directory = get_municipal_directory()
    municipal_match = municipal_directory.resolve(lat, lon)
    if municipal_match is not None:
        logger.info(
            "Point (%.5f, %.5f) matched municipal corporation %s (%s).",
            lat,
            lon,
            municipal_match.name,
            municipal_match.boundary_quality,
        )
        return municipal_directory.match_to_authority(
            municipal_match,
            lat=lat,
            lon=lon,
            address=resolved_address,
        )

    # 4. Safe Central Fallback Result (outside all covered polygons)
    logger.info(
        "Point (%.5f, %.5f) outside all covered municipalities. "
        "Assigning central grievance fallback.",
        lat,
        lon,
    )
    return {
        **CENTRAL_FALLBACK_AUTHORITY,
        "sanitary_inspector_email": central_grievance_email(),
        "ward_matched": False,
        "municipal_matched": False,
        "match_type": "central_fallback",
        "boundary_quality": "fallback",
        "confidence": "low",
        "latitude": float(lat),
        "longitude": float(lon),
        "resolved_address": resolved_address,
        "formatted_address": resolved_address,
    }


def haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great circle distance between two points on the Earth's surface
    in meters using the spherical Haversine formula.

    Args:
        lat1, lon1: Latitude and longitude of point 1 (in degrees).
        lat2, lon2: Latitude and longitude of point 2 (in degrees).

    Returns:
        Distance in meters (float).
    """
    earth_radius_meters = 6371000.0  # Earth's mean radius

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2)
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return earth_radius_meters * c


def is_duplicate_incident(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    threshold_meters: float = 30.0,
) -> bool:
    """
    Determine if two civic incident reports are spatial duplicates based on
    a proximity threshold in meters (default: 30.0 meters).

    Args:
        lat1, lon1: Coordinates of the first incident.
        lat2, lon2: Coordinates of the second incident.
        threshold_meters: Maximum distance in meters to consider incidents duplicates (default 30.0m).

    Returns:
        bool: True if distance is within threshold_meters, False otherwise.
    """
    distance = haversine_distance_meters(lat1, lon1, lat2, lon2)
    return distance <= threshold_meters
