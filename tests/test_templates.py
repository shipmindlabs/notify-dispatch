"""Tests for templates with typed placeholders."""

from __future__ import annotations

import pytest

from notify_dispatch import (
    MissingVariableError,
    Template,
    TemplateDefinitionError,
    UnknownVariableError,
    Variable,
    VariableTypeError,
)

ORDER_CONFIRMED = Template(
    name="order_confirmed",
    text="Order {order_id} confirmed: {item_count} item(s), total {total:.2f}.",
    variables=(
        Variable("order_id", str),
        Variable("item_count", int),
        Variable("total", float),
    ),
)


def test_render_fills_every_placeholder():
    body = ORDER_CONFIRMED.render(
        {"order_id": "A-1042", "item_count": 3, "total": 59.9}
    )
    assert body == "Order A-1042 confirmed: 3 item(s), total 59.90."


def test_missing_variable_raises_instead_of_leaking_placeholder():
    with pytest.raises(MissingVariableError) as excinfo:
        ORDER_CONFIRMED.render({"order_id": "A-1042", "item_count": 3})
    assert excinfo.value.missing == ("total",)
    assert "total" in str(excinfo.value)


def test_unknown_variable_is_rejected():
    with pytest.raises(UnknownVariableError) as excinfo:
        ORDER_CONFIRMED.render(
            {"order_id": "A-1042", "item_count": 3, "total": 59.9, "coupon": "X"}
        )
    assert excinfo.value.unknown == ("coupon",)


def test_wrong_type_is_rejected():
    with pytest.raises(VariableTypeError) as excinfo:
        ORDER_CONFIRMED.render(
            {"order_id": "A-1042", "item_count": "3", "total": 59.9}
        )
    assert excinfo.value.name == "item_count"
    assert excinfo.value.expected is int


def test_bool_is_not_accepted_where_a_number_is_declared():
    with pytest.raises(VariableTypeError):
        ORDER_CONFIRMED.render(
            {"order_id": "A-1042", "item_count": True, "total": 59.9}
        )


def test_bool_variable_accepts_bool():
    template = Template(
        name="delivery",
        text="Signature required: {signature}.",
        variables=(Variable("signature", bool),),
    )
    assert template.render({"signature": True}) == "Signature required: True."


def test_variable_names_are_exposed():
    assert ORDER_CONFIRMED.variable_names == ("order_id", "item_count", "total")


def test_template_without_variables_renders_as_is():
    template = Template(name="ping", text="Your driver is nearby.")
    assert template.render({}) == "Your driver is nearby."


def test_undeclared_placeholder_fails_at_definition_time():
    with pytest.raises(TemplateDefinitionError):
        Template(
            name="broken",
            text="Order {order_id} for {customer}.",
            variables=(Variable("order_id"),),
        )


def test_declared_but_unused_variable_fails_at_definition_time():
    with pytest.raises(TemplateDefinitionError):
        Template(
            name="broken",
            text="Order confirmed.",
            variables=(Variable("order_id"),),
        )


def test_duplicate_declaration_is_rejected():
    with pytest.raises(TemplateDefinitionError):
        Template(
            name="broken",
            text="Order {order_id}.",
            variables=(Variable("order_id"), Variable("order_id", int)),
        )


def test_positional_placeholders_are_rejected():
    with pytest.raises(TemplateDefinitionError):
        Template(name="broken", text="Order {}.", variables=())


def test_attribute_access_is_rejected():
    with pytest.raises(TemplateDefinitionError):
        Template(
            name="broken",
            text="Order {order.id}.",
            variables=(Variable("order"),),
        )


def test_malformed_text_is_rejected():
    with pytest.raises(TemplateDefinitionError):
        Template(name="broken", text="Order {order_id.", variables=())


def test_invalid_variable_name_is_rejected():
    with pytest.raises(TemplateDefinitionError):
        Variable("order id")
