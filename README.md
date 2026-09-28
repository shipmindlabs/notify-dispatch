# notify-dispatch

Template-driven notifications across SMS, push and email: one dispatch API,
provider adapters, background delivery.

A notification is a template, a recipient and one call. The dispatcher renders
the text, hands it to the first channel the recipient can actually be reached
on, and — once you name the event — delivers it a single time, however often
that event arrives.

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

The rules a template is held to:

- **Text and declaration must agree.** An undeclared `{placeholder}`, a declared
  variable the text never uses, or the same name declared twice raises
  `TemplateDefinitionError` at construction time — before the template is ever
  sent.
- **Placeholders are plain names.** `{}`, `{0}`, `{order.id}` and nested format
  specs are rejected; a format spec on a name (`{total:.2f}`) is not.
- **Values are checked at render time**, and raise `MissingVariableError`,
  `UnknownVariableError` or `VariableTypeError` — the last one also for a `bool`
  where `int` or `float` is declared, because a flag where a count belongs is a
  bug rather than a `1`.
- **A send renders once.** The body is produced before any channel is chosen, so
  a template error never reaches a provider, and every retry hands over the same
  string.

## Channels

A channel is an adapter: an object with a `channel` and a `deliver(message)`
that hands the rendered body to a provider and returns whatever reference the
provider gives back. Three adapters ship with the package, one per channel.

| Channel        | Address on `Recipient` | Adapter        | Provider is called with    |
| -------------- | ---------------------- | -------------- | -------------------------- |
| `Channel.PUSH` | `device_token`         | `PushAdapter`  | `(token, body)`            |
| `Channel.SMS`  | `phone`                | `SmsAdapter`   | `(phone, body)`            |
| `Channel.EMAIL`| `email`                | `EmailAdapter` | `(address, subject, body)` |

```python
from notify_dispatch import (
    Channel, Dispatcher, EmailAdapter, PushAdapter, SmsAdapter,
)

dispatcher = Dispatcher([
    PushAdapter(lambda token, body: fcm.send(token, body)),
    SmsAdapter(lambda phone, body: twilio.messages.create(phone, body).sid),
    EmailAdapter(
        lambda address, subject, body: ses.send(address, subject, body),
        default_subject="Notification",
    ),
])

dispatcher.channels  # (<Channel.PUSH: 'push'>, <Channel.SMS: 'sms'>, <Channel.EMAIL: 'email'>)
```

A dispatcher holds at most one adapter per channel: `register()` adds one later
and replaces whatever was there, which is how a provider is swapped without
touching a call site. `Dispatcher(..., order=(Channel.EMAIL, Channel.SMS))`
changes which channel is tried first, and `dispatcher.channels` reports the
registered channels in that order.

Anything satisfying the `ChannelAdapter` protocol is an adapter, so a provider
whose call does not fit the three shipped ones needs no subclassing:

```python
from dataclasses import dataclass

from notify_dispatch import Channel, Message

@dataclass(frozen=True)
class ShortCodeAdapter:
    channel: Channel = Channel.SMS

    def deliver(self, message: Message) -> str | None:
        return gateway.publish(to=message.address, text=message.body).id

dispatcher.register(ShortCodeAdapter())
```

The `Message` an adapter receives carries the template name, the rendered body,
the channel and address it was routed to, the subject, the urgency and the
locale that was used.

## Sending

One call sends a notification. The dispatcher renders the template and picks the
first channel the recipient can be reached on.

```python
from notify_dispatch import Recipient

receipt = dispatcher.send(
    Recipient(phone="+49151000000", email="ada@example.com"),
    confirmation,
    {"order_id": "A-1042", "item_count": 3, "total": 59.9},
)
receipt.channel  # <Channel.SMS: 'sms'> — no device token, so push was skipped
```

Routing follows the recipient's `preferred` channels first, then the
dispatcher's order (push, SMS, email by default). Pass `channel=Channel.EMAIL`
to pin a single channel. A candidate without an address, or without a registered
adapter, is skipped. If none is left, `send` raises `UnroutableError`; if the
provider itself fails, the error is wrapped in `DeliveryError` carrying the
channel and address.

## Locales

A notification is one template with a variant per language, keyed by language
tag. A request for `de-AT` uses the `de-AT` variant if there is one, then `de`,
then the default locale — so a new language is a new key, not a new column.

```python
from notify_dispatch import LocalizedTemplate, Variable

shipped = LocalizedTemplate.from_texts(
    "order_shipped",
    {
        "en": "Order {order_id} is on its way.",
        "de": "Bestellung {order_id} ist unterwegs.",
        "pt-BR": "O pedido {order_id} está a caminho.",
    },
    variables=(Variable("order_id", str),),
)

shipped.render({"order_id": "A-1042"}, locale="pt_br")
# 'O pedido A-1042 está a caminho.'

receipt = dispatcher.send(
    Recipient(phone="+49151000000", locale="de-AT"), shipped, {"order_id": "A-1042"}
)
receipt.message.locale  # 'de' — there is no de-AT variant, so the language matched
```

Tags are normalised, so `pt_br` and `PT-br` are both `pt-BR`. The chain drops
one subtag at a time and ends at the default locale — the first variant given,
unless `default=` names another — so a language nobody translated is sent in the
default one instead of raising. A bare `pt` does not silently become `pt-BR`:
only tags you actually wrote are matched.

Every variant declares the same variables, checked when the `LocalizedTemplate`
is built, so a German text that forgot `{order_id}` raises `LocaleMismatchError`
there rather than on the first send to a German recipient. `Recipient(locale=…)`
carries the language, `send(..., locale=…)` overrides it for one message, and
`receipt.message.locale` records which variant was used.

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

The key is yours to choose, and choosing it is the whole contract:

- **A key names an event, not a call.** Two sends sharing a key are the same
  notification, so scope the key to the event *and* the recipient when one event
  notifies several people.
- **A key is remembered only once a provider accepted the handover.** A template
  error, an `UnroutableError`, a quiet-hours refusal or an exhausted
  `DeliveryError` leaves the key free, so the next redelivery is a real attempt.
  What is remembered is the receipt the retries produced, not the first try.
- **A repeat is not sent, and not even rendered.** The first receipt comes back
  with `duplicate=True`; its reference, channel and attempt count are those of
  the delivery that actually happened.
- **A key still in flight raises `DuplicateSendError`.** A second send that
  arrives while the first is on its way to a provider is refused rather than
  queued or silently dropped.
- **A key is forgotten after its ttl**, counted from the moment it completed.
  `DEFAULT_DEDUP_TTL` is 24 hours, long enough to outlive a provider's
  redelivery window.

```python
from datetime import timedelta

from notify_dispatch import DedupStore

store = DedupStore(ttl=timedelta(hours=6))
dispatcher = Dispatcher(adapters, dedup=store)

store.get("order-confirmed:evt-1")  # the DedupRecord, or None if it is unknown
store.purge()                        # drop what is past its ttl
```

Pass `ttl=None` to remember keys for as long as the process lives, hand the same
store to several dispatchers to deduplicate across them, and call
`dispatcher.dedup.purge()` from whatever already runs periodically. The store
keeps its keys in memory: one process deduplicates against itself, and two
processes share nothing unless something else in front of them does.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
pytest
```

The suite reaches no provider and waits for nothing: every adapter hands over to
a recorder, and every dispatcher is built with the test's own clock and a sleep
that records the backoff instead of spending it.

## License

MIT — see [LICENSE](LICENSE).

Maintained by [Shipmind Labs](https://shipmindlabs.com).
