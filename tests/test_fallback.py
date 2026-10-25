"""Tests for falling back to the next channel after an exhausted delivery."""

from __future__ import annotations

from datetime import time

import pytest
from conftest import BODY, CONTEXT, NIGHT, ORDER_CONFIRMED, Recorder

from notify_dispatch import (
    Channel,
    DeliveryError,
    EmailAdapter,
    PushAdapter,
    QuietHours,
    Recipient,
    RetryPolicy,
    SmsAdapter,
    Urgency,
)

REACHABLE = Recipient(device_token="tok", phone="+491", email="a@example.com")
QUIET_SMS = QuietHours(start=time(22, 0), end=time(7, 0), channels=(Channel.SMS,))


def test_push_fails_sms_delivers_and_nothing_is_a_dead_letter(make_dispatcher):
    push, sms = Recorder(failures=99), Recorder(reference="sms-1")
    dispatcher = make_dispatcher(PushAdapter(push), SmsAdapter(sms), fallback=True)

    receipt = dispatcher.send(REACHABLE, ORDER_CONFIRMED, CONTEXT)

    assert receipt.channel is Channel.SMS
    assert receipt.reference == "sms-1"
    assert receipt.attempts == 2
    assert push.calls == [("tok", BODY)]
    assert sms.calls == [("+491", BODY)]
    assert len(dispatcher.dead_letters) == 0


def test_without_fallback_the_chosen_channel_is_the_only_one(make_dispatcher):
    push, sms = Recorder(failures=99), Recorder()
    dispatcher = make_dispatcher(PushAdapter(push), SmsAdapter(sms))

    with pytest.raises(DeliveryError) as excinfo:
        dispatcher.send(REACHABLE, ORDER_CONFIRMED, CONTEXT)

    assert excinfo.value.channel is Channel.PUSH
    assert not sms.calls
    assert len(dispatcher.dead_letters) == 1


def test_every_attempt_is_counted_across_the_channels(make_dispatcher, sleeps):
    push, sms = Recorder(failures=99), Recorder(failures=1)
    dispatcher = make_dispatcher(
        PushAdapter(push),
        SmsAdapter(sms),
        retry=RetryPolicy(attempts=2, backoff=0.25),
        fallback=True,
    )

    receipt = dispatcher.send(REACHABLE, ORDER_CONFIRMED, CONTEXT)

    assert receipt.channel is Channel.SMS
    assert receipt.attempts == 4
    assert len(push.calls) == 2
    assert len(sms.calls) == 2
    assert sleeps == [0.25, 0.25]


def test_an_exhausted_last_channel_writes_one_dead_letter(make_dispatcher):
    push = Recorder(failures=99)
    sms = Recorder(failures=99)
    email = Recorder(failures=99)
    dispatcher = make_dispatcher(
        PushAdapter(push), SmsAdapter(sms), EmailAdapter(email), fallback=True
    )

    with pytest.raises(DeliveryError) as excinfo:
        dispatcher.send(REACHABLE, ORDER_CONFIRMED, CONTEXT)

    assert excinfo.value.channel is Channel.EMAIL
    assert [attempt.channel for attempt in excinfo.value.attempts] == [
        Channel.PUSH,
        Channel.SMS,
        Channel.EMAIL,
    ]
    assert [attempt.number for attempt in excinfo.value.attempts] == [1, 2, 3]

    (letter,) = dispatcher.dead_letters.letters
    assert letter.channel is Channel.EMAIL
    assert len(letter.attempts) == 3


def test_a_silenced_channel_is_not_used_as_a_fallback(make_dispatcher):
    push, sms, email = Recorder(failures=99), Recorder(), Recorder()
    dispatcher = make_dispatcher(
        PushAdapter(push), SmsAdapter(sms), EmailAdapter(email), fallback=True
    )
    recipient = Recipient(
        device_token="tok",
        phone="+491",
        email="a@example.com",
        quiet_hours=QUIET_SMS,
    )

    receipt = dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT, now=NIGHT)

    assert receipt.channel is Channel.EMAIL
    assert not sms.calls


def test_an_urgent_send_falls_back_onto_a_silenced_channel(make_dispatcher):
    push, sms = Recorder(failures=99), Recorder()
    dispatcher = make_dispatcher(PushAdapter(push), SmsAdapter(sms), fallback=True)
    recipient = Recipient(device_token="tok", phone="+491", quiet_hours=QUIET_SMS)

    receipt = dispatcher.send(
        recipient, ORDER_CONFIRMED, CONTEXT, urgency=Urgency.URGENT, now=NIGHT
    )

    assert receipt.channel is Channel.SMS
    assert sms.calls == [("+491", BODY)]


def test_a_channel_without_an_address_is_skipped_on_the_way_down(make_dispatcher):
    push, sms, email = Recorder(failures=99), Recorder(), Recorder()
    dispatcher = make_dispatcher(
        PushAdapter(push), SmsAdapter(sms), EmailAdapter(email), fallback=True
    )

    receipt = dispatcher.send(
        Recipient(device_token="tok", email="a@example.com"), ORDER_CONFIRMED, CONTEXT
    )

    assert receipt.channel is Channel.EMAIL
    assert not sms.calls


def test_a_pinned_channel_has_nothing_to_fall_back_to(make_dispatcher):
    push, sms = Recorder(failures=99), Recorder()
    dispatcher = make_dispatcher(PushAdapter(push), SmsAdapter(sms), fallback=True)

    with pytest.raises(DeliveryError):
        dispatcher.send(REACHABLE, ORDER_CONFIRMED, CONTEXT, channel=Channel.PUSH)

    assert not sms.calls
    assert len(dispatcher.dead_letters) == 1


def test_a_per_call_fallback_overrides_the_dispatcher(make_dispatcher):
    push, sms = Recorder(failures=99), Recorder()
    dispatcher = make_dispatcher(PushAdapter(push), SmsAdapter(sms))

    receipt = dispatcher.send(REACHABLE, ORDER_CONFIRMED, CONTEXT, fallback=True)

    assert receipt.channel is Channel.SMS
    assert not dispatcher.fallback


def test_a_per_call_fallback_may_also_switch_it_off(make_dispatcher):
    push, sms = Recorder(failures=99), Recorder()
    dispatcher = make_dispatcher(PushAdapter(push), SmsAdapter(sms), fallback=True)

    with pytest.raises(DeliveryError):
        dispatcher.send(REACHABLE, ORDER_CONFIRMED, CONTEXT, fallback=False)

    assert not sms.calls


def test_a_dedup_key_is_kept_with_the_channel_that_worked(make_dispatcher):
    push, sms = Recorder(failures=99), Recorder(reference="sms-1")
    dispatcher = make_dispatcher(PushAdapter(push), SmsAdapter(sms), fallback=True)

    first = dispatcher.send(REACHABLE, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")
    second = dispatcher.send(REACHABLE, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    assert first.channel is Channel.SMS
    assert second.duplicate
    assert second.channel is Channel.SMS
    assert second.reference == "sms-1"
    assert second.attempts == 2
    assert len(push.calls) == 1
    assert len(sms.calls) == 1


def test_a_failure_on_every_channel_leaves_the_dedup_key_free(make_dispatcher):
    push, sms = Recorder(failures=99), Recorder(failures=1)
    dispatcher = make_dispatcher(PushAdapter(push), SmsAdapter(sms), fallback=True)

    with pytest.raises(DeliveryError):
        dispatcher.send(REACHABLE, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")
    receipt = dispatcher.send(REACHABLE, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    assert not receipt.duplicate
    assert receipt.channel is Channel.SMS
    assert len(dispatcher.dead_letters) == 1
