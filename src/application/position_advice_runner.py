from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.position_advice_authority import (
    AuthorityResolution,
    normalize_account_label,
    scope_for,
)
from src.application.ledger.api import (
    decision_state_snapshot,
    open_position_ledger,
)
from src.application.position_advice_authority_service import (
    read_authority_resolution,
)
from src.application.position_advice_input_builder import (
    build_immutable_input,
    build_with_stable_inputs,
    publish_current_manifest,
    write_immutable_json,
)
from src.application.position_advice_plan_builder import (
    build_position_advice_plan,
    quote_rows_from_source_payloads,
)
from src.application.position_advice_source_receipts import (
    PositionAdviceSourceError,
    adopt_source_snapshot,
    build_source_manifest,
    safe_existing_relative_path,
    validate_source_manifest,
)
from src.application.opening_candidate_snapshot import (
    ranked_opening_candidate_decisions,
    validate_opening_candidate_snapshot,
)
from src.infrastructure.io_utils import atomic_write_text


POSITION_ADVICE_RUNNER_SCHEMA = "position_advice_runner.v2"


class PositionAdviceRunnerError(RuntimeError):
    """Raised when a v2 run cannot prove a complete, coherent advice plan."""


def run_position_advice_v2_from_account_run(
    *,
    base: Path,
    account_run_root: Path,
    account_run_id: str,
    account: str,
    broker: str,
    included_markets: Iterable[str],
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    capacity_pool_authority_id: str | None,
    source_receipts: Iterable[Mapping[str, Any]],
    data_config_path: Path,
    decision_state_snapshot_override: Mapping[str, Any] | None = None,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Account Run facade over retained or live ledger decision facts."""

    normalized_account = normalize_account_label(account)
    if isinstance(decision_state_snapshot_override, Mapping):
        decision_snapshot_reader = lambda: dict(
            decision_state_snapshot_override
        )
    else:
        repo = open_position_ledger(Path(data_config_path))
        decision_snapshot_reader = lambda: decision_state_snapshot(
            repo,
            account=normalized_account,
            portfolio_scope_id=scope_for(normalized_account),
        )
    return run_position_advice_v2(
        base=base,
        account_run_root=account_run_root,
        account_run_id=account_run_id,
        normalized_account=normalized_account,
        broker=broker,
        included_markets=included_markets,
        normalized_portfolio_source=normalized_portfolio_source,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
        capacity_pool_authority_id=capacity_pool_authority_id,
        source_receipts=source_receipts,
        decision_snapshot_reader=decision_snapshot_reader,
        now=now,
    )


def run_position_advice_v2(
    *,
    base: Path,
    account_run_root: Path,
    account_run_id: str,
    normalized_account: str,
    broker: str,
    included_markets: Iterable[str],
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    capacity_pool_authority_id: str | None,
    source_receipts: Iterable[Mapping[str, Any]],
    decision_snapshot_reader: Callable[[], Mapping[str, Any]],
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Build and publish one advisory-only Position Advice v2 plan."""

    checked_at = _timestamp(now or datetime.now(timezone.utc))
    account = normalize_account_label(normalized_account)
    run_id = _required_text(account_run_id, "account_run_id")
    base_path = Path(base).resolve()
    account_root = Path(account_run_root).resolve()
    run_root = base_path / "output_runs" / run_id
    _assert_account_run_root(
        account_root=account_root,
        run_root=run_root,
        account=account,
    )
    markets = _markets(included_markets)
    resolution = read_authority_resolution(
        base=base_path,
        normalized_account=account,
        normalized_portfolio_source=normalized_portfolio_source,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
    )
    if resolution.resolution_status == "first_use_default_v1" or (
        resolution.resolution_status == "resolved"
        and resolution.mode == "v1"
    ):
        return _non_publish_result(
            status="skipped_v1_authority",
            run_id=run_id,
            account=account,
            resolution=resolution,
        )
    if (
        resolution.resolution_status != "resolved"
        or resolution.mode not in {"v2_shadow", "v2"}
        or not resolution.policy_hash
    ):
        return _non_publish_result(
            status="authority_conflict",
            run_id=run_id,
            account=account,
            resolution=resolution,
        )

    adopted = _adopt_all_sources(
        source_receipts=source_receipts,
        account_root=account_root,
        account_run_id=run_id,
        account=account,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
        now=checked_at,
    )
    source_manifest = build_source_manifest(
        account_run_id=run_id,
        portfolio_scope_id=resolution.portfolio_scope_id,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
        adopted_sources=adopted,
        required_for_actions={
            "quotes": ("short_put", "covered_call"),
            "opening_candidates": ("short_put", "covered_call"),
            "portfolio": ("short_put", "covered_call"),
            "ledger_decision_state": ("short_put", "covered_call"),
            "cash_capacity": ("short_put",),
            "share_coverage": ("covered_call",),
            "fx": ("short_put",),
        },
    )
    manifest_path = (
        account_root / "state" / "position_advice_source_manifest.v2.json"
    )
    write_immutable_json(
        manifest_path,
        source_manifest,
        hash_field="source_manifest_hash",
    )

    def source_manifest_reader() -> dict[str, Any]:
        return validate_source_manifest(
            _read_json_object(manifest_path),
            consumer_run_root=account_root,
            now=checked_at,
            expected_account_run_id=run_id,
            expected_scope_id=resolution.portfolio_scope_id,
            expected_identity_hash=portfolio_account_identity_hash,
        )

    stable = build_with_stable_inputs(
        decision_snapshot_reader=decision_snapshot_reader,
        source_manifest_reader=source_manifest_reader,
        build=lambda state, manifest: _build_bound_artifacts(
            account_run_id=run_id,
            account=account,
            broker=broker,
            included_markets=markets,
            normalized_portfolio_source=normalized_portfolio_source,
            portfolio_account_identity_hash=portfolio_account_identity_hash,
            capacity_pool_authority_id=capacity_pool_authority_id,
            authority_resolution=resolution,
            account_root=account_root,
            source_manifest=manifest,
            decision_state_snapshot=state,
            checked_at=checked_at,
        ),
    )
    artifacts = dict(stable["artifact"])
    immutable_input = dict(artifacts["input"])
    advice = dict(artifacts["advice"])
    input_path = account_root / "state" / "position_advice_input.v2.json"
    advice_path = account_root / "position_advice.v2.json"
    csv_path = account_root / "position_advice.v2.csv"
    text_path = account_root / "position_advice.v2.txt"
    write_immutable_json(input_path, immutable_input, hash_field="input_hash")
    write_immutable_json(advice_path, advice, hash_field="artifact_hash")
    atomic_write_text(csv_path, render_position_advice_csv(advice))
    atomic_write_text(text_path, render_position_advice_text(advice))

    switched = publish_current_manifest(
        base=base_path,
        run_id=run_id,
        account_run_root=account_root,
        normalized_account=account,
        broker=broker,
        included_markets=markets,
        normalized_portfolio_source=normalized_portfolio_source,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
        source_manifest_relpath=_relative_to_run(run_root, manifest_path),
        advice_artifact_relpath=_relative_to_run(run_root, advice_path),
        input_artifact_relpath=_relative_to_run(run_root, input_path),
        expected_decision_state_fingerprint=str(
            stable["decision_state_snapshot"]["decision_state_fingerprint"]
        ),
        decision_snapshot_reader=decision_snapshot_reader,
        now=checked_at,
    )
    return {
        "schema_version": POSITION_ADVICE_RUNNER_SCHEMA,
        "status": "published",
        "account_run_id": run_id,
        "account": account,
        "portfolio_scope_id": resolution.portfolio_scope_id,
        "authority_mode": resolution.mode,
        "build_attempt": stable["attempt"],
        "rows": len(advice["rows"]),
        "model_actionable_rows": sum(
            1 for row in advice["rows"] if row.get("model_actionable") is True
        ),
        "model_trade_actionable_rows": sum(
            1
            for row in advice["rows"]
            if row.get("model_trade_actionable") is True
        ),
        "human_review_required_rows": sum(
            1
            for row in advice["rows"]
            if row.get("human_review_required") is True
        ),
        "actionable_rows": sum(
            1 for row in advice["rows"] if row.get("actionable") is True
        ),
        "portfolio_plan_id": advice["portfolio_plan_id"],
        "source_manifest_hash": source_manifest["source_manifest_hash"],
        "decision_state_fingerprint": advice["decision_state_fingerprint"],
        "current_switched": True,
        "current_manifest_hash": switched["manifest"][
            "current_manifest_hash"
        ],
        "paths": {
            "json": str(advice_path),
            "csv": str(csv_path),
            "text": str(text_path),
            "input": str(input_path),
            "source_manifest": str(manifest_path),
            "current": str(switched["path"]),
        },
        "notified": False,
    }


def render_position_advice_csv(advice: Mapping[str, Any]) -> str:
    fields = [
        "position_id",
        "strategy_family",
        "strategy_group_id",
        "leg_role",
        "lifecycle_state",
        "group_structure_state",
        "recommendation",
        "model_actionable",
        "model_trade_actionable",
        "human_review_required",
        "actionable",
        "action_scope",
        "comparison_currency",
        "net_carry_improvement_H",
        "net_carry_improvement_H_base_cny",
        "payback_days",
        "reason_codes",
        "quote_as_of",
        "promotion_scope_status",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for raw in advice.get("rows") or []:
        row = dict(raw)
        writer.writerow(
            {
                field: (
                    json.dumps(
                        row.get(field),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if isinstance(row.get(field), (dict, list))
                    else row.get(field)
                )
                for field in fields
            }
        )
    return buffer.getvalue()


def render_position_advice_text(advice: Mapping[str, Any]) -> str:
    rows = [dict(item) for item in advice.get("rows") or []]
    lines = [
        "# Position Advice v2",
        "",
        f"- 账户：{advice.get('account')}",
        f"- 模式：{advice.get('authority_mode')}",
        f"- 组合计划：{advice.get('portfolio_plan_id')}",
        f"- 正式可行动建议：{sum(1 for item in rows if item.get('actionable') is True)}",
        "",
    ]
    if not rows:
        lines.append("本轮没有开放期权持仓。")
    for row in rows:
        reasons = "、".join(str(item) for item in row.get("reason_codes") or [])
        lines.append(
            "- "
            f"{row.get('position_id')} | {row.get('strategy_family')} | "
            f"{row.get('recommendation')} | "
            f"{'需用户确认' if row.get('actionable') else '不行动'}"
            + (f" | {reasons}" if reasons else "")
        )
    lines.extend(
        [
            "",
            "> 这是只读建议，不会自动下单或修改持仓。",
            "",
        ]
    )
    return "\n".join(lines)


def _build_bound_artifacts(
    *,
    account_run_id: str,
    account: str,
    broker: str,
    included_markets: list[str],
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    capacity_pool_authority_id: str | None,
    authority_resolution: AuthorityResolution,
    account_root: Path,
    source_manifest: Mapping[str, Any],
    decision_state_snapshot: Mapping[str, Any],
    checked_at: str,
) -> dict[str, Any]:
    payloads = _read_source_payloads(account_root, source_manifest)
    candidate_source = _single_payload(payloads, "opening_candidates")
    cash_source = _single_payload(payloads, "cash_capacity")
    coverage_source = _single_payload(payloads, "share_coverage")
    fx_source = _single_payload(payloads, "fx")
    validate_opening_candidate_snapshot(
        candidate_source,
        expected_run_id=account_run_id,
        expected_account=account,
        verify_dependency_root=account_root.parents[3],
    )
    candidate_decisions = ranked_opening_candidate_decisions(candidate_source)
    immutable_input = build_immutable_input(
        account_run_id=account_run_id,
        normalized_account=account,
        broker=broker,
        included_markets=included_markets,
        portfolio_scope_id=authority_resolution.portfolio_scope_id,
        normalized_portfolio_source=normalized_portfolio_source,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
        capacity_pool_authority_id=capacity_pool_authority_id,
        authority_resolution=authority_resolution,
        source_manifest_relpath=(
            "state/position_advice_source_manifest.v2.json"
        ),
        source_manifest=source_manifest,
        decision_state_snapshot=decision_state_snapshot,
        candidate_inputs={
            "candidates": [
                dict(item.get("normalized_input") or {})
                for item in candidate_decisions
            ],
            "candidate_decisions": candidate_decisions,
        },
        economic_inputs={
            "capacity": {
                "cash": cash_source.get("cash_capacity"),
                "shares": coverage_source.get("share_coverage"),
            },
            "fx": fx_source.get("fx"),
            "fees": {"schedule": "futu_option_fee_current"},
            "quote_quality": {
                "quote_snapshot_count": len(payloads.get("quotes", [])),
            },
        },
        built_at=checked_at,
    )
    plan = build_position_advice_plan(
        immutable_input=immutable_input,
        candidate_decisions=candidate_decisions,
        quote_rows=quote_rows_from_source_payloads(
            payloads.get("quotes", [])
        ),
        cash_capacity=dict(cash_source.get("cash_capacity") or {}),
        share_coverage=dict(
            coverage_source.get("share_coverage") or {}
        ),
        fx_payload=dict(fx_source),
        checked_at=checked_at,
    )
    advice = {**plan, "artifact_hash": canonical_sha256(plan)}
    return {"input": immutable_input, "advice": advice}


def _adopt_all_sources(
    *,
    source_receipts: Iterable[Mapping[str, Any]],
    account_root: Path,
    account_run_id: str,
    account: str,
    portfolio_account_identity_hash: str,
    now: str,
) -> list[dict[str, Any]]:
    adopted: list[dict[str, Any]] = []
    account_scoped = {
        "opening_candidates",
        "portfolio",
        "ledger_decision_state",
        "cash_capacity",
        "share_coverage",
    }
    for raw in source_receipts:
        item = dict(raw)
        source_kind = str(item.get("source_kind") or "").strip()
        adopted.append(
            adopt_source_snapshot(
                receipt_path=Path(
                    _required_text(item.get("receipt_path"), "receipt_path")
                ),
                producer_root=Path(
                    _required_text(item.get("producer_root"), "producer_root")
                ),
                consumer_run_root=account_root,
                consumer_account_run_id=account_run_id,
                now=now,
                expected_account=(
                    account if source_kind in account_scoped else None
                ),
                expected_identity_hash=(
                    portfolio_account_identity_hash
                    if source_kind in account_scoped
                    else None
                ),
            )
        )
    if not adopted:
        raise PositionAdviceSourceError("position advice source list is empty")
    return adopted


def _read_source_payloads(
    account_root: Path,
    source_manifest: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for entry in source_manifest.get("source_manifest") or []:
        item = dict(entry)
        path = safe_existing_relative_path(
            account_root,
            item.get("payload_relpath"),
        )
        payload = _read_json_object(path)
        if item.get("source_kind") == "quotes":
            payload["_position_advice_quote_snapshot_id"] = item.get(
                "snapshot_id"
            )
            payload["_position_advice_quote_as_of"] = item.get(
                "source_observed_at"
            )
        output.setdefault(str(item["source_kind"]), []).append(payload)
    return output


def _single_payload(
    payloads: Mapping[str, list[Mapping[str, Any]]],
    kind: str,
) -> dict[str, Any]:
    rows = payloads.get(kind) or []
    if len(rows) != 1:
        raise PositionAdviceRunnerError(
            f"exactly one {kind} source is required"
        )
    return dict(rows[0])


def _non_publish_result(
    *,
    status: str,
    run_id: str,
    account: str,
    resolution: AuthorityResolution,
) -> dict[str, Any]:
    return {
        "schema_version": POSITION_ADVICE_RUNNER_SCHEMA,
        "status": status,
        "account_run_id": run_id,
        "account": account,
        "portfolio_scope_id": resolution.portfolio_scope_id,
        "authority_mode": resolution.mode,
        "reason_codes": list(resolution.reason_codes),
        "current_switched": False,
        "notified": False,
    }


def _assert_account_run_root(
    *,
    account_root: Path,
    run_root: Path,
    account: str,
) -> None:
    if (
        not run_root.exists()
        or not run_root.is_dir()
        or run_root.is_symlink()
    ):
        raise PositionAdviceRunnerError("output run root is invalid")
    expected = run_root / "accounts" / account
    if (
        account_root != expected.resolve()
        or not account_root.exists()
        or not account_root.is_dir()
        or account_root.is_symlink()
    ):
        raise PositionAdviceRunnerError("account run root is invalid")


def _relative_to_run(run_root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=True).relative_to(
            run_root.resolve()
        ).as_posix()
    except ValueError as exc:
        raise PositionAdviceRunnerError(
            "artifact escapes output run"
        ) from exc


def _read_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise PositionAdviceRunnerError("artifact may not be a symlink")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PositionAdviceRunnerError(
            f"artifact is unreadable: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise PositionAdviceRunnerError("artifact must be an object")
    return payload


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _markets(values: Iterable[str]) -> list[str]:
    markets = sorted({str(item or "").strip().upper() for item in values})
    if not markets or any(item not in {"US", "HK"} for item in markets):
        raise ValueError("included_markets are invalid")
    return markets


def _timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "POSITION_ADVICE_RUNNER_SCHEMA",
    "PositionAdviceRunnerError",
    "render_position_advice_csv",
    "render_position_advice_text",
    "run_position_advice_v2",
    "run_position_advice_v2_from_account_run",
]
