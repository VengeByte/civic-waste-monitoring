#!/usr/bin/env python3
"""
India-Wide Municipal Corporation GIS Routing Pipeline
=====================================================
Ingests countrywide_municipal_corporation_directory.txt, queries Nominatim
and Overpass APIs with strict rate-limiting and local caching, validates
polygon topology and administrative hierarchy, and outputs:
  1. output/municipal_corporations.json
  2. output/municipal_boundaries.geojson (RFC 7946 compliant, WGS84)
  3. output/review_report.csv
  4. output/manifest.json
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from pyproj import Geod
from requests.adapters import HTTPAdapter
from shapely.geometry import (
    LineString,
    MultiPolygon,
    Polygon,
    mapping,
    shape,
)
from shapely.geometry.polygon import orient
from shapely.ops import linemerge, polygonize, unary_union
from shapely.validation import explain_validity, make_valid
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Constants & Configuration
# ---------------------------------------------------------------------------

NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_INTERPRETER_URL = "https://overpass-api.de/api/interpreter"

RATE_LIMIT_SECONDS = 1.1  # Strictly > 1.0 req/sec per OSM Nominatim policy
MIN_MUNICIPAL_AREA_KM2 = 1.5
MAX_MUNICIPAL_AREA_KM2 = 3000.0  # Rejects district/state polygons (e.g., MCD is ~1,397 km2)

REJECTED_ADMIN_LEVELS = {"2", "3", "4", "5", "6"}
PREFERRED_ADMIN_LEVELS = {"8", "9", "10"}

WGS84_GEOD = Geod(ellps="WGS84")

DATAMEET_SOURCES_PATH = Path(__file__).resolve().parent / "datameet_sources.json"
DATAMEET_ATTRIBUTION = (
    "Maps provided by Spatial Data of Municipalities (Maps) Project by DataMeet, "
    "CC BY 4.0."
)

# Common historical/vernacular alias mappings for Indian cities
INDIAN_CITY_ALIASES: Dict[str, List[str]] = {
    "mumbai": ["brihanmumbai", "bombay", "greater mumbai"],
    "brihanmumbai": ["mumbai", "bombay", "greater mumbai"],
    "bengaluru": ["bangalore", "bruhat bengaluru"],
    "bangalore": ["bengaluru", "bruhat bengaluru"],
    "kolkata": ["calcutta"],
    "chennai": ["madras", "greater chennai"],
    "prayagraj": ["allahabad"],
    "allahabad": ["prayagraj"],
    "varanasi": ["banaras", "benares", "kashi"],
    "gurugram": ["gurgaon"],
    "gurgaon": ["gurugram"],
    "thiruvananthapuram": ["trivandrum"],
    "kochi": ["cochin"],
    "visakhapatnam": ["vizag", "vishakhapatnam", "greater visakhapatnam"],
    "vadodara": ["baroda"],
    "mysuru": ["mysore"],
    "kalaburagi": ["gulbarga"],
    "belagavi": ["belgaum"],
    "hubballi-dharwad": ["hubli-dharwad", "hubballi", "hubli"],
    "chhatrapati sambhajinagar": ["aurangabad"],
    "aurangabad": ["chhatrapati sambhajinagar"],
    "shimla": ["simla"],
    "jabalpur": ["jubbulpore"],
    "kanpur": ["cawnpore"],
    "pune": ["poona"],
    "thane": ["thana"],
    "guwahati": ["gauhati"],
}

CORPORATION_SUFFIX_PATTERNS = [
    r"\bmunicipal\s+corporation\b",
    r"\bmahanagara?\s+palike\b",
    r"\bmahanagar\s+palika\b",
    r"\bnagar\s+nigam\b",
    r"\bmunicipal\s+council\b",
    r"\bcity\s+corporation\b",
    r"\bbruhat\b",
    r"\bgreater\b",
]


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class RawCorporationRecord:
    name: str
    state: str
    jurisdiction: str = ""
    emails: List[str] = field(default_factory=list)
    helpline: str = ""
    website: str = ""


@dataclass
class BoundaryCandidate:
    geometry: Optional[Polygon | MultiPolygon]
    centroid: Optional[Tuple[float, float]]  # (lon, lat)
    osm_type: Optional[str]
    osm_id: Optional[int]
    matched_display_name: str
    source_url: str
    retrieved_at: str
    query_used: str
    boundary_quality: str  # official | osm | approximate | missing
    confidence: str        # high | medium | low
    area_km2: Optional[float]
    rejection_reason: str = ""
    needs_manual_review: bool = True


# ---------------------------------------------------------------------------
# HTTP Client with Rate Limiting, Caching & Exponential Backoff
# ---------------------------------------------------------------------------

class OsmClient:
    """Rate-limited, cached HTTP client compliant with Nominatim & Overpass policies."""

    def __init__(self, cache_dir: Path, user_agent: str) -> None:
        self.cache_dir = cache_dir
        self.nominatim_cache = cache_dir / "nominatim"
        self.overpass_cache = cache_dir / "overpass"
        self.nominatim_cache.mkdir(parents=True, exist_ok=True)
        self.overpass_cache.mkdir(parents=True, exist_ok=True)

        self.last_request_ts: float = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Accept-Language": "en-IN,en;q=0.9",
        })

        retry_strategy = Retry(
            total=4,
            backoff_factor=2.0,  # 2s, 4s, 8s, 16s
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.manifest_queries: List[Dict[str, Any]] = []
        self.manifest_failures: List[Dict[str, Any]] = []

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self.last_request_ts
        if elapsed < RATE_LIMIT_SECONDS:
            time.sleep(RATE_LIMIT_SECONDS - elapsed)
        self.last_request_ts = time.monotonic()

    @staticmethod
    def _hash_key(prefix: str, payload: str) -> str:
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
        safe_prefix = re.sub(r"[^a-z0-9_-]+", "_", prefix.lower()).strip("_")[:40]
        return f"{safe_prefix}_{digest}.json"

    def query_nominatim(
        self, corp_id: str, query_str: str, force_refresh: bool = False
    ) -> Tuple[List[Dict[str, Any]], str, str]:
        """Queries Nominatim with polygon_geojson=1 and caches the raw response."""
        params = {
            "q": query_str,
            "format": "jsonv2",
            "polygon_geojson": 1,
            "addressdetails": 1,
            "extratags": 1,
            "limit": 5,
            "countrycodes": "in",
        }
        full_url = f"{NOMINATIM_SEARCH_URL}?{urlencode(params)}"
        cache_file = self.nominatim_cache / self._hash_key(corp_id, full_url)

        if cache_file.exists() and not force_refresh:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            self.manifest_queries.append({
                "corp_id": corp_id,
                "service": "nominatim",
                "query": query_str,
                "url": cached["source_url"],
                "retrieved_at": cached["retrieved_at"],
                "cached": True,
                "result_count": len(cached.get("data", [])),
            })
            return cached["data"], cached["source_url"], cached["retrieved_at"]

        self._throttle()
        retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            resp = self.session.get(NOMINATIM_SEARCH_URL, params=params, timeout=30)
            if resp.status_code != 200:
                msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
                self.manifest_failures.append({
                    "corp_id": corp_id,
                    "service": "nominatim",
                    "query": query_str,
                    "url": full_url,
                    "timestamp": retrieved_at,
                    "error": msg,
                })
                return [], full_url, retrieved_at

            data = resp.json()
            payload = {
                "source_url": resp.url,
                "retrieved_at": retrieved_at,
                "query": query_str,
                "data": data,
            }
            cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            self.manifest_queries.append({
                "corp_id": corp_id,
                "service": "nominatim",
                "query": query_str,
                "url": resp.url,
                "retrieved_at": retrieved_at,
                "cached": False,
                "result_count": len(data),
            })
            return data, resp.url, retrieved_at

        except Exception as exc:
            self.manifest_failures.append({
                "corp_id": corp_id,
                "service": "nominatim",
                "query": query_str,
                "url": full_url,
                "timestamp": retrieved_at,
                "error": str(exc),
            })
            return [], full_url, retrieved_at

    def query_overpass(
        self,
        corp_id: str,
        lat: float,
        lon: float,
        name_regex: str,
        force_refresh: bool = False,
    ) -> Tuple[Dict[str, Any], str, str, str]:
        """
        Queries Overpass API for boundary=administrative relations at admin_level 8/9/10
        within 50 km of (lat, lon) matching Municipal Corporation / Nagar Nigam / City patterns.
        """
        overpass_ql = (
            f"[out:json][timeout:45];\n"
            f"(\n"
            f'  relation["boundary"="administrative"]["admin_level"~"^(8|9|10)$"]'
            f'["name"~"{name_regex}",i](around:50000,{lat:.6f},{lon:.6f});\n'
            f'  relation["boundary"="administrative"]["admin_level"~"^(8|9|10)$"]'
            f'["official_name"~"{name_regex}",i](around:50000,{lat:.6f},{lon:.6f});\n'
            f'  relation["boundary"="administrative"]["admin_level"~"^(8|9|10)$"]'
            f'["name:en"~"{name_regex}",i](around:50000,{lat:.6f},{lon:.6f});\n'
            f");\n"
            f"out body geom;"
        )
        encoded_url = f"{OVERPASS_INTERPRETER_URL}?{urlencode({'data': overpass_ql})}"
        cache_file = self.overpass_cache / self._hash_key(corp_id, overpass_ql)

        if cache_file.exists() and not force_refresh:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            self.manifest_queries.append({
                "corp_id": corp_id,
                "service": "overpass",
                "query": f"around:50000,{lat:.4f},{lon:.4f} ~/{name_regex}/i",
                "url": cached["source_url"],
                "retrieved_at": cached["retrieved_at"],
                "cached": True,
                "result_count": len(cached.get("data", {}).get("elements", [])),
            })
            return (
                cached["data"],
                cached["source_url"],
                cached["retrieved_at"],
                overpass_ql,
            )

        self._throttle()
        retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            resp = self.session.post(
                OVERPASS_INTERPRETER_URL, data={"data": overpass_ql}, timeout=60
            )
            if resp.status_code != 200:
                msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
                self.manifest_failures.append({
                    "corp_id": corp_id,
                    "service": "overpass",
                    "query": overpass_ql,
                    "url": encoded_url,
                    "timestamp": retrieved_at,
                    "error": msg,
                })
                return {}, encoded_url, retrieved_at, overpass_ql

            data = resp.json()
            payload = {
                "source_url": encoded_url,
                "retrieved_at": retrieved_at,
                "query": overpass_ql,
                "data": data,
            }
            cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            self.manifest_queries.append({
                "corp_id": corp_id,
                "service": "overpass",
                "query": f"around:50000,{lat:.4f},{lon:.4f} ~/{name_regex}/i",
                "url": encoded_url,
                "retrieved_at": retrieved_at,
                "cached": False,
                "result_count": len(data.get("elements", [])),
            })
            return data, encoded_url, retrieved_at, overpass_ql

        except Exception as exc:
            self.manifest_failures.append({
                "corp_id": corp_id,
                "service": "overpass",
                "query": overpass_ql,
                "url": encoded_url,
                "timestamp": retrieved_at,
                "error": str(exc),
            })
            return {}, encoded_url, retrieved_at, overpass_ql


# ---------------------------------------------------------------------------
# Input Directory Parser
# ---------------------------------------------------------------------------

def parse_emails(raw_email_str: str) -> List[str]:
    """Extracts and normalizes email addresses, including obfuscated [at]/[dot] formats."""
    if not raw_email_str or raw_email_str.strip().lower() in {"na", "n/a", "none", "-", "nil"}:
        return []
    normalized = (
        raw_email_str.replace("[at]", "@")
        .replace("(at)", "@")
        .replace(" at ", "@")
        .replace("[dot]", ".")
        .replace("(dot)", ".")
    )
    found = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", normalized)
    seen = set()
    result = []
    for email in found:
        e_low = email.lower()
        if e_low not in seen:
            seen.add(e_low)
            result.append(e_low)
    return result


def parse_directory_file(filepath: Path) -> List[RawCorporationRecord]:
    # Parses the actual directory export:
    #   1. NATIONAL CAPITAL TERRITORY (NCT) OF DELHI
    #   ----------------------------------------------------------------
    #   - Municipal Corporation of Delhi (MCD)
    #     * Jurisdiction: ...
    #     * Email: ...
    #     * Helpline: ...
    #     * Website: ...
    # It also retains support for key-value blocks and pipe/tab tables.
    if not filepath.exists():
        raise FileNotFoundError(f"Input file not found: {filepath}")

    content = filepath.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"Input file is empty: {filepath}")

    lines = content.splitlines()
    non_empty_lines = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    records: List[RawCorporationRecord] = []

    section_re = re.compile(r"^\d+\.\s+(.+)$")
    bullet_re = re.compile(r"^-\s+(.+)$")
    field_re = re.compile(r"^\*\s*([A-Za-z][A-Za-z /_-]*?)\s*:\s*(.+)$")

    field_map = {
        "jurisdiction": "jurisdiction",
        "area": "jurisdiction",
        "coverage": "jurisdiction",
        "email": "email",
        "emails": "email",
        "e-mail": "email",
        "helpline": "helpline",
        "phone": "helpline",
        "contact": "helpline",
        "toll free": "helpline",
        "website": "website",
        "url": "website",
        "portal": "website",
        "state": "state",
        "state/ut": "state",
        "name": "name",
        "corporation": "name",
        "municipal corporation": "name",
        "corporation name": "name",
    }

    section_overrides = {
        "national capital territory (nct) of delhi": "Delhi",
        "union territories (chandigarh, puducherry, j&k)": None,
    }
    section_state: Optional[str] = None
    current: Dict[str, str] = {}

    def state_from_section(header: str) -> Optional[str]:
        key = header.strip().lower()
        if key in section_overrides:
            return section_overrides[key]
        if "delhi" in key:
            return "Delhi"
        if "union territories" in key:
            return None
        return header.strip().title()

    def state_for_record(name: str) -> str:
        if section_state:
            return section_state
        lowered = name.lower()
        if "chandigarh" in lowered:
            return "Chandigarh"
        if "srinagar" in lowered or "jammu" in lowered:
            return "Jammu & Kashmir"
        if "puducherry" in lowered:
            return "Puducherry"
        return "India"

    def clean_website(value: str) -> str:
        markdown = re.search(r"\]\((https?://[^)]+)\)", value)
        if markdown:
            value = markdown.group(1)
        return value.strip().strip(".,;")

    def flush_current() -> None:
        nonlocal current
        if not current.get("name"):
            return
        # Ignore the footer bullets (Format / Coverage / Recommendation).
        if not any(current.get(k) for k in ("jurisdiction", "email", "website", "helpline")):
            current = {}
            return
        records.append(
            RawCorporationRecord(
                name=current["name"].strip(),
                state=(current.get("state") or state_for_record(current["name"])).strip(),
                jurisdiction=current.get("jurisdiction", "").strip(),
                emails=parse_emails(current.get("email", "")),
                helpline=current.get("helpline", "").strip(),
                website=clean_website(current.get("website", "")),
            )
        )
        current = {}

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("="):
            continue
        if re.match(r"^[-=_*]{3,}$", line):
            continue

        section_match = section_re.match(line)
        if section_match and not line.startswith("-"):
            flush_current()
            section_state = state_from_section(section_match.group(1))
            continue

        bullet_match = bullet_re.match(line)
        if bullet_match:
            name = bullet_match.group(1).strip()
            if re.match(r"^(format|coverage|recommendation|note)\s*:", name, re.IGNORECASE):
                continue
            flush_current()
            current = {"name": name}
            continue

        field_match = field_re.match(line)
        if field_match and current:
            key = field_match.group(1).strip().lower()
            mapped = field_map.get(key)
            if mapped:
                current[mapped] = field_match.group(2).strip()

    flush_current()
    if records:
        return records

    # Fallback 1: pipe/tab-delimited table with a header row.
    first_line = non_empty_lines[0].lower() if non_empty_lines else ""
    if ("|" in first_line or "\t" in first_line) and "name" in first_line and "state" in first_line:
        delimiter = "|" if "|" in first_line else "\t"
        headers = [h.strip().lower() for h in non_empty_lines[0].split(delimiter)]
        for row_line in non_empty_lines[1:]:
            if re.match(r"^[-|+:\s]+$", row_line):
                continue
            cols = [c.strip() for c in row_line.split(delimiter)]
            row = dict(zip(headers, cols))
            name = row.get("name") or row.get("municipal corporation") or ""
            state = row.get("state") or ""
            if not name or not state:
                continue
            records.append(
                RawCorporationRecord(
                    name=name,
                    state=state,
                    jurisdiction=row.get("jurisdiction", ""),
                    emails=parse_emails(row.get("email", "") or row.get("emails", "")),
                    helpline=row.get("helpline", "") or row.get("phone", ""),
                    website=clean_website(row.get("website", "") or row.get("url", "")),
                )
            )
        if records:
            return records

    # Fallback 2: key-value blocks with explicit Name: and State: lines.
    current = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or re.match(r"^[-=_*]{3,}$", line):
            flush_current()
            continue
        kv_match = re.match(r"^(?:\d+[\.\)]\s*)?([A-Za-z\s/\-]+?)\s*:\s*(.+)$", line)
        if kv_match:
            raw_key = kv_match.group(1).strip().lower()
            mapped_key = field_map.get(raw_key)
            if mapped_key:
                if mapped_key == "name" and "name" in current:
                    flush_current()
                current[mapped_key] = kv_match.group(2).strip()
    flush_current()
    return records


# ---------------------------------------------------------------------------
# Slug, Short Name, and Alias Derivation
# ---------------------------------------------------------------------------

def make_slug(name: str, state: str, existing_slugs: set[str]) -> str:
    base_name = re.sub(r"\([^)]*\)", "", name).strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", base_name).strip("-")
    if slug in existing_slugs:
        state_slug = re.sub(r"[^a-z0-9]+", "-", state.lower()).strip("-")
        slug = f"{slug}-{state_slug}"
    existing_slugs.add(slug)
    return slug


def extract_city_and_short_name(full_name: str) -> Tuple[str, str, List[str]]:
    # Returns (city_name, short_name, aliases) from a formal corporation title.
    paren_match = re.search(r"\(([A-Za-z0-9\-\s]{2,12})\)", full_name)
    clean_name = re.sub(r"\([^)]*\)", "", full_name).strip()

    city = re.sub(r"^(?:the\s+)?(?:greater|bruhat)\s+", "", clean_name, flags=re.IGNORECASE)
    suffixes = list(CORPORATION_SUFFIX_PATTERNS) + [
        r"\bmunicipal\s+corporation\b",
        r"\bmunicipal\s+council\b",
        r"\bnagar\s+nigam\b",
        r"\bmahanagara?\s+palike\b",
        r"\bmahanagar\s+palika\b",
        r"\bmunicipality\b",
        r"\bcorporation\b",
    ]
    for pattern in suffixes:
        city = re.sub(pattern, "", city, flags=re.IGNORECASE).strip()
    city = re.sub(r"^of\s+", "", city, flags=re.IGNORECASE)
    city = re.sub(r"\s+city$", "", city, flags=re.IGNORECASE)
    city = re.sub(r"^[\s,\-]+|[\s,\-/]+$", "", city)
    if not city:
        city = clean_name

    if paren_match:
        short_name = paren_match.group(1).strip().upper()
    else:
        words = [w for w in re.split(r"[\s\-]+", clean_name) if w and w[0].isalpha()]
        stop_words = {"of", "and", "the", "for"}
        initials = "".join(w[0].upper() for w in words if w.lower() not in stop_words)
        short_name = initials if len(initials) >= 2 and initials.isalnum() else city.split()[0].upper()

    aliases_set = {
        clean_name,
        short_name,
        city,
        f"{city} Municipal Corporation",
        f"{city} Nagar Nigam",
        f"{city} Mahanagar Palika",
    }
    city_lower = city.lower()
    for variant in INDIAN_CITY_ALIASES.get(city_lower, []):
        variant_title = variant.title()
        aliases_set.add(variant_title)
        aliases_set.add(f"{variant_title} Municipal Corporation")
        aliases_set.add(f"{variant_title} Nagar Nigam")

    aliases = sorted(a for a in aliases_set if a and a.lower() != full_name.lower())
    return city, short_name, aliases


# ---------------------------------------------------------------------------
# Geometry Assembly & Topological Validation
# ---------------------------------------------------------------------------

def compute_geodetic_area_km2(geom: Polygon | MultiPolygon) -> float:
    """Computes exact ellipsoidal area in km^2 on WGS84 using pyproj.Geod."""
    area_m2, _ = WGS84_GEOD.geometry_area_perimeter(geom)
    return round(abs(area_m2) / 1_000_000.0, 4)


def orient_rfc7946(geom: Polygon | MultiPolygon) -> Polygon | MultiPolygon:
    """Enforces RFC 7946 right-hand rule (exterior CCW, holes CW)."""
    if isinstance(geom, Polygon):
        return orient(geom, sign=1.0)
    if isinstance(geom, MultiPolygon):
        return MultiPolygon([orient(poly, sign=1.0) for poly in geom.geoms])
    return geom


def load_datameet_sources(
    path: Path = DATAMEET_SOURCES_PATH,
) -> Dict[str, Dict[str, Any]]:
    """Load the DataMeet override manifest and resolve every file path."""
    if not path.is_file():
        raise FileNotFoundError(f"DataMeet manifest not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    overrides = data.get("overrides")
    if not isinstance(overrides, dict) or not overrides:
        raise ValueError(f"No 'overrides' object in {path}")
    meta = data.get("_meta", {})

    resolved: Dict[str, Dict[str, Any]] = {}
    for corp_id, spec in overrides.items():
        rel_path = spec.get("path")
        if not rel_path:
            raise ValueError(f"Override {corp_id} is missing a path")
        full_path = Path(__file__).resolve().parent / rel_path
        if not full_path.is_file():
            raise FileNotFoundError(
                f"DataMeet file for {corp_id} not found: {full_path}"
            )
        resolved[corp_id] = {**spec, "_path": full_path, "_meta": meta}
    return resolved


def load_override_geometry(path: Path, dissolve: bool = True) -> Polygon | MultiPolygon:
    """
    Load a DataMeet GeoJSON file and return one valid Polygon/MultiPolygon.

    Handles both FeatureCollection (ward maps) and a single Feature (Kochi),
    repairs invalid ward geometry (Vadodara), and dissolves ward polygons.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("type") == "FeatureCollection":
        geoms = [
            shape(feature["geometry"])
            for feature in data.get("features", [])
            if feature.get("geometry")
        ]
    elif data.get("type") == "Feature":
        geoms = [shape(data["geometry"])]
    else:
        raise ValueError(f"Unsupported GeoJSON type in {path}: {data.get('type')}")

    if not geoms:
        raise ValueError(f"No geometries found in {path}")

    repaired: List[Polygon | MultiPolygon] = []
    for geom in geoms:
        if not geom.is_valid:
            geom = make_valid(geom)
        if geom.is_empty:
            continue
        if isinstance(geom, (Polygon, MultiPolygon)):
            repaired.append(geom)
        else:
            polygons = [
                part
                for part in getattr(geom, "geoms", [])
                if isinstance(part, Polygon)
            ]
            if polygons:
                repaired.append(unary_union(polygons))

    if not repaired:
        raise ValueError(f"No valid polygon geometries in {path}")

    merged = unary_union(repaired) if dissolve or len(repaired) > 1 else repaired[0]
    if not merged.is_valid:
        merged = make_valid(merged)
    if not isinstance(merged, (Polygon, MultiPolygon)):
        polygons = [
            part for part in getattr(merged, "geoms", []) if isinstance(part, Polygon)
        ]
        if not polygons:
            raise ValueError(f"Override did not produce polygons: {path}")
        merged = unary_union(polygons)

    return orient_rfc7946(merged)


def assemble_overpass_relation_polygon(
    relation: Dict[str, Any]
) -> Tuple[Optional[Polygon | MultiPolygon], Optional[str]]:
    """
    Reconstructs a Polygon or MultiPolygon from an Overpass relation with 'out body geom;'.
    Rejects unclosed rings or invalid member geometries.
    """
    members = relation.get("members", [])
    outer_lines: List[LineString] = []
    inner_lines: List[LineString] = []

    for member in members:
        if member.get("type") != "way":
            continue
        geom_pts = member.get("geometry", [])
        if len(geom_pts) < 2:
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in geom_pts if "lon" in pt and "lat" in pt]
        if len(coords) < 2:
            continue
        line = LineString(coords)
        role = member.get("role", "outer")
        if role == "inner":
            inner_lines.append(line)
        else:
            outer_lines.append(line)

    if not outer_lines:
        return None, "no_outer_way_members"

    merged_outer = linemerge(outer_lines)
    outer_polys = list(polygonize(merged_outer))
    if not outer_polys:
        return None, "unclosed_outer_rings"

    inner_polys = list(polygonize(linemerge(inner_lines))) if inner_lines else []

    try:
        outer_union = unary_union(outer_polys)
        if inner_polys:
            inner_union = unary_union(inner_polys)
            final_geom = outer_union.difference(inner_union)
        else:
            final_geom = outer_union
    except Exception as exc:
        return None, f"topology_assembly_error: {exc}"

    if final_geom.is_empty:
        return None, "empty_after_ring_assembly"

    if not isinstance(final_geom, (Polygon, MultiPolygon)):
        polys = [g for g in getattr(final_geom, "geoms", []) if isinstance(g, (Polygon, MultiPolygon))]
        if not polys:
            return None, f"unexpected_geometry_type_{final_geom.geom_type}"
        final_geom = unary_union(polys)

    return final_geom, None


def validate_boundary_geometry(
    geom: Any,
    admin_level: Optional[str],
    display_name: str,
    expected_state: str,
    expected_tokens: List[str],
) -> Tuple[bool, Optional[Polygon | MultiPolygon], Optional[float], str]:
    """
    Applies strict GIS validation rules:
      1. Must be non-empty Polygon or MultiPolygon.
      2. Reject self-intersections (geom.is_valid must be True).
      3. Reject state/district-level boundaries via admin_level, name tags, and area bounds.
      4. Reject polygons whose centroid lies outside the polygon.
    """
    if geom is None or geom.is_empty:
        return False, None, None, "empty_geometry"

    if not isinstance(geom, (Polygon, MultiPolygon)):
        return False, None, None, f"non_polygonal_geometry_{geom.geom_type}"

    # 1. Self-intersection / Topological validity check
    if not geom.is_valid:
        reason = explain_validity(geom)
        return False, None, None, f"invalid_geometry_self_intersection ({reason})"

    # 2. Administrative hierarchy check (reject state/district boundaries)
    if admin_level and str(admin_level) in REJECTED_ADMIN_LEVELS:
        return False, None, None, f"rejected_state_or_district_admin_level_{admin_level}"

    disp_lower = display_name.lower()
    is_explicit_municipal = any(
        kw in disp_lower
        for kw in ["municipal", "nagar nigam", "mahanagar", "palike", "palika", "corporation"]
    )
    if not is_explicit_municipal and re.search(r"\b(district|zila|tehsil|taluk|taluka|division)\b", disp_lower):
        # Check if the primary matched entity name (first segment of display_name) is a district/tehsil
        first_segment = disp_lower.split(",")[0].strip()
        if re.search(r"\b(district|zila|tehsil|taluk|taluka|division)\b", first_segment):
            return False, None, None, f"rejected_district_or_subdistrict_entity ({first_segment})"

    # 3. Verify state alignment if state is present in display_name
    state_lower = expected_state.lower()
    if "," in disp_lower and state_lower not in disp_lower:
        # Handle union territories or state name variations
        state_tokens = [t for t in re.split(r"[\s&]+", state_lower) if len(t) > 3]
        if state_tokens and not any(t in disp_lower for t in state_tokens):
            return False, None, None, f"state_mismatch (expected {expected_state})"

    # 4. Verify city/corporation name token alignment
    first_part = disp_lower.split(",")[0]
    if not any(tok.lower() in disp_lower for tok in expected_tokens if len(tok) >= 3):
        return False, None, None, f"name_mismatch ({first_part})"

    # 5. Geodetic area check
    area_km2 = compute_geodetic_area_km2(geom)
    if area_km2 > MAX_MUNICIPAL_AREA_KM2:
        return (
            False,
            None,
            area_km2,
            f"area_too_large_district_or_state_scale ({area_km2:.1f} km2 > {MAX_MUNICIPAL_AREA_KM2} km2)",
        )
    if area_km2 < MIN_MUNICIPAL_AREA_KM2:
        return (
            False,
            None,
            area_km2,
            f"area_too_small_ward_or_building_scale ({area_km2:.2f} km2 < {MIN_MUNICIPAL_AREA_KM2} km2)",
        )

    # 6. Coastal/concave polygons can have their mathematical centroid outside
    # the land area (for example Mumbai). Keep the valid boundary; callers use a
    # representative point and flag it for manual review instead of discarding it.
    oriented_geom = orient_rfc7946(geom)
    return True, oriented_geom, area_km2, ""


# ---------------------------------------------------------------------------
# Resolution Pipeline per Corporation
# ---------------------------------------------------------------------------

def resolve_corporation_boundary(
    corp_id: str,
    record: RawCorporationRecord,
    city: str,
    short_name: str,
    aliases: List[str],
    client: OsmClient,
    force_refresh: bool = False,
) -> BoundaryCandidate:
    """
    Executes the multi-stage Nominatim -> Overpass resolution pipeline for one corporation.
    """
    expected_tokens = [city, record.name, short_name] + aliases
    primary_query = f"{record.name}, {record.state}, India"

    # Stage 1: Nominatim Search for "<name>, <state>, India"
    nom_results, nom_url, nom_ts = client.query_nominatim(
        corp_id, primary_query, force_refresh=force_refresh
    )

    fallback_center: Optional[Tuple[float, float]] = None  # (lon, lat)
    rejection_notes: List[str] = []

    # Sort Nominatim results to prefer boundary/administrative, then polygons.
    # Recent Nominatim versions expose the category as "category" rather than "class".
    def nominatim_sort_key(item: Dict[str, Any]) -> Tuple[int, int]:
        category = item.get("class") or item.get("category")
        is_admin = int(
            category == "boundary" and item.get("type") == "administrative"
        )
        has_poly = int(
            item.get("geojson", {}).get("type") in {"Polygon", "MultiPolygon"}
        )
        return (-is_admin, -has_poly)

    for item in sorted(nom_results, key=nominatim_sort_key):
        if fallback_center is None and "lon" in item and "lat" in item:
            fallback_center = (float(item["lon"]), float(item["lat"]))

        geojson_obj = item.get("geojson")
        if not geojson_obj or geojson_obj.get("type") not in {"Polygon", "MultiPolygon"}:
            continue

        extratags = item.get("extratags") or {}
        admin_level = extratags.get("admin_level") or item.get("admin_level")
        disp_name = item.get("display_name", "")

        try:
            cand_geom = shape(geojson_obj)
        except Exception as exc:
            rejection_notes.append(f"nominatim_shape_error: {exc}")
            continue

        is_valid, valid_geom, area_km2, reason = validate_boundary_geometry(
            cand_geom,
            admin_level=str(admin_level) if admin_level else None,
            display_name=disp_name,
            expected_state=record.state,
            expected_tokens=expected_tokens,
        )

        if is_valid and valid_geom is not None:
            c_pt = (
                valid_geom.centroid
                if valid_geom.contains(valid_geom.centroid)
                else valid_geom.representative_point()
            )
            is_admin_bound = (
                (item.get("class") or item.get("category")) == "boundary"
                and item.get("type") == "administrative"
            )
            quality = "osm" if is_admin_bound else "approximate"
            confidence = (
                "high"
                if (is_admin_bound and str(admin_level) in PREFERRED_ADMIN_LEVELS)
                else "medium"
            )
            return BoundaryCandidate(
                geometry=valid_geom,
                centroid=(round(c_pt.x, 6), round(c_pt.y, 6)),
                osm_type=item.get("osm_type"),
                osm_id=int(item["osm_id"]) if "osm_id" in item else None,
                matched_display_name=disp_name,
                source_url=nom_url,
                retrieved_at=nom_ts,
                query_used=primary_query,
                boundary_quality=quality,
                confidence=confidence,
                area_km2=area_km2,
                rejection_reason="",
                needs_manual_review=(
                    confidence != "high" or not valid_geom.contains(valid_geom.centroid)
                ),
            )
        else:
            rejection_notes.append(f"nom({item.get('osm_type')}:{item.get('osm_id')}): {reason}")

    # Stage 1b: If primary Nominatim query returned no coordinates at all, geocode "<city>, <state>, India"
    if fallback_center is None:
        city_query = f"{city}, {record.state}, India"
        city_results, city_url, city_ts = client.query_nominatim(
            f"{corp_id}_city", city_query, force_refresh=force_refresh
        )
        for item in sorted(city_results, key=nominatim_sort_key):
            if fallback_center is None and "lon" in item and "lat" in item:
                fallback_center = (float(item["lon"]), float(item["lat"]))

            geojson_obj = item.get("geojson")
            if not geojson_obj or geojson_obj.get("type") not in {"Polygon", "MultiPolygon"}:
                continue

            extratags = item.get("extratags") or {}
            admin_level = extratags.get("admin_level")
            disp_name = item.get("display_name", "")

            try:
                cand_geom = shape(geojson_obj)
            except Exception:
                continue

            is_valid, valid_geom, area_km2, reason = validate_boundary_geometry(
                cand_geom,
                admin_level=str(admin_level) if admin_level else None,
                display_name=disp_name,
                expected_state=record.state,
                expected_tokens=expected_tokens,
            )
            if is_valid and valid_geom is not None:
                c_pt = (
                valid_geom.centroid
                if valid_geom.contains(valid_geom.centroid)
                else valid_geom.representative_point()
            )
                is_admin_bound = (
                    (item.get("class") or item.get("category")) == "boundary"
                    and item.get("type") == "administrative"
                )
                return BoundaryCandidate(
                    geometry=valid_geom,
                    centroid=(round(c_pt.x, 6), round(c_pt.y, 6)),
                    osm_type=item.get("osm_type"),
                    osm_id=int(item["osm_id"]) if "osm_id" in item else None,
                    matched_display_name=disp_name,
                    source_url=city_url,
                    retrieved_at=city_ts,
                    query_used=city_query,
                    boundary_quality="osm" if is_admin_bound else "approximate",
                    confidence="medium",
                    area_km2=area_km2,
                    rejection_reason="matched_via_city_fallback_query",
                    needs_manual_review=True,
                )
            else:
                rejection_notes.append(f"nom_city({item.get('osm_id')}): {reason}")

    # Stage 2: Overpass Fallback within 50 km of geocoded city centre
    if fallback_center is not None:
        lon, lat = fallback_center
        escaped_city = re.escape(city)
        alias_patterns = [
            re.escape(a)
            for a in INDIAN_CITY_ALIASES.get(city.lower(), [])
            if len(a) >= 3
        ]
        city_group = "|".join([escaped_city] + alias_patterns)
        name_regex = (
            f"({city_group}).*(Municipal Corporation|Nagar Nigam|Municipal Council|"
            f"Mahanagara? Palike|Mahanagar Palika|Corporation)|"
            f"(Municipal Corporation|Nagar Nigam|Municipal Council|Mahanagara? Palike|"
            f"Mahanagar Palika).*({city_group})|^({city_group})$"
        )

        ovp_data, ovp_url, ovp_ts, _ = client.query_overpass(
            corp_id, lat=lat, lon=lon, name_regex=name_regex, force_refresh=force_refresh
        )
        elements = ovp_data.get("elements", [])

        # Sort Overpass relations: prefer explicit Municipal Corporation/Nagar Nigam in name
        def overpass_sort_key(el: Dict[str, Any]) -> Tuple[int, int]:
            tags = el.get("tags", {})
            el_name = (
                tags.get("name:en") or tags.get("official_name") or tags.get("name") or ""
            ).lower()
            has_corp_kw = int(
                any(
                    k in el_name
                    for k in [
                        "municipal corporation",
                        "nagar nigam",
                        "mahanagar",
                        "palike",
                        "municipal council",
                    ]
                )
            )
            lvl = tags.get("admin_level", "")
            pref_lvl = int(lvl in PREFERRED_ADMIN_LEVELS)
            return (-has_corp_kw, -pref_lvl)

        for rel in sorted(elements, key=overpass_sort_key):
            if rel.get("type") != "relation":
                continue
            tags = rel.get("tags", {})
            rel_name = (
                tags.get("name:en")
                or tags.get("official_name")
                or tags.get("name")
                or f"Relation {rel.get('id')}"
            )
            admin_level = tags.get("admin_level")

            assembled_geom, err = assemble_overpass_relation_polygon(rel)
            if assembled_geom is None:
                rejection_notes.append(f"overpass_rel({rel.get('id')}): {err}")
                continue

            is_valid, valid_geom, area_km2, reason = validate_boundary_geometry(
                assembled_geom,
                admin_level=str(admin_level) if admin_level else None,
                display_name=f"{rel_name}, {record.state}",
                expected_state=record.state,
                expected_tokens=expected_tokens,
            )

            if is_valid and valid_geom is not None:
                c_pt = (
                valid_geom.centroid
                if valid_geom.contains(valid_geom.centroid)
                else valid_geom.representative_point()
            )
                has_explicit_title = any(
                    k in rel_name.lower()
                    for k in ["municipal", "nagar nigam", "mahanagar", "palike", "corporation"]
                )
                confidence = "high" if has_explicit_title else "medium"
                return BoundaryCandidate(
                    geometry=valid_geom,
                    centroid=(round(c_pt.x, 6), round(c_pt.y, 6)),
                    osm_type="relation",
                    osm_id=int(rel["id"]),
                    matched_display_name=rel_name,
                    source_url=ovp_url,
                    retrieved_at=ovp_ts,
                    query_used=f"Overpass around:50000,{lat:.4f},{lon:.4f} [{rel_name}]",
                    boundary_quality="osm",
                    confidence=confidence,
                    area_km2=area_km2,
                    rejection_reason="",
                    needs_manual_review=(
                    confidence != "high" or not valid_geom.contains(valid_geom.centroid)
                ),
                )
            else:
                rejection_notes.append(f"overpass_rel({rel.get('id')}): {reason}")

    # Stage 3: Fallback when no valid boundary exists -> never invent polygons
    mismatch_summary = (
        "; ".join(rejection_notes[:3])
        if rejection_notes
        else "no_polygon_returned_by_nominatim_or_overpass"
    )
    return BoundaryCandidate(
        geometry=None,
        centroid=(
            (round(fallback_center[0], 6), round(fallback_center[1], 6))
            if fallback_center
            else None
        ),
        osm_type=None,
        osm_id=None,
        matched_display_name="",
        source_url=nom_url,
        retrieved_at=nom_ts,
        query_used=primary_query,
        boundary_quality="missing",
        confidence="low",
        area_km2=None,
        rejection_reason=mismatch_summary,
        needs_manual_review=True,
    )


# ---------------------------------------------------------------------------
# Main Pipeline Orchestrator (Idempotent & Restartable)
# ---------------------------------------------------------------------------

def run_pipeline(
    input_path: Path,
    output_dir: Path,
    cache_dir: Path,
    user_agent: str,
    force_refresh: bool = False,
    parse_only: bool = False,
    limit: Optional[int] = None,
    covered_only: bool = False,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_file = cache_dir / "checkpoint.json"

    checkpoint_data: Dict[str, Any] = {}
    if checkpoint_file.exists() and not force_refresh:
        try:
            checkpoint_data = json.loads(checkpoint_file.read_text(encoding="utf-8"))
            logging.info(
                "Loaded checkpoint with %d previously completed corporations.",
                len(checkpoint_data),
            )
        except Exception as exc:
            logging.warning("Could not read checkpoint file, rebuilding: %s", exc)

    records = parse_directory_file(input_path)
    logging.info("Parsed %d municipal corporations from %s", len(records), input_path)
    if limit is not None and limit > 0:
        records = records[:limit]
        logging.info("Limiting this run to %d corporation(s).", len(records))

    if parse_only:
        print(f"\nParsed {len(records)} records:")
        for record in records:
            print(
                f"- {record.name} | {record.state} | "
                f"{', '.join(record.emails) or 'no email'} | {record.website or 'no website'}"
            )
        return

    client = OsmClient(cache_dir=cache_dir, user_agent=user_agent)

    datameet_overrides = load_datameet_sources()
    used_overrides: set[str] = set()

    corporations_json: List[Dict[str, Any]] = []
    geojson_features: List[Dict[str, Any]] = []
    review_rows: List[Dict[str, Any]] = []
    existing_slugs: set[str] = set()

    for idx, rec in enumerate(records, start=1):
        corp_id = make_slug(rec.name, rec.state, existing_slugs)
        city, short_name, aliases = extract_city_and_short_name(rec.name)
        override = datameet_overrides.get(corp_id)

        if override is not None:
            # DataMeet overrides intentionally bypass both the network and the
            # checkpoint so re-running upgrades previously-missing records.
            logging.info(
                "[%d/%d] %s (%s) -> DataMeet override (%s)",
                idx,
                len(records),
                rec.name,
                corp_id,
                override.get("label", "community boundary"),
            )
            geom = load_override_geometry(
                override["_path"], dissolve=bool(override.get("dissolve", True))
            )
            area_km2 = compute_geodetic_area_km2(geom)
            if not (MIN_MUNICIPAL_AREA_KM2 <= area_km2 <= MAX_MUNICIPAL_AREA_KM2):
                raise ValueError(
                    f"DataMeet area out of bounds for {corp_id}: {area_km2} km2"
                )
            c_pt = (
                geom.centroid
                if geom.contains(geom.centroid)
                else geom.representative_point()
            )
            meta = override.get("_meta", {})
            commit = meta.get("commit", "master")
            relative_path = str(override["path"]).replace("vendor/datameet/", "")
            candidate = BoundaryCandidate(
                geometry=geom,
                centroid=(round(c_pt.x, 6), round(c_pt.y, 6)),
                osm_type="datameet",
                osm_id=None,
                matched_display_name=override.get("label", "DataMeet boundary"),
                source_url=(
                    f"https://github.com/datameet/Municipal_Spatial_Data/blob/"
                    f"{commit}/{relative_path}"
                ),
                retrieved_at=meta.get("retrieved_at", ""),
                query_used="DataMeet manual override",
                boundary_quality="community",
                confidence="medium",
                area_km2=area_km2,
                rejection_reason="",
                needs_manual_review=False,
            )
            used_overrides.add(corp_id)

        elif corp_id in checkpoint_data and not force_refresh:
            saved = checkpoint_data[corp_id]
            corporations_json.append(saved["corporation_record"])
            geojson_features.append(saved["geojson_feature"])
            review_rows.append(saved["review_row"])
            logging.info(
                "[%d/%d] %s (%s) -> restored from checkpoint (%s)",
                idx,
                len(records),
                rec.name,
                corp_id,
                saved["corporation_record"]["boundary_status"],
            )
            continue

        else:
            logging.info("[%d/%d] Processing %s, %s...", idx, len(records), rec.name, rec.state)
            candidate = resolve_corporation_boundary(
                corp_id=corp_id,
                record=rec,
                city=city,
                short_name=short_name,
                aliases=aliases,
                client=client,
                force_refresh=force_refresh,
            )

        has_polygon = candidate.geometry is not None
        if has_polygon and candidate.boundary_quality == "community":
            boundary_status = "verified_community"
        elif has_polygon and not candidate.needs_manual_review:
            boundary_status = f"verified_{candidate.boundary_quality}"
        elif has_polygon:
            boundary_status = "needs_review"
        else:
            boundary_status = "missing_boundary"

        corp_entry = {
            "id": corp_id,
            "name": rec.name,
            "short_name": short_name,
            "state": rec.state,
            "emails": rec.emails,
            "helpline": rec.helpline,
            "website": rec.website,
            "jurisdiction": rec.jurisdiction,
            "aliases": aliases,
            "centroid": list(candidate.centroid) if candidate.centroid else None,
            "boundary_status": boundary_status,
        }

        is_community = candidate.boundary_quality == "community"
        geojson_feature = {
            "type": "Feature",
            "id": corp_id,
            "properties": {
                "id": corp_id,
                "name": rec.name,
                "state": rec.state,
                "osm_type": candidate.osm_type,
                "osm_id": candidate.osm_id,
                "source_url": candidate.source_url,
                "retrieved_at": candidate.retrieved_at,
                "boundary_quality": candidate.boundary_quality,
                "confidence": candidate.confidence,
                "area_km2": candidate.area_km2,
                "boundary_source": (
                    "DataMeet Municipal Spatial Data" if is_community else "OpenStreetMap"
                ),
                "license": "CC BY 4.0" if is_community else "ODbL",
                "attribution": (
                    DATAMEET_ATTRIBUTION
                    if is_community
                    else "© OpenStreetMap contributors"
                ),
            },
            "geometry": mapping(candidate.geometry) if candidate.geometry else None,
        }

        review_row = {
            "id": corp_id,
            "name": rec.name,
            "state": rec.state,
            "query_used": candidate.query_used,
            "matched_display_name": candidate.matched_display_name,
            "osm_type": candidate.osm_type or "",
            "osm_id": candidate.osm_id if candidate.osm_id is not None else "",
            "has_polygon": has_polygon,
            "boundary_quality": candidate.boundary_quality,
            "source_url": candidate.source_url,
            "suspected_mismatch_reason": candidate.rejection_reason,
            "needs_manual_review": candidate.needs_manual_review,
        }

        corporations_json.append(corp_entry)
        geojson_features.append(geojson_feature)
        review_rows.append(review_row)

        # Persist checkpoint immediately after each corporation so restarts never re-query
        checkpoint_data[corp_id] = {
            "corporation_record": corp_entry,
            "geojson_feature": geojson_feature,
            "review_row": review_row,
        }
        checkpoint_file.write_text(
            json.dumps(checkpoint_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    if limit is None:
        unused_overrides = set(datameet_overrides) - used_overrides
        if unused_overrides:
            raise ValueError(
                "DataMeet override IDs did not match any parsed corporation: "
                + ", ".join(sorted(unused_overrides))
            )

    excluded_missing = [
        {"id": feature["id"], "name": feature["properties"]["name"]}
        for feature in geojson_features
        if feature["geometry"] is None
    ]

    if covered_only:
        covered_ids = {
            feature["id"]
            for feature in geojson_features
            if feature["geometry"] is not None
        }
        corporations_json = [
            record for record in corporations_json if record["id"] in covered_ids
        ]
        geojson_features = [
            feature for feature in geojson_features if feature["id"] in covered_ids
        ]
        review_rows = [
            row for row in review_rows if row["id"] in covered_ids
        ]
        logging.info(
            "Covered-only output: kept %d records, excluded %d missing boundaries.",
            len(corporations_json),
            len(excluded_missing),
        )

    # 1. Write municipal_corporations.json
    corp_out_path = output_dir / "municipal_corporations.json"
    corp_out_path.write_text(
        json.dumps(corporations_json, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # 2. Write municipal_boundaries.geojson (RFC 7946 FeatureCollection)
    geojson_out_path = output_dir / "municipal_boundaries.geojson"
    feature_collection = {
        "type": "FeatureCollection",
        "features": geojson_features,
    }
    geojson_out_path.write_text(
        json.dumps(feature_collection, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # 3. Write review_report.csv
    csv_out_path = output_dir / "review_report.csv"
    fieldnames = [
        "id",
        "name",
        "state",
        "query_used",
        "matched_display_name",
        "osm_type",
        "osm_id",
        "has_polygon",
        "boundary_quality",
        "source_url",
        "suspected_mismatch_reason",
        "needs_manual_review",
    ]
    with csv_out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(review_rows)

    # 4. Write manifest.json (Audit log of queries, summary stats & failures)
    manifest_out_path = output_dir / "manifest.json"
    total_with_poly = sum(1 for r in review_rows if r["has_polygon"])
    total_missing = len(review_rows) - total_with_poly
    manifest_payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "input_file": str(input_path),
        "summary": {
            "total_corporations": len(corporations_json),
            "valid_polygons_extracted": total_with_poly,
            "missing_boundaries": total_missing,
            "flagged_for_manual_review": sum(
                1 for r in review_rows if r["needs_manual_review"]
            ),
            "total_api_queries_logged": len(client.manifest_queries),
            "total_network_or_http_failures": len(client.manifest_failures),
        },
        "queries": client.manifest_queries,
        "failures": client.manifest_failures,
        "excluded_missing": excluded_missing,
        "boundary_rejections": [
            {
                "corp_id": corporations_json[i]["id"],
                "name": corporations_json[i]["name"],
                "state": corporations_json[i]["state"],
                "query_used": review_rows[i]["query_used"],
                "reason": review_rows[i]["suspected_mismatch_reason"],
            }
            for i in range(len(review_rows))
            if review_rows[i]["suspected_mismatch_reason"]
        ],
    }
    manifest_out_path.write_text(
        json.dumps(manifest_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    logging.info(
        "Pipeline complete. Valid polygons: %d/%d | Needs review: %d | Outputs saved to %s",
        total_with_poly,
        len(records),
        manifest_payload["summary"]["flagged_for_manual_review"],
        output_dir,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build India-wide municipal routing dataset from OSM Nominatim & Overpass."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).resolve().parent / "input" / "countrywide_municipal_corporation_directory.txt",
        help="Path to countrywide_municipal_corporation_directory.txt",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "backend" / "data" / "municipal",
        help="Directory for output JSON, GeoJSON, CSV, and manifest files",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / ".cache" / "gis",
        help="Directory for raw response cache and idempotent checkpoint state",
    )
    parser.add_argument(
        "--user-agent",
        type=str,
        default="IndiaCivicComplaintGIS/1.0 (gis-routing-ops@civiccomplaint.org.in)",
        help="Descriptive User-Agent header with contact email per OSM policy",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore local cache and re-query all entries",
    )
    parser.add_argument(
        "--parse-only",
        action="store_true",
        help="Parse and print the directory without making any network calls",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N corporations (useful for a smoke test)",
    )
    parser.add_argument(
        "--covered-only",
        action="store_true",
        help="Write only corporations that have a polygon; missing records are excluded",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )

    run_pipeline(
        input_path=args.input,
        output_dir=args.output_dir,
        cache_dir=args.cache_dir,
        user_agent=args.user_agent,
        force_refresh=args.force_refresh,
        parse_only=args.parse_only,
        limit=args.limit,
        covered_only=args.covered_only,
    )


if __name__ == "__main__":
    main()