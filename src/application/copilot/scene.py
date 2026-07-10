from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.application.copilot.contracts import (
    CapabilityHintDefinition,
    ExecutionContract,
    SceneDefinition,
    SceneManifest,
)

MONTHLY_INCOME_ATTRIBUTION_ANALYSIS_VIEWS = (
    "account_monthly_performance",
    "account_monthly_income_components",
    "monthly_income_summary",
    "symbol_income_attribution",
)

CURRENT_EXPOSURE_ANALYSIS_VIEWS = (
    "open_option_exposure",
    "expiration_risk_buckets",
)

SCENE_CATALOG: tuple[SceneDefinition, ...] = (
    SceneDefinition(
        name="operations_diagnostics",
        capabilities=("runtime_status", "candidate_filter_diagnostics", "close_advice_notification_diagnostics"),
        required_scope=("config_key",),
        allowed_tools=("runtime_status", "candidate_filter_explain", "close_advice_read"),
        environments=("local", "eval"),
        phase_readiness="local_only",
        allow_mock_observations=True,
        mock_environments=("eval",),
        fixture_ids=("candidate_filter_diagnostics_model_ready", "close_advice_notification_diagnostics_model_ready"),
        task_guidance=(
            "Use read-only runtime and diagnostic observations to explain the likely cause.",
            "Finish only when current observations support the answer or missing evidence is explicit.",
            "Do not infer filter reasons that are not present in the observations.",
            "Use runtime notification diagnosis and close-advice observations to explain missing close-advice notifications.",
            "Do not claim a notification was sent unless an observation shows a confirmed send.",
        ),
        capability_hints=(
            CapabilityHintDefinition(
                capability="runtime_status",
                activation_terms=(("健康",), ("运行",), ("runtime",), ("health",)),
                activation_reason="scene_runtime_hint",
                tools=("runtime_status",),
            ),
            CapabilityHintDefinition(
                capability="candidate_filter_diagnostics",
                activation_terms=(
                    ("候选",),
                    ("筛选",),
                    ("过滤",),
                    ("入选",),
                    ("选上",),
                    ("candidate",),
                    ("filter",),
                ),
                activation_reason="scene_candidate_filter_hint",
                required_scope=("symbol",),
                tools=("runtime_status", "candidate_filter_explain"),
            ),
            CapabilityHintDefinition(
                capability="close_advice_notification_diagnostics",
                activation_terms=(
                    ("close advice", "通知"),
                    ("close-advice", "通知"),
                    ("close_advice", "通知"),
                    ("平仓", "通知"),
                ),
                activation_reason="scene_close_advice_notification_hint",
                tools=("runtime_status", "close_advice_read"),
            ),
        ),
    ),
    SceneDefinition(
        name="monthly_income_attribution",
        capabilities=("monthly_income_attribution",),
        required_scope=("config_key", "month"),
        allowed_tools=(
            "analysis_catalog",
            "analysis_query",
            "monthly_income_report",
        ),
        environments=("local", "eval"),
        phase_readiness="local_only",
        requires_answer_synthesis=True,
        allow_mock_observations=True,
        mock_environments=("eval",),
        fixture_ids=("june_income_attribution_basic",),
        task_guidance=(
            "Explain monthly income attribution from read-only income component observations.",
            "Identify income sources only when supported by observations.",
            "If income evidence is missing or weak, make missing_data explicit instead of completing the attribution.",
            "Do not turn grouped rows into the final response.",
        ),
        capability_hints=(
            CapabilityHintDefinition(
                capability="monthly_income_attribution",
                activation_terms=(
                    ("收益",),
                    ("收入",),
                    ("月", "利润"),
                    ("月", "盈利"),
                    ("月", "亏损"),
                    ("月", "盈亏"),
                    ("月", "赚钱"),
                    ("月", "赚得"),
                    ("income",),
                    ("attribution",),
                ),
                activation_reason="phase2_monthly_income_attribution_hint",
                tools=(
                    "analysis_catalog",
                    "analysis_query",
                    "monthly_income_report",
                ),
            ),
        ),
        tool_static_payloads={
            "analysis_catalog": {"views": list(MONTHLY_INCOME_ATTRIBUTION_ANALYSIS_VIEWS)},
            "analysis_query": {"views": list(MONTHLY_INCOME_ATTRIBUTION_ANALYSIS_VIEWS), "limit": 200},
            "monthly_income_report": {"include_rows": True},
        },
    ),
    SceneDefinition(
        name="current_option_exposure",
        capabilities=("current_option_exposure",),
        required_scope=("config_key",),
        allowed_tools=(
            "analysis_catalog",
            "analysis_query",
            "option_positions_read",
        ),
        environments=("local", "eval"),
        phase_readiness="local_only",
        requires_answer_synthesis=True,
        allow_mock_observations=True,
        mock_environments=("eval",),
        fixture_ids=("current_option_exposure_model_ready",),
        task_guidance=(
            "Analyze current open option exposure and expiration concentration from read-only observations.",
            "Use exposure rows and expiration buckets to identify concentration only when supported by observations.",
            "If open exposure evidence is missing or weak, make missing_data explicit instead of completing the analysis.",
            "Do not turn grouped rows into the final response.",
        ),
        capability_hints=(
            CapabilityHintDefinition(
                capability="current_option_exposure",
                activation_terms=(
                    ("期权", "风险", "暴露"),
                    ("期权", "仓位", "风险"),
                    ("期权", "仓位", "集中"),
                    ("期权", "持仓", "风险"),
                    ("期权", "持仓", "集中"),
                    ("期权", "敞口"),
                    ("风险暴露",),
                    ("暴露", "集中"),
                    ("敞口", "集中"),
                    ("exposure",),
                    ("concentration",),
                ),
                activation_reason="phase2_current_option_exposure_hint",
                tools=(
                    "analysis_catalog",
                    "analysis_query",
                    "option_positions_read",
                ),
            ),
        ),
        tool_static_payloads={
            "analysis_catalog": {"views": list(CURRENT_EXPOSURE_ANALYSIS_VIEWS)},
            "analysis_query": {"views": list(CURRENT_EXPOSURE_ANALYSIS_VIEWS), "limit": 200},
            "option_positions_read": {"action": "list", "status": "open", "limit": 200},
        },
    ),
)
INVALID_CONTRACT_VALUE = "__invalid_contract_value__"


@dataclass(frozen=True)
class SceneSelectionDecision:
    selected_scene: SceneDefinition | None
    candidate_scenes: tuple[str, ...]
    rejected_scenes: tuple[dict[str, str], ...]
    execution_environment: str
    scene_override: str | None = None

    def trace(self) -> dict[str, Any]:
        return {
            "selected_scene": self.selected_scene.name if self.selected_scene else None,
            "candidate_scenes": list(self.candidate_scenes),
            "rejected_scenes": [dict(item) for item in self.rejected_scenes],
            "scene_override": self.scene_override,
            "selection_environment": self.execution_environment,
        }


def capability_hint_definitions() -> tuple[CapabilityHintDefinition, ...]:
    return tuple(hint for scene in SCENE_CATALOG for hint in scene.capability_hints)


def scene_phase_readiness(scene_name: str) -> str:
    scene = _scene_by_name(scene_name)
    return scene.phase_readiness if scene else ""


def select_scene(
    *,
    capabilities: set[str],
    execution_environment: str,
    scene_override: str | None = None,
) -> SceneSelectionDecision:
    if scene_override:
        scene = next((item for item in SCENE_CATALOG if item.name == scene_override), None)
        if scene is None:
            return SceneSelectionDecision(
                selected_scene=None,
                candidate_scenes=(),
                rejected_scenes=({"scene_name": scene_override, "reason": "scene_not_found"},),
                execution_environment=execution_environment,
                scene_override=scene_override,
            )
        if execution_environment not in scene.environments:
            return SceneSelectionDecision(
                selected_scene=None,
                candidate_scenes=(scene.name,),
                rejected_scenes=({"scene_name": scene.name, "reason": "environment_not_allowed"},),
                execution_environment=execution_environment,
                scene_override=scene_override,
            )
        return SceneSelectionDecision(
            selected_scene=scene,
            candidate_scenes=(scene.name,),
            rejected_scenes=(),
            execution_environment=execution_environment,
            scene_override=scene_override,
        )

    candidate_scenes: list[str] = []
    rejected_scenes: list[dict[str, str]] = []
    matched_scenes: list[SceneDefinition] = []
    for scene in SCENE_CATALOG:
        if execution_environment not in scene.environments:
            rejected_scenes.append({"scene_name": scene.name, "reason": "environment_not_allowed"})
            continue
        candidate_scenes.append(scene.name)
        if capabilities.intersection(scene.capabilities):
            matched_scenes.append(scene)
            continue
        rejected_scenes.append({"scene_name": scene.name, "reason": "capability_mismatch"})
    if len(matched_scenes) > 1:
        rejected_scenes.extend({"scene_name": scene.name, "reason": "ambiguous_scene_match"} for scene in matched_scenes)
        selected_scene = None
    else:
        selected_scene = matched_scenes[0] if matched_scenes else None
    return SceneSelectionDecision(
        selected_scene=selected_scene,
        candidate_scenes=tuple(candidate_scenes),
        rejected_scenes=tuple(rejected_scenes),
        execution_environment=execution_environment,
        scene_override=None,
    )


def reject_scene_override_for_environment(
    *,
    scene_override: str,
    execution_environment: str,
) -> SceneSelectionDecision:
    return SceneSelectionDecision(
        selected_scene=None,
        candidate_scenes=(),
        rejected_scenes=({"scene_name": scene_override, "reason": "scene_override_requires_eval"},),
        execution_environment=execution_environment,
        scene_override=scene_override,
    )


def missing_required_scope(scene: SceneDefinition, scope: dict[str, Any]) -> list[str]:
    return [name for name in scene.required_scope if not _scope_value_present(scope.get(name))]


def missing_capability_scope(scene: SceneDefinition, capabilities: set[str], scope: dict[str, Any]) -> list[str]:
    required: list[str] = []
    for hint in scene.capability_hints:
        if hint.capability not in capabilities:
            continue
        required.extend(hint.required_scope)
    return _missing_scope(required, scope)


def scene_policy_rejection_reason(contract: ExecutionContract) -> str | None:
    scene = _scene_by_name(contract.scene_name)
    if scene is None:
        return f"unknown scene {contract.scene_name}"
    selected_scene = _optional_contract_string(contract.decision_trace, "selected_scene")
    if selected_scene != contract.scene_name:
        return "contract selected scene does not match scene catalog"
    read_only = _contract_bool(contract.policy, "read_only")
    if read_only is not True:
        return "contract is not read-only"
    requested_capabilities = _requested_capabilities(contract)
    if not requested_capabilities:
        return "contract has no requested scene capabilities"
    unknown_capabilities = sorted(requested_capabilities - set(scene.capabilities))
    if unknown_capabilities:
        return "contract capabilities do not match scene catalog"
    if not _project_tools_for_capabilities(scene, requested_capabilities):
        return "contract capabilities do not map to allowed scene tools"
    missing_scope = missing_required_scope(scene, contract.input)
    if missing_scope:
        return "contract missing required scope: " + ", ".join(missing_scope)
    missing_capability = missing_capability_scope(scene, requested_capabilities, contract.input)
    if missing_capability:
        return "contract missing capability scope: " + ", ".join(missing_capability)
    requested_tools = _optional_policy_tuple(contract.policy, "allowed_tools")
    if requested_tools is not None and requested_tools != scene.allowed_tools:
        return "contract tools do not match scene catalog"
    requested_environments = _optional_policy_tuple(contract.policy, "allowed_environments")
    if requested_environments is not None and requested_environments != scene.environments:
        return "contract environments do not match scene catalog"
    phase_readiness = _optional_contract_string(contract.policy, "phase_readiness")
    if phase_readiness and phase_readiness != scene.phase_readiness:
        return "contract phase readiness does not match scene catalog"
    if "requires_answer_synthesis" in contract.policy:
        requires_answer_synthesis = _contract_bool(contract.policy, "requires_answer_synthesis")
        if requires_answer_synthesis is None or requires_answer_synthesis != bool(scene.requires_answer_synthesis):
            return "contract answer-synthesis policy does not match scene catalog"
    if "requires_recommendations" in contract.policy:
        requires_recommendations = _contract_bool(contract.policy, "requires_recommendations")
        if requires_recommendations is None or requires_recommendations != bool(scene.requires_recommendations):
            return "contract recommendation policy does not match scene catalog"
    if "allow_mock_observations" in contract.policy:
        allow_mock_observations = _contract_bool(contract.policy, "allow_mock_observations")
        if allow_mock_observations is None or allow_mock_observations != bool(scene.allow_mock_observations):
            return "contract mock policy does not match scene catalog"
    requested_mock_environments = _optional_policy_tuple(contract.policy, "mock_environments")
    if requested_mock_environments is not None and requested_mock_environments != scene.mock_environments:
        return "contract mock policy does not match scene catalog"
    requested_fixture_ids = _optional_policy_tuple(contract.policy, "fixture_ids")
    if requested_fixture_ids is not None and requested_fixture_ids != scene.fixture_ids:
        return "contract mock policy does not match scene catalog"
    if contract.execution_environment not in scene.environments:
        return f"environment {contract.execution_environment} is not allowed for scene {contract.scene_name}"
    if scene.phase_readiness == "eval_only" and contract.execution_environment != "eval":
        return f"scene {contract.scene_name} is eval-only"
    fixture_id = _optional_contract_string(contract.input, "fixture_id")
    if fixture_id is None:
        return "contract fixture id is malformed"
    if contract.execution_environment in scene.mock_environments and scene.fixture_ids and not fixture_id:
        return "mock environment requires an explicit eval fixture"
    if fixture_id:
        if contract.execution_environment not in scene.mock_environments:
            return f"mock observations are not allowed in {contract.execution_environment}"
        if not scene.allow_mock_observations:
            return "mock observations require explicit policy allowance"
        if fixture_id not in scene.fixture_ids:
            return f"fixture {fixture_id or '<missing>'} is not allowed"
    return None


def policy_for_scene(scene: SceneDefinition) -> dict[str, Any]:
    return {
        "read_only": True,
        "allow_mock_observations": bool(scene.allow_mock_observations),
        "requires_answer_synthesis": bool(scene.requires_answer_synthesis),
        "requires_recommendations": bool(scene.requires_recommendations),
        "max_model_turns": max(3, len(scene.allowed_tools) + 1),
        "max_tool_calls": max(3, len(scene.allowed_tools)),
        "timeout_seconds": 30,
    }


def build_scene_manifest(contract: ExecutionContract, run_id: str) -> SceneManifest:
    scene = _scene_by_name(contract.scene_name)
    policy = policy_for_scene(scene) if scene else contract.policy
    allowed_tools = _project_allowed_tools(scene, contract, policy)
    return SceneManifest(
        run_id=run_id,
        scene_name=contract.scene_name,
        execution_environment=contract.execution_environment,
        messages=[
            {"role": "system", "content": "Select the next action using only allowed read-only OM tools."},
            {"role": "user", "content": _optional_contract_string(contract.input, "user_message") or ""},
        ],
        allowed_tools=allowed_tools,
        limits={
            "max_model_turns": int(policy.get("max_model_turns") or 3),
            "max_tool_calls": int(policy.get("max_tool_calls") or 3),
            "timeout_seconds": int(policy.get("timeout_seconds") or 30),
        },
        output_schema=_scene_output_schema(scene),
        task_guidance={
            "scene": scene.name if scene else contract.scene_name,
            "instructions": list(scene.task_guidance) if scene else [],
            "answer_dimensions": list(scene.answer_dimensions) if scene else [],
            "requires_answer_synthesis": bool(policy.get("requires_answer_synthesis") is True),
            "requires_recommendations": bool(policy.get("requires_recommendations") is True),
        },
        tool_static_payloads=_tool_static_payloads(scene, allowed_tools),
    )

def _scene_by_name(scene_name: str) -> SceneDefinition | None:
    return next((item for item in SCENE_CATALOG if item.name == scene_name), None)


def _scene_output_schema(scene: SceneDefinition | None) -> dict[str, Any]:
    if scene is None or not isinstance(scene.output_schema, dict):
        return {"type": "AnswerReport"}
    return dict(scene.output_schema)


def _project_allowed_tools(
    scene: SceneDefinition | None,
    contract: ExecutionContract,
    policy: dict[str, Any],
) -> list[str]:
    scene_tools = scene.allowed_tools if scene else _policy_tuple(policy, "allowed_tools")
    if not scene:
        return list(scene_tools)

    return _project_tools_for_capabilities(scene, _requested_capabilities(contract))


def _tool_static_payloads(scene: SceneDefinition | None, allowed_tools: list[str]) -> dict[str, dict[str, Any]]:
    if scene is None:
        return {}
    allowed = set(allowed_tools)
    return {
        tool_name: dict(payload)
        for tool_name, payload in scene.tool_static_payloads.items()
        if tool_name in allowed and isinstance(payload, dict)
    }


def _requested_capabilities(contract: ExecutionContract) -> set[str]:
    raw = contract.decision_trace.get("requested_capabilities")
    if raw is None:
        raw = ()
    if not isinstance(raw, (list, tuple, set)):
        return set()
    capabilities: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            return {INVALID_CONTRACT_VALUE}
        text = " ".join(item.split())
        if not text:
            return {INVALID_CONTRACT_VALUE}
        capabilities.add(text)
    return capabilities


def _policy_tuple(policy: dict[str, Any], key: str) -> tuple[str, ...]:
    raw = policy.get(key)
    if raw is None:
        raw = ()
    if not isinstance(raw, (list, tuple)):
        return (INVALID_CONTRACT_VALUE,)
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return (INVALID_CONTRACT_VALUE,)
        text = " ".join(item.split())
        if not text:
            return (INVALID_CONTRACT_VALUE,)
        values.append(text)
    return tuple(values)


def _optional_policy_tuple(policy: dict[str, Any], key: str) -> tuple[str, ...] | None:
    if key not in policy:
        return None
    return _policy_tuple(policy, key)


def _contract_bool(values: dict[str, Any], key: str) -> bool | None:
    if key not in values:
        return None
    raw = values.get(key)
    if isinstance(raw, bool):
        return raw
    return None


def _optional_contract_string(values: dict[str, Any], key: str) -> str | None:
    raw = values.get(key)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        return None
    return " ".join(raw.split())


def _tools_for_capabilities(scene: SceneDefinition, requested_capabilities: set[str]) -> set[str]:
    return {
        tool_name
        for hint in scene.capability_hints
        if hint.capability in requested_capabilities
        for tool_name in hint.tools
    }


def _dedupe_non_empty(values: list[str] | tuple[str, ...]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split())
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _project_tools_for_capabilities(scene: SceneDefinition, requested_capabilities: set[str]) -> list[str]:
    requested_tools = _tools_for_capabilities(scene, requested_capabilities)
    return [tool_name for tool_name in scene.allowed_tools if tool_name in requested_tools]


def _missing_scope(required_scope: list[str], scope: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for name in required_scope:
        if name not in missing and not _scope_value_present(scope.get(name)):
            missing.append(name)
    return missing


def _scope_value_present(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
