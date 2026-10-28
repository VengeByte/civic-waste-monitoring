"""
In-process circuit breakers for VLM providers and individual models.

A provider/model is skipped while its circuit is open, which prevents the router from
paying the latency of repeatedly calling a dead or rate-limited endpoint. State is
process-local; a multi-worker deployment would need a shared store (e.g. Redis) to
share breaker state across workers.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

DEFAULT_FAILURE_THRESHOLD = 3


def provider_key(name: str) -> str:
    """Breaker key for a provider."""
    return f"provider:{name}"


def model_key(provider_name: str, model_id: str) -> str:
    """Breaker key for a specific provider/model pair."""
    return f"model:{provider_name}:{model_id}"


class CircuitBreakerRegistry:
    """
    Consecutive-failure circuit breaker keyed by an arbitrary string.

    After ``failure_threshold`` consecutive failures the circuit opens for the
    cooldown supplied with the last failure. Once the cooldown expires the next call
    is allowed through as a half-open probe; a success closes the circuit, another
    failure opens it again.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    ) -> None:
        self._clock = clock
        self._threshold = max(1, int(failure_threshold))
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._last_error: dict[str, str] = {}

    def is_allowed(self, key: str) -> bool:
        """True when the breaker is closed or its cooldown has elapsed."""
        with self._lock:
            return self._clock() >= self._open_until.get(key, 0.0)

    def cooldown_remaining(self, key: str) -> float:
        """Seconds remaining before a half-open probe is allowed."""
        with self._lock:
            return max(0.0, self._open_until.get(key, 0.0) - self._clock())

    def record_success(self, key: str) -> None:
        """Reset failure state after a successful call."""
        with self._lock:
            self._failures.pop(key, None)
            self._open_until.pop(key, None)
            self._last_error.pop(key, None)

    def record_failure(
        self,
        key: str,
        cooldown_seconds: float = 60.0,
        error_class: Optional[str] = None,
        force_open: bool = False,
    ) -> bool:
        """
        Record a failure and open the circuit once the threshold is reached.

        Args:
            key: Breaker key.
            cooldown_seconds: How long to keep the circuit open.
            error_class: Optional normalized failure class for health output.
            force_open: Open immediately (used for auth failures and dead models).

        Returns:
            True if this failure opened the circuit, otherwise False.
        """
        with self._lock:
            failures = self._failures.get(key, 0) + 1
            self._failures[key] = failures
            if error_class:
                self._last_error[key] = error_class

            if not force_open and failures < self._threshold:
                return False

            self._failures[key] = 0
            self._open_until[key] = self._clock() + max(1.0, float(cooldown_seconds))
            return True

    def snapshot(self) -> dict[str, dict[str, float | int | str]]:
        """Return a secret-free view of breaker state for health endpoints."""
        now = self._clock()
        with self._lock:
            keys = set(self._failures) | set(self._open_until) | set(self._last_error)
            return {
                key: {
                    "consecutive_failures": self._failures.get(key, 0),
                    "cooldown_remaining_seconds": round(
                        max(0.0, self._open_until.get(key, 0.0) - now), 1
                    ),
                    "last_error": self._last_error.get(key, ""),
                }
                for key in sorted(keys)
            }

    def reset(self) -> None:
        """Clear all breaker state. Intended for tests and operator resets."""
        with self._lock:
            self._failures.clear()
            self._open_until.clear()
            self._last_error.clear()


# Process-wide breaker state used by the router.
CIRCUIT_BREAKER = CircuitBreakerRegistry()
