"""
Short-Lived Evidence Store for Civic Report Photographs.

The civic reporting pipeline keeps the sanitized (PII-blurred) photograph only in
memory for a bounded time so that it can be:

1. Linked from the complaint letter as an unguessable, read-only evidence URL.
2. Attached to a server-side email dispatch when SMTP delivery is configured.

This intentionally replaces a database for the current prototype: evidence links are
bearer capabilities, valid for ``EVIDENCE_TTL_SECONDS`` (default 24 hours), capped by
``EVIDENCE_MAX_ENTRIES`` to bound memory use, and purged lazily on access.

Environment variables:
    EVIDENCE_TTL_SECONDS   Link lifetime in seconds (default 86400, minimum 60).
    EVIDENCE_MAX_ENTRIES   Maximum retained photographs (default 100, minimum 1).
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

DEFAULT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_MAX_ENTRIES = 100
MIN_TTL_SECONDS = 60


def _env_int(name: str, default: int, minimum: int) -> int:
    """Read a positive integer environment variable, falling back safely."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


def utc_iso(timestamp: float) -> str:
    """Format a POSIX timestamp as an RFC 3339 UTC string."""
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


@dataclass
class EvidenceRecord:
    """A single retained report photograph plus its compiled dispatch draft."""

    token: str
    image_bytes: bytes
    created_at: float
    expires_at: float
    draft: Optional[dict[str, Any]] = None
    authority: Optional[dict[str, Any]] = None
    sent_count: int = 0
    last_sent_at: Optional[float] = None

    @property
    def expires_at_iso(self) -> str:
        """RFC 3339 expiry timestamp for API responses and the UI."""
        return utc_iso(self.expires_at)

    @property
    def expired(self) -> bool:
        """Whether the record's retention window has elapsed."""
        return time.time() >= self.expires_at


class EvidenceStore:
    """
    Thread-safe, in-memory, TTL-bounded store for sanitized report photographs.

    The store is deliberately tiny: FastAPI runs the synchronous ``smtplib`` work in
    a worker thread, so access is guarded by a lock. Tokens are 256-bit URL-safe
    random strings and act as bearer capabilities; they are never derived from the
    report coordinates or image content.
    """

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_seconds = max(MIN_TTL_SECONDS, int(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._records: dict[str, EvidenceRecord] = {}
        self._lock = threading.Lock()

    def create(
        self,
        image_bytes: bytes,
        draft: Optional[dict[str, Any]] = None,
        authority: Optional[dict[str, Any]] = None,
        ttl_seconds: Optional[int] = None,
    ) -> EvidenceRecord:
        """
        Retain a sanitized photograph and return its bearer-token record.

        Args:
            image_bytes: Sanitized JPEG bytes. Must be non-empty.
            draft: Optional compiled complaint draft to retain alongside the image.
            authority: Optional resolved municipal authority metadata.
            ttl_seconds: Optional per-record lifetime override.

        Returns:
            The newly created EvidenceRecord.

        Raises:
            ValueError: If ``image_bytes`` is empty.
        """
        if not image_bytes:
            raise ValueError("Evidence image_bytes must be non-empty.")

        now = self._clock()
        ttl = self._ttl_seconds if ttl_seconds is None else max(MIN_TTL_SECONDS, int(ttl_seconds))
        record = EvidenceRecord(
            token=secrets.token_urlsafe(32),
            image_bytes=image_bytes,
            created_at=now,
            expires_at=now + ttl,
            draft=draft,
            authority=authority,
        )

        with self._lock:
            self._purge_locked(now)
            self._evict_oldest_locked(limit=self._max_entries - 1)
            self._records[record.token] = record

        return record

    def get(self, token: str) -> Optional[EvidenceRecord]:
        """Return a live record, or None when the token is unknown or expired."""
        if not token:
            return None

        now = self._clock()
        with self._lock:
            record = self._records.get(token)
            if record is None:
                return None
            if record.expires_at <= now:
                del self._records[token]
                return None
            return record

    def set_draft(
        self,
        token: str,
        draft: dict[str, Any],
        authority: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Attach the compiled complaint draft to an existing record."""
        with self._lock:
            record = self._records.get(token)
            if record is None or record.expires_at <= self._clock():
                return False
            record.draft = draft
            if authority is not None:
                record.authority = authority
            return True

    def record_delivery(self, token: str) -> Optional[int]:
        """Record a successful server-side send and return the new send count."""
        with self._lock:
            record = self._records.get(token)
            if record is None or record.expires_at <= self._clock():
                return None
            record.sent_count += 1
            record.last_sent_at = self._clock()
            return record.sent_count

    def purge_expired(self) -> int:
        """Remove expired records and return how many were dropped."""
        with self._lock:
            return self._purge_locked(self._clock())

    def clear(self) -> None:
        """Drop every record. Intended for tests and operator resets."""
        with self._lock:
            self._records.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def _purge_locked(self, now: float) -> int:
        expired = [token for token, rec in self._records.items() if rec.expires_at <= now]
        for token in expired:
            del self._records[token]
        return len(expired)

    def _evict_oldest_locked(self, limit: int) -> None:
        while len(self._records) > max(0, limit):
            oldest = min(self._records, key=lambda token: self._records[token].created_at)
            del self._records[oldest]


# Process-wide store used by the API. TTL and capacity are configurable via env.
EVIDENCE_STORE = EvidenceStore(
    ttl_seconds=_env_int("EVIDENCE_TTL_SECONDS", DEFAULT_TTL_SECONDS, MIN_TTL_SECONDS),
    max_entries=_env_int("EVIDENCE_MAX_ENTRIES", DEFAULT_MAX_ENTRIES, 1),
)
