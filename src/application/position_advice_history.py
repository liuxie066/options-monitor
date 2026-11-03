"""Historical references retain original sealed bytes and original decision time."""
from __future__ import annotations

import csv
from io import StringIO
from pathlib import Path
from typing import Any, Mapping

from domain.domain.close_advice import STRICT_CLOSE_POLICY_VERSION, has_complete_close_metrics
from src.application.close_advice_report_manifest import read_close_advice_report_snapshot
from src.application.position_advice_inputs import aware_time, position_economics_sha256


def find_position_reference(
    position: Mapping[str, Any], *, report_paths: list[Path], provider: str,
    now_utc: Any,
) -> dict[str, Any] | None:
    candidates: list[tuple[Any, dict[str, Any]]] = []
    for path in report_paths:
        snapshot = read_close_advice_report_snapshot(
            csv_path=path, account=str(position["account"]), expected_quote_mode="frozen_snapshot",
        )
        if not snapshot["validation"].get("ok"):
            continue
        rows = csv.DictReader(StringIO(snapshot["csv_bytes"].decode("utf-8")))
        for row in rows:
            observed = aware_time(row.get("assessment_as_of_utc"))
            if observed is None or observed > now_utc or row.get("assessment_mode") != "current" or str(row.get("current_actionable")).lower() != "true":
                continue
            if row.get("policy_version") != STRICT_CLOSE_POLICY_VERSION or row.get("market_data_provider") != provider or row.get("account") != position["account"]:
                continue
            if row.get("position_economics_sha256") != position_economics_sha256(position):
                continue
            if position.get("input_source") != "user_provided" and row.get("position_lot_id") != (position.get("lot_id") or position.get("record_id")):
                continue
            manual_time = aware_time(position.get("input_as_of_utc"))
            if position.get("input_source") == "user_provided" and (manual_time is None or manual_time > observed):
                continue
            if row.get("recommendation_state") not in {"hold", "close"} or row.get("evaluation_status") != "priced" or row.get("decision_evidence_status") != "complete" or not has_complete_close_metrics(row):
                continue
            candidates.append((observed, {**row, "reference_report_path": str(path), "reference_run_id": snapshot["validation"].get("run_id")}))
    if not candidates:
        return None
    _observed, reference = max(candidates, key=lambda item: item[0])
    return {
        **reference,
        "input_source": position.get("input_source", "ledger"),
        "input_id": position.get("input_id"),
        "input_as_of_utc": position.get("input_as_of_utc"),
        "position_lot_id": None if position.get("input_source") == "user_provided" else reference.get("position_lot_id"),
        "assessment_mode": "historical_reference", "current_actionable": False,
        "reference_recommendation_state": reference["recommendation_state"],
        "recommendation_state": "not_evaluable", "evaluation_status": "historical_reference",
        "decision_evidence_status": "not_evaluable",
        "reason": "休市期间仅供历史参考；保留原快照时点、价格与DTE，不代表当前建议",
    }


def reference_report_paths(runtime_root: Path, *, account: str) -> list[Path]:
    # Bounded directory traversal; only validated account reports can become references.
    paths: list[Path] = []
    roots = [runtime_root / "output_runs"]
    requests = runtime_root / "output_shared" / "position_advice_requests"
    if requests.is_dir():
        roots.extend(path / "output_runs" for path in sorted(requests.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:100] if path.is_dir())
    for root in roots:
        if not root.is_dir():
            continue
        for run in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
            if run.is_dir():
                path = run / "accounts" / account / "close_advice.csv"
                if path.is_file():
                    paths.append(path)
    return paths
