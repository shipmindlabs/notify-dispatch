# notify-dispatch

Template-driven notifications across SMS, push and email: one dispatch API,
provider adapters, background delivery.

## Status

Pre-alpha. The public API is not stable yet.

## Installation

```bash
pip install notify-dispatch
```

## Templates

A template declares the variables it needs and the type of each one. Values are
validated before a provider ever sees the message, so a forgotten `order_id`
fails in your tests instead of reaching a customer as a literal `{order_id}`.

```python
from notify_dispatch import Template, Variable

confirmation = Template(
    name="order_confirmed",
    text="Order {order_id} confirmed: {item_count} item(s), total {total:.2f}.",
    variables=(
        Variable("order_id", str),
        Variable("item_count", int),
        Variable("total", float),
    ),
)

confirmation.render({"order_id": "A-1042", "item_count": 3, "total": 59.9})
# 'Order A-1042 confirmed: 3 item(s), total 59.90.'

confirmation.render({"order_id": "A-1042", "item_count": 3})
# MissingVariableError: template 'order_confirmed' is missing values for: total
```

Rendering raises `MissingVariableError`, `UnknownVariableError` or
`VariableTypeError`. A template whose text and declaration disagree — an
undeclared `{placeholder}`, or a declared variable the text never uses — raises
`TemplateDefinitionError` at construction time.

## Sending

One call sends a notification. The dispatcher renders the template and picks the
first channel the recipient can be reached on; SMS, push and email are adapters
behind the same protocol.

```python
from notify_dispatch import (
    Dispatcher, EmailAdapter, PushAdapter, Recipient, SmsAdapter,
)

dispatcher = Dispatcher([
    PushAdapter(lambda token, body: fcm.send(token, body)),
    SmsAdapter(lambda phone, body: twilio.messages.create(phone, body).sid),
    EmailAdapter(lambda address, subject, body: ses.send(address, subject, body)),
])

receipt = dispatcher.send(
    Recipient(phone="+49151000000", email="ada@example.com"),
    confirmation,
    {"order_id": "A-1042", "item_count": 3, "total": 59.9},
)
receipt.channel  # <Channel.SMS: 'sms'> — no device token, so push was skipped
```

Routing follows the recipient's `preferred` channels first, then the
dispatcher's order (push, SMS, email by default). Pass `channel=Channel.EMAIL`
to pin a single channel. If no candidate has both an address and a registered
adapter, `send` raises `UnroutableError`; if the provider itself fails, the
error is wrapped in `DeliveryError` carrying the channel and address.

## Quiet hours

A recipient can carry a do-not-disturb window. Inside it the silenced channels
are skipped, so routing falls through to a channel that is still allowed. If
nothing is left, `send` raises `QuietHoursError` rather than waking someone up.

```python
from datetime import time

from notify_dispatch import Channel, QuietHours, Recipient, Urgency

ada = Recipient(
    device_token="tok",
    email="ada@example.com",
    preferred=(Channel.PUSH,),
    quiet_hours=QuietHours(time(22, 0), time(7, 0), channels=(Channel.PUSH, Channel.SMS)),
)

dispatcher.send(ada, confirmation, context)               # 23:30 — email, push is silent
dispatcher.send(ada, outage, context, urgency=Urgency.URGENT)  # push anyway
```

Only an explicit `urgency=Urgency.URGENT` overrides the window; the chosen
urgency travels on to the adapter as `message.urgency`. The window is read
against the dispatcher's clock — pass `now=` for a fixed moment, or
`Dispatcher(..., clock=...)` to control it everywhere. A window whose start is
later than its end (22:00–07:00) wraps past midnight.

## Attempts, retries and dead letters

A delivery is attempted once by default. Pass a `RetryPolicy` to try again with
a growing backoff. Whatever still fails is kept in `dispatcher.dead_letters`
together with every attempt that was made — a lost notification is something you
can list, not a line someone has to find in a worker log.

```python
from notify_dispatch import DeliveryError, Dispatcher, RetryPolicy, SmsAdapter

dispatcher = Dispatcher(
    [SmsAdapter(lambda phone, body: twilio.messages.create(phone, body).sid)],
    retry=RetryPolicy(attempts=3, backoff=0.5),  # waits 0.5s, then 1.0s
)

try:
    receipt = dispatcher.send(ada, confirmation, context)
    receipt.attempts  # 2 — the first handover failed
except DeliveryError as exc:
    exc.attempts  # every failed handover, each with its error and timestamp

for letter in dispatcher.dead_letters:
    print(letter.template, letter.address, letter.last_error)

dispatcher.dead_letters.drain()  # hand them over to whoever replays them
```

The backoff is multiplied per attempt and capped at `max_backoff`, and only
errors listed in `retry_on` are retried, so a rejected address fails once rather
than three times. Pass `retry=` to a single `send` to override the dispatcher's
policy. A `DeadLetterQueue(limit=...)` keeps the newest letters and drops the
oldest; pass your own queue to share one across dispatchers. Waiting happens on
the calling thread, so `Dispatcher(..., sleep=...)` is injectable in tests.

## Deduplication

A webhook that is redelivered is the same event twice, not two notifications.
Pass the id you already have — the upstream delivery id, an outbox row id,
anything stable across redeliveries — as `dedup_key`, and the second call hands
back the first receipt instead of sending again.

```python
receipt = dispatcher.send(
    ada, confirmation, context, dedup_key=f"order-confirmed:{event_id}"
)
receipt.duplicate  # False the first time, True for every redelivery
```

The key is yours to choose, and choosing it is the whole contract: two sends
sharing a key are the same notification, so scope it to the event *and* the
recipient when one event notifies several people. A key is only remembered once
a provider accepted the handover — an `UnroutableError`, a quiet-hours refusal
or an exhausted `DeliveryError` leaves it free, so the next redelivery is a real
attempt. A second send that arrives while the first is still in the air raises
`DuplicateSendError`.

Keys are kept in memory for `DEFAULT_DEDUP_TTL` (24 hours), long enough to
outlive a provider's redelivery window. Pass
`Dispatcher(..., dedup=DedupStore(ttl=...))` to change it or to share one store
between dispatchers, and call `dispatcher.dedup.purge()` from whatever already
runs periodically.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
pytest
```

## License

MIT — see [LICENSE](LICENSE).

Maintained by [Shipmind Labs](https://shipmindlabs.com).
