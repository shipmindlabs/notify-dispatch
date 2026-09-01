"""Template-driven notifications across SMS, push and email."""

from notify_dispatch.dispatch import (
    DEFAULT_CHANNEL_ORDER,
    Channel,
    ChannelAdapter,
    DeliveryError,
    DispatchError,
    Dispatcher,
    EmailAdapter,
    Message,
    PushAdapter,
    QuietHours,
    QuietHoursError,
    Receipt,
    Recipient,
    SmsAdapter,
    UnroutableError,
    Urgency,
)
from notify_dispatch.templates import (
    MissingVariableError,
    Template,
    TemplateDefinitionError,
    TemplateError,
    UnknownVariableError,
    Variable,
    VariableTypeError,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_CHANNEL_ORDER",
    "Channel",
    "ChannelAdapter",
    "DeliveryError",
    "DispatchError",
    "Dispatcher",
    "EmailAdapter",
    "Message",
    "MissingVariableError",
    "PushAdapter",
    "QuietHours",
    "QuietHoursError",
    "Receipt",
    "Recipient",
    "SmsAdapter",
    "Template",
    "TemplateDefinitionError",
    "TemplateError",
    "UnknownVariableError",
    "UnroutableError",
    "Urgency",
    "Variable",
    "VariableTypeError",
    "__version__",
]
