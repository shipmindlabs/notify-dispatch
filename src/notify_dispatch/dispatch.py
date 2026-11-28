"""One dispatch API across SMS, push and email.

A dispatcher renders a template and hands the result to the adapter for the
first channel the recipient can actually be reached on. Every adapter satisfies
the same protocol, so swapping a provider does not touch any call site. A
handover that fails can be retried, a channel that has used up its attempts can
fall back to the next one the recipient is reachable on, a delivery that never
succeeds is kept as a dead letter instead of disappearing, an event that arrives
twice under the same dedup key is delivered once, an address that asked not to
be notified is skipped before a provider ever sees it, and a recipient with a
locale gets the variant of the template written in their language. One call also
carries a notification to a whole audience, with a result for every recipient
rather than an exception for the first one nobody can reach.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, time, tzinfo
from enum import Enum
from time import sleep as _sleep
from typing import Protocol

from notify_dispatch.dedup import DedupStore
from notify_dispatch.delivery import NO_RETRY, Attempt, DeadLetterQueue, RetryPolicy
from notify_dispatch.locales import LocalizedTemplate, normalize_locale
from notify_dispatch.suppress import Suppression, SuppressionList
from notify_dispatch.templates import Template

__all__ = [
    "DEFAULT_CHANNEL_ORDER",
    "Batch",
    "BatchResult",
    "Channel",
    "ChannelAdapter",
    "DeliveryError",
    "DispatchError",
    "Dispatcher",
    "DuplicateSendError",
    "EmailAdapter",
    "Message",
    "PushAdapter",
    "QuietHours",
    "QuietHoursError",
    "Receipt",
    "Recipient",
    "SmsAdapter",
    "SuppressedError",
    "UnroutableError",
    "Urgency",
]


class Channel(str, Enum):
    """A way of reaching a recipient."""

    SMS = "sms"
    PUSH = "push"
    EMAIL = "email"


class Urgency(str, Enum):
    """How far a message may go to reach someone."""

    NORMAL = "normal"
    URGENT = "urgent"


DEFAULT_CHANNEL_ORDER: tuple[Channel, ...] = (Channel.PUSH, Channel.SMS, Channel.EMAIL)


class DispatchError(Exception):
    """Base class for every error raised while dispatching a notification."""


class UnroutableError(DispatchError):
    """No candidate channel had both an address and a registered adapter."""

    def __init__(self, template: str, tried: Iterable[Channel]) -> None:
        self.template = template
        self.tried = tuple(tried)
        listed = ", ".join(channel.value for channel in self.tried) or "nothing"
        super().__init__(f"template {template!r} has no usable channel; tried {listed}")


class SuppressedError(DispatchError):
    """Every usable channel is refused by the suppression list.

    ``refused`` maps each channel to the entry that refused it, and
    ``suppressed`` names those channels in the order they were tried.
    """

    def __init__(self, template: str, refused: Mapping[Channel, Suppression]) -> None:
        self.template = template
        self.refused = dict(refused)
        self.suppressed = tuple(self.refused)
        listed = ", ".join(channel.value for channel in self.suppressed)
        reasons = sorted(
            {entry.reason for entry in self.refused.values() if entry.reason}
        )
        detail = f" ({'; '.join(reasons)})" if reasons else ""
        super().__init__(f"template {template!r} is suppressed on {listed}{detail}")


class QuietHoursError(DispatchError):
    """Every usable channel is inside the recipient's do-not-disturb window.

    ``until`` is the end of the window on the clock the window is read on, and
    carries that zone when the window has one.
    """

    def __init__(self, template: str, silenced: Iterable[Channel], until: time) -> None:
        self.template = template
        self.silenced = tuple(silenced)
        self.until = until
        listed = ", ".join(channel.value for channel in self.silenced)
        super().__init__(
            f"template {template!r} is silenced on {listed} until "
            f"{until.isoformat('minutes')}; send with Urgency.URGENT to override"
        )


class DeliveryError(DispatchError):
    """Every attempt on every channel tried failed; the message is a dead letter.

    ``channel`` and ``address`` are those of the last channel tried; ``attempts``
    holds every handover, from every channel.
    """

    def __init__(
        self,
        channel: Channel,
        address: str,
        cause: BaseException,
        attempts: Iterable[Attempt] = (),
    ) -> None:
        self.channel = channel
        self.address = address
        self.cause = cause
        self.attempts = tuple(attempts)
        tried = f" after {len(self.attempts)} attempts" if len(self.attempts) > 1 else ""
        super().__init__(
            f"{channel.value} delivery to {address!r} failed{tried}: {cause}"
        )


class DuplicateSendError(DispatchError):
    """A send with the same dedup key is still on its way to a provider."""

    def __init__(self, key: str, template: str) -> None:
        self.key = key
        self.template = template
        super().__init__(
            f"template {template!r} is already in flight for dedup key {key!r}"
        )


@dataclass(frozen=True)
class QuietHours:
    """A wall-clock window during which some channels stay silent.

    A window whose start is later than its end wraps past midnight. With a
    ``tz`` the window is read on that clock: a moment is converted into it
    before the clock is read, and a moment that names no zone is refused
    rather than guessed at.
    """

    start: time
    end: time
    channels: tuple[Channel, ...] = tuple(Channel)
    tz: tzinfo | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "channels", tuple(self.channels))
        if self.start == self.end:
            raise ValueError("quiet hours start and end must differ")
        if not self.channels:
            raise ValueError("quiet hours must silence at least one channel")

    @property
    def until(self) -> time:
        """The end of the window, carrying the zone it is read on."""
        if self.tz is None:
            return self.end
        return self.end.replace(tzinfo=self.tz)

    def covers(self, moment: datetime | time) -> bool:
        """Return whether a moment falls inside the window."""
        at = self._reading(moment)
        if self.start < self.end:
            return self.start <= at < self.end
        return at >= self.start or at < self.end

    def silences(self, channel: Channel, moment: datetime | time) -> bool:
        return channel in self.channels and self.covers(moment)

    def _reading(self, moment: datetime | time) -> time:
        if self.tz is None:
            return moment.time() if isinstance(moment, datetime) else moment
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise ValueError(
                "quiet hours with a tz can only read an aware datetime, "
                f"got {moment!r}"
            )
        return moment.astimezone(self.tz).time()


@dataclass(frozen=True)
class Recipient:
    """Where a person can be reached, how they prefer it, and in which language."""

    phone: str | None = None
    device_token: str | None = None
    email: str | None = None
    preferred: tuple[Channel, ...] = ()
    quiet_hours: QuietHours | None = None
    locale: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "preferred", tuple(self.preferred))
        if self.locale is not None:
            object.__setattr__(self, "locale", normalize_locale(self.locale))

    def address_for(self, channel: Channel) -> str | None:
        """Return the address for a channel, or None if there is none."""
        addresses = {
            Channel.SMS: self.phone,
            Channel.PUSH: self.device_token,
            Channel.EMAIL: self.email,
        }
        return addresses[channel]

    def is_quiet(self, channel: Channel, moment: datetime | time) -> bool:
        """Return whether a channel is inside the do-not-disturb window."""
        if self.quiet_hours is None:
            return False
        return self.quiet_hours.silences(channel, moment)

    @property
    def reachable_channels(self) -> tuple[Channel, ...]:
        return tuple(
            channel for channel in Channel if self.address_for(channel) is not None
        )


@dataclass(frozen=True)
class Message:
    """A rendered notification, routed and ready for a provider."""

    template: str
    body: str
    channel: Channel
    address: str
    subject: str | None = None
    urgency: Urgency = Urgency.NORMAL
    locale: str | None = None


@dataclass(frozen=True)
class Receipt:
    """What was sent, the reference the provider gave back, and what it took.

    ``attempts`` counts every handover the notification took, including the ones
    on a channel that was given up on. A receipt handed back for a repeated
    dedup key carries ``duplicate=True`` and the reference of the delivery that
    actually happened.
    """

    message: Message
    reference: str | None = None
    attempts: int = 1
    duplicate: bool = False

    @property
    def channel(self) -> Channel:
        return self.message.channel


@dataclass(frozen=True)
class BatchResult:
    """What one recipient of a batch produced: a receipt or the error it raised."""

    recipient: Recipient
    receipt: Receipt | None = None
    error: DispatchError | None = None

    @property
    def is_sent(self) -> bool:
        return self.receipt is not None

    @property
    def is_failed(self) -> bool:
        return self.error is not None


@dataclass(frozen=True)
class Batch:
    """One notification sent to several recipients, one result each, in order.

    A recipient who could not be reached is a result in the batch rather than
    an exception, so one unroutable address does not keep the rest of an
    audience from being notified.
    """

    template: str
    results: tuple[BatchResult, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))

    @property
    def receipts(self) -> tuple[Receipt, ...]:
        return tuple(
            result.receipt for result in self.results if result.receipt is not None
        )

    @property
    def failures(self) -> tuple[BatchResult, ...]:
        return tuple(result for result in self.results if result.is_failed)

    @property
    def sent(self) -> int:
        return len(self.receipts)

    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def all_sent(self) -> bool:
        return not self.failures

    def __len__(self) -> int:
        return len(self.results)

    def __iter__(self) -> Iterator[BatchResult]:
        return iter(self.results)

    def __str__(self) -> str:
        return (
            f"{self.template!r} sent to {self.sent} of "
            f"{len(self.results)} recipient(s)"
        )


class ChannelAdapter(Protocol):
    """The single interface every provider integration implements."""

    channel: Channel

    def deliver(self, message: Message) -> str | None: ...


@dataclass(frozen=True)
class SmsAdapter:
    """Hands the rendered body to an SMS provider as (phone, body)."""

    provider: Callable[[str, str], str | None]
    channel: Channel = Channel.SMS

    def deliver(self, message: Message) -> str | None:
        return self.provider(message.address, message.body)


@dataclass(frozen=True)
class PushAdapter:
    """Hands the rendered body to a push provider as (device token, body)."""

    provider: Callable[[str, str], str | None]
    channel: Channel = Channel.PUSH

    def deliver(self, message: Message) -> str | None:
        return self.provider(message.address, message.body)


@dataclass(frozen=True)
class EmailAdapter:
    """Hands the rendered body to an email provider as (address, subject, body)."""

    provider: Callable[[str, str, str], str | None]
    default_subject: str = ""
    channel: Channel = Channel.EMAIL

    def deliver(self, message: Message) -> str | None:
        subject = message.subject or self.default_subject
        return self.provider(message.address, subject, message.body)


class Dispatcher:
    """Renders a template once and delivers it over the channels it routes to."""

    def __init__(
        self,
        adapters: Iterable[ChannelAdapter] = (),
        *,
        order: Sequence[Channel] = DEFAULT_CHANNEL_ORDER,
        clock: Callable[[], datetime] = datetime.now,
        retry: RetryPolicy = NO_RETRY,
        fallback: bool = False,
        dead_letters: DeadLetterQueue | None = None,
        dedup: DedupStore | None = None,
        suppressions: SuppressionList | None = None,
        sleep: Callable[[float], None] = _sleep,
    ) -> None:
        self._adapters: dict[Channel, ChannelAdapter] = {}
        self._order = tuple(order)
        self._clock = clock
        self._retry = retry
        self._fallback = fallback
        self._dead_letters = (
            dead_letters if dead_letters is not None else DeadLetterQueue()
        )
        self._dedup = dedup if dedup is not None else DedupStore()
        self._suppressions = (
            suppressions if suppressions is not None else SuppressionList()
        )
        self._sleep = sleep
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ChannelAdapter) -> None:
        """Add an adapter, replacing any adapter for the same channel."""
        self._adapters[adapter.channel] = adapter

    @property
    def channels(self) -> tuple[Channel, ...]:
        return tuple(channel for channel in self._order if channel in self._adapters)

    @property
    def dead_letters(self) -> DeadLetterQueue:
        """The messages that ran out of attempts."""
        return self._dead_letters

    @property
    def dedup(self) -> DedupStore:
        """The keys that were already delivered."""
        return self._dedup

    @property
    def suppressions(self) -> SuppressionList:
        """The addresses that may not be handed to a provider."""
        return self._suppressions

    @property
    def retry_policy(self) -> RetryPolicy:
        return self._retry

    @property
    def fallback(self) -> bool:
        """Whether an exhausted channel hands the message to the next one."""
        return self._fallback

    def send(
        self,
        recipient: Recipient,
        template: Template | LocalizedTemplate,
        context: Mapping[str, object] | None = None,
        *,
        channel: Channel | None = None,
        subject: str | None = None,
        urgency: Urgency = Urgency.NORMAL,
        now: datetime | None = None,
        retry: RetryPolicy | None = None,
        fallback: bool | None = None,
        dedup_key: str | None = None,
        locale: str | None = None,
    ) -> Receipt:
        """Render the template and deliver it, or raise before anything is sent.

        With ``fallback`` the next reachable, unsilenced channel is tried once a
        channel has used up its attempts, and only an exhausted last channel
        writes a dead letter. A ``dedup_key`` is remembered only once a provider
        accepted the message: a redelivery of the same event gets the first
        receipt back instead of being sent again, while a send that failed
        leaves the key free. A ``locale`` overrides the recipient's own and is
        only consulted for a ``LocalizedTemplate``. An address on the
        suppression list is skipped even for an urgent send.
        """
        moment = now if now is not None else self._clock()
        if dedup_key is None:
            return self._send(
                recipient,
                template,
                context,
                channel=channel,
                subject=subject,
                urgency=urgency,
                moment=moment,
                retry=retry,
                fallback=fallback,
                locale=locale,
            )
        seen = self._dedup.claim(dedup_key, at=moment)
        if seen is not None:
            if seen.receipt is None:
                raise DuplicateSendError(dedup_key, template.name)
            return replace(seen.receipt, duplicate=True)
        try:
            receipt = self._send(
                recipient,
                template,
                context,
                channel=channel,
                subject=subject,
                urgency=urgency,
                moment=moment,
                retry=retry,
                fallback=fallback,
                locale=locale,
            )
        except BaseException:
            self._dedup.release(dedup_key)
            raise
        self._dedup.complete(dedup_key, receipt, at=moment)
        return receipt

    def send_many(
        self,
        recipients: Iterable[Recipient],
        template: Template | LocalizedTemplate,
        context: Mapping[str, object] | None = None,
        *,
        channel: Channel | None = None,
        subject: str | None = None,
        urgency: Urgency = Urgency.NORMAL,
        now: datetime | None = None,
        retry: RetryPolicy | None = None,
        fallback: bool | None = None,
        dedup_key: Callable[[Recipient], str] | None = None,
        locale: str | None = None,
    ) -> Batch:
        """Send one notification to several recipients and report each outcome.

        Every recipient is routed, localized and retried exactly as it would be
        on its own, against a single reading of the clock, so a quiet-hours
        window cannot close between the first recipient and the last. A
        ``DispatchError`` settles its own recipient and the batch carries on; a
        template error is the same for everybody and is raised instead, before
        any provider is reached. A ``dedup_key`` is a function of the recipient,
        because one key names one notification.
        """
        if dedup_key is not None and not callable(dedup_key):
            raise TypeError(
                "a batch dedup key must be a function of the recipient, so that "
                "two recipients of one event do not share a key"
            )
        moment = now if now is not None else self._clock()
        results: list[BatchResult] = []
        for recipient in recipients:
            try:
                receipt = self.send(
                    recipient,
                    template,
                    context,
                    channel=channel,
                    subject=subject,
                    urgency=urgency,
                    now=moment,
                    retry=retry,
                    fallback=fallback,
                    dedup_key=dedup_key(recipient) if dedup_key is not None else None,
                    locale=locale,
                )
            except DispatchError as exc:
                results.append(BatchResult(recipient=recipient, error=exc))
            else:
                results.append(BatchResult(recipient=recipient, receipt=receipt))
        return Batch(template=template.name, results=tuple(results))

    def _send(
        self,
        recipient: Recipient,
        template: Template | LocalizedTemplate,
        context: Mapping[str, object] | None,
        *,
        channel: Channel | None,
        subject: str | None,
        urgency: Urgency,
        moment: datetime,
        retry: RetryPolicy | None,
        fallback: bool | None,
        locale: str | None,
    ) -> Receipt:
        chosen, variant = self._localize(
            template, locale if locale is not None else recipient.locale
        )
        body = variant.render(context or {})
        candidates = (channel,) if channel is not None else self._preference(recipient)
        may_fall_back = fallback if fallback is not None else self._fallback
        routes: list[tuple[ChannelAdapter, Message]] = []
        refused: dict[Channel, Suppression] = {}
        silenced: list[Channel] = []
        for candidate in candidates:
            address = recipient.address_for(candidate)
            adapter = self._adapters.get(candidate)
            if address is None or adapter is None:
                continue
            entry = self._suppressions.suppression_for(address, candidate)
            if entry is not None:
                refused[candidate] = entry
                continue
            if urgency is not Urgency.URGENT and recipient.is_quiet(candidate, moment):
                silenced.append(candidate)
                continue
            message = Message(
                template=variant.name,
                body=body,
                channel=candidate,
                address=address,
                subject=subject,
                urgency=urgency,
                locale=chosen,
            )
            routes.append((adapter, message))
            if not may_fall_back:
                break
        if routes:
            return self._deliver(routes, retry or self._retry)
        # a quiet window reopens in the morning, an opt-out does not
        if refused:
            raise SuppressedError(variant.name, refused)
        if silenced:
            assert recipient.quiet_hours is not None
            raise QuietHoursError(variant.name, silenced, recipient.quiet_hours.until)
        raise UnroutableError(variant.name, candidates)

    def _localize(
        self, template: Template | LocalizedTemplate, locale: str | None
    ) -> tuple[str | None, Template]:
        if isinstance(template, LocalizedTemplate):
            return template.resolve(locale)
        return None, template

    def _deliver(
        self,
        routes: Sequence[tuple[ChannelAdapter, Message]],
        policy: RetryPolicy,
    ) -> Receipt:
        attempts: list[Attempt] = []
        for adapter, message in routes:
            # the policy is a budget per channel, so the next one starts fresh
            on_channel = 0
            while True:
                on_channel += 1
                try:
                    reference = adapter.deliver(message)
                except Exception as exc:
                    attempts.append(
                        Attempt(
                            number=len(attempts) + 1,
                            channel=message.channel,
                            address=message.address,
                            error=exc,
                            at=self._clock(),
                        )
                    )
                    if policy.allows(on_channel, exc):
                        self._sleep(policy.backoff_for(on_channel))
                        continue
                    break
                return Receipt(
                    message=message, reference=reference, attempts=len(attempts) + 1
                )
        exhausted = routes[-1][1]
        error = attempts[-1].error
        self._dead_letters.record(exhausted, attempts, at=self._clock())
        raise DeliveryError(
            exhausted.channel, exhausted.address, error, attempts
        ) from error

    def _preference(self, recipient: Recipient) -> tuple[Channel, ...]:
        order = list(recipient.preferred)
        order.extend(channel for channel in self._order if channel not in order)
        return tuple(order)
