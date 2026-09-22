"""Tests for delivery attempts, retries and dead letters."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import ADA, BODY, CONTEXT, NOW, ORDER_CONFIRMED, Recorder

from notify_dispatch import (
    NO_RETRY,
    Channel,
    DeadLetterQueue,
    DeliveryError,
    EmailAdapter,
    Message,
    Recipient,
    RetryPolicy,
    SmsAdapter,
    UnroutableError,
)


def test_a_failed_attempt_is_retried_until_it_succeeds(make_dispatcher, sleeps):
    provider = Recorder(failures=1)
    dispatcher = make_dispatcher(SmsAdapter(provider), retry=RetryPolicy(attempts=3))

    receipt = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert receipt.reference == "ref-1"
    assert receipt.attempts == 2
    assert len(provider.calls) == 2
    assert sleeps == [0.5]
    assert len(dispatcher.dead_letters) == 0


def test_backoff_grows_per_attempt_and_stops_at_the_maximum(make_dispatcher, sleeps):
    policy = RetryPolicy(attempts=4, backoff=0.5, multiplier=2.0, max_backoff=1.5)
    dispatcher = make_dispatcher(SmsAdapter(Recorder(failures=99)), retry=policy)

    with pytest.raises(DeliveryError):
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert sleeps == [0.5, 1.0, 1.5]


def test_a_long_backoff_is_recorded_rather_than_waited(make_dispatcher, sleeps):
    policy = RetryPolicy(attempts=2, backoff=30.0, max_backoff=30.0)
    dispatcher = make_dispatcher(SmsAdapter(Recorder(failures=99)), retry=policy)

    with pytest.raises(DeliveryError):
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert sleeps == [30.0]


def test_a_successful_first_attempt_waits_for_nothing(make_dispatcher, sleeps):
    dispatcher = make_dispatcher(SmsAdapter(Recorder()), retry=RetryPolicy(attempts=3))

    receipt = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert receipt.attempts == 1
    assert sleeps == []
    assert not dispatcher.dead_letters.letters


def test_exhausted_attempts_land_in_the_dead_letter_list(make_dispatcher):
    policy = RetryPolicy(attempts=3, backoff=0.0)
    dispatcher = make_dispatcher(SmsAdapter(Recorder(failures=99)), retry=policy)

    with pytest.raises(DeliveryError) as excinfo:
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)
    assert len(excinfo.value.attempts) == 3
    assert "after 3 attempts" in str(excinfo.value)

    (letter,) = dispatcher.dead_letters.letters
    assert letter.template == "order_confirmed"
    assert letter.channel is Channel.SMS
    assert letter.address == "+491"
    assert letter.message.body == BODY
    assert isinstance(letter.last_error, RuntimeError)
    assert [attempt.number for attempt in letter.attempts] == [1, 2, 3]
    assert letter.recorded_at == NOW


def test_an_attempt_records_the_channel_and_the_address(make_dispatcher):
    dispatcher = make_dispatcher(SmsAdapter(Recorder(failures=99)))

    with pytest.raises(DeliveryError) as excinfo:
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    (attempt,) = excinfo.value.attempts
    assert attempt.channel is Channel.SMS
    assert attempt.address == "+491"
    assert "attempt 1 on sms" in str(attempt)


def test_attempts_are_stamped_from_the_injected_clock(make_dispatcher):
    policy = RetryPolicy(attempts=2, backoff=0.0)
    dispatcher = make_dispatcher(SmsAdapter(Recorder(failures=99)), retry=policy)

    with pytest.raises(DeliveryError) as excinfo:
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert [attempt.at for attempt in excinfo.value.attempts] == [NOW, NOW]


def test_a_moving_clock_stamps_every_attempt_differently(make_dispatcher, clock):
    clock.step = timedelta(seconds=5)
    policy = RetryPolicy(attempts=3, backoff=0.0)
    dispatcher = make_dispatcher(SmsAdapter(Recorder(failures=99)), retry=policy)

    with pytest.raises(DeliveryError) as excinfo:
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    stamps = [attempt.at for attempt in excinfo.value.attempts]
    assert len(set(stamps)) == 3
    assert stamps == sorted(stamps)
    assert dispatcher.dead_letters.letters[0].recorded_at >= stamps[-1]


def test_without_a_retry_policy_a_failure_is_attempted_once(make_dispatcher, sleeps):
    provider = Recorder(failures=99)
    dispatcher = make_dispatcher(SmsAdapter(provider))

    with pytest.raises(DeliveryError) as excinfo:
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert len(provider.calls) == 1
    assert sleeps == []
    assert len(excinfo.value.attempts) == 1
    assert len(dispatcher.dead_letters) == 1


def test_an_error_the_policy_does_not_cover_is_not_retried(make_dispatcher):
    provider = Recorder(failures=99, error=ValueError("unknown number"))
    policy = RetryPolicy(attempts=3, backoff=0.0, retry_on=(TimeoutError,))
    dispatcher = make_dispatcher(SmsAdapter(provider), retry=policy)

    with pytest.raises(DeliveryError):
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert len(provider.calls) == 1
    assert len(dispatcher.dead_letters) == 1


def test_a_per_call_policy_overrides_the_dispatcher_policy(make_dispatcher):
    provider = Recorder(failures=1)
    dispatcher = make_dispatcher(SmsAdapter(provider))

    receipt = dispatcher.send(
        ADA, ORDER_CONFIRMED, CONTEXT, retry=RetryPolicy(attempts=2, backoff=0.0)
    )

    assert receipt.attempts == 2
    assert len(dispatcher.dead_letters) == 0


def test_a_per_call_policy_may_also_switch_retries_off(make_dispatcher, sleeps):
    provider = Recorder(failures=1)
    policy = RetryPolicy(attempts=3, backoff=0.0)
    dispatcher = make_dispatcher(SmsAdapter(provider), retry=policy)

    with pytest.raises(DeliveryError):
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT, retry=NO_RETRY)

    assert len(provider.calls) == 1
    assert sleeps == []


def test_a_successful_send_records_nothing(make_dispatcher):
    dispatcher = make_dispatcher(SmsAdapter(Recorder()))

    receipt = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert receipt.attempts == 1
    assert not dispatcher.dead_letters.letters


def test_a_shared_queue_collects_failures_from_several_dispatchers(make_dispatcher):
    queue = DeadLetterQueue()
    sms = make_dispatcher(SmsAdapter(Recorder(failures=99)), dead_letters=queue)
    email = make_dispatcher(EmailAdapter(Recorder(failures=99)), dead_letters=queue)

    with pytest.raises(DeliveryError):
        sms.send(ADA, ORDER_CONFIRMED, CONTEXT)
    with pytest.raises(DeliveryError):
        email.send(Recipient(email="a@example.com"), ORDER_CONFIRMED, CONTEXT)

    assert [letter.channel for letter in queue] == [Channel.SMS, Channel.EMAIL]


def test_a_limited_queue_keeps_the_most_recent_letters(make_dispatcher):
    queue = DeadLetterQueue(limit=1)
    dispatcher = make_dispatcher(SmsAdapter(Recorder(failures=99)), dead_letters=queue)

    for phone in ("+491", "+492"):
        with pytest.raises(DeliveryError):
            dispatcher.send(Recipient(phone=phone), ORDER_CONFIRMED, CONTEXT)

    assert len(queue) == 1
    assert queue.letters[0].address == "+492"


def test_draining_the_queue_returns_and_empties_it(make_dispatcher):
    dispatcher = make_dispatcher(SmsAdapter(Recorder(failures=99)))
    with pytest.raises(DeliveryError):
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    drained = dispatcher.dead_letters.drain()

    assert len(drained) == 1
    assert len(dispatcher.dead_letters) == 0


def test_a_send_that_never_reached_a_provider_is_not_a_dead_letter(make_dispatcher):
    dispatcher = make_dispatcher(SmsAdapter(Recorder()))

    with pytest.raises(UnroutableError):
        dispatcher.send(Recipient(email="a@example.com"), ORDER_CONFIRMED, CONTEXT)

    assert len(dispatcher.dead_letters) == 0


def test_no_retry_allows_a_single_attempt():
    assert NO_RETRY.attempts == 1
    assert not NO_RETRY.allows(1, RuntimeError("down"))


def test_a_policy_retries_only_the_errors_it_names():
    policy = RetryPolicy(attempts=2, retry_on=(TimeoutError,))
    assert policy.allows(1, TimeoutError("slow"))
    assert not policy.allows(1, ValueError("bad address"))
    assert not policy.allows(2, TimeoutError("slow"))


def test_backoff_grows_and_then_stays_at_the_maximum():
    policy = RetryPolicy(attempts=5, backoff=0.5, multiplier=3.0, max_backoff=4.0)
    assert [policy.backoff_for(number) for number in (1, 2, 3, 4)] == [
        0.5,
        1.5,
        4.0,
        4.0,
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"attempts": 0},
        {"backoff": -1.0},
        {"multiplier": 0.5},
        {"backoff": 2.0, "max_backoff": 1.0},
        {"retry_on": ()},
    ],
)
def test_an_unusable_policy_is_rejected(kwargs):
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


def test_backoff_is_counted_from_the_first_attempt():
    with pytest.raises(ValueError):
        RetryPolicy().backoff_for(0)


def test_a_dead_letter_needs_at_least_one_attempt():
    message = Message(
        template="order_confirmed",
        body=BODY,
        channel=Channel.SMS,
        address="+491",
    )
    with pytest.raises(ValueError):
        DeadLetterQueue().record(message, ())


def test_a_queue_that_holds_nothing_is_rejected():
    with pytest.raises(ValueError):
        DeadLetterQueue(limit=0)
