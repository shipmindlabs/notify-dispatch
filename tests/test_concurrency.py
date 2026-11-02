"""Tests for two threads racing on one dedup key.

The race is synchronised by Events rather than by sleeping: the provider
announces that it is holding the first handover and then waits to be released,
so the second send is made while the first is demonstrably in flight.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from conftest import ADA, BODY, CONTEXT, NOW, ORDER_CONFIRMED

from notify_dispatch import Channel, DeliveryError, DuplicateSendError, SmsAdapter

TIMEOUT = 5.0


class GatedProvider:
    """A provider that announces a handover and holds it until released.

    The first ``failures`` handovers raise once they are let go.
    """

    def __init__(self, *, reference: str = "sms-1", failures: int = 0) -> None:
        self.reference = reference
        self.failures = failures
        self.arrived = Event()
        self.release = Event()
        self.calls: list[tuple[str, ...]] = []
        self._lock = Lock()

    def __call__(self, *args: str) -> str:
        with self._lock:
            self.calls.append(args)
            number = len(self.calls)
        self.arrived.set()
        self.release.wait(TIMEOUT)
        if number <= self.failures:
            raise RuntimeError("provider is down")
        return self.reference


def test_two_threads_racing_on_one_key_reach_the_provider_once(make_dispatcher):
    provider = GatedProvider()
    dispatcher = make_dispatcher(SmsAdapter(provider))

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(
            dispatcher.send, ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1"
        )
        assert provider.arrived.wait(TIMEOUT)

        in_flight = dispatcher.dedup.get("evt-1", at=NOW)
        assert in_flight is not None
        assert in_flight.is_pending

        loser = pool.submit(
            dispatcher.send, ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1"
        )
        with pytest.raises(DuplicateSendError) as excinfo:
            loser.result(TIMEOUT)
        assert excinfo.value.key == "evt-1"
        assert len(provider.calls) == 1

        provider.release.set()
        receipt = winner.result(TIMEOUT)

    assert len(provider.calls) == 1
    assert not receipt.duplicate
    assert receipt.reference == "sms-1"

    record = dispatcher.dedup.get("evt-1", at=NOW)
    assert record is not None
    assert not record.is_pending
    assert record.receipt.reference == receipt.reference
    assert record.receipt.channel is receipt.channel

    third = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    assert third.duplicate
    assert third.reference == "sms-1"
    assert third.channel is Channel.SMS
    assert third.message.body == BODY
    assert len(provider.calls) == 1
    assert len(dispatcher.dead_letters) == 0


def test_a_winner_that_fails_frees_the_key_the_loser_was_refused(make_dispatcher):
    provider = GatedProvider(failures=1)
    dispatcher = make_dispatcher(SmsAdapter(provider))

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(
            dispatcher.send, ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1"
        )
        assert provider.arrived.wait(TIMEOUT)

        loser = pool.submit(
            dispatcher.send, ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1"
        )
        with pytest.raises(DuplicateSendError):
            loser.result(TIMEOUT)

        provider.release.set()
        with pytest.raises(DeliveryError):
            winner.result(TIMEOUT)

    assert dispatcher.dedup.get("evt-1", at=NOW) is None
    assert len(dispatcher.dedup) == 0
    assert len(dispatcher.dead_letters) == 1

    receipt = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    assert not receipt.duplicate
    assert receipt.reference == "sms-1"
    assert len(provider.calls) == 2
