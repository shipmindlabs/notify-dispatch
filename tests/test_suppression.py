"""Tests for addresses that may not be handed to a provider."""

from __future__ import annotations

import pytest
from conftest import ADA, BODY, CONTEXT, NIGHT, NOW, ORDER_CONFIRMED, OVERNIGHT, Recorder

from notify_dispatch import (
    Channel,
    PushAdapter,
    Recipient,
    SmsAdapter,
    SuppressedError,
    Suppression,
    SuppressionList,
    Urgency,
)

REACHABLE = Recipient(device_token="tok", phone="+491", email="a@example.com")


def test_a_suppressed_channel_is_skipped_like_a_missing_address(make_dispatcher):
    push, sms = Recorder(), Recorder()
    blocked = SuppressionList()
    blocked.suppress("tok", channel=Channel.PUSH, reason="token was rejected")
    dispatcher = make_dispatcher(
        PushAdapter(push), SmsAdapter(sms), suppressions=blocked
    )

    receipt = dispatcher.send(REACHABLE, ORDER_CONFIRMED, CONTEXT)

    assert receipt.channel is Channel.SMS
    assert not push.calls
    assert sms.calls == [("+491", BODY)]


def test_a_recipient_with_nothing_left_is_refused(make_dispatcher):
    sms = Recorder()
    blocked = SuppressionList([Suppression("+491", reason="unsubscribed")])
    dispatcher = make_dispatcher(SmsAdapter(sms), suppressions=blocked)

    with pytest.raises(SuppressedError) as excinfo:
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert excinfo.value.suppressed == (Channel.SMS,)
    assert excinfo.value.refused[Channel.SMS].reason == "unsubscribed"
    assert "unsubscribed" in str(excinfo.value)
    assert not sms.calls


def test_an_urgent_send_does_not_override_a_suppression(make_dispatcher):
    sms = Recorder()
    blocked = SuppressionList([Suppression("+491", channel=Channel.SMS)])
    dispatcher = make_dispatcher(SmsAdapter(sms), suppressions=blocked)

    with pytest.raises(SuppressedError):
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, urgency=Urgency.URGENT)

    assert not sms.calls


def test_a_suppression_is_reported_before_a_silenced_channel(make_dispatcher):
    push, sms = Recorder(), Recorder()
    blocked = SuppressionList([Suppression("tok", channel=Channel.PUSH)])
    dispatcher = make_dispatcher(
        PushAdapter(push), SmsAdapter(sms), suppressions=blocked
    )
    recipient = Recipient(device_token="tok", phone="+491", quiet_hours=OVERNIGHT)

    with pytest.raises(SuppressedError) as excinfo:
        dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT, now=NIGHT, fallback=True)

    assert excinfo.value.suppressed == (Channel.PUSH,)
    assert not push.calls and not sms.calls


def test_a_suppressed_send_leaves_the_dedup_key_free(make_dispatcher):
    sms = Recorder()
    blocked = SuppressionList([Suppression("+491")])
    dispatcher = make_dispatcher(SmsAdapter(sms), suppressions=blocked)

    with pytest.raises(SuppressedError):
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")
    blocked.allow("+491")
    receipt = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    assert not receipt.duplicate
    assert len(sms.calls) == 1


def test_a_dispatcher_without_a_list_suppresses_nothing(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))

    dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert len(dispatcher.suppressions) == 0
    assert sms.calls == [("+491", BODY)]


def test_a_shared_list_refuses_on_every_dispatcher(make_dispatcher):
    blocked = SuppressionList()
    first = make_dispatcher(SmsAdapter(Recorder()), suppressions=blocked)
    second = make_dispatcher(SmsAdapter(Recorder()), suppressions=blocked)
    blocked.suppress("+491", channel=Channel.SMS)

    for dispatcher in (first, second):
        with pytest.raises(SuppressedError):
            dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)


def test_an_entry_without_a_channel_covers_every_channel():
    blocked = SuppressionList()
    blocked.suppress("a@example.com")

    assert blocked.is_suppressed("a@example.com", Channel.EMAIL)
    assert blocked.is_suppressed("a@example.com", Channel.SMS)


def test_an_entry_with_a_channel_leaves_the_others_alone():
    blocked = SuppressionList()
    blocked.suppress("+491", channel=Channel.SMS)

    assert blocked.is_suppressed("+491", Channel.SMS)
    assert not blocked.is_suppressed("+491", Channel.PUSH)
    assert not blocked.is_suppressed("+492", Channel.SMS)


def test_the_channels_own_entry_is_the_one_reported():
    blocked = SuppressionList()
    blocked.suppress("+491", reason="unsubscribed")
    blocked.suppress("+491", channel=Channel.SMS, reason="carrier rejected it")

    assert blocked.suppression_for("+491", Channel.SMS).reason == "carrier rejected it"
    assert blocked.suppression_for("+491", Channel.EMAIL).reason == "unsubscribed"


def test_allowing_an_address_lifts_every_entry_it_has():
    blocked = SuppressionList()
    blocked.suppress("+491", channel=Channel.SMS)
    blocked.suppress("+491")

    lifted = blocked.allow("+491")

    assert len(lifted) == 2
    assert not blocked.is_suppressed("+491", Channel.SMS)
    assert len(blocked) == 0


def test_allowing_one_channel_keeps_the_rest():
    blocked = SuppressionList()
    blocked.suppress("+491", channel=Channel.SMS)
    blocked.suppress("tok", channel=Channel.PUSH)

    assert blocked.allow("+491", channel=Channel.SMS)
    assert blocked.allow("+491", channel=Channel.SMS) == ()
    assert blocked.is_suppressed("tok", Channel.PUSH)


def test_an_entry_records_when_and_why():
    blocked = SuppressionList()

    entry = blocked.suppress("+491", channel=Channel.SMS, reason="unsubscribed", at=NOW)

    assert entry.at == NOW
    assert entry.covers("+491", Channel.SMS)
    assert "unsubscribed" in str(entry)
    assert blocked.entries == (entry,)
    assert list(blocked) == [entry]


def test_clearing_the_list_forgets_every_entry():
    blocked = SuppressionList([Suppression("+491")])

    blocked.clear()

    assert len(blocked) == 0
    assert not blocked.is_suppressed("+491", Channel.SMS)


def test_an_empty_address_is_rejected():
    with pytest.raises(ValueError):
        SuppressionList().suppress("")
