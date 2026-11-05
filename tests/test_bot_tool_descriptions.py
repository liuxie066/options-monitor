from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json

from jsonschema import Draft202012Validator
import pytest

from src.application.agent_tool_registry import AGENT_TOOL_REGISTRY, get_tool_definition
from src.application.bot import tools
from src.application.bot.contracts import ExecutionContract
from src.application.bot.scene import build_scene_manifest


def _serialized_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _without_descriptions(value):
    if isinstance(value, list):
        return [_without_descriptions(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: (
            {name: _without_descriptions(schema) for name, schema in child.items()}
            if key in {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}
            else deepcopy(child)
            if key in {"default", "enum", "const", "examples"}
            else _without_descriptions(child)
        )
        for key, child in value.items() if key != "description"
    }


def _constraint_fingerprint(descriptions) -> str:
    stable = [
        {"name": item["name"], "input_schema": _without_descriptions(item["input_schema"]),
         **({"default_input": item["default_input"]} if "default_input" in item else {})}
        for item in descriptions
    ]
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_catalog_and_scene_preserve_all_baseline_input_constraints() -> None:
    # Captured from 2ff8514b3 before compression. Only schema description
    # annotations are excluded; names, defaults and every validation keyword remain.
    catalog = tools.tool_descriptions(list(AGENT_TOOL_REGISTRY))
    assert _constraint_fingerprint(catalog) == "1ec25d5a3fed2a740d0e50a6e73f48930dd45c7a6586e40b9e77d2981beb5e97"
    contract = ExecutionContract("test", "test", "om_chat", "eval", {"user_message": "status"}, {}, {})
    descriptions = build_scene_manifest(contract, "test").tool_descriptions
    wire = [{key: item[key] for key in ("name", "description", "input_schema")} for item in descriptions]
    assert _constraint_fingerprint(wire) == "6c78ca9f5e16d4c09317cfcc30ae558bf9e437d68f121ca27b6c97b028bf6ffb"


def test_catalog_and_actual_scene_stay_within_measured_definition_budgets() -> None:
    catalog = tools.tool_descriptions(list(AGENT_TOOL_REGISTRY))
    assert len(catalog) == len(tools.available_read_tools())
    assert {item["name"] for item in catalog} == set(tools.available_read_tools())
    # Baseline 2ff8514b3: 78,764 bytes for the catalog, 16,969 for the Scene.
    assert _serialized_bytes(catalog) < 50_000
    contract = ExecutionContract("test", "test", "om_chat", "eval", {"user_message": "status"}, {}, {})
    manifest = build_scene_manifest(contract, "test")
    wire = [{key: item[key] for key in ("name", "description", "input_schema")} for item in manifest.tool_descriptions]
    assert _serialized_bytes(wire) < 14_000


@pytest.mark.parametrize("name", tools.available_read_tools())
def test_compact_catalog_keeps_purpose_constraints_and_valid_visible_example(name: str) -> None:
    definition = get_tool_definition(name)
    original_manifest = definition.to_manifest()
    description = tools.tool_descriptions((name,))[0]
    schema = description["input_schema"]

    assert definition.description in description["description"]
    assert schema == tools._bot_input_schema(definition)
    Draft202012Validator.check_schema(schema)
    assert "read_only" not in description["capabilities"]
    assert description["capabilities"] == [item for item in definition.capabilities if item != "read_only"]
    assert len(description["examples"]) <= 1
    for example in description["examples"]:
        assert set(example) == {"input"}
        assert set(example["input"]) <= set(schema["properties"])
        assert set(example["input"]) != {"config_key"}
        Draft202012Validator(schema).validate({**description["default_input"], **example["input"]})
    # The operator/execution owner must retain its complete metadata.
    assert definition.to_manifest() == original_manifest


def test_catalog_drops_duplicate_result_paths_but_preserves_full_owner_contract(monkeypatch) -> None:
    original = get_tool_definition("project_files")
    contract = {
        "schema_version": "test.v1",
        "source_label": "test source",
        "primary_rows": "rows",
        "fact_fields": ["rows[].id", "rows[].price", "coverage.complete"],
        "model_value_fields": ["rows", "coverage"],
        "pagination": {"mode": "keyset"},
        "missing_data_fields": ["rows[].price"],
    }
    definition = replace(original, output_contract=deepcopy(contract), output_contract_resolver=None)
    monkeypatch.setattr(tools, "get_tool_definition", lambda name: definition)

    item = tools.tool_descriptions((original.name,))[0]

    assert item["output_contract"] == {
        "schema_version": "test.v1",
        "primary_rows": "rows",
        "fact_fields": ["rows", "coverage"],
    }
    assert "Returns primary collection `rows`." in item["description"]
    assert "Source: test source." in item["description"]
    assert "rows[].price" not in item["description"]
    assert definition.resolve_output_contract({}) == contract
    item["output_contract"]["fact_fields"].append("mutated")
    assert definition.resolve_output_contract({}) == contract


def test_catalog_resolves_contract_with_static_payload_before_compressing() -> None:
    item = tools.tool_descriptions(
        ("option_positions_read",), static_payloads={"option_positions_read": {"action": "events"}}
    )[0]
    assert item["output_contract"]["schema_version"] == "option_positions_read.events_output.v1"
    assert item["output_contract"]["primary_rows"] == "rows"
    assert "next_cursor" in item["output_contract"]["fact_fields"]
    assert get_tool_definition("option_positions_read").resolve_output_contract({"action": "events"})["pagination"] == {
        "mode": "keyset",
        "cursor_ttl_seconds": 1800,
        "snapshot_boundary": "ingest_seq",
        "order": ["trade_time_ms DESC", "event_id DESC"],
    }


def test_catalog_example_omits_operator_paths_and_wrapper_metadata(monkeypatch) -> None:
    original = get_tool_definition("project_files")
    definition = replace(
        original,
        examples=(
            {"input": {"config_key": "us", "runtime_root": "/operator/root"}},
            {"title": "Search", "input": {"action": "search", "query": "wheel", "runtime_root": "/operator/root"}},
            {"input": {"action": "read", "relative_name": "AGENTS.md"}},
        ),
    )
    monkeypatch.setattr(tools, "get_tool_definition", lambda name: definition)

    item = tools.tool_descriptions((original.name,))[0]

    assert item["examples"] == [{"input": {"action": "search", "query": "wheel"}}]
    assert definition.examples[1]["input"]["runtime_root"] == "/operator/root"
