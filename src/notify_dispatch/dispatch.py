"""One dispatch API across SMS, push and email.

A dispatcher renders a template and hands the result to the adapter for the
first channel the recipient can actually be reached on. Every adapter satisfies
the same protocol, so swapping a provider does not touch any call site.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Protocol

from notify_dispatch.templates import Template

__all__ = [
    "DEFAULT_CHANNEL_ORDER",
    "Channel",
    "ChannelAdapter",
    "DeliveryError",
    "DispatchError",
    "Dispatcher",
    "EmailAdapter",
    "Message",
    "PushAdapter",
    "QuietHours",
    "QuietHoursError",
    "Receipt",
    "Recipient",
    "SmsAdapter",
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


class QuietHoursError(DispatchError):
    """Every usable channel is inside the recipient's do-not-disturb window."""

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
    """The adapter for the chosen channel failed to hand the message over."""

    def __init__(self, channel: Channel, address: str, cause: BaseException) -> None:
        self.channel = channel
        self.address = address
        self.cause = cause
        super().__init__(f"{channel.value} delivery to {address!r} failed: {cause}")


@dataclass(frozen=True)
class QuietHours:
    """A wall-clock window during which some channels stay silent.

    A window whose start is later than its end wraps past midnight.
    """

    start: time
    end: time
    channels: tuple[Channel, ...] = tuple(Channel)

    def __post_init__(self) -> None:
        object.__setattr__(self, "channels", tuple(self.channels))
        if self.start == self.end:
            raise ValueError("quiet hours start and end must differ")
        if not self.channels:
            raise ValueError("quiet hours must silence at least one channel")

    def covers(self, moment: datetime | time) -> bool:
        """Return whether a moment falls inside the window."""
        at = moment.time() if isinstance(moment, datetime) else moment
        if self.start < self.end:
            return self.start <= at < self.end
        return at >= self.start or at < self.end

    def silences(self, channel: Channel, moment: datetime | time) -> bool:
        return channel in self.channels and self.covers(moment)


@dataclass(frozen=True)
class Recipient:
    """Where a person can be reached, and how they prefer to be reached."""

    phone: str | None = None
    device_token: str | None = None
    email: str | None = None
    preferred: tuple[Channel, ...] = ()
    quiet_hours: QuietHours | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "preferred", tuple(self.preferred))

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


@dataclass(frozen=True)
class Receipt:
    """What was sent, and the reference the provider gave back."""

    message: Message
    reference: str | None = None

    @property
    def channel(self) -> Channel:
        return self.message.channel


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
    """Renders a template once and delivers it over a single chosen channel."""

    def __init__(
        self,
        adapters: Iterable[ChannelAdapter] = (),
        *,
        order: Sequence[Channel] = DEFAULT_CHANNEL_ORDER,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._adapters: dict[Channel, ChannelAdapter] = {}
        self._order = tuple(order)
        self._clock = clock
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ChannelAdapter) -> None:
        """Add an adapter, replacing any adapter for the same channel."""
        self._adapters[adapter.channel] = adapter

    @property
    def channels(self) -> tuple[Channel, ...]:
        return tuple(channel for channel in self._order if channel in self._adapters)

    def send(
        self,
        recipient: Recipient,
        template: Template,
        context: Mapping[str, object] | None = None,
        *,
        channel: Channel | None = None,
        subject: str | None = None,
        urgency: Urgency = Urgency.NORMAL,
        now: datetime | None = None,
    ) -> Receipt:
        """Render the template and deliver it, or raise before anything is sent."""
        body = template.render(context or {})
        moment = now if now is not None else self._clock()
        candidates = (channel,) if channel is not None else self._preference(recipient)
        silenced: list[Channel] = []
        for candidate in candidates:
            address = recipient.address_for(candidate)
            adapter = self._adapters.get(candidate)
            if address is None or adapter is None:
                continue
            if urgency is not Urgency.URGENT and recipient.is_quiet(candidate, moment):
                silenced.append(candidate)
                continue
            message = Message(
                template=template.name,
                body=body,
                channel=candidate,
                address=address,
                subject=subject,
                urgency=urgency,
            )
            try:
                reference = adapter.deliver(message)
            except Exception as exc:
                raise DeliveryError(candidate, address, exc) from exc
            return Receipt(message=message, reference=reference)
        if silenced:
            assert recipient.quiet_hours is not None
            raise QuietHoursError(template.name, silenced, recipient.quiet_hours.end)
        raise UnroutableError(template.name, candidates)

    def _preference(self, recipient: Recipient) -> tuple[Channel, ...]:
        order = list(recipient.preferred)
        order.extend(channel for channel in self._order if channel not in order)
        return tuple(order)
