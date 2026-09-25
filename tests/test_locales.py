"""Tests for templates per locale and their fallback chain."""

from __future__ import annotations

import pytest
from conftest import CONTEXT, Recorder

from notify_dispatch import (
    Channel,
    LocaleError,
    LocaleMismatchError,
    LocalizedTemplate,
    MalformedLocaleError,
    MissingLocaleError,
    MissingVariableError,
    Recipient,
    RetryPolicy,
    SmsAdapter,
    Template,
    Variable,
    fallback_chain,
    normalize_locale,
)

ORDER_ID = (Variable("order_id", str),)

TEXTS = {
    "en": "Order {order_id} is on its way.",
    "de": "Bestellung {order_id} ist unterwegs.",
    "pt-BR": "O pedido {order_id} está a caminho.",
}

SHIPPED = LocalizedTemplate.from_texts("order_shipped", TEXTS, variables=ORDER_ID)


def test_an_exact_locale_uses_its_own_variant():
    matched, template = SHIPPED.resolve("de")

    assert matched == "de"
    assert template.render(CONTEXT) == "Bestellung A-1042 ist unterwegs."


def test_a_region_falls_back_to_its_language():
    assert SHIPPED.resolve("de-AT")[0] == "de"
    assert SHIPPED.render(CONTEXT, locale="de-AT").startswith("Bestellung")


def test_an_untranslated_locale_falls_back_to_the_default():
    assert SHIPPED.resolve("fr-CA")[0] == "en"
    assert SHIPPED.render(CONTEXT, locale="fr-CA") == "Order A-1042 is on its way."


def test_without_a_locale_the_default_is_used():
    assert SHIPPED.resolve()[0] == "en"
    assert SHIPPED.render(CONTEXT) == "Order A-1042 is on its way."


def test_a_language_does_not_borrow_a_region_variant():
    assert SHIPPED.resolve("pt")[0] == "en"


def test_the_default_is_the_first_variant_unless_named():
    german = LocalizedTemplate.from_texts(
        "order_shipped", TEXTS, variables=ORDER_ID, default="de"
    )

    assert SHIPPED.default == "en"
    assert german.default == "de"
    assert german.resolve("fr")[0] == "de"


def test_tags_are_normalized_on_the_way_in_and_out():
    assert SHIPPED.locales == ("en", "de", "pt-BR")
    assert SHIPPED.resolve("pt_br")[0] == "pt-BR"
    assert SHIPPED.render(CONTEXT, locale="PT-br") == "O pedido A-1042 está a caminho."


def test_a_chain_drops_one_subtag_at_a_time():
    assert fallback_chain("zh-hant-tw", "en") == ("zh-Hant-TW", "zh-Hant", "zh", "en")


def test_a_chain_does_not_repeat_the_default():
    assert fallback_chain("en-GB", "en") == ("en-GB", "en")
    assert fallback_chain("en-GB") == ("en-GB", "en")


@pytest.mark.parametrize(
    ("tag", "canonical"),
    [
        ("de", "de"),
        ("PT_br", "pt-BR"),
        ("zh-hant", "zh-Hant"),
        ("es-419", "es-419"),
        (" en-gb ", "en-GB"),
    ],
)
def test_canonical_forms(tag, canonical):
    assert normalize_locale(tag) == canonical


@pytest.mark.parametrize("tag", ["", "   ", "de-", "e n", "123", "english-uk"])
def test_a_malformed_tag_is_rejected(tag):
    with pytest.raises(MalformedLocaleError):
        normalize_locale(tag)


def test_a_variant_declaring_other_variables_is_rejected():
    with pytest.raises(LocaleMismatchError) as excinfo:
        LocalizedTemplate(
            name="order_shipped",
            variants={
                "en": Template("order_shipped", TEXTS["en"], ORDER_ID),
                "de": Template(
                    "order_shipped",
                    "Bestellung {number} ist unterwegs.",
                    (Variable("number", str),),
                ),
            },
        )
    assert excinfo.value.locale == "de"
    assert excinfo.value.expected == ("order_id: str",)


def test_a_variant_declaring_another_type_is_rejected():
    with pytest.raises(LocaleMismatchError):
        LocalizedTemplate(
            name="order_shipped",
            variants={
                "en": Template("order_shipped", TEXTS["en"], ORDER_ID),
                "de": Template(
                    "order_shipped",
                    "Bestellung {order_id} ist unterwegs.",
                    (Variable("order_id", int),),
                ),
            },
        )


def test_a_default_without_a_variant_is_rejected():
    with pytest.raises(MissingLocaleError):
        LocalizedTemplate.from_texts(
            "order_shipped", TEXTS, variables=ORDER_ID, default="fr"
        )


def test_a_template_without_variants_is_rejected():
    with pytest.raises(LocaleError):
        LocalizedTemplate(name="order_shipped", variants={})


def test_the_same_locale_written_twice_is_rejected():
    with pytest.raises(LocaleError):
        LocalizedTemplate.from_texts(
            "order_shipped",
            {"pt-BR": TEXTS["pt-BR"], "pt_br": TEXTS["pt-BR"]},
            variables=ORDER_ID,
        )


def test_the_declared_variables_are_exposed_once():
    assert SHIPPED.variables == ORDER_ID


def test_a_missing_value_still_raises_in_the_localized_path():
    with pytest.raises(MissingVariableError):
        SHIPPED.render({}, locale="de")


def test_a_recipient_locale_picks_the_variant(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))

    receipt = dispatcher.send(Recipient(phone="+491", locale="de-AT"), SHIPPED, CONTEXT)

    assert receipt.message.locale == "de"
    assert receipt.channel is Channel.SMS
    assert sms.calls == [("+491", "Bestellung A-1042 ist unterwegs.")]


def test_an_explicit_locale_overrides_the_recipient(make_dispatcher):
    sms = Recorder()
    dispatcher = make_dispatcher(SmsAdapter(sms))

    receipt = dispatcher.send(
        Recipient(phone="+491", locale="de"), SHIPPED, CONTEXT, locale="pt-BR"
    )

    assert receipt.message.locale == "pt-BR"
    assert sms.calls == [("+491", "O pedido A-1042 está a caminho.")]


def test_a_recipient_without_a_locale_gets_the_default(make_dispatcher):
    dispatcher = make_dispatcher(SmsAdapter(Recorder()))

    receipt = dispatcher.send(Recipient(phone="+491"), SHIPPED, CONTEXT)

    assert receipt.message.locale == "en"
    assert receipt.message.body == "Order A-1042 is on its way."


def test_a_plain_template_is_sent_without_a_locale(make_dispatcher):
    dispatcher = make_dispatcher(SmsAdapter(Recorder()))
    template = Template("order_shipped", TEXTS["en"], ORDER_ID)

    receipt = dispatcher.send(Recipient(phone="+491", locale="de"), template, CONTEXT)

    assert receipt.message.locale is None
    assert receipt.message.body == "Order A-1042 is on its way."


def test_a_localized_body_is_rendered_once_and_retried_as_is(make_dispatcher, sleeps):
    sms = Recorder(failures=1)
    dispatcher = make_dispatcher(
        SmsAdapter(sms), retry=RetryPolicy(attempts=2, backoff=0.25)
    )

    receipt = dispatcher.send(Recipient(phone="+491", locale="de-AT"), SHIPPED, CONTEXT)

    first, second = sms.bodies
    # the same string object twice: one variant rendered once, handed over twice
    assert first is second
    assert first == "Bestellung A-1042 ist unterwegs."
    assert receipt.message.locale == "de"
    assert receipt.attempts == 2
    assert sleeps == [0.25]


def test_a_recipient_normalizes_its_locale():
    assert Recipient(phone="+491", locale="de_at").locale == "de-AT"


def test_a_recipient_with_an_unusable_locale_is_rejected():
    with pytest.raises(MalformedLocaleError):
        Recipient(phone="+491", locale="de-")
