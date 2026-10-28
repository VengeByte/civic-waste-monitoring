"""
Asynchronous Multi-Provider Vision-Language Model (VLM) Client.

Routing is driven by ``backend/vlm_providers.json``. The client attempts providers in
order, and within each provider attempts its models in order, with these semantics:

- 401/403: the provider is unhealthy; open its circuit and stop using it.
- Model missing/decommissioned (404/410 or explicit provider text): open that model's
  circuit and move to the next model.
- 429/5xx/timeouts/connection errors: retry the same model once with backoff, then
  move to the next model.
- 400: retry once without ``response_format`` if JSON mode was requested, then move on.
- 200 with invalid JSON/schema: retry once, then move on.
- Overall deadline: bounded by ``VLM_TOTAL_TIMEOUT_SECONDS`` (default 30 s).

API keys are resolved exclusively from environment variables named in the provider
config; literal keys are rejected by the config loader.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import time
from enum import Enum
from typing import Any, Iterator, Optional, Sequence

import httpx

from backend.circuit_breaker import CIRCUIT_BREAKER, model_key, provider_key
from backend.env_loader import env_float, load_env_file
from backend.vlm_config import DEFAULT_PROVIDERS, VLMProviderConfig, get_api_key
from backend.vlm_metrics import VLM_METRICS

logger = logging.getLogger("backend.vlm_router")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [vlm_router]: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

RETRY_BASE_DELAY_SECONDS = 0.5
RETRY_MAX_DELAY_SECONDS = 3.0
MAX_RETRY_AFTER_SECONDS = 5.0
DEFAULT_TOTAL_TIMEOUT_SECONDS = 30.0

# Cooldowns applied when a circuit opens.
COOLDOWN_TRANSIENT_SECONDS = 60.0
COOLDOWN_DEAD_MODEL_SECONDS = 15 * 60.0
COOLDOWN_AUTH_SECONDS = 10 * 60.0


class AllVLMProvidersFailedError(RuntimeError):
    """Raised when every configured provider/model fails or is unavailable."""


class FailureKind(str, Enum):
    """Normalized failure classes used by routing and metrics."""

    AUTH = "auth"
    DEAD_MODEL = "dead_model"
    RATE_LIMIT = "rate_limit"
    UPSTREAM = "upstream"
    TRANSPORT = "transport"
    BAD_REQUEST = "bad_request"
    BAD_OUTPUT = "bad_output"
    OTHER = "other"


SYSTEM_PROMPT = """You are an expert municipal sanitation and civic waste inspection AI.
Analyze the provided image and output a strict JSON object adhering EXACTLY to the following schema:
{
  "garbage_detected": true,
  "confidence": 0.95,
  "waste_types": ["plastic bags", "domestic waste"],
  "severity": "low",
  "official_complaint_description": "A substantial accumulation of discarded plastic bags and uncollected domestic refuse is obstructing the pedestrian walkway. Immediate municipal sanitation dispatch is requested for clearance and sanitization."
}

Field requirements:
- "garbage_detected": boolean (true if garbage, litter, overflowing bins, or dumping is detected; false otherwise).
- "confidence": float between 0.0 and 1.0 indicating model confidence.
- "waste_types": list of strings detailing specific types of visible waste (e.g., ["plastic bags", "organic waste", "bottles", "cardboard"]). If no garbage is detected, use an empty list [].
- "severity": strictly one of "low", "medium", or "high" reflecting waste volume and civic obstruction. If no garbage is detected, use "low".
- "official_complaint_description": 2-3 formal sentences describing the garbage pile and demanding municipal sanitation clearance. If no garbage is detected, formally state that no sanitation violation was observed.

Output Instructions:
Return ONLY the raw JSON object. Do not include markdown formatting, code fences (e.g. ```json), or explanatory notes.
"""

REQUIRED_SCHEMA_KEYS = {
    "garbage_detected",
    "confidence",
    "waste_types",
    "severity",
    "official_complaint_description",
}


class VLMResult(dict):
    """
    Structured dictionary representation of a VLM analysis result.

    Supports attribute access and tuple unpacking for backwards compatibility:
        data, provider = await analyze_with_vlm(...)

    Metadata available on both the mapping and as attributes:
        provider / provider_used, model, attempts, fallback_used, latency_ms
    """

    def __init__(
        self,
        data: dict[str, Any],
        provider: str,
        model: Optional[str] = None,
        attempts: Optional[list[dict[str, Any]]] = None,
        fallback_used: bool = False,
        latency_ms: Optional[float] = None,
    ) -> None:
        super().__init__(data)
        self.provider = provider
        self.provider_used = provider
        self.model = model or ""
        self.attempts = attempts or []
        self.fallback_used = bool(fallback_used)
        self.latency_ms = round(float(latency_ms), 1) if latency_ms is not None else None

        self["provider"] = provider
        self["provider_used"] = provider
        self["model"] = self.model
        self["attempts"] = self.attempts
        self["fallback_used"] = self.fallback_used
        if self.latency_ms is not None:
            self["latency_ms"] = self.latency_ms

        self.data = data
        self.data["provider"] = provider
        self.data["provider_used"] = provider
        self.data["model"] = self.model
        self.data["attempts"] = self.attempts
        self.data["fallback_used"] = self.fallback_used
        if self.latency_ms is not None:
            self.data["latency_ms"] = self.latency_ms

    def __iter__(self) -> Iterator[Any]:
        return iter((self.data, self.provider))


def extract_and_validate_json(raw_text: str) -> dict[str, Any]:
    """
    Parse raw model output, strip markdown fences, and enforce the civic waste schema.

    Raises:
        ValueError: If JSON is invalid or fails schema constraints.
    """
    cleaned = raw_text.strip()

    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
    if match:
        cleaned = match.group(1).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Failed to parse response as JSON: {exc}. Raw snippet: {cleaned[:150]}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(f"Expected top-level JSON object, received {type(data).__name__}")

    missing_keys = REQUIRED_SCHEMA_KEYS - set(data.keys())
    if missing_keys:
        raise ValueError(f"Missing required schema keys: {missing_keys}")

    if not isinstance(data["garbage_detected"], bool):
        raise ValueError(
            f"'garbage_detected' must be bool, got {type(data['garbage_detected']).__name__}"
        )

    if not isinstance(data["confidence"], (int, float)):
        raise ValueError(
            f"'confidence' must be numeric, got {type(data['confidence']).__name__}"
        )
    data["confidence"] = max(0.0, min(1.0, float(data["confidence"])))

    if not isinstance(data["waste_types"], list):
        raise ValueError(
            f"'waste_types' must be a list, got {type(data['waste_types']).__name__}"
        )
    data["waste_types"] = [str(w).strip() for w in data["waste_types"] if str(w).strip()]

    severity = str(data["severity"]).lower().strip()
    if severity not in ("low", "medium", "high"):
        raise ValueError(
            f"'severity' must be 'low', 'medium', or 'high', got '{data['severity']}'"
        )
    data["severity"] = severity

    desc = str(data["official_complaint_description"]).strip()
    if not desc:
        raise ValueError("'official_complaint_description' must be a non-empty string")
    data["official_complaint_description"] = desc

    return data


def build_chat_completion_payload(
    image_bytes: bytes,
    model: str,
    system_prompt: str = SYSTEM_PROMPT,
    user_prompt: str = "Analyze this image for civic garbage reporting according to the system instructions.",
    json_mode: bool = True,
) -> dict[str, Any]:
    """
    Build an OpenAI-compatible vision payload with base64 image encoding.

    Args:
        image_bytes: Raw JPEG/PNG bytes (already PII-sanitized by the caller).
        model: Model identifier.
        system_prompt: System role prompt.
        user_prompt: User instruction text.
        json_mode: Whether the provider supports ``response_format=json_object``.

    Returns:
        JSON-serializable request payload.
    """
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:image/jpeg;base64,{image_b64}"

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "temperature": 0.0,
        "max_tokens": 1500,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    return payload


def classify_http_failure(status_code: int, body: str) -> FailureKind:
    """Map an HTTP status plus provider body to a routing failure class."""
    text = (body or "").lower()

    if status_code in (401, 403):
        return FailureKind.AUTH
    if "invalid api key" in text or "incorrect api key" in text:
        return FailureKind.AUTH

    dead_markers = (
        "decommissioned",
        "no longer available",
        "no provider serves",
        "model not found",
        "does not exist",
        "does not support image",
        "unsupported model",
        "retired",
    )
    if status_code in (404, 410) or any(marker in text for marker in dead_markers):
        return FailureKind.DEAD_MODEL

    if status_code == 429:
        return FailureKind.RATE_LIMIT
    if status_code >= 500:
        return FailureKind.UPSTREAM
    if status_code in (408, 425):
        return FailureKind.TRANSPORT
    if status_code == 400:
        return FailureKind.BAD_REQUEST
    return FailureKind.OTHER


def _parse_retry_after(response: Optional[httpx.Response]) -> Optional[float]:
    """Read a numeric Retry-After header, capped to avoid stalling the pipeline."""
    if response is None:
        return None
    raw = (response.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return min(MAX_RETRY_AFTER_SECONDS, max(0.0, float(raw)))
    except ValueError:
        return None


def _backoff_delay(attempt: int, retry_after: Optional[float]) -> float:
    """Exponential backoff with jitter, honouring a provider Retry-After."""
    if retry_after is not None:
        return retry_after
    base = min(RETRY_MAX_DELAY_SECONDS, RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
    return min(RETRY_MAX_DELAY_SECONDS, base * random.uniform(0.8, 1.2))


def _can_retry(deadline: float, provider_timeout: float) -> bool:
    """Only retry when enough of the overall budget remains for another attempt."""
    remaining = deadline - time.monotonic()
    return remaining > max(1.5, min(provider_timeout, 20.0) * 0.5)


async def _sleep_with_deadline(delay: float, deadline: float) -> None:
    """Sleep for ``delay`` without overrunning the overall deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return
    await asyncio.sleep(min(max(0.0, delay), remaining))


def _record_attempt(
    attempts: list[dict[str, Any]],
    provider: str,
    model: str,
    status_code: int,
    latency_ms: float,
    failure: Optional[FailureKind],
) -> None:
    attempts.append(
        {
            "provider": provider,
            "model": model,
            "status": status_code,
            "latency_ms": round(latency_ms, 1),
            "error_class": failure.value if failure else "",
        }
    )


def _failure_detail(provider: str, model: str, failure: FailureKind, detail: str) -> str:
    """Build a bounded, secret-free failure record for the final exception."""
    return f"{provider}/{model} [{failure.value}]: {detail[:180]}"


async def analyze_with_vlm(
    image_bytes: bytes,
    timeout: Optional[float] = None,
    providers: Optional[Sequence[VLMProviderConfig]] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> VLMResult:
    """
    Analyze an image through the configured provider/model fallback chain.

    Args:
        image_bytes: Sanitized image bytes.
        timeout: Optional overall deadline override in seconds. Defaults to
            ``VLM_TOTAL_TIMEOUT_SECONDS`` (30 s).
        providers: Optional explicit provider sequence (used by tests).
        client: Optional pre-configured httpx.AsyncClient.

    Returns:
        VLMResult with validated fields plus provider/model/attempt metadata.

    Raises:
        ValueError: If the image is empty.
        AllVLMProvidersFailedError: If no provider produces schema-valid output
            before the deadline.
    """
    if not image_bytes:
        raise ValueError("image_bytes must be non-empty bytes.")

    load_env_file()

    provider_list = tuple(providers) if providers is not None else DEFAULT_PROVIDERS
    if not provider_list:
        raise ValueError("At least one VLM provider must be configured.")

    total_timeout = (
        float(timeout)
        if timeout is not None
        else env_float("VLM_TOTAL_TIMEOUT_SECONDS", DEFAULT_TOTAL_TIMEOUT_SECONDS, 5.0)
    )
    started_at = time.monotonic()
    deadline = started_at + max(5.0, total_timeout)

    failure_records: list[str] = []
    attempts: list[dict[str, Any]] = []
    deadline_hit = False

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=total_timeout)

    try:
        for provider in provider_list:
            api_key = get_api_key(provider)
            if not api_key:
                message = (
                    f"{provider.name}: missing API key environment variable "
                    f"'{provider.api_key_env}'"
                )
                logger.warning("Skipping %s", message)
                failure_records.append(message)
                continue

            p_key = provider_key(provider.name)
            if not CIRCUIT_BREAKER.is_allowed(p_key):
                remaining = CIRCUIT_BREAKER.cooldown_remaining(p_key)
                message = f"{provider.name}: circuit open for another {remaining:.0f}s"
                logger.warning("Skipping %s", message)
                failure_records.append(message)
                continue

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            if provider.extra_headers:
                headers.update(provider.extra_headers)

            provider_auth_failed = False

            for model in provider.models:
                m_key = model_key(provider.name, model.id)
                if not CIRCUIT_BREAKER.is_allowed(m_key):
                    remaining = CIRCUIT_BREAKER.cooldown_remaining(m_key)
                    message = (
                        f"{provider.name}/{model.id}: model circuit open for another "
                        f"{remaining:.0f}s"
                    )
                    logger.warning("Skipping %s", message)
                    failure_records.append(message)
                    continue

                max_attempts = max(0, provider.max_retries) + 1
                json_mode = model.json_mode

                for attempt in range(1, max_attempts + 1):
                    remaining = deadline - time.monotonic()
                    if remaining <= 1.0:
                        failure_records.append(
                            f"{provider.name}/{model.id}: overall deadline of "
                            f"{total_timeout:.0f}s reached"
                        )
                        deadline_hit = True
                        break

                    payload = build_chat_completion_payload(
                        image_bytes=image_bytes,
                        model=model.id,
                        json_mode=json_mode,
                    )
                    logger.info(
                        "Attempting VLM analysis via [%s] (model: %s, attempt %d/%d, json_mode=%s)...",
                        provider.name,
                        model.id,
                        attempt,
                        max_attempts,
                        json_mode,
                    )

                    attempt_started = time.monotonic()
                    response: Optional[httpx.Response] = None
                    failure: Optional[FailureKind] = None
                    detail = ""
                    retry_after: Optional[float] = None

                    try:
                        response = await http_client.post(
                            provider.endpoint,
                            headers=headers,
                            json=payload,
                            timeout=min(provider.timeout_seconds, max(1.0, remaining)),
                        )
                    except httpx.TimeoutException as exc:
                        failure = FailureKind.TRANSPORT
                        detail = f"timeout: {exc}"
                    except httpx.RequestError as exc:
                        failure = FailureKind.TRANSPORT
                        detail = f"network error: {exc}"
                    except Exception as exc:  # pragma: no cover - defensive
                        failure = FailureKind.OTHER
                        detail = f"unexpected {type(exc).__name__}: {exc}"

                    latency_ms = (time.monotonic() - attempt_started) * 1000.0

                    if response is None:
                        _record_attempt(
                            attempts, provider.name, model.id, 0, latency_ms, failure
                        )
                        VLM_METRICS.record_attempt(
                            provider.name,
                            model.id,
                            success=False,
                            latency_ms=latency_ms,
                            error_class=failure.value if failure else "unknown",
                        )
                        failure_records.append(
                            _failure_detail(
                                provider.name,
                                model.id,
                                failure or FailureKind.OTHER,
                                detail,
                            )
                        )
                        CIRCUIT_BREAKER.record_failure(
                            m_key,
                            COOLDOWN_TRANSIENT_SECONDS,
                            failure.value if failure else "other",
                        )
                        CIRCUIT_BREAKER.record_failure(
                            p_key,
                            COOLDOWN_TRANSIENT_SECONDS,
                            failure.value if failure else "other",
                        )
                        if attempt < max_attempts and _can_retry(
                            deadline, provider.timeout_seconds
                        ):
                            VLM_METRICS.record_retry()
                            await _sleep_with_deadline(
                                _backoff_delay(attempt, None), deadline
                            )
                            continue
                        break

                    status_code = response.status_code
                    retry_after = _parse_retry_after(response)

                    if status_code == 200:
                        try:
                            response_data = response.json()
                            choices = response_data.get("choices")
                            if not choices or not isinstance(choices, list):
                                raise ValueError("missing or invalid 'choices' list")
                            content = (
                                choices[0].get("message", {}).get("content", "")
                            )
                            if not content or not str(content).strip():
                                raise ValueError("empty message content")
                            validated = extract_and_validate_json(str(content))
                        except (ValueError, json.JSONDecodeError, AttributeError) as exc:
                            failure = FailureKind.BAD_OUTPUT
                            detail = f"invalid model output: {exc}"
                        else:
                            _record_attempt(
                                attempts,
                                provider.name,
                                model.id,
                                status_code,
                                latency_ms,
                                None,
                            )
                            VLM_METRICS.record_attempt(
                                provider.name,
                                model.id,
                                success=True,
                                latency_ms=latency_ms,
                            )
                            CIRCUIT_BREAKER.record_success(p_key)
                            CIRCUIT_BREAKER.record_success(m_key)

                            used_models = {(a["provider"], a["model"]) for a in attempts}
                            fallback_used = len(used_models) > 1
                            if fallback_used:
                                VLM_METRICS.record_fallback()

                            total_latency_ms = (time.monotonic() - started_at) * 1000.0
                            logger.info(
                                "VLM success via [%s] model %s after %d attempt(s), %.0f ms.",
                                provider.name,
                                model.id,
                                len(attempts),
                                total_latency_ms,
                            )
                            return VLMResult(
                                data=validated,
                                provider=provider.name,
                                model=model.id,
                                attempts=attempts,
                                fallback_used=fallback_used,
                                latency_ms=total_latency_ms,
                            )
                    else:
                        body = response.text or ""
                        failure = classify_http_failure(status_code, body)
                        detail = f"HTTP {status_code}: {body[:180]}"

                    _record_attempt(
                        attempts,
                        provider.name,
                        model.id,
                        status_code,
                        latency_ms,
                        failure or FailureKind.OTHER,
                    )
                    VLM_METRICS.record_attempt(
                        provider.name,
                        model.id,
                        success=False,
                        latency_ms=latency_ms,
                        error_class=(failure or FailureKind.OTHER).value,
                    )
                    failure_records.append(
                        _failure_detail(
                            provider.name,
                            model.id,
                            failure or FailureKind.OTHER,
                            detail,
                        )
                    )

                    kind = failure or FailureKind.OTHER

                    if kind == FailureKind.AUTH:
                        CIRCUIT_BREAKER.record_failure(
                            p_key,
                            COOLDOWN_AUTH_SECONDS,
                            kind.value,
                            force_open=True,
                        )
                        logger.error(
                            "%s rejected its API key; opening provider circuit.",
                            provider.name,
                        )
                        provider_auth_failed = True
                        break

                    if kind == FailureKind.DEAD_MODEL:
                        CIRCUIT_BREAKER.record_failure(
                            m_key,
                            COOLDOWN_DEAD_MODEL_SECONDS,
                            kind.value,
                            force_open=True,
                        )
                        logger.warning(
                            "%s/%s is unavailable; skipping it for %.0f minutes.",
                            provider.name,
                            model.id,
                            COOLDOWN_DEAD_MODEL_SECONDS / 60.0,
                        )
                        break

                    if kind == FailureKind.BAD_REQUEST:
                        CIRCUIT_BREAKER.record_failure(m_key, 300.0, kind.value)
                        if json_mode and attempt < max_attempts:
                            logger.info(
                                "%s/%s rejected JSON mode; retrying without response_format.",
                                provider.name,
                                model.id,
                            )
                            json_mode = False
                            VLM_METRICS.record_retry()
                            continue
                        break

                    if kind == FailureKind.BAD_OUTPUT:
                        CIRCUIT_BREAKER.record_failure(
                            m_key, COOLDOWN_TRANSIENT_SECONDS, kind.value
                        )
                        if attempt < max_attempts and _can_retry(
                            deadline, provider.timeout_seconds
                        ):
                            VLM_METRICS.record_retry()
                            await _sleep_with_deadline(
                                _backoff_delay(attempt, None), deadline
                            )
                            continue
                        break

                    # RATE_LIMIT / UPSTREAM / TRANSPORT / OTHER are retryable.
                    CIRCUIT_BREAKER.record_failure(
                        m_key, COOLDOWN_TRANSIENT_SECONDS, kind.value
                    )
                    CIRCUIT_BREAKER.record_failure(
                        p_key, COOLDOWN_TRANSIENT_SECONDS, kind.value
                    )
                    if attempt < max_attempts and _can_retry(
                        deadline, provider.timeout_seconds
                    ):
                        VLM_METRICS.record_retry()
                        delay = _backoff_delay(attempt, retry_after)
                        logger.info(
                            "Retrying %s/%s in %.2fs after %s.",
                            provider.name,
                            model.id,
                            delay,
                            kind.value,
                        )
                        await _sleep_with_deadline(delay, deadline)
                        continue
                    break

                if provider_auth_failed or deadline_hit:
                    break

            if provider_auth_failed or deadline_hit:
                break

    finally:
        if owns_client:
            await http_client.aclose()

    if deadline_hit:
        failure_records.append(
            f"Overall VLM deadline of {total_timeout:.0f}s exceeded."
        )

    error_summary = "; ".join(failure_records) if failure_records else "No active providers."
    raise AllVLMProvidersFailedError(
        f"All VLM providers failed in fallback sequence. Details: {error_summary[:1200]}"
    )
