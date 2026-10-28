"""
Countrywide municipal-corporation lookup.

Loads the 30 covered records from ``backend/data/municipal`` and resolves a pin with a
Shapely STRtree. Records without geometry are not in the covered dataset; a pin outside
every polygon is handled by the central grievance fallback in ``authority_router``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from shapely.geometry import Point, shape
from shapely.strtree import STRtree

logger = logging.getLogger("backend.municipal_directory")

DATA_DIR = Path(__file__).resolve().parent / "data" / "municipal"
CORPORATIONS_PATH = DATA_DIR / "municipal_corporations.json"
BOUNDARIES_PATH = DATA_DIR / "municipal_boundaries.geojson"


@dataclass(frozen=True)
class MunicipalContact:
    """A covered municipal corporation and its boundary metadata."""

    id: str
    name: str
    state: str
    emails: tuple[str, ...]
    helpline: str
    website: str
    jurisdiction: str
    boundary_quality: str
    confidence: str
    area_km2: float

    @property
    def primary_email(self) -> str:
        return self.emails[0] if self.emails else ""


class MunicipalDirectory:
    """Point-in-polygon resolver over the covered municipal boundaries."""

    def __init__(
        self,
        corporations_path: Path = CORPORATIONS_PATH,
        boundaries_path: Path = BOUNDARIES_PATH,
    ) -> None:
        self._geometries: list[Any] = []
        self._entries: list[MunicipalContact] = []
        self._tree: Optional[STRtree] = None

        if not corporations_path.is_file() or not boundaries_path.is_file():
            logger.warning(
                "Municipal dataset not found (%s / %s); countrywide routing disabled.",
                corporations_path,
                boundaries_path,
            )
            return

        contacts_raw = json.loads(corporations_path.read_text(encoding="utf-8"))
        raw_by_id = {record["id"]: record for record in contacts_raw}

        boundaries_raw = json.loads(boundaries_path.read_text(encoding="utf-8"))
        for feature in boundaries_raw.get("features", []):
            if not feature.get("geometry"):
                continue
            properties = feature.get("properties", {})
            record = raw_by_id.get(properties.get("id", ""))
            if record is None:
                continue
            try:
                geometry = shape(feature["geometry"])
            except Exception:
                logger.warning("Skipping invalid geometry for %s", record["id"])
                continue
            if geometry.is_empty:
                continue

            contact = MunicipalContact(
                id=record["id"],
                name=record["name"],
                state=record["state"],
                emails=tuple(record.get("emails", [])),
                helpline=record.get("helpline", ""),
                website=record.get("website", ""),
                jurisdiction=record.get("jurisdiction", ""),
                boundary_quality=properties.get("boundary_quality", ""),
                confidence=properties.get("confidence", ""),
                area_km2=float(properties.get("area_km2", 0.0) or 0.0),
            )
            self._geometries.append(geometry)
            self._entries.append(contact)

        if self._geometries:
            self._tree = STRtree(self._geometries)
            logger.info(
                "Loaded %d municipal corporation polygons from %s",
                len(self._geometries),
                boundaries_path.name,
            )

    @property
    def count(self) -> int:
        return len(self._entries)

    def resolve(self, lat: float, lon: float) -> Optional[MunicipalContact]:
        """Return the smallest covered corporation containing the pin, if any."""
        if self._tree is None:
            return None

        point = Point(float(lon), float(lat))
        candidates = self._tree.query(point)
        matches: list[MunicipalContact] = []
        for index in candidates:
            if self._geometries[int(index)].covers(point):
                matches.append(self._entries[int(index)])

        if not matches:
            return None

        # Overlaps exist (for example Pune/PCMC); prefer the smaller, more specific body.
        return min(matches, key=lambda contact: contact.area_km2 or float("inf"))

    def match_to_authority(
        self,
        contact: MunicipalContact,
        lat: float,
        lon: float,
        address: str,
    ) -> dict[str, Any]:
        """Convert a municipal match into the authority dictionary used by main.py."""
        return {
            "municipality_name": contact.name,
            "ward_number": "N/A",
            "ward_name": f"{contact.name}, {contact.state}",
            "sanitary_inspector_email": contact.primary_email,
            "moh_email": "",
            "commissioner_email": "",
            "whatsapp_helpline": "",
            "website": contact.website,
            "helpline": contact.helpline,
            "ward_matched": False,
            "municipal_matched": True,
            "match_type": "municipal_polygon",
            "boundary_quality": contact.boundary_quality,
            "confidence": contact.confidence,
            "area_km2": contact.area_km2,
            "jurisdiction": contact.jurisdiction,
            "latitude": float(lat),
            "longitude": float(lon),
            "resolved_address": address,
            "formatted_address": address,
        }


_DIRECTORY: Optional[MunicipalDirectory] = None


def get_municipal_directory() -> MunicipalDirectory:
    """Load the countrywide directory once per process."""
    global _DIRECTORY
    if _DIRECTORY is None:
        try:
            _DIRECTORY = MunicipalDirectory()
        except Exception:
            logger.exception("Failed to load the municipal directory; routing disabled.")
            _DIRECTORY = MunicipalDirectory(
                corporations_path=Path("__missing__"),
                boundaries_path=Path("__missing__"),
            )
    return _DIRECTORY
