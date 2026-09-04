"""Tests for delivery attempts, retries and dead letters."""

from __future__ import annotations

from datetime import datetime

import pytest

from notify_dispatch import (
    NO_RETRY,
    Channel,
    DeadLetterQueue,
    DeliveryError,
    Dispatcher,
    EmailAdapter,
    Message,
    Recipient,
    RetryPolicy,
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
NOW = datetime(2026, 3, 1, 10, 0)
ADA = Recipient(phone="+491")


class FlakyProvider:
    """Fails the first ``failures`` calls, then succeeds."""

    def __init__(
        self,
        failures: int = 0,
        *,
        reference: str | None = "ref-1",
        error: BaseException | None = None,
    ) -> None:
        self.failures = failures
        self.reference = reference
        self.error = error if error is not None else RuntimeError("provider is down")
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> str | None:
        self.calls.append(args)
        if len(self.calls) <= self.failures:
            raise self.error
        return self.reference


def make_dispatcher(provider, **kwargs):
    """Return a dispatcher over SMS plus the list of backoffs it slept."""
    slept: list[float] = []
    dispatcher = Dispatcher(
        [SmsAdapter(provider)], clock=lambda: NOW, sleep=slept.append, **kwargs
    )
    return dispatcher, slept


def test_a_failed_attempt_is_retried_until_it_succeeds():
    provider = FlakyProvider(failures=1)
    dispatcher, slept = make_dispatcher(provider, retry=RetryPolicy(attempts=3))

    receipt = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert receipt.reference == "ref-1"
    assert receipt.attempts == 2
    assert len(provider.calls) == 2
    assert slept == [0.5]
    assert len(dispatcher.dead_letters) == 0


def test_backoff_grows_per_attempt_and_stops_at_the_maximum():
    policy = RetryPolicy(attempts=4, backoff=0.5, multiplier=2.0, max_backoff=1.5)
    dispatcher, slept = make_dispatcher(FlakyProvider(failures=99), retry=policy)

    with pytest.raises(DeliveryError):
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert slept == [0.5, 1.0, 1.5]


def test_exhausted_attempts_land_in_the_dead_letter_list():
    policy = RetryPolicy(attempts=3, backoff=0.0)
    dispatcher, _ = make_dispatcher(FlakyProvider(failures=99), retry=policy)

    with pytest.raises(DeliveryError) as excinfo:
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)
    assert len(excinfo.value.attempts) == 3
    assert "after 3 attempts" in str(excinfo.value)

    (letter,) = dispatcher.dead_letters.letters
    assert letter.template == "order_confirmed"
    assert letter.channel is Channel.SMS
    assert letter.address == "+491"
    assert letter.message.body == "Order A-1042 confirmed."
    assert isinstance(letter.last_error, RuntimeError)
    assert [attempt.number for attempt in letter.attempts] == [1, 2, 3]
    assert letter.recorded_at == NOW


def test_without_a_retry_policy_a_failure_is_attempted_once():
    provider = FlakyProvider(failures=99)
    dispatcher, slept = make_dispatcher(provider)

    with pytest.raises(DeliveryError) as excinfo:
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert len(provider.calls) == 1
    assert slept == []
    assert len(excinfo.value.attempts) == 1
    assert len(dispatcher.dead_letters) == 1


def test_an_error_the_policy_does_not_cover_is_not_retried():
    provider = FlakyProvider(failures=99, error=ValueError("unknown number"))
    policy = RetryPolicy(attempts=3, backoff=0.0, retry_on=(TimeoutError,))
    dispatcher, _ = make_dispatcher(provider, retry=policy)

    with pytest.raises(DeliveryError):
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert len(provider.calls) == 1
    assert len(dispatcher.dead_letters) == 1


def test_a_per_call_policy_overrides_the_dispatcher_policy():
    provider = FlakyProvider(failures=1)
    dispatcher, _ = make_dispatcher(provider)

    receipt = dispatcher.send(
        ADA, ORDER_CONFIRMED, CONTEXT, retry=RetryPolicy(attempts=2, backoff=0.0)
    )

    assert receipt.attempts == 2
    assert len(dispatcher.dead_letters) == 0


def test_a_successful_send_records_nothing():
    dispatcher, _ = make_dispatcher(FlakyProvider())

    receipt = dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    assert receipt.attempts == 1
    assert not dispatcher.dead_letters.letters


def test_a_shared_queue_collects_failures_from_several_dispatchers():
    queue = DeadLetterQueue()
    sms = Dispatcher([SmsAdapter(FlakyProvider(failures=99))], dead_letters=queue)
    email = Dispatcher([EmailAdapter(FlakyProvider(failures=99))], dead_letters=queue)

    with pytest.raises(DeliveryError):
        sms.send(ADA, ORDER_CONFIRMED, CONTEXT)
    with pytest.raises(DeliveryError):
        email.send(Recipient(email="a@example.com"), ORDER_CONFIRMED, CONTEXT)

    assert [letter.channel for letter in queue] == [Channel.SMS, Channel.EMAIL]


def test_a_limited_queue_keeps_the_most_recent_letters():
    queue = DeadLetterQueue(limit=1)
    dispatcher, _ = make_dispatcher(FlakyProvider(failures=99), dead_letters=queue)

    for phone in ("+491", "+492"):
        with pytest.raises(DeliveryError):
            dispatcher.send(Recipient(phone=phone), ORDER_CONFIRMED, CONTEXT)

    assert len(queue) == 1
    assert queue.letters[0].address == "+492"


def test_draining_the_queue_returns_and_empties_it():
    dispatcher, _ = make_dispatcher(FlakyProvider(failures=99))
    with pytest.raises(DeliveryError):
        dispatcher.send(ADA, ORDER_CONFIRMED, CONTEXT)

    drained = dispatcher.dead_letters.drain()

    assert len(drained) == 1
    assert len(dispatcher.dead_letters) == 0


def test_a_send_that_never_reached_a_provider_is_not_a_dead_letter():
    dispatcher, _ = make_dispatcher(FlakyProvider())

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
        body="Order A-1042 confirmed.",
        channel=Channel.SMS,
        address="+491",
    )
    with pytest.raises(ValueError):
        DeadLetterQueue().record(message, ())


def test_a_queue_that_holds_nothing_is_rejected():
    with pytest.raises(ValueError):
        DeadLetterQueue(limit=0)
