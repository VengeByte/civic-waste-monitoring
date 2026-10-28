"""
Tests for circuit breakers, router metrics, provider catalog validation, and the
provider health endpoint.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.circuit_breaker import (
    CircuitBreakerRegistry,
    model_key,
    provider_key,
)
from backend.main import app
from backend.provider_check import (
    ProviderHealth,
    check_all,
    check_provider,
    clear_health_cache,
)
from backend.vlm_config import VLMModelConfig, VLMProviderConfig
from backend.vlm_metrics import VLMMetrics


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_provider(
    name: str,
    api_key_env: str,
    models: list[str],
    *,
    endpoint: str = "https://provider.test/v1/chat/completions",
    extra_headers: dict[str, str] | None = None,
) -> VLMProviderConfig:
    return VLMProviderConfig(
        name=name,
        endpoint=endpoint,
        api_key_env=api_key_env,
        models=tuple(VLMModelConfig(id=model) for model in models),
        timeout_seconds=5.0,
        max_retries=0,
        extra_headers=extra_headers or {},
    )


def catalog(models: list[dict]) -> dict:
    return {"data": models}


# ============================================================================
# Circuit breaker
# ============================================================================

def test_breaker_opens_after_threshold_and_recovers():
    clock = FakeClock()
    breaker = CircuitBreakerRegistry(clock=clock, failure_threshold=2)
    key = provider_key("Example")

    assert breaker.is_allowed(key)
    assert breaker.record_failure(key, 30) is False
    assert breaker.is_allowed(key)
    assert breaker.record_failure(key, 30) is True
    assert not breaker.is_allowed(key)
    assert breaker.cooldown_remaining(key) == pytest.approx(30)

    clock.advance(31)
    assert breaker.is_allowed(key)

    breaker.record_success(key)
    assert breaker.snapshot() == {}


def test_breaker_force_open_and_reset():
    clock = FakeClock()
    breaker = CircuitBreakerRegistry(clock=clock, failure_threshold=5)

    assert breaker.record_failure("model:x", 600, "dead_model", force_open=True)
    assert not breaker.is_allowed("model:x")
    assert breaker.snapshot()["model:x"]["cooldown_remaining_seconds"] == 600

    breaker.reset()
    assert breaker.is_allowed("model:x")
    assert breaker.snapshot() == {}


def test_breaker_key_helpers():
    assert provider_key("Google Gemini") == "provider:Google Gemini"
    assert model_key("Google Gemini", "gemini-3.5-flash") == (
        "model:Google Gemini:gemini-3.5-flash"
    )


# ============================================================================
# Metrics
# ============================================================================

def test_metrics_aggregate_and_reset():
    metrics = VLMMetrics()
    metrics.record_attempt("P", "m", success=True, latency_ms=100)
    metrics.record_attempt("P", "m", success=False, latency_ms=300, error_class="upstream")
    metrics.record_retry()
    metrics.record_fallback()

    snapshot = metrics.snapshot()
    assert snapshot["attempts"] == 2
    assert snapshot["successes"] == 1
    assert snapshot["failures"] == 1
    assert snapshot["retries"] == 1
    assert snapshot["fallbacks"] == 1
    assert snapshot["success_rate"] == 0.5
    assert snapshot["avg_latency_ms"] == 200.0
    assert snapshot["models"]["P/m"]["last_error"] == "upstream"

    metrics.reset()
    assert metrics.snapshot()["attempts"] == 0


# ============================================================================
# Provider catalog validation
# ============================================================================

def test_provider_check_ok():
    provider = make_provider("Example", "EXAMPLE_KEY", ["vision-a", "vision-b"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        assert request.headers["authorization"] == "Bearer fake"
        return httpx.Response(
            200,
            json=catalog([{"id": "vision-a"}, {"id": "vision-b"}]),
        )

    async def _run():
        with patch.dict(os.environ, {"EXAMPLE_KEY": "fake"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await check_provider(provider, client=client)

    health = asyncio.run(_run())
    assert health.status == "ok"
    assert health.ok
    assert health.models_missing == []
    assert health.models_available == ["vision-a", "vision-b"]
    assert health.catalog_size == 2
    assert health.vision_ok is None  # Gemini-style catalogs omit modalities


def test_provider_check_normalizes_models_prefix():
    provider = make_provider(
        "GeminiLike",
        "GEMINI_LIKE_KEY",
        ["gemini-3.5-flash"],
        endpoint="https://gemini.test/v1beta/openai/chat/completions",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=catalog([{"id": "models/gemini-3.5-flash"}]))

    async def _run():
        with patch.dict(os.environ, {"GEMINI_LIKE_KEY": "fake"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await check_provider(provider, client=client)

    assert asyncio.run(_run()).status == "ok"


def test_provider_check_reports_missing_model():
    provider = make_provider("Example", "EXAMPLE_KEY", ["vision-a", "retired-model"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=catalog([{"id": "vision-a"}]))

    async def _run():
        with patch.dict(os.environ, {"EXAMPLE_KEY": "fake"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await check_provider(provider, client=client)

    health = asyncio.run(_run())
    assert health.status == "model_missing"
    assert health.models_missing == ["retired-model"]
    assert not health.ok


def test_provider_check_flags_non_vision_model():
    provider = make_provider("OpenRouterLike", "OR_KEY", ["text-only"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=catalog(
                [{"id": "text-only", "architecture": {"input_modalities": ["text"]}}]
            ),
        )

    async def _run():
        with patch.dict(os.environ, {"OR_KEY": "fake"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await check_provider(provider, client=client)

    health = asyncio.run(_run())
    assert health.status == "vision_unsupported"
    assert health.vision_ok is False


def test_provider_check_http_error_and_missing_key():
    provider = make_provider("Example", "EXAMPLE_KEY", ["vision-a"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    async def _run_with_key():
        with patch.dict(os.environ, {"EXAMPLE_KEY": "fake"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await check_provider(provider, client=client)

    assert asyncio.run(_run_with_key()).status == "http_error"

    os.environ.pop("EXAMPLE_KEY", None)
    missing = asyncio.run(check_provider(provider))
    assert missing.status == "missing_key"


def test_check_all_uses_cache_and_force():
    provider = make_provider("Example", "EXAMPLE_KEY", ["vision-a"])
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=catalog([{"id": "vision-a"}]))

    async def _run():
        with patch.dict(os.environ, {"EXAMPLE_KEY": "fake"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                first = await check_all([provider], client=client, force=True)
                second = await check_all([provider], client=client)
                third = await check_all([provider], client=client, force=True)
        return first, second, third

    first, second, third = asyncio.run(_run())
    assert first[0].status == "ok"
    assert second[0].status == "ok"
    assert third[0].status == "ok"
    assert calls["count"] == 2  # cached call did not hit HTTP

    clear_health_cache()


# ============================================================================
# Health endpoints
# ============================================================================

client = TestClient(app)


def test_health_includes_provider_counts():
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["vlm_providers"] >= 1
    assert isinstance(body["vlm_keys_configured"], int)


def test_provider_health_endpoint_reports_status_and_metrics():
    health = ProviderHealth(
        name="Example",
        status="ok",
        detail="All configured models are available.",
        models_configured=["vision-a"],
        models_available=["vision-a"],
    )

    with patch(
        "backend.main.check_all", new=AsyncMock(return_value=[health])
    ):
        response = client.get("/api/health/providers")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["providers"][0]["name"] == "Example"
    assert "circuits" in body
    assert "metrics" in body
    assert "api_key" not in response.text.lower()
