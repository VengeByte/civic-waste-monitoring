"""
Minimal in-process metrics for the VLM fallback router.

The goal is operational visibility without adding Prometheus: counts of attempts,
successes, failures, retries and fallbacks, plus per-model latency and last error.
All values are secret-free and safe to expose on an internal health endpoint.
"""

from __future__ import annotations

import threading
from typing import Any, Optional


class VLMMetrics:
    """Thread-safe counters for provider/model attempts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts = 0
        self._successes = 0
        self._failures = 0
        self._retries = 0
        self._fallbacks = 0
        self._total_latency_ms = 0.0
        self._models: dict[str, dict[str, Any]] = {}

    def record_attempt(
        self,
        provider: str,
        model: str,
        *,
        success: bool,
        latency_ms: float,
        error_class: Optional[str] = None,
    ) -> None:
        """Record one HTTP completion (successful or failed)."""
        key = f"{provider}/{model}"
        with self._lock:
            self._attempts += 1
            self._total_latency_ms += max(0.0, latency_ms)
            if success:
                self._successes += 1
            else:
                self._failures += 1

            entry = self._models.setdefault(
                key,
                {
                    "attempts": 0,
                    "successes": 0,
                    "failures": 0,
                    "total_latency_ms": 0.0,
                    "last_error": "",
                },
            )
            entry["attempts"] += 1
            entry["total_latency_ms"] += max(0.0, latency_ms)
            if success:
                entry["successes"] += 1
            else:
                entry["failures"] += 1
                if error_class:
                    entry["last_error"] = error_class

    def record_retry(self) -> None:
        """Record that an attempt is being retried."""
        with self._lock:
            self._retries += 1

    def record_fallback(self) -> None:
        """Record that a non-primary provider/model produced the result."""
        with self._lock:
            self._fallbacks += 1

    def snapshot(self) -> dict[str, Any]:
        """Return aggregate and per-model counters."""
        with self._lock:
            attempts = self._attempts
            models: dict[str, dict[str, Any]] = {}
            for key, entry in sorted(self._models.items()):
                entry_attempts = entry["attempts"]
                models[key] = {
                    "attempts": entry_attempts,
                    "successes": entry["successes"],
                    "failures": entry["failures"],
                    "avg_latency_ms": round(
                        entry["total_latency_ms"] / entry_attempts, 1
                    )
                    if entry_attempts
                    else 0.0,
                    "last_error": entry["last_error"],
                }

            return {
                "attempts": attempts,
                "successes": self._successes,
                "failures": self._failures,
                "retries": self._retries,
                "fallbacks": self._fallbacks,
                "success_rate": round(self._successes / attempts, 4) if attempts else 0.0,
                "avg_latency_ms": round(self._total_latency_ms / attempts, 1)
                if attempts
                else 0.0,
                "models": models,
            }

    def reset(self) -> None:
        """Clear all counters. Intended for tests."""
        with self._lock:
            self._attempts = 0
            self._successes = 0
            self._failures = 0
            self._retries = 0
            self._fallbacks = 0
            self._total_latency_ms = 0.0
            self._models.clear()


# Process-wide metrics used by the router.
VLM_METRICS = VLMMetrics()
