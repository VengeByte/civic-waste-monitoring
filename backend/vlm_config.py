"""
Provider and model registry for the multi-provider VLM client.

The routing table lives in ``backend/vlm_providers.json`` so model IDs, timeouts,
retry counts, and payload capabilities can change without touching Python. API keys
are never stored in the config: each provider declares the environment variable that
holds its key.

Environment:
    VLM_CONFIG_PATH   Optional path to an alternative provider JSON file.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from backend.env_loader import load_env_file

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "vlm_providers.json"
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ProviderConfigError(ValueError):
    """Raised when the provider JSON is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class VLMModelConfig:
    """A single model candidate within a provider."""

    id: str
    json_mode: bool = True


@dataclass(frozen=True)
class VLMProviderConfig:
    """A provider endpoint plus its ordered model candidates."""

    name: str
    endpoint: str
    api_key_env: str
    models: tuple[VLMModelConfig, ...]
    timeout_seconds: float = 10.0
    max_retries: int = 1
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def model_ids(self) -> tuple[str, ...]:
        """Configured model IDs in priority order."""
        return tuple(model.id for model in self.models)

    @property
    def primary_model(self) -> str:
        """The first model attempted for this provider."""
        return self.models[0].id

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize without any secret material."""
        return {
            "name": self.name,
            "endpoint": self.endpoint,
            "api_key_env": self.api_key_env,
            "models": [
                {"id": model.id, "json_mode": model.json_mode}
                for model in self.models
            ],
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
        }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProviderConfigError(message)


def _parse_provider(raw: Any, index: int) -> VLMProviderConfig:
    _require(isinstance(raw, dict), f"providers[{index}] must be an object")

    name = str(raw.get("name", "")).strip()
    endpoint = str(raw.get("endpoint", "")).strip()
    api_key_env = str(raw.get("api_key_env", "")).strip()

    _require(bool(name), f"providers[{index}].name is required")
    _require(
        endpoint.startswith("https://") or endpoint.startswith("http://"),
        f"providers[{index}].endpoint must be an http(s) URL",
    )
    _require(
        bool(api_key_env) and bool(ENV_NAME_RE.fullmatch(api_key_env)),
        (
            f"providers[{index}].api_key_env must be an environment variable name "
            f"such as GEMINI_API_KEY, not a literal key"
        ),
    )

    raw_models = raw.get("models")
    _require(
        isinstance(raw_models, list) and bool(raw_models),
        f"providers[{index}].models must be a non-empty list",
    )

    models: list[VLMModelConfig] = []
    seen_ids: set[str] = set()
    for model_index, item in enumerate(raw_models):
        if isinstance(item, str):
            model_id = item.strip()
            json_mode = True
        elif isinstance(item, dict):
            model_id = str(item.get("id", "")).strip()
            json_mode = bool(item.get("json_mode", True))
        else:
            raise ProviderConfigError(
                f"providers[{index}].models[{model_index}] must be a string or object"
            )
        _require(
            bool(model_id),
            f"providers[{index}].models[{model_index}].id is required",
        )
        _require(
            model_id not in seen_ids,
            f"providers[{index}] has duplicate model id '{model_id}'",
        )
        seen_ids.add(model_id)
        models.append(VLMModelConfig(id=model_id, json_mode=json_mode))

    timeout_raw = raw.get("timeout_seconds", 10.0)
    try:
        timeout_seconds = float(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise ProviderConfigError(
            f"providers[{index}].timeout_seconds must be numeric"
        ) from exc
    _require(
        1.0 <= timeout_seconds <= 120.0,
        f"providers[{index}].timeout_seconds must be between 1 and 120",
    )

    retries_raw = raw.get("max_retries", 1)
    try:
        max_retries = int(retries_raw)
    except (TypeError, ValueError) as exc:
        raise ProviderConfigError(
            f"providers[{index}].max_retries must be an integer"
        ) from exc
    _require(
        0 <= max_retries <= 3,
        f"providers[{index}].max_retries must be between 0 and 3",
    )

    headers_raw = raw.get("extra_headers", {}) or {}
    _require(
        isinstance(headers_raw, dict),
        f"providers[{index}].extra_headers must be an object",
    )
    extra_headers = {str(k): str(v) for k, v in headers_raw.items()}

    return VLMProviderConfig(
        name=name,
        endpoint=endpoint,
        api_key_env=api_key_env,
        models=tuple(models),
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        extra_headers=extra_headers,
    )


def load_provider_configs(path: Optional[Path | str] = None) -> tuple[VLMProviderConfig, ...]:
    """
    Load and validate the provider registry.

    Args:
        path: Optional explicit JSON path; defaults to ``VLM_CONFIG_PATH`` or the
            bundled ``backend/vlm_providers.json``.

    Returns:
        Immutable tuple of provider configurations in fallback order.

    Raises:
        ProviderConfigError: If the file is missing, malformed, or contains literal
            API keys instead of environment variable names.
    """
    load_env_file()

    config_path = Path(
        path or os.getenv("VLM_CONFIG_PATH", "").strip() or DEFAULT_CONFIG_PATH
    )
    if not config_path.is_file():
        raise ProviderConfigError(f"VLM provider config not found: {config_path}")

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderConfigError(f"Could not read VLM provider config: {exc}") from exc

    _require(isinstance(raw, dict), "VLM provider config must be a JSON object")
    raw_providers = raw.get("providers")
    _require(
        isinstance(raw_providers, list) and bool(raw_providers),
        "VLM provider config must contain a non-empty 'providers' list",
    )

    providers = tuple(
        _parse_provider(item, index) for index, item in enumerate(raw_providers)
    )

    names = [provider.name for provider in providers]
    _require(len(names) == len(set(names)), "Provider names must be unique")
    return providers


def get_api_key(provider: VLMProviderConfig) -> str:
    """
    Resolve a provider's API key from the environment.

    There is intentionally no literal fallback: keys must never live in source.
    """
    load_env_file()
    return os.getenv(provider.api_key_env, "").strip()


# Loaded once per process; ``VLM_CONFIG_PATH`` can point at an alternative file.
DEFAULT_PROVIDERS: tuple[VLMProviderConfig, ...] = load_provider_configs()


def configured_api_key_count() -> int:
    """Count providers that currently have a usable key (never returns keys)."""
    return sum(1 for provider in DEFAULT_PROVIDERS if get_api_key(provider))
