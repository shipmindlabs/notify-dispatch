"""Deduplication of notifications by a caller-supplied key.

A webhook that is redelivered is the same event twice, not two notifications.
The caller names the event -- an upstream delivery id, an outbox row id,
whatever stays stable across redeliveries -- and a key that already produced a
receipt hands that receipt back instead of reaching a provider again.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from notify_dispatch.dispatch import Receipt

__all__ = [
    "DEFAULT_DEDUP_TTL",
    "DedupRecord",
    "DedupStore",
]

DEFAULT_DEDUP_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class DedupRecord:
    """What a dedup key has produced so far."""

    key: str
    claimed_at: datetime
    receipt: Receipt | None = None
    completed_at: datetime | None = None

    @property
    def is_pending(self) -> bool:
        """Whether the delivery holding this key is still in flight."""
        return self.receipt is None

    @property
    def touched_at(self) -> datetime:
        """The moment the record's lifetime is counted from."""
        return self.completed_at if self.completed_at is not None else self.claimed_at

    def __str__(self) -> str:
        state = "in flight" if self.is_pending else "delivered"
        return f"{self.key!r} ({state})"


class DedupStore:
    """The keys already handled, so a redelivered event does not notify twice."""

    def __init__(self, *, ttl: timedelta | None = DEFAULT_DEDUP_TTL) -> None:
        if ttl is not None and ttl <= timedelta(0):
            raise ValueError("a dedup ttl must be positive")
        self._ttl = ttl
        self._records: dict[str, DedupRecord] = {}
        self._lock = Lock()

    @property
    def ttl(self) -> timedelta | None:
        return self._ttl

    def claim(self, key: str, *, at: datetime) -> DedupRecord | None:
        """Reserve a key, or return the live record that already holds it."""
        _require_key(key)
        with self._lock:
            record = self._records.get(key)
            if record is not None and not self._expired(record, at):
                return record
            self._records[key] = DedupRecord(key=key, claimed_at=at)
            return None

    def complete(self, key: str, receipt: Receipt, *, at: datetime) -> DedupRecord:
        """Attach the receipt a claimed key produced and return the record."""
        _require_key(key)
        with self._lock:
            claimed = self._records.get(key)
            record = DedupRecord(
                key=key,
                claimed_at=claimed.claimed_at if claimed is not None else at,
                receipt=receipt,
                completed_at=at,
            )
            self._records[key] = record
            return record

    def release(self, key: str) -> None:
        """Give up a claim that produced nothing, so a redelivery may try again."""
        _require_key(key)
        with self._lock:
            record = self._records.get(key)
            if record is not None and record.is_pending:
                del self._records[key]

    def get(self, key: str, *, at: datetime | None = None) -> DedupRecord | None:
        """Return the live record for a key, or None if it is unknown or stale."""
        _require_key(key)
        moment = at if at is not None else datetime.now()
        with self._lock:
            record = self._records.get(key)
            if record is None or self._expired(record, moment):
                return None
            return record

    def purge(self, *, at: datetime | None = None) -> int:
        """Drop every record past its ttl and return how many were dropped."""
        moment = at if at is not None else datetime.now()
        with self._lock:
            stale = [
                key
                for key, record in self._records.items()
                if self._expired(record, moment)
            ]
            for key in stale:
                del self._records[key]
            return len(stale)

    def clear(self) -> None:
        """Forget every key."""
        with self._lock:
            self._records.clear()

    def _expired(self, record: DedupRecord, at: datetime) -> bool:
        if self._ttl is None:
            return False
        return at - record.touched_at >= self._ttl

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[DedupRecord]:
        return iter(tuple(self._records.values()))

    def __repr__(self) -> str:
        return f"DedupStore({len(self._records)} keys, ttl={self._ttl})"


def _require_key(key: str) -> None:
    if not key:
        raise ValueError("a dedup key must not be empty")
