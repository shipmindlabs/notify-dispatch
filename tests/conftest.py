"""Recording adapters and an injected clock, shared by the whole suite.

No test reaches a provider and no test waits: every adapter hands over to a
Recorder, and every dispatcher is built with the test's own clock and a sleep
that records the backoff instead of spending it.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

from notify_dispatch import (
    ChannelAdapter,
    Dispatcher,
    QuietHours,
    Recipient,
    Template,
    Variable,
)

NOW = datetime(2026, 3, 1, 10, 0)
NIGHT = datetime(2026, 3, 1, 23, 30)
OVERNIGHT = QuietHours(start=time(22, 0), end=time(7, 0))

ORDER_CONFIRMED = Template(
    name="order_confirmed",
    text="Order {order_id} confirmed.",
    variables=(Variable("order_id", str),),
)

CONTEXT = {"order_id": "A-1042"}
BODY = "Order A-1042 confirmed."
ADA = Recipient(phone="+491")


class Recorder:
    """A provider that records handovers instead of reaching anyone.

    The first ``failures`` handovers raise, so a retry can be observed without
    a real provider having a bad day.
    """

    def __init__(
        self,
        *,
        reference: str | None = "ref-1",
        failures: int = 0,
        error: BaseException | None = None,
    ) -> None:
        self.reference = reference
        self.failures = failures
        self.error = error if error is not None else RuntimeError("provider is down")
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> str | None:
        self.calls.append(args)
        if len(self.calls) <= self.failures:
            raise self.error
        return self.reference

    @property
    def bodies(self) -> list[str]:
        return [call[-1] for call in self.calls]


class FakeClock:
    """The only source of time in the tests; frozen unless a step is set."""

    def __init__(self, now: datetime = NOW, step: timedelta = timedelta(0)) -> None:
        self.now = now
        self.step = step

    def __call__(self) -> datetime:
        moment = self.now
        self.now += self.step
        return moment

    def advance(self, delta: timedelta) -> datetime:
        self.now += delta
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    """The moment every dispatcher in a test reads."""
    return FakeClock()


@pytest.fixture
def sleeps() -> list[float]:
    """The backoffs a dispatcher asked to wait for, instead of waiting."""
    return []


@pytest.fixture
def make_dispatcher(clock: FakeClock, sleeps: list[float]):
    """Build a dispatcher whose time comes from the test, not from the world."""

    def build(*adapters: ChannelAdapter, **kwargs) -> Dispatcher:
        kwargs.setdefault("clock", clock)
        kwargs.setdefault("sleep", sleeps.append)
        return Dispatcher(adapters, **kwargs)

    return build
