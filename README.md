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
