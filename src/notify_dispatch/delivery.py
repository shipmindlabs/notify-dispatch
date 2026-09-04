"""Delivery attempts, retries and dead letters.

A provider that fails once is often not a provider that fails forever, so a
delivery can be retried with a growing backoff. When the attempts run out the
message is not gone: it is kept as a dead letter, together with every attempt
that was made, for a human to inspect.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from notify_dispatch.dispatch import Channel, Message

__all__ = [
    "NO_RETRY",
    "Attempt",
    "DeadLetter",
    "DeadLetterQueue",
    "RetryPolicy",
]


@dataclass(frozen=True)
class RetryPolicy:
    """How often a failed delivery is retried, and how long between tries."""

    attempts: int = 3
    backoff: float = 0.5
    multiplier: float = 2.0
    max_backoff: float = 30.0
    retry_on: tuple[type[BaseException], ...] = (Exception,)

    def __post_init__(self) -> None:
        object.__setattr__(self, "retry_on", tuple(self.retry_on))
        if self.attempts < 1:
            raise ValueError("a retry policy must allow at least one attempt")
        if self.backoff < 0:
            raise ValueError("backoff must not be negative")
        if self.multiplier < 1:
            raise ValueError("multiplier must not shrink the backoff")
        if self.max_backoff < self.backoff:
            raise ValueError("max_backoff must not be smaller than backoff")
        if not self.retry_on:
            raise ValueError("a retry policy must name at least one retryable error")

    def backoff_for(self, attempt: int) -> float:
        """Return the seconds to wait after the given (1-based) attempt failed."""
        if attempt < 1:
            raise ValueError("attempts are counted from 1")
        return min(self.backoff * self.multiplier ** (attempt - 1), self.max_backoff)

    def allows(self, attempt: int, error: BaseException) -> bool:
        """Return whether another attempt may follow this failure."""
        return attempt < self.attempts and isinstance(error, self.retry_on)


NO_RETRY = RetryPolicy(attempts=1)


@dataclass(frozen=True)
class Attempt:
    """One handover to a provider that did not work."""

    number: int
    channel: Channel
    address: str
    error: BaseException
    at: datetime

    def __str__(self) -> str:
        return (
            f"attempt {self.number} on {self.channel.value} to "
            f"{self.address!r}: {self.error}"
        )


@dataclass(frozen=True)
class DeadLetter:
    """A message that exhausted its attempts, kept for someone to look at."""

    message: Message
    attempts: tuple[Attempt, ...]
    recorded_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempts", tuple(self.attempts))
        if not self.attempts:
            raise ValueError("a dead letter must carry at least one attempt")

    @property
    def template(self) -> str:
        return self.message.template

    @property
    def channel(self) -> Channel:
        return self.message.channel

    @property
    def address(self) -> str:
        return self.message.address

    @property
    def last_error(self) -> BaseException:
        return self.attempts[-1].error

    def __str__(self) -> str:
        return (
            f"{self.template!r} to {self.address!r} over {self.channel.value} "
            f"failed {len(self.attempts)} time(s): {self.last_error}"
        )


class DeadLetterQueue:
    """The failures a worker would otherwise drop into a log and forget."""

    def __init__(
        self, letters: Iterable[DeadLetter] = (), *, limit: int | None = None
    ) -> None:
        if limit is not None and limit < 1:
            raise ValueError("a dead letter queue must hold at least one letter")
        self._letters: deque[DeadLetter] = deque(letters, maxlen=limit)

    def record(
        self,
        message: Message,
        attempts: Iterable[Attempt],
        *,
        at: datetime | None = None,
    ) -> DeadLetter:
        """Keep a failed message and return the letter that was stored."""
        letter = DeadLetter(
            message=message,
            attempts=tuple(attempts),
            recorded_at=at if at is not None else datetime.now(),
        )
        self._letters.append(letter)
        return letter

    @property
    def letters(self) -> tuple[DeadLetter, ...]:
        return tuple(self._letters)

    @property
    def limit(self) -> int | None:
        return self._letters.maxlen

    def drain(self) -> tuple[DeadLetter, ...]:
        """Return every letter and leave the queue empty."""
        letters = tuple(self._letters)
        self._letters.clear()
        return letters

    def __len__(self) -> int:
        return len(self._letters)

    def __iter__(self) -> Iterator[DeadLetter]:
        return iter(tuple(self._letters))

    def __repr__(self) -> str:
        return f"DeadLetterQueue({len(self._letters)} letters)"
