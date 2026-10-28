"""
Startup / on-demand provider validation.

Each provider is asked for its model catalog via the standard ``/models`` endpoint.
The check verifies that every configured model ID still exists, and — where the
catalog exposes modality metadata, as OpenRouter does — that the model accepts images.

This catches retired model slugs before a citizen report fails. Results are cached for
``VLM_HEALTH_CACHE_SECONDS`` (default 60 s) and never include API keys.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import httpx

from backend.env_loader import env_float
from backend.vlm_config import VLMProviderConfig, get_api_key


@dataclass
class ProviderHealth:
    """Secret-free health result for one provider."""

    name: str
    status: str
    detail: str = ""
    models_configured: list[str] = field(default_factory=list)
    models_available: list[str] = field(default_factory=list)
    models_missing: list[str] = field(default_factory=list)
    vision_ok: Optional[bool] = None
    catalog_size: int = 0

    @property
    def ok(self) -> bool:
        """True when every configured model is present and vision-capable."""
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "models_configured": self.models_configured,
            "models_available": self.models_available,
            "models_missing": self.models_missing,
            "vision_ok": self.vision_ok,
            "catalog_size": self.catalog_size,
        }


def _models_url(endpoint: str) -> str:
    suffix = "/chat/completions"
    if endpoint.endswith(suffix):
        return endpoint[: -len(suffix)] + "/models"
    return endpoint.rstrip("/") + "/models"


def _normalize_model_id(model_id: str) -> str:
    return model_id[len("models/") :] if model_id.startswith("models/") else model_id


async def check_provider(
    provider: VLMProviderConfig,
    client: Optional[httpx.AsyncClient] = None,
    timeout: float = 8.0,
) -> ProviderHealth:
    """
    Validate one provider's configured models against its live catalog.

    Network and HTTP errors are reported as health states rather than raised.
    """
    configured = list(provider.model_ids)

    if not get_api_key(provider):
        return ProviderHealth(
            name=provider.name,
            status="missing_key",
            detail=f"Environment variable {provider.api_key_env} is not set.",
            models_configured=configured,
        )

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=timeout)
    headers = {"Authorization": f"Bearer {get_api_key(provider)}"}
    if provider.extra_headers:
        headers.update(provider.extra_headers)

    try:
        response = await http_client.get(_models_url(provider.endpoint), headers=headers)
        if response.status_code != 200:
            return ProviderHealth(
                name=provider.name,
                status="http_error",
                detail=f"GET /models returned HTTP {response.status_code}: {response.text[:160]}",
                models_configured=configured,
            )

        payload = response.json()
        raw_models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(raw_models, list):
            return ProviderHealth(
                name=provider.name,
                status="error",
                detail="Provider /models response did not contain a 'data' list.",
                models_configured=configured,
            )

        available: list[str] = []
        image_support: dict[str, bool] = {}
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            model_id = _normalize_model_id(str(item.get("id", "")))
            if not model_id:
                continue
            available.append(model_id)
            architecture = item.get("architecture")
            if isinstance(architecture, dict):
                modalities = architecture.get("input_modalities")
                if isinstance(modalities, list):
                    image_support[model_id] = "image" in modalities

        available_set = set(available)
        missing = [model_id for model_id in configured if model_id not in available_set]
        if missing:
            return ProviderHealth(
                name=provider.name,
                status="model_missing",
                detail="Configured model(s) no longer present: " + ", ".join(missing),
                models_configured=configured,
                models_available=[m for m in configured if m in available_set],
                catalog_size=len(available_set),
                models_missing=missing,
            )

        unsupported = [
            model_id
            for model_id in configured
            if image_support.get(model_id) is False
        ]
        if unsupported:
            return ProviderHealth(
                name=provider.name,
                status="vision_unsupported",
                detail="Model(s) do not accept image input: " + ", ".join(unsupported),
                models_configured=configured,
                models_available=[m for m in configured if m in available_set],
                catalog_size=len(available_set),
                vision_ok=False,
            )

        vision_ok = True if all(m in image_support for m in configured) else None
        return ProviderHealth(
            name=provider.name,
            status="ok",
            detail="All configured models are available.",
            models_configured=configured,
            models_available=[m for m in configured if m in available_set],
            catalog_size=len(available_set),
            vision_ok=vision_ok,
        )

    except Exception as exc:
        return ProviderHealth(
            name=provider.name,
            status="error",
            detail=f"{type(exc).__name__}: {exc}",
            models_configured=configured,
        )
    finally:
        if owns_client:
            await http_client.aclose()


_CACHE: dict[str, Any] = {"at": 0.0, "results": []}
_CACHE_TTL_SECONDS = env_float("VLM_HEALTH_CACHE_SECONDS", 60.0, 0.0)


async def check_all(
    providers: Sequence[VLMProviderConfig],
    *,
    force: bool = False,
    use_cache: bool = True,
    client: Optional[httpx.AsyncClient] = None,
) -> list[ProviderHealth]:
    """
    Check every provider sequentially, optionally using the short-lived cache.

    Sequential checks avoid hitting provider rate limits in parallel.
    """
    now = time.monotonic()
    if (
        use_cache
        and not force
        and _CACHE["results"]
        and now - _CACHE["at"] < _CACHE_TTL_SECONDS
    ):
        return list(_CACHE["results"])

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=8.0)
    try:
        results = [
            await check_provider(provider, client=http_client)
            for provider in providers
        ]
    finally:
        if owns_client:
            await http_client.aclose()

    if use_cache:
        _CACHE["at"] = now
        _CACHE["results"] = results
    return results


def clear_health_cache() -> None:
    """Reset the health cache. Intended for tests and operator refreshes."""
    _CACHE["at"] = 0.0
    _CACHE["results"] = []
