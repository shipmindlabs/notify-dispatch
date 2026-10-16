"""Tests for the outbox, its tickets and the worker that drains it."""

from __future__ import annotations

import pytest
from conftest import ADA, BODY, CONTEXT, NIGHT, NOW, ORDER_CONFIRMED, OVERNIGHT, Recorder

from notify_dispatch import (
    Channel,
    DeliveryError,
    EmailAdapter,
    MissingVariableError,
    Outbox,
    PushAdapter,
    QuietHoursError,
    Recipient,
    SmsAdapter,
    TicketPendingError,
    TicketState,
    Urgency,
    Worker,
)


def test_enqueue_returns_a_pending_ticket_and_reaches_nobody(make_dispatcher):
    sms = Recorder()
    outbox = Outbox(make_dispatcher(SmsAdapter(sms)))

    ticket = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)

    assert not sms.calls
    assert ticket.state is TicketState.PENDING
    assert ticket.receipt is None and ticket.error is None
    assert len(outbox) == 1
    assert outbox.pending == (ticket,)


def test_drain_sends_the_queued_tickets_in_order(make_dispatcher):
    sms = Recorder()
    outbox = Outbox(make_dispatcher(SmsAdapter(sms)))
    first = outbox.enqueue(Recipient(phone="+491"), ORDER_CONFIRMED, CONTEXT)
    second = outbox.enqueue(Recipient(phone="+492"), ORDER_CONFIRMED, CONTEXT)

    handled = outbox.drain()

    assert handled == (first, second)
    assert [call[0] for call in sms.calls] == ["+491", "+492"]
    assert len(outbox) == 0


def test_a_drained_ticket_carries_its_receipt(make_dispatcher):
    outbox = Outbox(make_dispatcher(SmsAdapter(Recorder(reference="sms-1"))))
    ticket = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)

    outbox.drain()

    assert ticket.state is TicketState.SENT
    assert ticket.result() is ticket.receipt
    assert ticket.receipt.reference == "sms-1"
    assert ticket.receipt.message.body == BODY
    assert ticket.error is None


def test_a_failed_send_settles_the_ticket_instead_of_raising(make_dispatcher):
    dispatcher = make_dispatcher(SmsAdapter(Recorder(failures=99)))
    outbox = Outbox(dispatcher)
    ticket = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)

    handled = outbox.drain()

    assert handled == (ticket,)
    assert ticket.state is TicketState.FAILED
    assert isinstance(ticket.error, DeliveryError)
    with pytest.raises(DeliveryError):
        ticket.result()
    assert len(dispatcher.dead_letters) == 1


def test_a_template_error_is_carried_by_the_ticket(make_dispatcher):
    sms = Recorder()
    outbox = Outbox(make_dispatcher(SmsAdapter(sms)))
    ticket = outbox.enqueue(ADA, ORDER_CONFIRMED, {})

    outbox.drain()

    assert isinstance(ticket.error, MissingVariableError)
    assert not sms.calls


def test_one_failure_does_not_end_the_drain(make_dispatcher):
    sms = Recorder()
    outbox = Outbox(make_dispatcher(SmsAdapter(sms)))
    broken = outbox.enqueue(ADA, ORDER_CONFIRMED, {})
    fine = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)

    outbox.drain()

    assert broken.is_failed
    assert fine.is_sent
    assert sms.calls == [("+491", BODY)]


def test_a_second_drain_finds_nothing(make_dispatcher):
    sms = Recorder()
    outbox = Outbox(make_dispatcher(SmsAdapter(sms)))
    outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)

    outbox.drain()

    assert outbox.drain() == ()
    assert len(sms.calls) == 1


def test_a_limited_drain_leaves_the_rest_queued(make_dispatcher):
    sms = Recorder()
    outbox = Outbox(make_dispatcher(SmsAdapter(sms)))
    first = outbox.enqueue(Recipient(phone="+491"), ORDER_CONFIRMED, CONTEXT)
    second = outbox.enqueue(Recipient(phone="+492"), ORDER_CONFIRMED, CONTEXT)

    assert outbox.drain(limit=1) == (first,)
    assert second.is_pending
    assert outbox.pending == (second,)
    assert [call[0] for call in sms.calls] == ["+491"]


def test_send_options_travel_with_the_ticket(make_dispatcher):
    push, email = Recorder(), Recorder()
    outbox = Outbox(make_dispatcher(PushAdapter(push), EmailAdapter(email)))
    recipient = Recipient(device_token="tok", email="a@example.com")

    ticket = outbox.enqueue(
        recipient, ORDER_CONFIRMED, CONTEXT, channel=Channel.EMAIL, subject="Order"
    )
    outbox.drain()

    assert ticket.result().channel is Channel.EMAIL
    assert email.calls == [("a@example.com", "Order", BODY)]
    assert not push.calls


def test_quiet_hours_are_honoured_at_drain_time(make_dispatcher):
    sms = Recorder()
    outbox = Outbox(make_dispatcher(SmsAdapter(sms)))
    recipient = Recipient(phone="+491", quiet_hours=OVERNIGHT)

    silenced = outbox.enqueue(recipient, ORDER_CONFIRMED, CONTEXT, now=NIGHT)
    urgent = outbox.enqueue(
        recipient, ORDER_CONFIRMED, CONTEXT, now=NIGHT, urgency=Urgency.URGENT
    )
    outbox.drain()

    assert isinstance(silenced.error, QuietHoursError)
    assert urgent.is_sent
    assert sms.calls == [("+491", BODY)]


def test_a_dedup_key_is_honoured_across_tickets(make_dispatcher):
    sms = Recorder()
    outbox = Outbox(make_dispatcher(SmsAdapter(sms)))
    first = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")
    second = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT, dedup_key="evt-1")

    outbox.drain()

    assert len(sms.calls) == 1
    assert not first.result().duplicate
    assert second.result().duplicate


def test_a_ticket_keeps_the_context_it_was_enqueued_with(make_dispatcher):
    outbox = Outbox(make_dispatcher(SmsAdapter(Recorder())))
    context = dict(CONTEXT)

    ticket = outbox.enqueue(ADA, ORDER_CONFIRMED, context)
    context["order_id"] = "B-9"
    outbox.drain()

    assert ticket.context == CONTEXT
    assert ticket.result().message.body == BODY


def test_a_ticket_is_stamped_from_the_outbox_clock(make_dispatcher, clock):
    outbox = Outbox(make_dispatcher(SmsAdapter(Recorder())), clock=clock)

    ticket = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)

    assert ticket.enqueued_at == NOW


def test_the_result_of_a_pending_ticket_is_refused(make_dispatcher):
    outbox = Outbox(make_dispatcher(SmsAdapter(Recorder())))
    ticket = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)

    with pytest.raises(TicketPendingError):
        ticket.result()
    assert not ticket.wait(timeout=0)


def test_tickets_name_their_template_and_are_told_apart(make_dispatcher):
    outbox = Outbox(make_dispatcher(SmsAdapter(Recorder())))

    first = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)
    second = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)

    assert first.template_name == "order_confirmed"
    assert "order_confirmed" in repr(first)
    assert first.id != second.id


def test_a_drain_of_nothing_is_rejected(make_dispatcher):
    outbox = Outbox(make_dispatcher(SmsAdapter(Recorder())))

    with pytest.raises(ValueError):
        outbox.drain(limit=0)


def test_a_worker_drains_until_it_is_stopped(make_dispatcher):
    outbox = Outbox(make_dispatcher(SmsAdapter(Recorder())))
    worker = Worker(outbox, interval=0.01).start()
    try:
        ticket = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)
        assert ticket.wait(timeout=5)
    finally:
        worker.stop()

    assert ticket.result().reference == "ref-1"
    assert not worker.is_running


def test_stopping_a_worker_still_sends_what_was_queued(make_dispatcher):
    sms = Recorder()
    outbox = Outbox(make_dispatcher(SmsAdapter(sms)))
    worker = Worker(outbox, interval=0.01).start()

    ticket = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)
    worker.stop()

    assert ticket.is_sent
    assert len(outbox) == 0
    assert sms.calls == [("+491", BODY)]


def test_a_worker_starts_and_stops_as_a_context_manager(make_dispatcher):
    outbox = Outbox(make_dispatcher(SmsAdapter(Recorder())))

    with Worker(outbox, interval=0.01) as worker:
        assert worker.is_running
        ticket = outbox.enqueue(ADA, ORDER_CONFIRMED, CONTEXT)

    assert ticket.is_sent
    assert not worker.is_running


def test_starting_a_running_worker_is_rejected(make_dispatcher):
    outbox = Outbox(make_dispatcher(SmsAdapter(Recorder())))
    worker = Worker(outbox, interval=0.01).start()
    try:
        with pytest.raises(RuntimeError):
            worker.start()
    finally:
        worker.stop()


def test_stopping_a_worker_that_never_ran_is_harmless(make_dispatcher):
    worker = Worker(Outbox(make_dispatcher(SmsAdapter(Recorder()))))

    worker.stop()

    assert not worker.is_running


def test_an_interval_that_is_not_positive_is_rejected(make_dispatcher):
    outbox = Outbox(make_dispatcher(SmsAdapter(Recorder())))

    with pytest.raises(ValueError):
        Worker(outbox, interval=0)
