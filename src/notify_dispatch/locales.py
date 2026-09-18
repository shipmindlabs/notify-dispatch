"""Templates per locale, resolved through a fallback chain.

A notification is one template with a variant per language, not one column per
language. A request for ``de-AT`` uses the ``de-AT`` variant if there is one,
then ``de``, then the default locale, so adding a language is adding a key.
Every variant declares the same variables, so a locale that is only reached
long after it was written cannot be the one that fails to render.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from notify_dispatch.templates import Template, TemplateError, Variable

__all__ = [
    "LocaleError",
    "LocaleMismatchError",
    "LocalizedTemplate",
    "MalformedLocaleError",
    "MissingLocaleError",
    "fallback_chain",
    "normalize_locale",
]


class LocaleError(TemplateError):
    """Base class for every error raised while localizing a template."""


class MalformedLocaleError(LocaleError):
    """A string was used where a language tag was expected."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        super().__init__(f"{tag!r} is not a usable language tag")


class MissingLocaleError(LocaleError):
    """The locale a template falls back to has no variant of its own."""

    def __init__(self, name: str, locale: str, known: Iterable[str]) -> None:
        self.name = name
        self.locale = locale
        self.known = tuple(known)
        super().__init__(
            f"template {name!r} has no variant for its default locale {locale}; "
            f"it has {', '.join(self.known)}"
        )


class LocaleMismatchError(LocaleError):
    """A variant declares different variables than the default one."""

    def __init__(
        self, name: str, locale: str, expected: Iterable[str], found: Iterable[str]
    ) -> None:
        self.name = name
        self.locale = locale
        self.expected = tuple(expected)
        self.found = tuple(found)
        super().__init__(
            f"template {name!r} declares {_listed(self.found)} for {locale}, "
            f"but {_listed(self.expected)} for its default locale"
        )


def normalize_locale(tag: str) -> str:
    """Return a language tag in canonical form ('pt_br' -> 'pt-BR')."""
    subtags = tag.strip().replace("_", "-").split("-") if tag else []
    if not subtags or not _is_language(subtags[0]):
        raise MalformedLocaleError(tag)
    canonical = [subtags[0].lower()]
    for subtag in subtags[1:]:
        if not (1 <= len(subtag) <= 8 and subtag.isascii() and subtag.isalnum()):
            raise MalformedLocaleError(tag)
        if len(subtag) == 4 and subtag.isalpha():
            canonical.append(subtag.title())
        elif len(subtag) == 2 and subtag.isalpha():
            canonical.append(subtag.upper())
        else:
            canonical.append(subtag.lower())
    return "-".join(canonical)


def fallback_chain(tag: str, default: str | None = None) -> tuple[str, ...]:
    """Return the tags tried for a locale, from most to least specific."""
    subtags = normalize_locale(tag).split("-")
    chain = ["-".join(subtags[:count]) for count in range(len(subtags), 0, -1)]
    if default is not None:
        fallback = normalize_locale(default)
        if fallback not in chain:
            chain.append(fallback)
    return tuple(chain)


@dataclass(frozen=True)
class LocalizedTemplate:
    """One notification in several languages, keyed by language tag.

    An empty ``default`` means the first variant given.
    """

    name: str
    variants: Mapping[str, Template] = field(default_factory=dict)
    default: str = ""

    def __post_init__(self) -> None:
        if not self.variants:
            raise LocaleError(f"template {self.name!r} needs at least one variant")
        normalized: dict[str, Template] = {}
        for tag, template in self.variants.items():
            locale = normalize_locale(tag)
            if locale in normalized:
                raise LocaleError(
                    f"template {self.name!r} declares {locale} twice"
                )
            normalized[locale] = template
        object.__setattr__(self, "variants", MappingProxyType(normalized))
        default = (
            normalize_locale(self.default) if self.default else next(iter(normalized))
        )
        if default not in normalized:
            raise MissingLocaleError(self.name, default, normalized)
        object.__setattr__(self, "default", default)
        expected = _signature(normalized[default])
        for locale, template in normalized.items():
            found = _signature(template)
            if found != expected:
                raise LocaleMismatchError(self.name, locale, expected, found)

    @classmethod
    def from_texts(
        cls,
        name: str,
        texts: Mapping[str, str],
        variables: Iterable[Variable] = (),
        *,
        default: str = "",
    ) -> LocalizedTemplate:
        """Build a variant per locale from one text each."""
        declared = tuple(variables)
        variants = {
            locale: Template(name=name, text=text, variables=declared)
            for locale, text in texts.items()
        }
        return cls(name=name, variants=variants, default=default)

    @property
    def locales(self) -> tuple[str, ...]:
        return tuple(self.variants)

    @property
    def variables(self) -> tuple[Variable, ...]:
        return self.variants[self.default].variables

    def chain(self, locale: str | None = None) -> tuple[str, ...]:
        """Return the tags a request for this locale walks, in order."""
        if locale is None:
            return (self.default,)
        return fallback_chain(locale, self.default)

    def resolve(self, locale: str | None = None) -> tuple[str, Template]:
        """Return the locale actually used and the template it belongs to."""
        matched = next(
            (tag for tag in self.chain(locale) if tag in self.variants), self.default
        )
        return matched, self.variants[matched]

    def for_locale(self, locale: str | None = None) -> Template:
        """Return the template a request for this locale ends up on."""
        return self.resolve(locale)[1]

    def render(
        self, values: Mapping[str, object], *, locale: str | None = None
    ) -> str:
        """Render the variant for a locale, or the default one."""
        return self.for_locale(locale).render(values)

    def __repr__(self) -> str:
        return (
            f"LocalizedTemplate({self.name!r}, {', '.join(self.locales)}, "
            f"default={self.default})"
        )


def _signature(template: Template) -> tuple[str, ...]:
    return tuple(
        f"{variable.name}: {variable.type.__name__}"
        for variable in sorted(template.variables, key=lambda variable: variable.name)
    )


def _listed(signature: Iterable[str]) -> str:
    return ", ".join(signature) or "nothing"


def _is_language(subtag: str) -> bool:
    return 2 <= len(subtag) <= 3 and subtag.isascii() and subtag.isalpha()
