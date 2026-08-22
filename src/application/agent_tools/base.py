from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from src.application.tool_input_schema import build_tool_input_json_schema, validate_tool_input_payload

ToolHandlerResult = tuple[dict[str, Any], list[str], dict[str, Any]]
ToolHandler = Callable[[dict[str, Any]], ToolHandlerResult]
InputValidator = Callable[[dict[str, Any]], None]
WriteRequestPredicate = Callable[[dict[str, Any]], bool]
OutputContractResolver = Callable[[dict[str, Any]], dict[str, Any] | None]
CopilotInputNormalizer = Callable[[Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class AgentTool:
    name: str
    read_only: bool
    description: str
    requires: tuple[str, ...]
    capabilities: tuple[str, ...]
    input_schema: dict[str, Any]
    handler: ToolHandler = field(repr=False, compare=False)
    enabled: bool = True
    side_effects: tuple[str, ...] = ()
    risk_level: str | None = None
    requires_confirm: bool = False
    requires_env: tuple[str, ...] = ()
    safe_default_input: dict[str, Any] = field(default_factory=dict)
    examples: tuple[dict[str, Any], ...] = ()
    write_request_predicate: WriteRequestPredicate | None = field(default=None, repr=False, compare=False)
    input_validator: InputValidator | None = field(default=None, repr=False, compare=False)
    output_contract: dict[str, Any] = field(default_factory=dict)
    output_contract_resolver: OutputContractResolver | None = field(default=None, repr=False, compare=False)
    # One-line, model-facing purpose used by the canonical Copilot catalog.
    # This is metadata on the existing definition, not a second registry.
    catalog_summary: str = ""
    copilot_input_fields: tuple[str, ...] = ()
    copilot_input_schema: dict[str, Any] = field(default_factory=dict)
    copilot_input_normalizer: CopilotInputNormalizer | None = field(default=None, repr=False, compare=False)
    allow_additional_input: bool = True

    def resolved_risk_level(self) -> str:
        return self.risk_level or ("local_write" if self.side_effects else "read_only")

    def is_pure_read(self) -> bool:
        return (
            bool(self.read_only)
            and self.resolved_risk_level() == "read_only"
            and not self.side_effects
            and not self.requires_confirm
        )

    def is_write_requested(self, payload: dict[str, Any]) -> bool:
        if self.read_only:
            return False
        if self.write_request_predicate is not None:
            return bool(self.write_request_predicate(payload))
        if bool(payload.get("dry_run", False)):
            return False
        return bool(self.side_effects or self.requires_confirm or self.resolved_risk_level() != "read_only")

    def validate_input(self, payload: dict[str, Any]) -> None:
        schema = self.execution_input_json_schema()
        validate_tool_input_payload(
            tool_name=self.name,
            payload=payload,
            schema=schema,
            enforce_required=True,
        )
        if self.input_validator is not None:
            self.input_validator(payload)

    def call(self, payload: dict[str, Any]) -> ToolHandlerResult:
        self.validate_input(payload)
        return self.handler(payload)

    def input_json_schema(self) -> dict[str, Any]:
        schema = build_tool_input_json_schema(self.input_schema)
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, value in self.safe_default_input.items():
                if value is None:
                    continue
                if name in properties and isinstance(properties[name], dict):
                    properties[name].setdefault("default", deepcopy(value))
        return schema

    def execution_input_json_schema(self) -> dict[str, Any]:
        return build_tool_input_json_schema(
            self.input_schema,
            additional_properties=bool(self.allow_additional_input),
        )

    def resolve_output_contract(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.output_contract_resolver is not None:
            resolved = self.output_contract_resolver(payload)
            if isinstance(resolved, dict) and resolved:
                return deepcopy(resolved)
        return deepcopy(self.output_contract)

    def copilot_evidence_type(self) -> str:
        value = self.output_contract.get("evidence_type")
        if value not in {"point", "collection", "aggregate", "diagnostic", "mixed"}:
            raise ValueError(f"invalid or missing evidence_type: {self.name}")
        return str(value)

    def to_manifest(self) -> dict[str, Any]:
        side_effects = list(self.side_effects)
        output_contract = deepcopy(self.output_contract)
        return {
            "name": self.name,
            "read_only": self.read_only,
            "description": self.description,
            "requires": list(self.requires),
            "capabilities": list(self.capabilities),
            "side_effects": side_effects,
            "annotations": _manifest_annotations(self),
            "input_schema": dict(self.input_schema),
            "input_json_schema": self.input_json_schema(),
            "input_schema_version": "om-tool-input-v1",
            "output_schema": {},
            "risk_level": self.resolved_risk_level(),
            "requires_confirm": bool(self.requires_confirm),
            "requires_env": list(self.requires_env),
            "safe_default_input": dict(self.safe_default_input),
            "examples": deepcopy(list(self.examples)),
            "output_contract": output_contract,
            "catalog_summary": self.catalog_summary,
            "evidence_type": self.copilot_evidence_type() if self.is_pure_read() else "mixed",
        }


def _manifest_annotations(tool: AgentTool) -> dict[str, bool]:
    risk_level = tool.resolved_risk_level()
    return {
        "read_only": bool(tool.read_only),
        "destructive": bool("delete" in tool.side_effects or risk_level in {"destructive", "admin_write"}),
        "idempotent": bool(tool.read_only and not tool.side_effects and not tool.requires_confirm),
        "open_world": bool(tool.requires_env or risk_level in {"preview_admin", "remote_admin"}),
    }


def build_agent_tool(
    *,
    name: str,
    description: str,
    requires: tuple[str, ...],
    capabilities: tuple[str, ...],
    input_schema: dict[str, Any],
    handler: ToolHandler,
    enabled: bool = True,
    pure_read: bool = False,
    read_only: bool = False,
    side_effects: tuple[str, ...] = (),
    risk_level: str | None = "local_write",
    requires_confirm: bool = False,
    requires_env: tuple[str, ...] = (),
    safe_default_input: dict[str, Any] | None = None,
    examples: tuple[dict[str, Any], ...] = (),
    write_request_predicate: WriteRequestPredicate | None = None,
    input_validator: InputValidator | None = None,
    output_contract: dict[str, Any] | None = None,
    output_contract_resolver: OutputContractResolver | None = None,
    catalog_summary: str = "",
    copilot_input_fields: tuple[str, ...] = (),
    copilot_input_schema: dict[str, Any] | None = None,
    copilot_input_normalizer: CopilotInputNormalizer | None = None,
    allow_additional_input: bool = True,
) -> AgentTool:
    if pure_read:
        read_only = True
        side_effects = ()
        risk_level = "read_only"
        requires_confirm = False
    normalized_output_contract = deepcopy(output_contract or {})
    return AgentTool(
        name=name,
        read_only=bool(read_only),
        description=description,
        requires=requires,
        capabilities=capabilities,
        input_schema=input_schema,
        handler=handler,
        enabled=bool(enabled),
        side_effects=side_effects,
        risk_level=risk_level,
        requires_confirm=bool(requires_confirm),
        requires_env=requires_env,
        safe_default_input=dict(safe_default_input or {}),
        examples=examples,
        write_request_predicate=write_request_predicate,
        input_validator=input_validator,
        output_contract=normalized_output_contract,
        output_contract_resolver=output_contract_resolver,
        catalog_summary=str(catalog_summary or "").strip(),
        copilot_input_fields=tuple(copilot_input_fields),
        copilot_input_schema=deepcopy(copilot_input_schema or {}),
        copilot_input_normalizer=copilot_input_normalizer,
        allow_additional_input=bool(allow_additional_input),
    )
