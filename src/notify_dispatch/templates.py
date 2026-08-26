"""Notification templates with declared, typed placeholders.

A template states which variables it needs and what type each one carries.
Rendering without a value, or with a value of the wrong type, raises instead of
delivering a message that still contains a literal ``{order_id}``.
"""

from __future__ import annotations

import string
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

__all__ = [
    "MissingVariableError",
    "Template",
    "TemplateDefinitionError",
    "TemplateError",
    "UnknownVariableError",
    "Variable",
    "VariableTypeError",
]

_FORMATTER = string.Formatter()


class TemplateError(Exception):
    """Base class for every error raised while defining or rendering."""


class TemplateDefinitionError(TemplateError):
    """The template text and its declared variables disagree."""


class MissingVariableError(TemplateError):
    """Rendering was attempted without a value for a declared variable."""

    def __init__(self, template: str, missing: Iterable[str]) -> None:
        self.template = template
        self.missing = tuple(sorted(missing))
        super().__init__(
            f"template {template!r} is missing values for: {', '.join(self.missing)}"
        )


class UnknownVariableError(TemplateError):
    """Rendering was given values the template does not declare."""

    def __init__(self, template: str, unknown: Iterable[str]) -> None:
        self.template = template
        self.unknown = tuple(sorted(unknown))
        super().__init__(
            f"template {template!r} does not declare: {', '.join(self.unknown)}"
        )


class VariableTypeError(TemplateError):
    """A value was supplied with a type the variable does not accept."""

    def __init__(self, template: str, name: str, expected: type, value: object) -> None:
        self.template = template
        self.name = name
        self.expected = expected
        self.value = value
        super().__init__(
            f"template {template!r} expects {name!r} to be {expected.__name__}, "
            f"got {type(value).__name__}"
        )


@dataclass(frozen=True)
class Variable:
    """A placeholder a template needs before it can be rendered."""

    name: str
    type: type = str

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise TemplateDefinitionError(f"{self.name!r} is not a valid variable name")
        if not isinstance(self.type, type):
            raise TemplateDefinitionError(f"type of {self.name!r} must be a class")

    def accepts(self, value: object) -> bool:
        # bool is a subclass of int, but a flag where a count belongs is a bug.
        if isinstance(value, bool) and self.type in (int, float):
            return False
        return isinstance(value, self.type)


@dataclass(frozen=True)
class Template:
    """A message body together with the variables it declares."""

    name: str
    text: str
    variables: tuple[Variable, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "variables", tuple(self.variables))
        declared = [variable.name for variable in self.variables]
        duplicates = sorted({name for name in declared if declared.count(name) > 1})
        if duplicates:
            raise TemplateDefinitionError(
                f"template {self.name!r} declares {', '.join(duplicates)} twice"
            )
        used = _placeholders(self.text)
        undeclared = sorted(used - set(declared))
        if undeclared:
            raise TemplateDefinitionError(
                f"template {self.name!r} uses undeclared variables: "
                f"{', '.join(undeclared)}"
            )
        unused = sorted(set(declared) - used)
        if unused:
            raise TemplateDefinitionError(
                f"template {self.name!r} declares unused variables: "
                f"{', '.join(unused)}"
            )

    @property
    def variable_names(self) -> tuple[str, ...]:
        return tuple(variable.name for variable in self.variables)

    def render(self, values: Mapping[str, object]) -> str:
        """Return the message body, or raise if the values are not usable."""
        provided = set(values)
        expected = {variable.name for variable in self.variables}
        missing = expected - provided
        if missing:
            raise MissingVariableError(self.name, missing)
        unknown = provided - expected
        if unknown:
            raise UnknownVariableError(self.name, unknown)
        for variable in self.variables:
            value = values[variable.name]
            if not variable.accepts(value):
                raise VariableTypeError(self.name, variable.name, variable.type, value)
        return _FORMATTER.vformat(self.text, (), dict(values))


def _placeholders(text: str) -> set[str]:
    try:
        parsed = list(_FORMATTER.parse(text))
    except ValueError as exc:
        raise TemplateDefinitionError(f"malformed template text: {exc}") from exc

    names: set[str] = set()
    for _literal, field_name, format_spec, _conversion in parsed:
        if field_name is None:
            continue
        if not field_name.isidentifier():
            raise TemplateDefinitionError(
                f"placeholder {{{field_name}}} must be a plain variable name"
            )
        if format_spec and "{" in format_spec:
            raise TemplateDefinitionError(
                f"placeholder {{{field_name}}} may not use a nested format spec"
            )
        names.add(field_name)
    return names
