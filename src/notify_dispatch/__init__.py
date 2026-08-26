"""Template-driven notifications across SMS, push and email."""

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
    "MissingVariableError",
    "Template",
    "TemplateDefinitionError",
    "TemplateError",
    "UnknownVariableError",
    "Variable",
    "VariableTypeError",
    "__version__",
]
