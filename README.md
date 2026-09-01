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
