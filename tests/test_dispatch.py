"""Tests for the single dispatch API across channels."""

from __future__ import annotations

import pytest

from notify_dispatch import (
    Channel,
    DeliveryError,
    Dispatcher,
    EmailAdapter,
    MissingVariableError,
    PushAdapter,
    Recipient,
    SmsAdapter,
    Template,
    UnroutableError,
    Variable,
)

ORDER_CONFIRMED = Template(
    name="order_confirmed",
    text="Order {order_id} confirmed.",
    variables=(Variable("order_id", str),),
)

CONTEXT = {"order_id": "A-1042"}


class Provider:
    """Records what a provider was asked to send."""

    def __init__(self, reference: str | None = None, fail: bool = False) -> None:
        self.reference = reference
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> str | None:
        if self.fail:
            raise RuntimeError("provider is down")
        self.calls.append(args)
        return self.reference


def test_send_renders_once_and_delivers_over_the_chosen_channel():
    sms = Provider(reference="sms-1")
    dispatcher = Dispatcher([SmsAdapter(sms)])
    recipient = Recipient(phone="+4915100000")

    receipt = dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT)

    assert receipt.channel is Channel.SMS
    assert receipt.reference == "sms-1"
    assert receipt.message.body == "Order A-1042 confirmed."
    assert sms.calls == [("+4915100000", "Order A-1042 confirmed.")]


def test_default_order_prefers_push_over_sms_and_email():
    push, sms, email = Provider(), Provider(), Provider()
    dispatcher = Dispatcher(
        [SmsAdapter(sms), PushAdapter(push), EmailAdapter(email)]
    )
    recipient = Recipient(phone="+491", device_token="tok", email="a@example.com")

    receipt = dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT)

    assert receipt.channel is Channel.PUSH
    assert push.calls and not sms.calls and not email.calls


def test_recipient_preference_wins_over_dispatcher_order():
    push, email = Provider(), Provider()
    dispatcher = Dispatcher([PushAdapter(push), EmailAdapter(email)])
    recipient = Recipient(
        device_token="tok", email="a@example.com", preferred=(Channel.EMAIL,)
    )

    receipt = dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT, subject="Order")

    assert receipt.channel is Channel.EMAIL
    assert email.calls == [("a@example.com", "Order", "Order A-1042 confirmed.")]


def test_channel_without_an_address_is_skipped():
    push, sms = Provider(), Provider()
    dispatcher = Dispatcher([PushAdapter(push), SmsAdapter(sms)])

    receipt = dispatcher.send(Recipient(phone="+491"), ORDER_CONFIRMED, CONTEXT)

    assert receipt.channel is Channel.SMS
    assert not push.calls


def test_explicit_channel_overrides_routing():
    push, email = Provider(), Provider()
    dispatcher = Dispatcher([PushAdapter(push), EmailAdapter(email)])
    recipient = Recipient(device_token="tok", email="a@example.com")

    receipt = dispatcher.send(
        recipient, ORDER_CONFIRMED, CONTEXT, channel=Channel.EMAIL
    )

    assert receipt.channel is Channel.EMAIL
    assert not push.calls


def test_explicit_channel_without_an_adapter_is_unroutable():
    dispatcher = Dispatcher([PushAdapter(Provider())])
    recipient = Recipient(device_token="tok", email="a@example.com")

    with pytest.raises(UnroutableError) as excinfo:
        dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT, channel=Channel.EMAIL)
    assert excinfo.value.tried == (Channel.EMAIL,)


def test_recipient_without_a_reachable_channel_is_unroutable():
    dispatcher = Dispatcher([SmsAdapter(Provider())])

    with pytest.raises(UnroutableError):
        dispatcher.send(Recipient(email="a@example.com"), ORDER_CONFIRMED, CONTEXT)


def test_template_errors_surface_before_any_provider_is_called():
    sms = Provider()
    dispatcher = Dispatcher([SmsAdapter(sms)])

    with pytest.raises(MissingVariableError):
        dispatcher.send(Recipient(phone="+491"), ORDER_CONFIRMED, {})
    assert not sms.calls


def test_provider_failure_is_wrapped_with_the_channel_and_address():
    dispatcher = Dispatcher([SmsAdapter(Provider(fail=True))])

    with pytest.raises(DeliveryError) as excinfo:
        dispatcher.send(Recipient(phone="+491"), ORDER_CONFIRMED, CONTEXT)
    assert excinfo.value.channel is Channel.SMS
    assert excinfo.value.address == "+491"
    assert isinstance(excinfo.value.cause, RuntimeError)


def test_registering_replaces_the_adapter_for_a_channel():
    first, second = Provider(), Provider()
    dispatcher = Dispatcher([SmsAdapter(first)])
    dispatcher.register(SmsAdapter(second))

    dispatcher.send(Recipient(phone="+491"), ORDER_CONFIRMED, CONTEXT)

    assert not first.calls and second.calls


def test_registered_channels_follow_the_dispatcher_order():
    dispatcher = Dispatcher([EmailAdapter(Provider()), PushAdapter(Provider())])
    assert dispatcher.channels == (Channel.PUSH, Channel.EMAIL)


def test_reachable_channels_reflect_the_addresses_present():
    recipient = Recipient(phone="+491", email="a@example.com")
    assert recipient.reachable_channels == (Channel.SMS, Channel.EMAIL)
