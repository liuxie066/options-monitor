from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from src.application.strategy_lab.contracts import StrategyExperiment, StrategyPolicy, validate_strategy_type
from src.application.strategy_lab.dataset_contracts import StrategyLabDataset
from src.application.strategy_lab.preflight import run_strategy_lab_preflight
from src.application.strategy_lab.recommendation import recommend_strategy_lab
from src.application.strategy_lab.simulator import run_replay_backtest


EXPERIMENT_SCHEMA_VERSION = "strategy_lab_experiment.v1"


def run_strategy_lab_experiment(
    payload: Mapping[str, Any],
    *,
    dataset: StrategyLabDataset,
    base: Path,
) -> dict[str, Any]:
    payload_dict = dict(payload)
    preflight = run_strategy_lab_preflight(
        dataset,
        min_candidate_sample=_positive_int(payload_dict.get("min_candidate_sample"), default=5),
        min_outcome_sample=_positive_int(payload_dict.get("min_outcome_sample"), default=5),
        min_trace_or_reject_sample=_positive_int(payload_dict.get("min_trace_or_reject_sample"), default=1),
    )
    experiment = _experiment_from_payload(payload_dict, dataset=dataset, base=base)
    if preflight.get("status") != "evaluable":
        recommendation = recommend_strategy_lab(preflight, None)
        return {
            "schema_version": EXPERIMENT_SCHEMA_VERSION,
            "experiment_id": experiment.experiment_id,
            "dataset_id": dataset.dataset_id,
            "strategy_type": experiment.strategy_type,
            "status": "not_evaluable",
            "recommendation": recommendation,
            "preflight": preflight,
            "baseline_metrics": None,
            "candidate_metrics": None,
            "comparison": {},
            "warnings": list(preflight.get("warnings") or []),
        }

    result = run_replay_backtest(experiment, dataset.to_evidence())
    recommendation = recommend_strategy_lab(preflight, result)
    result_payload = result.to_dict()
    return {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "experiment_id": experiment.experiment_id,
        "dataset_id": dataset.dataset_id,
        "strategy_type": experiment.strategy_type,
        "status": "evaluable",
        "recommendation": recommendation,
        "preflight": preflight,
        "baseline_metrics": result_payload["baseline_metrics"],
        "candidate_metrics": result_payload["candidate_metrics"],
        "comparison": result_payload["comparison"],
        "evidence_summary": result_payload["evidence_summary"],
        "warnings": list(dict.fromkeys([*result_payload.get("warnings", []), *preflight.get("warnings", [])])),
    }


def _experiment_from_payload(payload: dict[str, Any], *, dataset: StrategyLabDataset, base: Path) -> StrategyExperiment:
    scope = dict(dataset.scope)
    strategy_type = validate_strategy_type(str(payload.get("strategy_type") or scope.get("strategy_type") or "sell_put"))
    experiment_id = str(payload.get("experiment_id") or "").strip() or f"{dataset.dataset_id}_experiment"
    candidate_params = dict(payload.get("candidate_params") or payload.get("policy_params") or {})
    grid_path = str(payload.get("candidate_grid") or payload.get("candidate_grid_path") or "").strip()
    if grid_path:
        candidate_params.update(_load_grid_first_candidate(grid_path, base=base))
    if not candidate_params:
        candidate_params = {"selection_source": "rules"}
    return StrategyExperiment(
        experiment_id=experiment_id,
        strategy_type=strategy_type,
        account=str(payload.get("account") or scope.get("account") or "").strip().lower() or None,
        start_date=str(payload.get("start_date") or scope.get("start_date") or "").strip() or None,
        end_date=str(payload.get("end_date") or scope.get("end_date") or "").strip() or None,
        baseline_policy=_policy(
            payload.get("baseline_policy"),
            default_name=str(payload.get("baseline") or "baseline_current"),
            strategy_type=strategy_type,
            default_params={"selection_source": "existing", **dict(payload.get("baseline_params") or {})},
        ),
        candidate_policy=_policy(
            payload.get("candidate_policy"),
            default_name="candidate",
            strategy_type=strategy_type,
            default_params={"selection_source": "rules", **candidate_params},
        ),
    )


def _policy(value: Any, *, default_name: str, strategy_type: str, default_params: dict[str, Any]) -> StrategyPolicy:
    payload = value if isinstance(value, dict) else {}
    params = dict(default_params)
    if isinstance(payload.get("params"), dict):
        params.update(payload["params"])
    return StrategyPolicy(
        name=str(payload.get("name") or default_name).strip() or default_name,
        strategy_type=validate_strategy_type(str(payload.get("strategy_type") or strategy_type)),
        params=params,
    )


def _load_grid_first_candidate(path_value: str, *, base: Path) -> dict[str, Any]:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        candidates = payload.get("candidates") or payload.get("grid") or payload.get("policies")
        if isinstance(candidates, list) and candidates:
            first = candidates[0]
            if isinstance(first, dict):
                return dict(first.get("params") if isinstance(first.get("params"), dict) else first)
        params = payload.get("params")
        if isinstance(params, dict):
            return dict(params)
        return {key: value for key, value in payload.items() if key not in {"candidates", "grid", "policies"}}
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return dict(payload[0].get("params") if isinstance(payload[0].get("params"), dict) else payload[0])
    return {}


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default

