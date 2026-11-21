"""Addresses that must not be notified, and why they must not be.

An unsubscribe, a hard bounce and a complaint are the same instruction: never
hand this address to a provider again. The list is consulted while a
notification is routed, so a suppressed address is skipped like one that is
missing, and a recipient with nothing left is refused rather than sent to.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from notify_dispatch.dispatch import Channel

__all__ = [
    "Suppression",
    "SuppressionList",
]


@dataclass(frozen=True)
class Suppression:
    """One address that may not be notified, on one channel or on all of them."""

    address: str
    channel: Channel | None = None
    reason: str = ""
    at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.address:
            raise ValueError("a suppressed address must not be empty")

    def covers(self, address: str, channel: Channel) -> bool:
        """Return whether this entry refuses a handover to an address."""
        if self.address != address:
            return False
        return self.channel is None or self.channel is channel

    def __str__(self) -> str:
        where = self.channel.value if self.channel is not None else "every channel"
        why = f": {self.reason}" if self.reason else ""
        return f"{self.address!r} on {where}{why}"


class SuppressionList:
    """The addresses a provider must not be handed, however often an event asks."""

    def __init__(self, entries: Iterable[Suppression] = ()) -> None:
        self._entries: dict[tuple[str, Channel | None], Suppression] = {
            (entry.address, entry.channel): entry for entry in entries
        }
        self._lock = Lock()

    def suppress(
        self,
        address: str,
        *,
        channel: Channel | None = None,
        reason: str = "",
        at: datetime | None = None,
    ) -> Suppression:
        """Refuse an address, on one channel or on every one, and return the entry."""
        entry = Suppression(address=address, channel=channel, reason=reason, at=at)
        with self._lock:
            self._entries[(entry.address, entry.channel)] = entry
        return entry

    def allow(
        self, address: str, *, channel: Channel | None = None
    ) -> tuple[Suppression, ...]:
        """Lift what an address is refused for and return the entries lifted.

        Without a channel every entry for the address is lifted; with one, only
        the entry for that channel, so a blanket entry survives it.
        """
        with self._lock:
            keys = [
                key
                for key in self._entries
                if key[0] == address and (channel is None or key[1] is channel)
            ]
            return tuple(self._entries.pop(key) for key in keys)

    def suppression_for(self, address: str, channel: Channel) -> Suppression | None:
        """Return the entry refusing this handover, the channel's own one first."""
        with self._lock:
            entry = self._entries.get((address, channel))
            if entry is not None:
                return entry
            return self._entries.get((address, None))

    def is_suppressed(self, address: str, channel: Channel) -> bool:
        """Return whether an address may still be handed to a channel."""
        return self.suppression_for(address, channel) is not None

    @property
    def entries(self) -> tuple[Suppression, ...]:
        with self._lock:
            return tuple(self._entries.values())

    def clear(self) -> None:
        """Forget every entry."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[Suppression]:
        return iter(self.entries)

    def __repr__(self) -> str:
        return f"SuppressionList({len(self._entries)} entries)"
