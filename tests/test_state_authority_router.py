"""
Unit tests for the State & Municipal ULB Authority Directory Router.
"""

from __future__ import annotations

import pytest

from backend.state_authority_router import StateAuthorityResolver, get_state_authority_resolver


def test_state_authority_resolver_singleton():
    resolver = get_state_authority_resolver()
    assert resolver is not None
    assert isinstance(resolver, StateAuthorityResolver)


def test_city_level_resolution():
    resolver = get_state_authority_resolver()
    # Test Lucknow resolution via address dict
    address_dict = {
        "city": "Lucknow",
        "state": "Uttar Pradesh",
        "country": "India",
    }
    match = resolver.resolve(
        lat=26.8467,
        lon=80.9462,
        address_dict=address_dict,
        resolved_address="Hazratganj, Lucknow, Uttar Pradesh, India",
    )
    assert match is not None
    assert match["municipality_name"] == "Lucknow Municipal Corporation"
    assert match["sanitary_inspector_email"] == "nnlko@up.nic.in"
    assert match["match_type"] == "state_city_directory"


def test_state_level_fallback_resolution():
    resolver = get_state_authority_resolver()
    # Test a location in Bihar without a specific city match
    address_dict = {
        "town": "Samastipur",
        "state": "Bihar",
        "country": "India",
    }
    match = resolver.resolve(
        lat=25.86,
        lon=85.78,
        address_dict=address_dict,
        resolved_address="Samastipur, Bihar, India",
    )
    assert match is not None
    assert match["municipality_name"] == "Urban Development & Housing Department, Bihar"
    assert match["sanitary_inspector_email"] == "cs-bihar@nic.in"
    assert match["match_type"] == "state_directorate"


def test_unmapped_state_resolution():
    resolver = get_state_authority_resolver()
    # Test completely unknown location (e.g. outside India and coordinate 0,0)
    address_dict = {
        "city": "Nowhere",
        "state": "Atlantis",
        "country": "Nowhere",
    }
    match = resolver.resolve(
        lat=0.0,
        lon=0.0,
        address_dict=address_dict,
        resolved_address="Nowhere, Atlantis",
    )
    assert match is None


def test_mysore_city_resolution():
    resolver = get_state_authority_resolver()
    match = resolver.resolve(
        lat=12.3600,
        lon=76.6000,
        resolved_address="Hootagalli, Mysuru, Karnataka, India",
    )
    assert match is not None
    assert match["municipality_name"] == "Mysuru City Corporation (MCC)"
    assert match["sanitary_inspector_email"] == "commissionermcc@gmail.com"
    assert match["match_type"] == "state_city_directory"


def test_karnataka_state_fallback_resolution():
    resolver = get_state_authority_resolver()
    match = resolver.resolve(
        lat=14.5000,
        lon=75.5000,
        resolved_address="Chitradurga Rural, Karnataka, India",
    )
    assert match is not None
    assert match["municipality_name"] == "Directorate of Municipal Administration, Karnataka"
    assert match["sanitary_inspector_email"] == "dmadir.kar@nic.in"
    assert match["match_type"] == "state_directorate"


