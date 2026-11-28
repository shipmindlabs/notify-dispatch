"""Tests for one notification sent to a whole audience."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from conftest import CONTEXT, NIGHT, ORDER_CONFIRMED, OVERNIGHT, FakeClock, Recorder

from notify_dispatch import (
    Channel,
    DeliveryError,
    LocalizedTemplate,
    MissingVariableError,
    PushAdapter,
    QuietHoursError,
    Recipient,
    SmsAdapter,
    SuppressedError,
    Suppression,
    SuppressionList,
    UnroutableError,
    Variable,
)

ADA = Recipient(phone="+491")
BEN = Recipient(phone="+492")
KENJI = Recipient(phone="+493", locale="de")
GRACE = Recipient(email="grace@example.com")

SHIPPED = LocalizedTemplate.from_texts(
    "order_shipped",
    {
        "en": "Order {order_id} is on its way.",
        "de": "Bestellung {order_id} ist unterwegs.",
    },
    variables=(Variable("order_id", str),),
)


def test_every_recipient_is_sent_to_in_order(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))

    batch = dispatcher.send_many([ADA, BEN], ORDER_CONFIRMED, CONTEXT)

    assert batch.template == "order_confirmed"
    assert batch.sent == 2
    assert batch.failed == 0
    assert batch.all_sent
    assert len(batch) == 2
    assert [result.recipient.phone for result in batch] == ["+491", "+492"]
    assert [call[0] for call in sms.calls] == ["+491", "+492"]
    assert all(result.is_sent for result in batch)


def test_a_recipient_nobody_can_reach_does_not_stop_the_batch(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))

    batch = dispatcher.send_many([GRACE, ADA], ORDER_CONFIRMED, CONTEXT)

    assert batch.sent == 1
    assert batch.failed == 1
    assert not batch.all_sent
    (failure,) = batch.failures
    assert failure.recipient is GRACE
    assert isinstance(failure.error, UnroutableError)
    assert failure.is_failed and not failure.is_sent
    assert len(batch.receipts) == 1
    assert sms.calls == [("+491", "Order A-1042 confirmed.")]


def test_a_failed_delivery_is_a_result_and_a_dead_letter(make_dispatcher):
    push, sms = Recorder(failures=99), Recorder()
    dispatcher = make_dispatcher(PushAdapter(push), SmsAdapter(sms))

    batch = dispatcher.send_many(
        [Recipient(device_token="tok"), ADA], ORDER_CONFIRMED, CONTEXT
    )

    assert isinstance(batch.failures[0].error, DeliveryError)
    assert batch.sent == 1
    assert len(dispatcher.dead_letters) == 1


def test_a_suppressed_recipient_is_a_result_too(make_dispatcher):
    sms = Recorder()
    blocked = SuppressionList([Suppression("+491", reason="unsubscribed")])
    dispatcher = make_dispatcher(SmsAdapter(sms), suppressions=blocked)

    batch = dispatcher.send_many([ADA, BEN], ORDER_CONFIRMED, CONTEXT)

    assert isinstance(batch.failures[0].error, SuppressedError)
    assert batch.sent == 1
    assert "1 of 2" in str(batch)


def test_a_silenced_recipient_is_refused_while_the_others_are_sent(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))
    asleep = Recipient(phone="+494", quiet_hours=OVERNIGHT)

    batch = dispatcher.send_many([ADA, asleep], ORDER_CONFIRMED, CONTEXT, now=NIGHT)

    assert batch.sent == 1
    assert isinstance(batch.failures[0].error, QuietHoursError)
    assert len(sms.calls) == 1


def test_the_whole_audience_shares_one_reading_of_the_clock(make_dispatcher):
    sms = Recorder()
    ticking = FakeClock(now=datetime(2026, 3, 1, 21, 59), step=timedelta(minutes=2))
    dispatcher = make_dispatcher(SmsAdapter(sms), clock=ticking)
    audience = [
        Recipient(phone="+491", quiet_hours=OVERNIGHT),
        Recipient(phone="+492", quiet_hours=OVERNIGHT),
    ]

    batch = dispatcher.send_many(audience, ORDER_CONFIRMED, CONTEXT)

    # the window opens at 22:00, which a per-recipient clock would already read
    assert batch.sent == 2
    assert len(sms.calls) == 2


def test_a_template_error_fails_the_batch_before_anything_is_sent(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))

    with pytest.raises(MissingVariableError):
        dispatcher.send_many([ADA, BEN], ORDER_CONFIRMED, {})

    assert not sms.calls


def test_a_redelivered_batch_is_deduplicated_per_recipient(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))

    def key(recipient: Recipient) -> str:
        return f"evt-1:{recipient.phone}"

    first = dispatcher.send_many([ADA, BEN], ORDER_CONFIRMED, CONTEXT, dedup_key=key)
    second = dispatcher.send_many([ADA, BEN], ORDER_CONFIRMED, CONTEXT, dedup_key=key)

    assert first.sent == second.sent == 2
    assert [receipt.duplicate for receipt in first.receipts] == [False, False]
    assert [receipt.duplicate for receipt in second.receipts] == [True, True]
    assert len(sms.calls) == 2
    assert len(dispatcher.dedup) == 2


def test_a_dedup_key_shared_by_the_audience_is_refused(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))

    with pytest.raises(TypeError):
        dispatcher.send_many([ADA, BEN], ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    assert not sms.calls


def test_each_recipient_gets_the_variant_of_its_own_locale(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))

    batch = dispatcher.send_many([ADA, KENJI], SHIPPED, CONTEXT)

    assert [receipt.message.locale for receipt in batch.receipts] == ["en", "de"]
    assert sms.bodies == [
        "Order A-1042 is on its way.",
        "Bestellung A-1042 ist unterwegs.",
    ]


def test_fallback_applies_to_every_recipient(make_dispatcher):
    push, sms = Recorder(failures=99), Recorder()
    dispatcher = make_dispatcher(PushAdapter(push), SmsAdapter(sms), fallback=True)
    audience = [
        Recipient(device_token="tok-1", phone="+491"),
        Recipient(device_token="tok-2", phone="+492"),
    ]

    batch = dispatcher.send_many(audience, ORDER_CONFIRMED, CONTEXT)

    assert [receipt.channel for receipt in batch.receipts] == [
        Channel.SMS,
        Channel.SMS,
    ]
    assert len(push.calls) == 2
    assert len(dispatcher.dead_letters) == 0


def test_an_empty_audience_sends_nothing(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))

    batch = dispatcher.send_many([], ORDER_CONFIRMED, CONTEXT)

    assert len(batch) == 0
    assert batch.receipts == ()
    assert batch.all_sent
    assert not sms.calls
