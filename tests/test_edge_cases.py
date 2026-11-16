"""Edge cases and failure modes of the behaviour added most recently.

The happy paths live in the suites next to this one; what is collected here are
the awkward corners: a window hit exactly on its boundary, a key that expires
while it is still in flight, a language subtag nobody should accept, a channel
that is unreachable only because its adapter was never registered, and the
things a ticket does when the send behind it was never going to work.
"""

from __future__ import annotations

from datetime import time, timedelta

import pytest
from conftest import ADA, CONTEXT, NIGHT, NOW, ORDER_CONFIRMED, OVERNIGHT, Recorder

from notify_dispatch import (
    Channel,
    DedupStore,
    DeliveryError,
    DuplicateSendError,
    EmailAdapter,
    LocalizedTemplate,
    MalformedLocaleError,
    Message,
    MissingLocaleError,
    Outbox,
    PushAdapter,
    QuietHours,
    QuietHoursError,
    Receipt,
    Recipient,
    RetryPolicy,
    SmsAdapter,
    Template,
    UnroutableError,
    Variable,
    Worker,
    fallback_chain,
    normalize_locale,
)

REACHABLE = Recipient(device_token="tok", phone="+491", email="a@example.com")
QUIET_SMS = QuietHours(start=time(22, 0), end=time(7, 0), channels=(Channel.SMS,))

ORDER_ID = (Variable("order_id", str),)
SHIPPED = LocalizedTemplate.from_texts(
    "order_shipped",
    {
        "en": "Order {order_id} is on its way.",
        "de": "Bestellung {order_id} ist unterwegs.",
    },
    variables=ORDER_ID,
)


def a_receipt() -> Receipt:
    message = Message(
        template="order_confirmed",
        body="Order A-1042 confirmed.",
        channel=Channel.SMS,
        address="+491",
    )
    return Receipt(message=message, reference="ref-1")


def test_a_wrapping_window_includes_its_start_and_not_its_end():
    window = QuietHours(start=time(22, 0), end=time(7, 0))

    assert window.covers(time(22, 0))
    assert window.covers(time(6, 59))
    assert not window.covers(time(7, 0))


def test_a_window_inside_one_day_does_not_wrap():
    window = QuietHours(start=time(9, 0), end=time(17, 0))

    assert window.covers(time(9, 0))
    assert not window.covers(time(17, 0))
    assert not window.covers(time(3, 0))


def test_a_window_that_ends_when_it_starts_is_rejected():
    with pytest.raises(ValueError):
        QuietHours(start=time(22, 0), end=time(22, 0))


def test_a_window_that_silences_nothing_is_rejected():
    with pytest.raises(ValueError):
        QuietHours(start=time(22, 0), end=time(7, 0), channels=())


def test_a_window_silences_every_channel_unless_it_names_some():
    assert QuietHours(start=time(22, 0), end=time(7, 0)).channels == tuple(Channel)


def test_a_pinned_channel_inside_the_window_is_refused(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))
    recipient = Recipient(
        phone="+491", email="a@example.com", quiet_hours=OVERNIGHT
    )

    with pytest.raises(QuietHoursError) as excinfo:
        dispatcher.send(
            recipient, ORDER_CONFIRMED, CONTEXT, channel=Channel.SMS, now=NIGHT
        )

    assert excinfo.value.silenced == (Channel.SMS,)
    assert "Urgency.URGENT" in str(excinfo.value)
    assert not sms.calls


def test_a_channel_without_an_adapter_is_unroutable_not_silenced(make_dispatcher):
    email = Recorder()
    dispatcher = make_dispatcher(EmailAdapter(email))
    recipient = Recipient(phone="+491", quiet_hours=OVERNIGHT)

    with pytest.raises(UnroutableError) as excinfo:
        dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT, now=NIGHT)

    assert Channel.SMS in excinfo.value.tried
    assert not email.calls


@pytest.mark.parametrize("tag", ["en-abcdefghi", "en-gb!", "de--AT", "e", "en gb"])
def test_an_unusable_subtag_is_rejected(tag):
    with pytest.raises(MalformedLocaleError):
        normalize_locale(tag)


def test_a_three_letter_language_and_a_script_keep_their_shape():
    assert normalize_locale("HAW") == "haw"
    assert normalize_locale("zh-hant-tw") == "zh-Hant-TW"


def test_a_chain_of_one_subtag_is_just_itself():
    assert fallback_chain("de") == ("de",)
    assert fallback_chain("de", "de") == ("de",)


def test_a_single_variant_answers_every_locale():
    ping = LocalizedTemplate.from_texts("ping", {"en": "Your driver is nearby."})

    assert ping.chain() == ("en",)
    assert ping.resolve("ja-JP") == ("en", ping.variants["en"])


def test_variants_may_declare_the_same_variables_in_another_order():
    declared = (Variable("order_id", str), Variable("item_count", int))
    template = LocalizedTemplate(
        name="order_shipped",
        variants={
            "en": Template(
                "order_shipped", "Order {order_id}: {item_count} item(s).", declared
            ),
            "de": Template(
                "order_shipped",
                "Bestellung {order_id}: {item_count} Teil(e).",
                declared[::-1],
            ),
        },
    )

    assert template.locales == ("en", "de")


def test_the_variants_of_a_localized_template_cannot_be_rewritten():
    with pytest.raises(TypeError):
        SHIPPED.variants["fr"] = SHIPPED.variants["en"]


def test_a_default_is_normalized_before_it_is_looked_for():
    with pytest.raises(MissingLocaleError):
        LocalizedTemplate(
            name="order_shipped",
            variants={
                "en": Template(
                    "order_shipped", "Order {order_id} is on its way.", ORDER_ID
                )
            },
            default="en_us",
        )


def test_a_malformed_locale_is_refused_before_a_provider_is_reached(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))

    with pytest.raises(MalformedLocaleError):
        dispatcher.send(ADA, SHIPPED, CONTEXT, locale="de-", dedup_key="evt-1")

    assert not sms.calls
    assert dispatcher.dedup.get("evt-1", at=NOW) is None


def test_a_claim_that_never_completed_expires_as_well():
    store = DedupStore(ttl=timedelta(hours=1))

    assert store.claim("evt-1", at=NOW) is None
    assert store.claim("evt-1", at=NOW + timedelta(minutes=30)) is not None
    assert store.claim("evt-1", at=NOW + timedelta(hours=1)) is None

    record = store.get("evt-1", at=NOW + timedelta(hours=1))
    assert record is not None
    assert record.is_pending


def test_a_ttl_is_counted_from_the_moment_a_key_completed():
    store = DedupStore(ttl=timedelta(hours=1))
    store.claim("evt-1", at=NOW)
    store.complete("evt-1", a_receipt(), at=NOW + timedelta(minutes=50))

    assert store.get("evt-1", at=NOW + timedelta(minutes=70)) is not None
    assert store.get("evt-1", at=NOW + timedelta(hours=2)) is None


def test_releasing_a_key_nobody_claimed_is_harmless():
    store = DedupStore()

    store.release("evt-1")

    assert len(store) == 0


def test_an_empty_key_is_rejected_wherever_it_is_used():
    store = DedupStore()

    with pytest.raises(ValueError):
        store.get("", at=NOW)
    with pytest.raises(ValueError):
        store.release("")
    with pytest.raises(ValueError):
        store.complete("", a_receipt(), at=NOW)


def test_a_record_says_whether_it_is_still_in_flight():
    store = DedupStore()
    store.claim("evt-1", at=NOW)

    assert "in flight" in str(store.get("evt-1", at=NOW))

    store.complete("evt-1", a_receipt(), at=NOW)

    assert "delivered" in str(store.get("evt-1", at=NOW))
    assert "1 keys" in repr(store)


def test_a_duplicate_receipt_does_not_replace_the_remembered_one(make_dispatcher):
    provider = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(provider))

    first = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")
    dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")
    third = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    record = dispatcher.dedup.get("evt-1", at=NOW)
    assert record is not None
    assert record.receipt == first
    assert not record.receipt.duplicate
    assert third.duplicate
    assert len(provider.calls) == 1


def test_a_key_in_flight_names_the_template_it_belongs_to(make_dispatcher):
    store = DedupStore()
    store.claim("evt-1", at=NOW)
    provider = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(provider), dedup=store)

    with pytest.raises(DuplicateSendError) as excinfo:
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    assert excinfo.value.template == "order_confirmed"
    assert "evt-1" in str(excinfo.value)
    assert store.get("evt-1", at=NOW) is not None
    assert not provider.calls


def test_an_error_the_policy_does_not_retry_falls_back_at_once(
    make_dispatcher, sleeps
):
    push = Recorder(failures=99, error=ValueError("the token is gone"))
    sms = Recorder(reference="sms-1")
    dispatcher = make_dispatcher(
        PushAdapter(push),
        SmsAdapter(sms),
        retry=RetryPolicy(attempts=3, backoff=0.25, retry_on=(TimeoutError,)),
        fallback=True,
    )

    receipt = dispatcher.send(REACHABLE, ORDER_CONFIRMED, CONTEXT)

    assert receipt.channel is Channel.SMS
    assert receipt.attempts == 2
    assert len(push.calls) == 1
    assert sleeps == []


def test_a_per_call_policy_is_a_budget_on_each_channel(make_dispatcher, sleeps):
    push, sms = Recorder(failures=99), Recorder(failures=99)
    dispatcher = make_dispatcher(PushAdapter(push), SmsAdapter(sms), fallback=True)

    with pytest.raises(DeliveryError) as excinfo:
        dispatcher.send(
            REACHABLE,
            ORDER_CONFIRMED,
            CONTEXT,
            retry=RetryPolicy(attempts=2, backoff=0.25),
        )

    assert len(push.calls) == 2
    assert len(sms.calls) == 2
    assert sleeps == [0.25, 0.25]
    assert [attempt.number for attempt in excinfo.value.attempts] == [1, 2, 3, 4]


def test_a_single_failed_handover_reads_as_one_attempt(make_dispatcher):
    sms = Recorder(failures=99)
    dispatcher = make_dispatcher(SmsAdapter(sms), fallback=True)

    with pytest.raises(DeliveryError) as excinfo:
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert len(excinfo.value.attempts) == 1
    assert "attempts" not in str(excinfo.value)
    assert excinfo.value.cause is sms.error
    assert excinfo.value.__cause__ is sms.error


def test_a_failed_channel_does_not_fall_back_onto_a_silenced_one(make_dispatcher):
    push, sms = Recorder(failures=99), Recorder()
    dispatcher = make_dispatcher(PushAdapter(push), SmsAdapter(sms), fallback=True)
    recipient = Recipient(device_token="tok", phone="+491", quiet_hours=QUIET_SMS)

    with pytest.raises(DeliveryError) as excinfo:
        dispatcher.send(recipient, ORDER_CONFIRMED, CONTEXT, now=NIGHT)

    assert excinfo.value.channel is Channel.PUSH
    assert not sms.calls
    assert len(dispatcher.dead_letters) == 1


def test_an_option_send_does_not_know_settles_its_ticket(make_dispatcher):
    sms = Recorder()
    outbox = Outbox(make_dispatcher(SmsAdapter(sms)))
    ticket = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT, priority="high")

    outbox.drain()

    assert ticket.is_failed
    assert isinstance(ticket.error, TypeError)
    assert not sms.calls


def test_a_limit_above_the_queue_drains_what_there_is(make_dispatcher):
    outbox = Outbox(make_dispatcher(SmsAdapter(Recorder())))
    ticket = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)

    assert outbox.drain(limit=5) == (ticket,)
    assert outbox.drain(limit=5) == ()


def test_an_empty_outbox_waits_and_a_queued_one_does_not(make_dispatcher):
    outbox = Outbox(make_dispatcher(SmsAdapter(Recorder())))

    assert not outbox.wait(timeout=0)

    outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)

    assert outbox.wait(timeout=0)


def test_a_settled_ticket_no_longer_waits(make_dispatcher):
    outbox = Outbox(make_dispatcher(SmsAdapter(Recorder())))
    ticket = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)

    outbox.drain()

    assert ticket.wait(timeout=0)
    assert ticket.result() is ticket.receipt


def test_a_failed_ticket_raises_the_very_error_it_kept(make_dispatcher):
    outbox = Outbox(make_dispatcher(SmsAdapter(Recorder(failures=99))))
    ticket = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)

    outbox.drain()

    with pytest.raises(DeliveryError) as excinfo:
        ticket.result()
    assert excinfo.value is ticket.error


def test_what_a_ticket_was_given_cannot_be_rewritten(make_dispatcher):
    outbox = Outbox(make_dispatcher(SmsAdapter(Recorder())))
    ticket = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT, subject="Order")

    with pytest.raises(TypeError):
        ticket.options["subject"] = "Something else"
    with pytest.raises(TypeError):
        ticket.context["order_id"] = "B-9"


def test_a_worker_can_be_started_again_after_it_was_stopped(make_dispatcher):
    sms = Recorder()
    outbox = Outbox(make_dispatcher(SmsAdapter(sms)))
    worker = Worker(outbox, interval=0.01).start()
    worker.stop()

    assert not worker.is_running

    worker.start()
    try:
        ticket = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)
        assert ticket.wait(timeout=5)
    finally:
        worker.stop()

    assert ticket.is_sent
    assert len(sms.calls) == 1


def test_stopping_a_worker_twice_is_harmless(make_dispatcher):
    outbox = Outbox(make_dispatcher(SmsAdapter(Recorder())))
    worker = Worker(outbox, interval=0.01).start()

    worker.stop()
    worker.stop()

    assert not worker.is_running
