"""Tests for deduplication by a caller-supplied key."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import ADA, BODY, CONTEXT, NIGHT, NOW, ORDER_CONFIRMED, OVERNIGHT, Recorder

from notify_dispatch import (
    Channel,
    DedupStore,
    DeliveryError,
    DuplicateSendError,
    Message,
    MissingVariableError,
    QuietHoursError,
    Receipt,
    Recipient,
    RetryPolicy,
    SmsAdapter,
    UnroutableError,
)


def a_receipt() -> Receipt:
    message = Message(
        template="order_confirmed",
        body=BODY,
        channel=Channel.SMS,
        address="+491",
    )
    return Receipt(message=message, reference="ref-1")


def test_the_same_key_is_delivered_once(make_dispatcher):
    provider = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(provider))

    first = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")
    second = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    assert len(provider.calls) == 1
    assert not first.duplicate
    assert second.duplicate
    assert second.reference == first.reference == "ref-1"
    assert second.channel is Channel.SMS
    assert len(dispatcher.dedup) == 1


def test_different_keys_are_both_delivered(make_dispatcher):
    provider = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(provider))

    dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")
    receipt = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-2")

    assert len(provider.calls) == 2
    assert not receipt.duplicate


def test_without_a_key_nothing_is_deduplicated(make_dispatcher):
    provider = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(provider))

    dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)
    dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert len(provider.calls) == 2
    assert len(dispatcher.dedup) == 0


def test_a_failed_delivery_leaves_the_key_free(make_dispatcher):
    provider = Recorder(failures=1)
    dispatcher = make_dispatcher(SmsAdapter(provider))

    with pytest.raises(DeliveryError):
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")
    receipt = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    assert len(provider.calls) == 2
    assert not receipt.duplicate
    assert len(dispatcher.dead_letters) == 1


def test_a_key_is_remembered_with_the_receipt_the_retries_produced(
    make_dispatcher, sleeps
):
    provider = Recorder(failures=1)
    dispatcher = make_dispatcher(
        SmsAdapter(provider), retry=RetryPolicy(attempts=2, backoff=0.25)
    )

    first = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")
    second = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    assert first.attempts == 2
    assert second.attempts == 2
    assert second.duplicate
    assert len(provider.calls) == 2
    assert sleeps == [0.25]


def test_an_unroutable_send_leaves_the_key_free(make_dispatcher):
    provider = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(provider))

    with pytest.raises(UnroutableError):
        dispatcher.send(
            Recipient(email="a@example.com"),
            ORDER_CONFIRMED,
            CONTEXT,
            dedup_key="evt-1",
        )
    receipt = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    assert not receipt.duplicate
    assert len(provider.calls) == 1


def test_a_quiet_hours_refusal_leaves_the_key_free(make_dispatcher):
    provider = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(provider))
    recipient = Recipient(phone="+491", quiet_hours=OVERNIGHT)

    with pytest.raises(QuietHoursError):
        dispatcher.send(
            recipient, ORDER_CONFIRMED, CONTEXT, now=NIGHT, dedup_key="evt-1"
        )
    receipt = dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    assert not receipt.duplicate
    assert len(provider.calls) == 1


def test_a_template_error_leaves_the_key_free(make_dispatcher):
    provider = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(provider))

    with pytest.raises(MissingVariableError):
        dispatcher.send(ADA, ORDER_CONFIRMED, {}, dedup_key="evt-1")
    receipt = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    assert not receipt.duplicate
    assert len(provider.calls) == 1


def test_a_key_still_in_flight_is_rejected(make_dispatcher):
    store = DedupStore()
    store.claim("evt-1", at=NOW)
    provider = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(provider), dedup=store)

    with pytest.raises(DuplicateSendError) as excinfo:
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    assert excinfo.value.key == "evt-1"
    assert not provider.calls


def test_a_key_expires_after_the_ttl(make_dispatcher, clock):
    provider = Recorder()
    dispatcher = make_dispatcher(
        SmsAdapter(provider), dedup=DedupStore(ttl=timedelta(hours=1))
    )

    dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")
    clock.advance(timedelta(hours=2))
    receipt = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    assert len(provider.calls) == 2
    assert not receipt.duplicate


def test_a_shared_store_deduplicates_across_dispatchers(make_dispatcher):
    store = DedupStore()
    first, second = Recorder(), Recorder()
    worker_a = make_dispatcher(SmsAdapter(first), dedup=store)
    worker_b = make_dispatcher(SmsAdapter(second), dedup=store)

    worker_a.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")
    receipt = worker_b.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    assert len(first.calls) == 1
    assert not second.calls
    assert receipt.duplicate


def test_a_completed_key_records_the_receipt():
    store = DedupStore()
    receipt = a_receipt()

    assert store.claim("evt-1", at=NOW) is None
    store.complete("evt-1", receipt, at=NOW)
    record = store.get("evt-1", at=NOW)

    assert record is not None
    assert record.receipt is receipt
    assert not record.is_pending
    assert record.claimed_at == NOW


def test_releasing_a_completed_key_keeps_it():
    store = DedupStore()
    store.claim("evt-1", at=NOW)
    store.complete("evt-1", a_receipt(), at=NOW)

    store.release("evt-1")

    assert store.get("evt-1", at=NOW) is not None


def test_releasing_a_pending_key_forgets_it():
    store = DedupStore()
    store.claim("evt-1", at=NOW)

    store.release("evt-1")

    assert store.get("evt-1", at=NOW) is None
    assert len(store) == 0


def test_a_store_without_a_ttl_remembers_forever():
    store = DedupStore(ttl=None)
    store.claim("evt-1", at=NOW)
    store.complete("evt-1", a_receipt(), at=NOW)

    assert store.get("evt-1", at=NOW + timedelta(days=365)) is not None


def test_purge_drops_only_expired_records():
    store = DedupStore(ttl=timedelta(hours=1))
    store.claim("old", at=NOW)
    store.claim("fresh", at=NOW + timedelta(hours=2))

    dropped = store.purge(at=NOW + timedelta(hours=2))

    assert dropped == 1
    assert [record.key for record in store] == ["fresh"]


def test_clearing_the_store_forgets_every_key():
    store = DedupStore()
    store.claim("evt-1", at=NOW)

    store.clear()

    assert len(store) == 0


def test_an_empty_key_is_rejected():
    with pytest.raises(ValueError):
        DedupStore().claim("", at=NOW)


def test_a_ttl_that_is_not_positive_is_rejected():
    with pytest.raises(ValueError):
        DedupStore(ttl=timedelta(0))
