from __future__ import annotations

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tool_registry import get_tool_definition
from src.application.tool_input_schema import build_tool_input_json_schema, validate_tool_input_payload


@pytest.mark.parametrize(("bounds", "value", "expected", "actual"), [
    ({"minLength": 2, "maxLength": 3}, "", "at least 2 characters", "0"),
    ({"minLength": 2, "maxLength": 3}, "a", "at least 2 characters", "1"),
    ({"minLength": 2, "maxLength": 3}, "ab", None, None),
    ({"minLength": 2, "maxLength": 3}, "abc", None, None),
    ({"minLength": 2, "maxLength": 3}, "abcd", "at most 3 characters", "4"),
    ({"minLength": 1}, "", "at least 1 characters", "0"),
    ({"minLength": 1}, "a", None, None),
    ({"maxLength": 1}, "ab", "at most 1 characters", "2"),
    ({"maxLength": 1}, "a", None, None),
    ({"maxLength": 0}, "a", "at most 0 characters", "1"),
    ({"minLength": 2, "maxLength": 2}, "汉😀", None, None),
    ({"minLength": 2, "maxLength": 2}, "e\u0301", None, None),
    ({"minLength": 1, "maxLength": 1}, "😀", None, None),
    ({"minLength": 1, "maxLength": 1}, " ", None, None),
])
def test_declared_string_lengths(bounds, value, expected, actual):
    schema = build_tool_input_json_schema({"value": {"type": "string", **bounds}})
    if expected is None:
        validate_tool_input_payload(tool_name="string_contract", payload={"value": value}, schema=schema)
        return

    with pytest.raises(AgentToolError) as error:
        validate_tool_input_payload(tool_name="string_contract", payload={"value": value}, schema=schema)

    assert error.value.code == "INPUT_ERROR"
    assert error.value.details["schema_errors"] == [
        {"path": "value", "expected": expected, "actual": actual},
    ]


@pytest.mark.parametrize("bounds", [{}, {"maxLength": 1}, {"minLength": 0}, {"maxLength": 0}])
def test_empty_strings_without_positive_minimum_remain_valid(bounds):
    field = {"type": "string", **bounds}
    schema = build_tool_input_json_schema({"value": field})
    assert schema["properties"]["value"] == field
    validate_tool_input_payload(tool_name="string_contract", payload={"value": ""}, schema=schema)


def test_optional_string_length_does_not_require_property():
    schema = build_tool_input_json_schema({"value": {"type": "string", "minLength": 1}})
    validate_tool_input_payload(tool_name="string_contract", payload={}, schema=schema, enforce_required=True)


@pytest.mark.parametrize(("value", "expected", "actual"), [
    (None, None, None),
    ("a", None, None),
    ("", "at least 1 characters", "0"),
    (7, "string or null", "integer"),
    ([], "string or null", "array"),
    ({}, "string or null", "object"),
])
def test_nullable_string_lengths_preserve_type_validation(value, expected, actual):
    schema = build_tool_input_json_schema({
        "value": {"type": ["string", "null"], "minLength": 1, "maxLength": 1},
    })
    if expected is None:
        validate_tool_input_payload(tool_name="string_contract", payload={"value": value}, schema=schema)
        return

    with pytest.raises(AgentToolError) as error:
        validate_tool_input_payload(tool_name="string_contract", payload={"value": value}, schema=schema)

    assert error.value.code == "INPUT_ERROR"
    assert error.value.details["schema_errors"] == [
        {"path": "value", "expected": expected, "actual": actual},
    ]


@pytest.mark.parametrize("value", [None, 7, [], {}])
def test_string_lengths_do_not_apply_to_other_allowed_types(value):
    schema = {
        "type": ["string", "null", "integer", "array", "object"],
        "minLength": 1,
        "maxLength": 1,
    }
    validate_tool_input_payload(tool_name="string_contract", payload=value, schema=schema)


def test_nested_array_string_length_uses_existing_error_path():
    schema = build_tool_input_json_schema({
        "nested": {"type": "object", "properties": {
            "labels": {"type": "array", "items": {"type": "string", "minLength": 1}},
        }},
    })
    validate_tool_input_payload(
        tool_name="string_contract", payload={"nested": {"labels": ["a", "b"]}}, schema=schema,
    )
    with pytest.raises(AgentToolError) as error:
        validate_tool_input_payload(
            tool_name="string_contract", payload={"nested": {"labels": ["a", ""]}}, schema=schema,
        )
    assert error.value.details["schema_errors"] == [
        {"path": "nested.labels[1]", "expected": "at least 1 characters", "actual": "0"},
    ]


def test_additional_property_string_length_uses_existing_error_path():
    schema = build_tool_input_json_schema({}, additional_properties={"type": "string", "maxLength": 1})
    validate_tool_input_payload(tool_name="string_contract", payload={"extra": "a"}, schema=schema)
    with pytest.raises(AgentToolError) as error:
        validate_tool_input_payload(tool_name="string_contract", payload={"extra": "ab"}, schema=schema)
    assert error.value.details["schema_errors"] == [
        {"path": "extra", "expected": "at most 1 characters", "actual": "2"},
    ]


def test_project_files_preserves_empty_root_and_cursor_with_declared_maximum():
    definition = get_tool_definition("project_files")
    assert definition is not None
    definition.validate_input({"action": "list", "resource": "project", "relative_name": "", "cursor": ""})
    with pytest.raises(AgentToolError) as error:
        definition.validate_input({"action": "list", "relative_name": "a" * 1025})
    assert error.value.code == "INPUT_ERROR"
    assert error.value.details["schema_errors"] == [
        {"path": "relative_name", "expected": "at most 1024 characters", "actual": "1025"},
    ]
