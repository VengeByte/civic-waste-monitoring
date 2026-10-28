"""Shared pytest fixtures for deterministic router tests."""

from __future__ import annotations

import pytest

from backend.circuit_breaker import CIRCUIT_BREAKER
from backend.provider_check import clear_health_cache
from backend.vlm_metrics import VLM_METRICS


@pytest.fixture(autouse=True)
def _reset_vlm_globals():
    """Prevent circuit-breaker and metric state leaking between tests."""
    CIRCUIT_BREAKER.reset()
    VLM_METRICS.reset()
    clear_health_cache()
    yield
    CIRCUIT_BREAKER.reset()
    VLM_METRICS.reset()
    clear_health_cache()
