"""Tests for the DataMeet override layer in the offline GIS pipeline."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("requests")
pytest.importorskip("pyproj")
pytest.importorskip("shapely")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_PATH = PROJECT_ROOT / "scripts" / "gis" / "build_municipal_dataset.py"
INPUT_PATH = (
    PROJECT_ROOT
    / "scripts"
    / "gis"
    / "input"
    / "countrywide_municipal_corporation_directory.txt"
)
DATA_DIR = PROJECT_ROOT / "backend" / "data" / "municipal"


def load_pipeline():
    spec = importlib.util.spec_from_file_location("gis_pipeline", PIPELINE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["gis_pipeline"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pipeline():
    return load_pipeline()


@pytest.fixture(scope="module")
def records(pipeline):
    return pipeline.parse_directory_file(INPUT_PATH)


@pytest.fixture(scope="module")
def overrides(pipeline):
    return pipeline.load_datameet_sources()


def test_directory_parses_all_54(pipeline, records):
    assert len(records) == 54
    assert records[0].name.startswith("Municipal Corporation of Delhi")
    assert records[-1].name.startswith("Puducherry")


def test_override_ids_match_parsed_corporations(pipeline, records, overrides):
    slugs: set[str] = set()
    parsed_ids = {pipeline.make_slug(r.name, r.state, slugs) for r in records}

    assert len(overrides) == 11
    assert set(overrides).issubset(parsed_ids), sorted(set(overrides) - parsed_ids)


def test_all_overrides_produce_valid_polygons(pipeline, overrides):
    for corp_id, spec in overrides.items():
        geom = pipeline.load_override_geometry(
            spec["_path"], dissolve=bool(spec.get("dissolve", True))
        )
        assert geom.is_valid, corp_id
        assert geom.geom_type in ("Polygon", "MultiPolygon"), corp_id
        area = pipeline.compute_geodetic_area_km2(geom)
        assert 1.5 <= area <= 3000, f"{corp_id}: {area} km²"


def test_kochi_single_feature_is_supported(pipeline, overrides):
    spec = overrides["kochi-municipal-corporation"]
    geom = pipeline.load_override_geometry(spec["_path"], dissolve=False)
    assert geom.geom_type == "Polygon"
    assert 90 <= pipeline.compute_geodetic_area_km2(geom) <= 105


def test_vadodara_invalid_wards_are_repaired(pipeline, overrides):
    spec = overrides["vadodara-municipal-corporation"]
    geom = pipeline.load_override_geometry(spec["_path"], dissolve=True)
    assert geom.is_valid
    assert 150 <= pipeline.compute_geodetic_area_km2(geom) <= 175


@pytest.mark.skipif(
    not (DATA_DIR / "municipal_boundaries.geojson").is_file(),
    reason="generated municipal dataset is not present",
)
def test_generated_dataset_contains_only_covered_cities():
    geojson = json.loads(
        (DATA_DIR / "municipal_boundaries.geojson").read_text(encoding="utf-8")
    )
    features = geojson["features"]
    contacts = json.loads(
        (DATA_DIR / "municipal_corporations.json").read_text(encoding="utf-8")
    )
    manifest = json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8"))

    assert len(features) == 30
    assert len(contacts) == 30
    assert all(feature["geometry"] is not None for feature in features)

    community = [
        f
        for f in features
        if f["properties"]["boundary_quality"] == "community"
    ]
    assert len(community) == 11
    assert len(manifest["excluded_missing"]) == 24
    assert manifest["summary"]["total_corporations"] == 30
    assert manifest["summary"]["missing_boundaries"] == 0

    for feature in community:
        props = feature["properties"]
        assert props["boundary_source"] == "DataMeet Municipal Spatial Data"
        assert props["license"] == "CC BY 4.0"
        assert props["source_url"].startswith(
            "https://github.com/datameet/Municipal_Spatial_Data/"
        )
