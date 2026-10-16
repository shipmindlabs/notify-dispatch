"""An outbox that defers a send to whoever drains it.

A notification caused by a web request should not make that request wait for a
provider. Enqueueing hands back a ticket and touches nothing; draining performs
the queued sends in the order they arrived and resolves every ticket to the
receipt it produced or the exception it raised, so a failed notification is an
object someone can look at rather than a traceback in a worker log. A worker is
that drain on a thread of its own.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from enum import Enum
from threading import Condition, Event, Thread
from types import MappingProxyType
from uuid import uuid4

from notify_dispatch.dispatch import DispatchError, Dispatcher, Receipt, Recipient
from notify_dispatch.locales import LocalizedTemplate
from notify_dispatch.templates import Template

__all__ = [
    "Outbox",
    "Ticket",
    "TicketPendingError",
    "TicketState",
    "Worker",
]


class TicketPendingError(DispatchError):
    """The outcome of a ticket was asked for before it was sent."""

    def __init__(self, ticket_id: str) -> None:
        self.ticket_id = ticket_id
        super().__init__(f"ticket {ticket_id!r} has not been drained yet")


class TicketState(str, Enum):
    """How far a queued send has got."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class Ticket:
    """A queued send, and the receipt or exception the drain resolved it to."""

    def __init__(
        self,
        recipient: Recipient,
        template: Template | LocalizedTemplate,
        context: Mapping[str, object] | None = None,
        options: Mapping[str, object] | None = None,
        *,
        enqueued_at: datetime,
    ) -> None:
        self.id = uuid4().hex
        self.recipient = recipient
        self.template = template
        self.context: Mapping[str, object] = MappingProxyType(dict(context or {}))
        self.options: Mapping[str, object] = MappingProxyType(dict(options or {}))
        self.enqueued_at = enqueued_at
        self._receipt: Receipt | None = None
        self._error: BaseException | None = None
        self._settled = Event()

    @property
    def template_name(self) -> str:
        return self.template.name

    @property
    def state(self) -> TicketState:
        if not self._settled.is_set():
            return TicketState.PENDING
        return TicketState.SENT if self._error is None else TicketState.FAILED

    @property
    def is_pending(self) -> bool:
        return self.state is TicketState.PENDING

    @property
    def is_sent(self) -> bool:
        return self.state is TicketState.SENT

    @property
    def is_failed(self) -> bool:
        return self.state is TicketState.FAILED

    @property
    def receipt(self) -> Receipt | None:
        """The receipt the send produced, or None while there is none."""
        return self._receipt

    @property
    def error(self) -> BaseException | None:
        """What the send raised, or None while it has not failed."""
        return self._error

    def result(self) -> Receipt:
        """Return the receipt, or raise whatever the send raised."""
        if self._error is not None:
            raise self._error
        if self._receipt is None:
            raise TicketPendingError(self.id)
        return self._receipt

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the ticket is settled and return whether it is."""
        return self._settled.wait(timeout)

    def _fulfil(self, receipt: Receipt) -> None:
        self._receipt = receipt
        self._settled.set()

    def _fail(self, error: BaseException) -> None:
        self._error = error
        self._settled.set()

    def __repr__(self) -> str:
        return f"Ticket({self.id!r}, {self.template_name!r}, {self.state.value})"


class Outbox:
    """Sends a caller queued instead of waiting for, kept in arrival order."""

    def __init__(
        self,
        dispatcher: Dispatcher,
        *,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._dispatcher = dispatcher
        self._clock = clock
        self._queue: deque[Ticket] = deque()
        self._arrivals = Condition()

    @property
    def dispatcher(self) -> Dispatcher:
        return self._dispatcher

    @property
    def pending(self) -> tuple[Ticket, ...]:
        """The tickets still waiting for a drain, in order."""
        with self._arrivals:
            return tuple(self._queue)

    def enqueue(
        self,
        recipient: Recipient,
        template: Template | LocalizedTemplate,
        context: Mapping[str, object] | None = None,
        **options: object,
    ) -> Ticket:
        """Queue a send and return its ticket without touching a provider.

        Every keyword ``Dispatcher.send`` accepts is accepted here and handed
        over unchanged when the ticket is drained.
        """
        ticket = Ticket(recipient, template, context, options, enqueued_at=self._clock())
        with self._arrivals:
            self._queue.append(ticket)
            self._arrivals.notify_all()
        return ticket

    def drain(self, *, limit: int | None = None) -> tuple[Ticket, ...]:
        """Send the queued tickets in order and return the ones handled.

        The sends happen on the calling thread, and a failure settles its own
        ticket instead of ending the drain.
        """
        if limit is not None and limit < 1:
            raise ValueError("a drain must be allowed at least one ticket")
        handled: list[Ticket] = []
        while limit is None or len(handled) < limit:
            ticket = self._take()
            if ticket is None:
                break
            self._send(ticket)
            handled.append(ticket)
        return tuple(handled)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until a ticket is queued and return whether one is."""
        with self._arrivals:
            return self._arrivals.wait_for(lambda: bool(self._queue), timeout)

    def wake(self) -> None:
        """Release whatever waits for a ticket, whether one arrived or not."""
        with self._arrivals:
            self._arrivals.notify_all()

    def _take(self) -> Ticket | None:
        with self._arrivals:
            return self._queue.popleft() if self._queue else None

    def _send(self, ticket: Ticket) -> None:
        try:
            receipt = self._dispatcher.send(
                ticket.recipient,
                ticket.template,
                ticket.context,
                **ticket.options,
            )
        except Exception as exc:
            ticket._fail(exc)
        else:
            ticket._fulfil(receipt)

    def __len__(self) -> int:
        with self._arrivals:
            return len(self._queue)

    def __iter__(self) -> Iterator[Ticket]:
        return iter(self.pending)

    def __repr__(self) -> str:
        return f"Outbox({len(self)} pending)"


class Worker:
    """Drains an outbox on a thread of its own until it is stopped."""

    def __init__(
        self,
        outbox: Outbox,
        *,
        interval: float = 0.05,
        name: str = "notify-dispatch-worker",
    ) -> None:
        if interval <= 0:
            raise ValueError("a worker interval must be positive")
        self._outbox = outbox
        self._interval = interval
        self._name = name
        self._stopping = Event()
        self._thread: Thread | None = None

    @property
    def outbox(self) -> Outbox:
        return self._outbox

    @property
    def interval(self) -> float:
        return self._interval

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> Worker:
        """Begin draining in the background and return the worker."""
        if self.is_running:
            raise RuntimeError("the worker is already running")
        self._stopping.clear()
        self._thread = Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float | None = None) -> None:
        """Stop the loop and join it, draining what is still queued first."""
        self._stopping.set()
        self._outbox.wake()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        while not self._stopping.is_set():
            if self._outbox.wait(self._interval):
                self._outbox.drain()
        self._outbox.drain()

    def __enter__(self) -> Worker:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()

    def __repr__(self) -> str:
        state = "running" if self.is_running else "stopped"
        return f"Worker({self._name!r}, {state})"
