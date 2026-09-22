"""Tests for the single dispatch API: rendering, routing and quiet hours."""

from __future__ import annotations

from datetime import time

import pytest
from conftest import ADA, BODY, CONTEXT, NIGHT, NOW, ORDER_CONFIRMED, OVERNIGHT, Recorder

from notify_dispatch import (
    Channel,
    DeliveryError,
    EmailAdapter,
    MissingVariableError,
    PushAdapter,
    QuietHours,
    QuietHoursError,
    Recipient,
    RetryPolicy,
    SmsAdapter,
    UnroutableError,
    Urgency,
    VariableTypeError,
)


def test_send_renders_once_and_delivers_over_the_chosen_channel(make_dispatcher):
    sms = Recorder(reference="sms-1")
    dispatcher = make_dispatcher(SmsAdapter(sms))

    receipt = dispatcher.send(Recipient(phone="+4915100000"), ORDER_CONFIRMED, CONTEXT)

    assert receipt.channel is Channel.SMS
    assert receipt.reference == "sms-1"
    assert receipt.attempts == 1
    assert receipt.message.body == BODY
    assert sms.calls == [("+4915100000", BODY)]


def test_the_rendered_body_is_reused_on_every_attempt(make_dispatcher):
    sms = Recorder(failures=1)
    dispatcher = make_dispatcher(
        SmsAdapter(sms), retry=RetryPolicy(attempts=2, backoff=0.0)
    )

    dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    first, second = sms.bodies
    # the same string object twice: rendered once, handed over twice
    assert first is second
    assert first == BODY


def test_default_order_prefers_push_over_sms_and_email(make_dispatcher):
    push, sms, email = Recorder(), Recorder(), Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms), PushAdapter(push), EmailAdapter(email))
    recipient = Recipient(phone="+491", device_token="tok", email="a@example.com")

    receipt = dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT)

    assert receipt.channel is Channel.PUSH
    assert push.calls == [("tok", BODY)]
    assert not sms.calls and not email.calls


def test_a_custom_order_changes_which_channel_wins(make_dispatcher):
    push, email = Recorder(), Recorder()
    dispatcher = make_dispatcher(
        PushAdapter(push),
        EmailAdapter(email),
        order=(Channel.EMAIL, Channel.PUSH, Channel.SMS),
    )
    recipient = Recipient(device_token="tok", email="a@example.com")

    receipt = dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT)

    assert dispatcher.channels == (Channel.EMAIL, Channel.PUSH)
    assert receipt.channel is Channel.EMAIL
    assert not push.calls


def test_recipient_preference_wins_over_dispatcher_order(make_dispatcher):
    push, email = Recorder(), Recorder()
    dispatcher = make_dispatcher(PushAdapter(push), EmailAdapter(email))
    recipient = Recipient(
        device_token="tok", email="a@example.com", preferred=(Channel.EMAIL,)
    )

    receipt = dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT, subject="Order")

    assert receipt.channel is Channel.EMAIL
    assert receipt.message.subject == "Order"
    assert email.calls == [("a@example.com", "Order", BODY)]
    assert not push.calls


def test_a_preferred_channel_without_an_adapter_falls_through(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))
    recipient = Recipient(
        phone="+491", device_token="tok", preferred=(Channel.PUSH, Channel.SMS)
    )

    receipt = dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT)

    assert receipt.channel is Channel.SMS
    assert sms.calls == [("+491", BODY)]


def test_a_preferred_channel_without_an_address_falls_through(make_dispatcher):
    push, email = Recorder(), Recorder()
    dispatcher = make_dispatcher(PushAdapter(push), EmailAdapter(email))
    recipient = Recipient(email="a@example.com", preferred=(Channel.PUSH, Channel.EMAIL))

    receipt = dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT)

    assert receipt.channel is Channel.EMAIL
    assert not push.calls


def test_channel_without_an_address_is_skipped(make_dispatcher):
    push, sms = Recorder(), Recorder()
    dispatcher = make_dispatcher(PushAdapter(push), SmsAdapter(sms))

    receipt = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert receipt.channel is Channel.SMS
    assert not push.calls


def test_explicit_channel_overrides_routing(make_dispatcher):
    push, email = Recorder(), Recorder()
    dispatcher = make_dispatcher(PushAdapter(push), EmailAdapter(email))
    recipient = Recipient(device_token="tok", email="a@example.com")

    receipt = dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT, channel=Channel.EMAIL)

    assert receipt.channel is Channel.EMAIL
    assert not push.calls


def test_explicit_channel_without_an_adapter_is_unroutable(make_dispatcher):
    dispatcher = make_dispatcher(PushAdapter(Recorder()))
    recipient = Recipient(device_token="tok", email="a@example.com")

    with pytest.raises(UnroutableError) as excinfo:
        dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT, channel=Channel.EMAIL)
    assert excinfo.value.tried == (Channel.EMAIL,)


def test_explicit_channel_without_an_address_is_unroutable(make_dispatcher):
    push, sms = Recorder(), Recorder()
    dispatcher = make_dispatcher(PushAdapter(push), SmsAdapter(sms))

    with pytest.raises(UnroutableError) as excinfo:
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, channel=Channel.PUSH)
    assert excinfo.value.tried == (Channel.PUSH,)
    assert not push.calls and not sms.calls


def test_recipient_without_a_reachable_channel_is_unroutable(make_dispatcher):
    dispatcher = make_dispatcher(SmsAdapter(Recorder()))

    with pytest.raises(UnroutableError) as excinfo:
        dispatcher.send(Recipient(email="a@example.com"), ORDER_CONFIRMED, CONTEXT)
    assert excinfo.value.template == "order_confirmed"


def test_email_falls_back_to_the_subject_of_its_adapter(make_dispatcher):
    email = Recorder()
    dispatcher = make_dispatcher(EmailAdapter(email, default_subject="Notification"))

    receipt = dispatcher.send(Recipient(email="a@example.com"), ORDER_CONFIRMED, CONTEXT)

    assert email.calls == [("a@example.com", "Notification", BODY)]
    assert receipt.message.subject is None


def test_template_errors_surface_before_any_provider_is_called(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))

    with pytest.raises(MissingVariableError):
        dispatcher.send(ADA, ORDER_CONFIRMED, {})
    assert not sms.calls


def test_a_value_of_the_wrong_type_never_reaches_a_provider(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))

    with pytest.raises(VariableTypeError):
        dispatcher.send(ADA, ORDER_CONFIRMED, {"order_id": 1042})
    assert not sms.calls


def test_provider_failure_is_wrapped_with_the_channel_and_address(make_dispatcher):
    dispatcher = make_dispatcher(SmsAdapter(Recorder(failures=1)))

    with pytest.raises(DeliveryError) as excinfo:
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)
    assert excinfo.value.channel is Channel.SMS
    assert excinfo.value.address == "+491"
    assert isinstance(excinfo.value.cause, RuntimeError)


def test_registering_replaces_the_adapter_for_a_channel(make_dispatcher):
    first, second = Recorder(), Recorder()
    dispatcher = make_dispatcher(SmsAdapter(first))
    dispatcher.register(SmsAdapter(second))

    dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert not first.calls and second.calls


def test_registered_channels_follow_the_dispatcher_order(make_dispatcher):
    dispatcher = make_dispatcher(EmailAdapter(Recorder()), PushAdapter(Recorder()))
    assert dispatcher.channels == (Channel.PUSH, Channel.EMAIL)


def test_reachable_channels_reflect_the_addresses_present():
    recipient = Recipient(phone="+491", email="a@example.com")
    assert recipient.reachable_channels == (Channel.SMS, Channel.EMAIL)


def test_quiet_hours_stop_a_normal_send(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))
    recipient = Recipient(phone="+491", quiet_hours=OVERNIGHT)

    with pytest.raises(QuietHoursError) as excinfo:
        dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT, now=NIGHT)
    assert excinfo.value.silenced == (Channel.SMS,)
    assert excinfo.value.until == time(7, 0)
    assert not sms.calls


def test_urgent_sends_override_quiet_hours(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))
    recipient = Recipient(phone="+491", quiet_hours=OVERNIGHT)

    receipt = dispatcher.send(
        recipient, ORDER_CONFIRMED, CONTEXT, urgency=Urgency.URGENT, now=NIGHT
    )

    assert receipt.channel is Channel.SMS
    assert receipt.message.urgency is Urgency.URGENT
    assert sms.calls == [("+491", BODY)]


def test_outside_the_window_a_normal_send_goes_through(make_dispatcher):
    dispatcher = make_dispatcher(SmsAdapter(Recorder()))
    recipient = Recipient(phone="+491", quiet_hours=OVERNIGHT)

    receipt = dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT, now=NOW)

    assert receipt.channel is Channel.SMS
    assert receipt.message.urgency is Urgency.NORMAL


def test_a_channel_outside_the_window_is_used_instead(make_dispatcher):
    push, email = Recorder(), Recorder()
    dispatcher = make_dispatcher(PushAdapter(push), EmailAdapter(email))
    recipient = Recipient(
        device_token="tok",
        email="a@example.com",
        quiet_hours=QuietHours(
            start=time(22, 0), end=time(7, 0), channels=(Channel.PUSH, Channel.SMS)
        ),
    )

    receipt = dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT, now=NIGHT)

    assert receipt.channel is Channel.EMAIL
    assert not push.calls


def test_a_pinned_channel_still_respects_quiet_hours(make_dispatcher):
    push = Recorder()
    dispatcher = make_dispatcher(PushAdapter(push))
    recipient = Recipient(device_token="tok", quiet_hours=OVERNIGHT)

    with pytest.raises(QuietHoursError):
        dispatcher.send(
            recipient, ORDER_CONFIRMED, CONTEXT, channel=Channel.PUSH, now=NIGHT
        )
    assert not push.calls


def test_the_dispatcher_clock_is_used_when_no_moment_is_given(make_dispatcher, clock):
    clock.now = NIGHT
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))
    recipient = Recipient(phone="+491", quiet_hours=OVERNIGHT)

    with pytest.raises(QuietHoursError):
        dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT)
    assert not sms.calls


def test_a_given_moment_overrides_the_dispatcher_clock(make_dispatcher, clock):
    clock.now = NIGHT
    dispatcher = make_dispatcher(SmsAdapter(Recorder()))
    recipient = Recipient(phone="+491", quiet_hours=OVERNIGHT)

    receipt = dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT, now=NOW)

    assert receipt.channel is Channel.SMS


def test_a_window_that_wraps_past_midnight_covers_both_sides():
    assert OVERNIGHT.covers(time(23, 30))
    assert OVERNIGHT.covers(time(2, 0))
    assert not OVERNIGHT.covers(time(7, 0))
    assert not OVERNIGHT.covers(time(12, 0))


def test_a_window_within_one_day_covers_only_that_range():
    window = QuietHours(start=time(9, 0), end=time(17, 0))
    assert window.covers(time(12, 0))
    assert not window.covers(time(8, 59))
    assert not window.covers(time(17, 0))


def test_a_recipient_without_quiet_hours_is_never_silenced():
    assert not ADA.is_quiet(Channel.SMS, NIGHT)


def test_an_empty_window_is_rejected():
    with pytest.raises(ValueError):
        QuietHours(start=time(22, 0), end=time(22, 0))


def test_a_window_silencing_nothing_is_rejected():
    with pytest.raises(ValueError):
        QuietHours(start=time(22, 0), end=time(7, 0), channels=())
