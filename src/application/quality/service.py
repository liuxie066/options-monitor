from __future__ import annotations

import os
import sqlite3
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from domain.domain.ledger.position_fields import effective_multiplier
from src.application.account_config import accounts_from_config
from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.ledger.api import (
    lifecycle_account_coherent_facts,
    open_trade_reconciliation_evidence_repo,
    read_current_decision_projection,
)
from src.application.quality.gate import (
    quality_consumer_telemetry_snapshot,
    quality_payload_has_lifecycle_rows,
    record_quality_consumer_read,
)
from src.application.quality.intake_checks import build_trade_intake_datasets
from src.application.quality.cutover import (
    CUTOVER_RECEIPT_SCHEMA,
    read_quality_hot_path_cutover_receipt,
)
from src.application.quality.ledger_checks import (
    build_current_ledger_dataset,
    build_ledger_datasets,
)
from src.application.quality.lifecycle_checks import (
    LIFECYCLE_SUMMARY_DATASET_ID,
    build_current_lifecycle_quality_dataset,
    build_lifecycle_datasets,
    build_lifecycle_quality_migration_summary,
)
from src.application.trades.lifecycle_reconciliation import (
    build_lifecycle_read_models_from_resolved_account,
)
from src.application.quality.model import (
    POLICY_VERSION,
    SCHEMA_VERSION,
    check_result,
    dataset_status,
    freshness,
    sha256_json,
    summarize,
    utc_iso,
    validate_payload,
)
from src.application.quality.position_checks import (
    build_opend_runtime_check,
    build_position_dataset,
    normalize_local_positions,
)
from src.application.quality.paths import (
    default_quality_artifact_path,
    default_quality_control_path,
    default_quality_hot_path_cutover_receipt_path,
    default_quality_integrity_artifact_path,
)
from src.application.quality.runtime_checks import build_runtime_checks, runtime_verdict
from src.application.quality.runtime_status_facade import read_runtime_status
from src.application.runtime_config_freshness import infer_runtime_config_market
from src.infrastructure.quality.artifact_repository import QualityArtifactRepository
from src.infrastructure.quality.control_state_repository import QualityControlStateRepository
from src.application.quality.opend_position_adapter import (
    OpenDOptionPositionAdapter,
    OpenDOptionSnapshot,
)


_ROOT = Path(__file__).resolve().parents[3]
_SCHEMA = _ROOT / "contracts" / "quality-monitoring" / "quality_status.v1.schema.json"
_VERSION = _ROOT / "VERSION"
_MARKET_TIMEZONES = {
    "us": ZoneInfo("America/New_York"),
    "hk": ZoneInfo("Asia/Hong_Kong"),
}
_AUTHORITATIVE_REFRESH_INTERVAL = timedelta(minutes=15)
_AUTHORITATIVE_REFRESH_START = time(hour=8, minute=30)
_DAY_END_REFRESH_TIME = time(hour=16, minute=30)
_DAY_END_GRACE = timedelta(minutes=15)


def _coherent_account_lifecycle_inputs(
    repo: Any,
    *,
    account: str,
    now_ms: int,
) -> dict[str, Any]:
    facts = lifecycle_account_coherent_facts(repo, account=account)
    cases = [
        dict(item)
        for item in facts.get("account_lifecycle_cases") or []
        if isinstance(item, dict)
    ]
    evidence_rows = [
        dict(item)
        for item in facts.get("account_lifecycle_evidence") or []
        if isinstance(item, dict)
    ]
    timing_policies = [
        dict(item)
        for item in facts.get("account_lifecycle_timing_policies") or []
        if isinstance(item, dict)
    ]
    local_lots = [
        dict(item)
        for item in facts.get("account_position_lots") or []
        if isinstance(item, dict)
    ]
    models_by_lot = build_lifecycle_read_models_from_resolved_account(
        cases=cases,
        allocations=[
            dict(item)
            for item in facts.get("account_lifecycle_allocations") or []
            if isinstance(item, dict)
        ],
        timing_policies=timing_policies,
        position_lots=local_lots,
        account_resolution=dict(
            facts.get("account_lifecycle_resolution") or {}
        ),
        void_event_ids=list(facts.get("effective_void_event_ids") or []),
        now_ms=now_ms,
    )
    models_by_case: dict[str, dict[str, Any]] = {}
    for model in models_by_lot.values():
        case_ids = {
            str(item or "").strip()
            for item in model.get("lifecycle_case_ids") or []
            if str(item or "").strip()
        }
        case_id = str(model.get("lifecycle_case_id") or "").strip()
        if case_id:
            case_ids.add(case_id)
        for model_case_id in case_ids:
            models_by_case[model_case_id] = dict(model)
    return {
        "cases": cases,
        "evidence_rows": evidence_rows,
        "timing_policies_by_case": {
            str(item.get("case_id") or "").strip(): item
            for item in timing_policies
            if str(item.get("case_id") or "").strip()
        },
        "local_lots": local_lots,
        "read_models_by_case": models_by_case,
    }


def _current_lifecycle_position_inputs(
    current_projection: dict[str, Any],
) -> dict[str, Any]:
    if current_projection.get("status") != "trusted":
        return {
            "cases": [],
            "local_lots": [],
            "read_models_by_case": {},
            "timing_policies_by_case": {},
            "coherent": False,
        }
    payload = current_projection.get("payload")
    lifecycle = payload.get("lifecycle") if isinstance(payload, dict) else None
    facts = (
        list(lifecycle.get("operational_cases") or [])
        if isinstance(lifecycle, dict)
        else []
    )
    lots = [
        dict(item)
        for item in current_projection.get("position_lots") or []
        if isinstance(item, dict)
    ]
    lots_by_id = {
        str(item.get("record_id") or "").strip(): item
        for item in lots
        if str(item.get("record_id") or "").strip()
    }
    cases: list[dict[str, Any]] = []
    timing: dict[str, dict[str, Any]] = {}
    for raw in facts:
        if not isinstance(raw, dict):
            return {"cases": [], "local_lots": lots, "read_models_by_case": {}, "timing_policies_by_case": {}, "coherent": False}
        fact = dict(raw)
        case_id = str(fact.get("case_id") or "").strip()
        contract = fact.get("contract")
        fact_timing = fact.get("timing")
        targets = fact.get("target_contracts_by_lot")
        if (
            not case_id
            or not isinstance(contract, dict)
            or not isinstance(fact_timing, dict)
            or not isinstance(targets, dict)
        ):
            return {"cases": [], "local_lots": lots, "read_models_by_case": {}, "timing_policies_by_case": {}, "coherent": False}
        multipliers = {
            effective_multiplier((lots_by_id.get(str(lot_id)) or {}).get("fields") or {})
            for lot_id in targets
        }
        multipliers.discard(None)
        if len(multipliers) != 1:
            return {"cases": [], "local_lots": lots, "read_models_by_case": {}, "timing_policies_by_case": {}, "coherent": False}
        cases.append(
            {
                "case_id": case_id,
                "account": fact.get("account"),
                "market": fact.get("market"),
                "status": fact.get("status"),
                "symbol": contract.get("symbol"),
                "option_type": contract.get("option_type"),
                "position_side": contract.get("position_side"),
                "strike": contract.get("strike"),
                "expiration_ymd": contract.get("expiration_ymd"),
                "multiplier": next(iter(multipliers)),
            }
        )
        timing[case_id] = {
            "settlement_deadline_ms": fact_timing.get(
                "settlement_deadline_ms"
            )
        }
    return {
        "cases": cases,
        "local_lots": lots,
        "read_models_by_case": dict(
            current_projection.get("lifecycle_by_case") or {}
        ),
        "timing_policies_by_case": timing,
        "coherent": True,
    }


class OMQualityService:
    def __init__(
        self,
        *,
        artifact_repository: QualityArtifactRepository | None = None,
        control_repository: QualityControlStateRepository | None = None,
        opend_adapter: OpenDOptionPositionAdapter | None = None,
        runtime_status_fn: Callable[[str, dict[str, Any]], dict[str, Any]] = read_runtime_status,
        instance_id: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
        ledger_probe_path: str | Path | None = None,
        integrity_artifact_repository: QualityArtifactRepository | None = None,
        cutover_receipt_path: str | Path | None = None,
    ) -> None:
        self.artifact_repository = artifact_repository or QualityArtifactRepository(
            default_quality_artifact_path()
        )
        self.control_repository = control_repository or QualityControlStateRepository(
            default_quality_control_path()
        )
        self.opend_adapter = opend_adapter or OpenDOptionPositionAdapter()
        self.runtime_status_fn = runtime_status_fn
        self.instance_id = (
            instance_id
            or str(os.environ.get("OM_QUALITY_INSTANCE_ID") or "").strip()
            or "options-monitor-local"
        )
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.integrity_artifact_repository = (
            integrity_artifact_repository
            or QualityArtifactRepository(default_quality_integrity_artifact_path())
        )
        self.cutover_receipt_path = Path(
            cutover_receipt_path
            or default_quality_hot_path_cutover_receipt_path()
        ).expanduser()
        self.ledger_probe_path = Path(
            ledger_probe_path
            or default_quality_artifact_path().parent.parent / "option_positions.sqlite3"
        ).expanduser().resolve()

    def read_published(
        self,
        *,
        consumer: str | None = None,
        account: str | None = None,
        market: str | None = None,
        lifecycle_rows_requested: bool = False,
    ) -> dict[str, Any] | None:
        payload = self.artifact_repository.read()
        record_quality_consumer_read(
            consumer=consumer,
            account=account,
            market=market,
            lifecycle_rows_requested=lifecycle_rows_requested,
            lifecycle_rows_returned=quality_payload_has_lifecycle_rows(
                payload,
                account=account,
                market=market,
            ),
        )
        migration = (
            ((payload or {}).get("extensions") or {}).get(
                "current_decision_migration"
            )
            if str(consumer or "").strip()
            else None
        )
        if isinstance(migration, dict):
            migration["quality_consumer_telemetry"] = (
                quality_consumer_telemetry_snapshot()
            )
        return payload

    def read_integrity_published(self) -> dict[str, Any] | None:
        return self.integrity_artifact_repository.read()

    def refresh(
        self,
        *,
        config_keys: list[str] | None = None,
        deep: bool = True,
        day_end_strict: bool = False,
    ) -> dict[str, Any]:
        return self._refresh(
            config_keys=config_keys,
            deep=deep,
            day_end_strict=day_end_strict,
            force_legacy=False,
            artifact_repository=self.artifact_repository,
        )

    def refresh_integrity(
        self,
        *,
        config_keys: list[str] | None = None,
        deep: bool = True,
        day_end_strict: bool = False,
    ) -> dict[str, Any]:
        return self._refresh(
            config_keys=config_keys,
            deep=deep,
            day_end_strict=day_end_strict,
            force_legacy=True,
            artifact_repository=self.integrity_artifact_repository,
        )

    def _refresh(
        self,
        *,
        config_keys: list[str] | None,
        deep: bool,
        day_end_strict: bool,
        force_legacy: bool,
        artifact_repository: QualityArtifactRepository,
    ) -> dict[str, Any]:
        now = self.now_fn().astimezone(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        observed_at = utc_iso(now)
        cutover = (
            {
                "schema_version": CUTOVER_RECEIPT_SCHEMA,
                "status": "inactive",
                "reason": "integrity_refresh",
            }
            if force_legacy
            else read_quality_hot_path_cutover_receipt(
                self.cutover_receipt_path
            )
        )
        current_only = cutover.get("status") == "active"
        previous_payload = self._previous_payload(artifact_repository)
        configs = self._load_configs(config_keys or ["us", "hk"])
        requested_markets = {market for _key, _path, _cfg, market in configs}
        previous_cutover_active = (
            (((previous_payload or {}).get("extensions") or {}).get("quality_hot_path_cutover") or {}).get("status")
            == "active"
        )
        if (
            current_only
            and not previous_cutover_active
            and requested_markets != {"us", "hk"}
        ):
            raise ValueError(
                "first current-only quality refresh must publish both markets"
            )
        previous_positions = self._position_dataset_index(previous_payload)
        runtime_statuses: list[dict[str, Any]] = []
        runtime_errors: list[dict[str, str]] = []
        for key, _path, _cfg, _market in configs:
            response = self.runtime_status_fn(
                "runtime_status",
                {
                    "config_key": key,
                    "include_service_status": True,
                },
            )
            data = response.get("data") if isinstance(response, dict) and isinstance(response.get("data"), dict) else None
            if data is None:
                runtime_errors.append(
                    {
                        "config_key": key,
                        "reason": str((response or {}).get("error") or "runtime_status unavailable"),
                    }
                )
                continue
            runtime_statuses.append(data)

        runtime_checks = build_runtime_checks(
            runtime_statuses=runtime_statuses,
            observed_at_utc=observed_at,
            now=now,
        )
        datasets: list[dict[str, Any]] = []
        control_state = self.control_repository.read()
        ledger_cache: dict[Path, Any] = {}
        lifecycle_account_cache: dict[
            tuple[Path, str], tuple[dict[str, Any] | None, str | None]
        ] = {}
        authoritative_refresh_scopes: list[dict[str, str]] = []
        migration_comparisons: list[dict[str, Any]] = []

        for key, _path, cfg, market in configs:
            accounts = accounts_from_config(cfg, fallback=())
            runtime_for_config = [
                item
                for item in runtime_statuses
                if str((item.get("config") or {}).get("config_key") or "").lower() == key
            ]
            datasets.extend(
                build_trade_intake_datasets(
                    runtime_statuses=runtime_for_config,
                    accounts=accounts,
                    market=market,
                    repo_root=repo_base(),
                    observed_at_utc=observed_at,
                    now=now,
                )
            )
            ledger_path = self._ledger_path(runtime_for_config)
            repo = None
            if ledger_path and ledger_path.exists():
                repo = ledger_cache.setdefault(
                    ledger_path,
                    open_trade_reconciliation_evidence_repo(ledger_path),
                )
                if not current_only:
                    datasets.extend(
                        build_ledger_datasets(
                            repo=repo,
                            accounts=accounts,
                            market=market,
                            observed_at_utc=observed_at,
                        )
                    )
            elif not current_only:
                datasets.extend(
                    self._unavailable_ledger_datasets(
                        accounts=accounts,
                        market=market,
                        observed_at=observed_at,
                    )
                )

            cases = (
                repo.list_trade_lifecycle_cases()
                if repo is not None and not current_only
                else []
            )
            evidence_rows = (
                repo.list_trade_lifecycle_evidence()
                if repo is not None and not current_only
                else []
            )
            timing_policies_by_case = (
                {
                    str(item.get("case_id") or ""): dict(item)
                    for item in (
                        repo.list_trade_lifecycle_timing_policies()
                    )
                    if isinstance(item, dict)
                    and str(item.get("case_id") or "").strip()
                }
                if repo is not None and not current_only
                else {}
            )
            local_lots = (
                repo.list_position_lots()
                if repo is not None and not current_only
                else []
            )
            calendar_start = self._calendar_start(cases, now=now)
            for account in accounts:
                current_projection: dict[str, Any] = {
                    "status": "absent",
                    "reason": "ledger_unavailable",
                }
                if current_only:
                    if repo is not None:
                        current_projection = read_current_decision_projection(
                            repo,
                            account=account,
                            now_ms=now_ms,
                        )
                    datasets.append(
                        build_current_ledger_dataset(
                            current_projection=current_projection,
                            account=account,
                            market=market,
                            observed_at_utc=observed_at,
                        )
                    )
                    account_inputs = _current_lifecycle_position_inputs(
                        current_projection
                    )
                    account_cases = list(account_inputs["cases"])
                    account_evidence_rows: list[dict[str, Any]] = []
                    account_timing_policies_by_case = dict(
                        account_inputs["timing_policies_by_case"]
                    )
                    account_local_lots = list(account_inputs["local_lots"])
                    account_read_models_by_case = dict(
                        account_inputs["read_models_by_case"]
                    )
                    lifecycle_coherent_read_available = bool(
                        account_inputs["coherent"]
                    )
                else:
                    account_cases = [
                        dict(item)
                        for item in cases
                        if str(item.get("account") or "").strip().lower()
                        == account
                    ]
                    account_evidence_rows = [
                        dict(item)
                        for item in evidence_rows
                        if str(item.get("account") or "").strip().lower()
                        == account
                    ]
                    account_case_ids = {
                        str(item.get("case_id") or "").strip()
                        for item in account_cases
                        if str(item.get("case_id") or "").strip()
                    }
                    account_timing_policies_by_case = {
                        case_id: dict(item)
                        for case_id, item in timing_policies_by_case.items()
                        if case_id in account_case_ids
                    }
                    account_local_lots = local_lots
                    account_read_models_by_case: dict[str, dict[str, Any]] = {}
                    lifecycle_coherent_read_available = repo is not None
                if (
                    not current_only
                    and repo is not None
                    and ledger_path is not None
                ):
                    cache_key = (ledger_path, account)
                    if cache_key not in lifecycle_account_cache:
                        try:
                            lifecycle_account_cache[cache_key] = (
                                _coherent_account_lifecycle_inputs(
                                    repo,
                                    account=account,
                                    now_ms=now_ms,
                                ),
                                None,
                            )
                        except (
                            sqlite3.Error,
                            TypeError,
                            ValueError,
                            OverflowError,
                        ) as exc:
                            lifecycle_account_cache[cache_key] = (
                                None,
                                str(exc),
                            )
                    account_inputs, lifecycle_read_error = (
                        lifecycle_account_cache[cache_key]
                    )
                    if account_inputs is None:
                        lifecycle_coherent_read_available = False
                        runtime_errors.append(
                            {
                                "config_key": key,
                                "account": account,
                                "reason": (
                                    lifecycle_read_error
                                    or "coherent lifecycle account read unavailable"
                                ),
                            }
                        )
                    else:
                        account_cases = list(account_inputs["cases"])
                        account_evidence_rows = list(
                            account_inputs["evidence_rows"]
                        )
                        account_timing_policies_by_case = dict(
                            account_inputs["timing_policies_by_case"]
                        )
                        account_local_lots = list(
                            account_inputs["local_lots"]
                        )
                        account_read_models_by_case = dict(
                            account_inputs["read_models_by_case"]
                        )
                previous_position = previous_positions.get((account, market))
                refresh_authoritative = bool(
                    deep
                    or day_end_strict
                    or self._authoritative_refresh_required(
                        previous=previous_position,
                        local_lots=account_local_lots,
                        account=account,
                        market=market,
                        now=now,
                        control_state=control_state,
                    )
                )
                if refresh_authoritative:
                    snapshot = self.opend_adapter.fetch(
                        cfg=cfg,
                        account=account,
                        market=market,
                        calendar_start=calendar_start,
                        calendar_end=now.date() + timedelta(days=14),
                    )
                    authoritative_refresh_scopes.append(
                        {"account": account, "market": market}
                    )
                    if snapshot.complete and snapshot.trading_days:
                        self._store_trading_days(
                            control_state=control_state,
                            market=market,
                            trading_days=snapshot.trading_days,
                        )
                    next_due = (
                        self._next_authoritative_refresh_due(
                            now=now,
                            market=market,
                            trading_days=snapshot.trading_days,
                        )
                        if snapshot.complete
                        else None
                    )
                    runtime_checks.append(
                        build_opend_runtime_check(
                            snapshot=snapshot,
                            observed_at_utc=observed_at,
                        )
                    )
                    position_dataset, control_state = build_position_dataset(
                        snapshot=snapshot,
                        local_lots=account_local_lots,
                        account=account,
                        market=market,
                        observed_at_utc=observed_at,
                        now=now,
                        control_state=control_state,
                        lifecycle_cases=account_cases,
                        lifecycle_read_models_by_case=(
                            account_read_models_by_case
                        ),
                        lifecycle_timing_policies_by_case=(
                            account_timing_policies_by_case
                        ),
                        lifecycle_coherent_read_available=(
                            lifecycle_coherent_read_available
                        ),
                        day_end_strict=day_end_strict,
                        next_authoritative_refresh_due_utc=(
                            utc_iso(next_due) if next_due is not None else None
                        ),
                    )
                    trading_days = snapshot.trading_days
                else:
                    position_dataset = self._carry_position_dataset(
                        previous=previous_position,
                        now=now,
                    )
                    runtime_checks.append(
                        self._carried_opend_runtime_check(
                            dataset=position_dataset,
                            account=account,
                            market=market,
                        )
                    )
                    trading_days = self._stored_trading_days(
                        control_state=control_state,
                        market=market,
                    )
                datasets.append(position_dataset)
                if current_only:
                    datasets.append(
                        build_current_lifecycle_quality_dataset(
                            current_quality=dict(
                                current_projection.get("lifecycle_quality") or {}
                            ),
                            projection_status=str(
                                current_projection.get("status") or "absent"
                            ),
                            projection_reason=str(
                                current_projection.get("reason") or ""
                            )
                            or None,
                            account=account,
                            market=market,
                            observed_at_utc=observed_at,
                        )
                    )
                    migration_comparisons.append(
                        {
                            "account": account,
                            "market": market,
                            "status": "cutover_active",
                            "current_projection_status": str(
                                current_projection.get("status") or "absent"
                            ),
                        }
                    )
                    continue
                legacy_lifecycle_datasets = build_lifecycle_datasets(
                    cases=account_cases,
                    evidence_rows=account_evidence_rows,
                    account=account,
                    market=market,
                    observed_at_utc=observed_at,
                    now=now,
                    trading_days=trading_days,
                    first_deep_by_case=dict(
                        control_state.get("lifecycle_first_deep_reconcile")
                        or {}
                    ),
                    timing_policies_by_case=(
                        account_timing_policies_by_case
                    ),
                    read_models_by_case=account_read_models_by_case,
                )
                datasets.extend(legacy_lifecycle_datasets)
                migration = {
                    "account": account,
                    "market": market,
                    "status": "not_available",
                    "current_projection_status": "absent",
                }
                if repo is not None:
                    try:
                        current_projection = read_current_decision_projection(
                            repo,
                            account=account,
                            now_ms=now_ms,
                        )
                        migration["current_projection_status"] = str(
                            current_projection.get("status")
                            or "data_unavailable"
                        )
                        if current_projection.get("status") == "trusted":
                            summary_dataset, comparison = (
                                build_lifecycle_quality_migration_summary(
                                    legacy_datasets=legacy_lifecycle_datasets,
                                    current_quality=dict(
                                        current_projection.get(
                                            "lifecycle_quality"
                                        )
                                        or {}
                                    ),
                                    account=account,
                                    market=market,
                                    observed_at_utc=observed_at,
                                    now_ms=now_ms,
                                    case_status_by_id={
                                        str(item.get("case_id") or "").strip(): str(
                                            item.get("status") or ""
                                        ).strip().lower()
                                        for item in account_cases
                                        if str(item.get("case_id") or "").strip()
                                    },
                                    read_models_by_case=(
                                        account_read_models_by_case
                                    ),
                                )
                            )
                            datasets.append(summary_dataset)
                            migration.update(
                                status=comparison["status"],
                                comparison=comparison,
                            )
                    except Exception as exc:
                        migration.update(
                            status="error",
                            reason=(
                                "quality_shadow_failed:"
                                f"{type(exc).__name__}"
                            ),
                        )
                migration_comparisons.append(migration)

        self._preserve_unrequested_markets(
            previous_payload=previous_payload,
            requested_markets=requested_markets,
            datasets=datasets,
            runtime_checks=runtime_checks,
            current_only=current_only,
        )
        control_state["updated_at_utc"] = observed_at
        control_state["last_probe_ledger_revision"] = self._ledger_revision()
        self.control_repository.write(control_state)
        runtime_status = runtime_verdict(runtime_checks)
        if runtime_errors and runtime_status == "healthy":
            runtime_status = "unknown"
        published_datasets = self._deduplicate_datasets(datasets)
        telemetry = quality_consumer_telemetry_snapshot()
        declared_lifecycle_read = any(
            item["legacy_rows_requested"]
            and item["consumer"] != "unexplained"
            for item in telemetry["entries"]
        )
        published_scopes = {
            (
                str((item.get("scope") or {}).get("account") or "").lower(),
                str((item.get("scope") or {}).get("market") or "").lower(),
            )
            for item in published_datasets
            if (item.get("scope") or {}).get("account")
            and (item.get("scope") or {}).get("market")
        }
        matched_scopes = {
            (str(item["account"]), str(item["market"]))
            for item in migration_comparisons
            if item["status"] == "matched"
        }
        migration_ready = (
            not current_only
            and
            bool(migration_comparisons)
            and all(
                item["status"] == "matched"
                for item in migration_comparisons
            )
            and published_scopes <= matched_scopes
            and telemetry["coverage_status"] == "observed"
            and declared_lifecycle_read
        )
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "producer": {
                "service": "options-monitor",
                "producer_version": _VERSION.read_text(encoding="utf-8").strip(),
                "policy_version": POLICY_VERSION,
                "instance_id": self.instance_id,
                "policy_summary": {
                    "regular_scan_minutes": 15,
                    "position_recheck_minutes": 5,
                    "position_first_recheck_minutes": 1,
                    "lifecycle_grace_hours_after_first_deep_reconcile": 2,
                },
            },
            "observed_at_utc": observed_at,
            "runtime": {
                "status": runtime_status,
                "as_of_utc": observed_at,
                "checks": runtime_checks,
                "extensions": {"runtime_status_errors": runtime_errors},
            },
            "datasets": published_datasets,
            "incidents": [],
            "extensions": {
                "onboarded": self._onboarded(),
                "deep_refresh": bool(authoritative_refresh_scopes),
                "deep_refresh_requested": deep,
                "day_end_strict": day_end_strict,
                "authoritative_refresh_scopes": authoritative_refresh_scopes,
                "quality_hot_path_cutover": cutover,
                "integrity_refresh": force_legacy,
                "current_decision_migration": {
                    "schema_version": "current_decision_migration.v1",
                    "status": (
                        "cutover_active"
                        if current_only
                        else "shadow_ready"
                        if migration_ready
                        else "not_ready"
                    ),
                    "comparisons": migration_comparisons,
                    "quality_consumer_telemetry": telemetry,
                },
            },
        }
        payload["summary"] = summarize(
            {
                **payload,
                "datasets": [
                    item
                    for item in published_datasets
                    if current_only
                    or item.get("dataset_id")
                    != LIFECYCLE_SUMMARY_DATASET_ID
                ],
            }
        )
        validate_payload(payload, schema_path=_SCHEMA)
        artifact_repository.write_atomic(payload)
        return payload

    def refresh_if_due(
        self,
        *,
        config_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run the normal refresh only when a cheap control-plane probe is due."""
        now = self.now_fn().astimezone(timezone.utc)
        control_state = self.control_repository.read()
        mismatch_due = any(
            self._utc_at_or_before(
                (item if isinstance(item, dict) else {}).get("next_recheck_at_utc"),
                now=now,
            )
            for item in (control_state.get("position_mismatches") or {}).values()
        )
        ledger_revision = self._ledger_revision()
        ledger_changed = bool(
            ledger_revision is not None
            and ledger_revision != control_state.get("last_probe_ledger_revision")
        )
        if not mismatch_due and not ledger_changed:
            return {
                "schema_version": "om.quality_recheck_result.v1",
                "status": "not_due",
                "checked_at_utc": utc_iso(now),
                "mismatch_due": False,
                "ledger_changed": False,
            }
        return self.refresh(
            config_keys=config_keys,
            deep=False,
            day_end_strict=False,
        )

    @staticmethod
    def _previous_payload(
        artifact_repository: QualityArtifactRepository,
    ) -> dict[str, Any] | None:
        payload = artifact_repository.read()
        producer = payload.get("producer") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != SCHEMA_VERSION
            or not isinstance(producer, dict)
            or producer.get("service") != "options-monitor"
        ):
            return None
        return payload

    @staticmethod
    def _position_dataset_index(
        payload: dict[str, Any] | None,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for item in (payload or {}).get("datasets") or []:
            if not isinstance(item, dict) or item.get("dataset_id") != "om.option_positions":
                continue
            scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
            account = str(scope.get("account") or "").strip().lower()
            market = str(scope.get("market") or "").strip().lower()
            if account and market:
                out[(account, market)] = item
        return out

    @staticmethod
    def _local_position_fingerprint(
        *,
        local_lots: list[dict[str, Any]],
        account: str,
    ) -> str | None:
        normalized, errors = normalize_local_positions(local_lots, account=account)
        if errors:
            return None
        return sha256_json(
            {key: format(value, "f") for key, value in sorted(normalized.items())}
        )

    def _authoritative_refresh_required(
        self,
        *,
        previous: dict[str, Any] | None,
        local_lots: list[dict[str, Any]],
        account: str,
        market: str,
        now: datetime,
        control_state: dict[str, Any],
    ) -> bool:
        if previous is None or previous.get("status") == "unavailable":
            return True
        snapshots = [
            item
            for item in previous.get("source_snapshots") or []
            if isinstance(item, dict)
        ]
        if not snapshots:
            return True
        source = snapshots[0]
        if not (
            source.get("complete") is True
            and source.get("refresh_cache") is True
            and str(source.get("environment") or "").upper() == "REAL"
        ):
            return True
        extensions = (
            previous.get("extensions")
            if isinstance(previous.get("extensions"), dict)
            else {}
        )
        current_fingerprint = self._local_position_fingerprint(
            local_lots=local_lots,
            account=account,
        )
        if (
            current_fingerprint is None
            or current_fingerprint
            != str(extensions.get("local_position_fingerprint") or "")
        ):
            return True
        next_due = self._parse_utc(
            extensions.get("next_authoritative_refresh_due_utc")
        )
        if next_due is None or next_due <= now.astimezone(timezone.utc):
            return True
        mismatch = (control_state.get("position_mismatches") or {}).get(
            f"{market}:{account}"
        )
        return bool(
            isinstance(mismatch, dict)
            and self._utc_at_or_before(
                mismatch.get("next_recheck_at_utc"),
                now=now,
            )
        )

    @staticmethod
    def _carry_position_dataset(
        *,
        previous: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        if previous is None:
            raise ValueError("previous option-position evidence is required")
        carried = deepcopy(previous)
        extensions = (
            carried.setdefault("extensions", {})
            if isinstance(carried.get("extensions"), dict)
            else {}
        )
        if extensions is not carried.get("extensions"):
            carried["extensions"] = extensions
        extensions["carried_forward"] = True
        snapshots = [
            item
            for item in carried.get("source_snapshots") or []
            if isinstance(item, dict)
        ]
        observed_at = str(
            (snapshots[0] if snapshots else {}).get("observed_at_utc")
            or carried.get("as_of_utc")
            or utc_iso(now)
        )
        observed = OMQualityService._parse_utc(observed_at) or now
        due = OMQualityService._parse_utc(
            extensions.get("next_authoritative_refresh_due_utc")
        )
        carried["freshness"] = freshness(
            observed_at_utc=observed_at,
            status="fresh",
            age_seconds=max(0.0, (now - observed).total_seconds()),
            grace_seconds=(
                max(0.0, (due - observed).total_seconds())
                if due is not None
                else None
            ),
            expected_by_utc=utc_iso(due) if due is not None else None,
        )
        return carried

    @staticmethod
    def _carried_opend_runtime_check(
        *,
        dataset: dict[str, Any],
        account: str,
        market: str,
    ) -> dict[str, Any]:
        snapshots = [
            item
            for item in dataset.get("source_snapshots") or []
            if isinstance(item, dict)
        ]
        source = snapshots[0] if snapshots else {}
        extensions = (
            dataset.get("extensions")
            if isinstance(dataset.get("extensions"), dict)
            else {}
        )
        return check_result(
            check_id="RT-OM-004",
            status="pass",
            scope={
                "account": account,
                "market": market,
                "source": "futu-opend",
            },
            observed_at_utc=str(
                source.get("observed_at_utc")
                or dataset.get("as_of_utc")
            ),
            reason_code="OPEND_AUTHORITATIVE_EVIDENCE_CURRENT",
            message=(
                "The last authoritative OpenD read remains current; "
                "no fixed-interval broker query was required."
            ),
            observed={
                "complete": source.get("complete") is True,
                "refresh_cache": source.get("refresh_cache") is True,
                "environment": source.get("environment"),
                "carried_forward": True,
            },
            expected={
                "complete": True,
                "refresh_cache": True,
                "environment": "REAL",
            },
            thresholds={
                "next_authoritative_refresh_due_utc": str(
                    extensions.get("next_authoritative_refresh_due_utc") or ""
                ),
            },
            evidence_refs=[],
        )

    @staticmethod
    def _next_authoritative_refresh_due(
        *,
        now: datetime,
        market: str,
        trading_days: list[date],
    ) -> datetime:
        zone = _MARKET_TIMEZONES.get(market, timezone.utc)
        local_now = now.astimezone(zone)
        candidates = sorted(set(trading_days))
        for trading_day in candidates:
            if trading_day < local_now.date():
                continue
            window_start = datetime.combine(
                trading_day,
                _AUTHORITATIVE_REFRESH_START,
                tzinfo=zone,
            )
            window_end = (
                datetime.combine(
                    trading_day,
                    _DAY_END_REFRESH_TIME,
                    tzinfo=zone,
                )
                + _DAY_END_GRACE
            )
            if local_now < window_start:
                return window_start.astimezone(timezone.utc)
            if local_now < window_end:
                return min(
                    local_now + _AUTHORITATIVE_REFRESH_INTERVAL,
                    window_end,
                ).astimezone(timezone.utc)
        fallback_day = local_now.date()
        for _ in range(8):
            fallback_day += timedelta(days=1)
            if fallback_day.weekday() < 5:
                return datetime.combine(
                    fallback_day,
                    _AUTHORITATIVE_REFRESH_START,
                    tzinfo=zone,
                ).astimezone(timezone.utc)
        return now + timedelta(days=1)

    @staticmethod
    def _store_trading_days(
        *,
        control_state: dict[str, Any],
        market: str,
        trading_days: list[date],
    ) -> None:
        calendars = control_state.setdefault("trading_days_by_market", {})
        calendars[market] = [
            item.isoformat()
            for item in sorted(set(trading_days))
        ]

    @staticmethod
    def _stored_trading_days(
        *,
        control_state: dict[str, Any],
        market: str,
    ) -> list[date]:
        out = []
        for raw in (control_state.get("trading_days_by_market") or {}).get(
            market,
            [],
        ):
            try:
                out.append(date.fromisoformat(str(raw)))
            except ValueError:
                continue
        return out

    @staticmethod
    def _preserve_unrequested_markets(
        *,
        previous_payload: dict[str, Any] | None,
        requested_markets: set[str],
        datasets: list[dict[str, Any]],
        runtime_checks: list[dict[str, Any]],
        current_only: bool = False,
    ) -> None:
        if previous_payload is None:
            return
        for item in previous_payload.get("datasets") or []:
            if not isinstance(item, dict):
                continue
            if current_only and item.get("dataset_id") in {
                "om.lifecycle_evidence",
                "om.lifecycle_history",
            }:
                continue
            scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
            market = str(scope.get("market") or "").strip().lower()
            if market and market not in requested_markets:
                datasets.append(deepcopy(item))
        previous_runtime = (
            previous_payload.get("runtime")
            if isinstance(previous_payload.get("runtime"), dict)
            else {}
        )
        for item in previous_runtime.get("checks") or []:
            if not isinstance(item, dict):
                continue
            scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
            market = str(scope.get("market") or "").strip().lower()
            if market and market not in requested_markets:
                runtime_checks.append(deepcopy(item))

    def _ledger_revision(self) -> str | None:
        try:
            connection = sqlite3.connect(
                f"file:{self.ledger_probe_path}?mode=ro",
                uri=True,
                timeout=1,
            )
            try:
                rows = connection.execute(
                    """
                    SELECT record_id, updated_at_ms
                    FROM position_lots
                    ORDER BY record_id
                    """
                ).fetchall()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            return None
        return sha256_json(rows)

    @staticmethod
    def _parse_utc(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(
                str(value or "").replace("Z", "+00:00")
            )
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _utc_at_or_before(
        cls,
        value: Any,
        *,
        now: datetime,
    ) -> bool:
        parsed = cls._parse_utc(value)
        return parsed is not None and parsed <= now.astimezone(timezone.utc)

    def _load_configs(
        self,
        keys: list[str],
    ) -> list[tuple[str, Path, dict[str, Any], str]]:
        out: list[tuple[str, Path, dict[str, Any], str]] = []
        for raw in keys:
            key = str(raw or "").strip().lower()
            try:
                path, cfg = load_runtime_config(config_key=key)
            except Exception:
                continue
            market = str(
                infer_runtime_config_market(
                    config_path=path,
                    config=cfg,
                )
                or key
            ).strip().lower()
            out.append((key, path, cfg, market))
        if not out:
            raise ValueError("no valid OM runtime config is available for quality refresh")
        return out

    @staticmethod
    def _ledger_path(runtime_statuses: list[dict[str, Any]]) -> Path | None:
        for item in runtime_statuses:
            ledger = item.get("ledger_store") if isinstance(item.get("ledger_store"), dict) else {}
            raw = str(ledger.get("sqlite_path") or "").strip()
            if raw:
                return Path(raw).expanduser().resolve()
        return None

    @staticmethod
    def _calendar_start(cases: list[dict[str, Any]], *, now: datetime) -> date:
        recent_floor = now.date() - timedelta(days=45)
        expirations: list[date] = []
        for item in cases:
            raw = str(item.get("expiration_ymd") or "")[:10]
            if raw < recent_floor.isoformat():
                continue
            try:
                expirations.append(date.fromisoformat(raw))
            except ValueError:
                continue
        return min(expirations, default=recent_floor)

    @staticmethod
    def _unavailable_snapshot(
        *,
        account: str,
        market: str,
        observed_at: str,
        reason: str,
    ) -> OpenDOptionSnapshot:
        return OpenDOptionSnapshot(
            account=account,
            market=market,
            environment="UNKNOWN",
            account_fingerprint="sha256:" + ("0" * 64),
            observed_at_utc=observed_at,
            snapshot_id=f"opend-unavailable-{account}",
            complete=False,
            refresh_cache=True,
            rows=[],
            trading_days=[],
            error_code=reason,
            error_message=reason,
        )

    @staticmethod
    def _unavailable_ledger_datasets(
        *,
        accounts: list[str],
        market: str,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        out = []
        for account in accounts:
            checks = [
                check_result(
                    check_id=check_id,
                    status="unknown",
                    scope={"account": account, "market": market},
                    observed_at_utc=observed_at,
                    reason_code="LEDGER_EVIDENCE_UNAVAILABLE",
                    message="Canonical ledger evidence is unavailable.",
                    evidence_refs=[],
                )
                for check_id in ("OM-LED-001", "OM-LED-002")
            ]
            out.append(
                dataset_status(
                    dataset_id="om.ledger_projection",
                    scope={"account": account, "market": market},
                    status="unavailable",
                    as_of_utc=observed_at,
                    checks=checks,
                    blocked_consumers=["option_position_report", "lifecycle", "close_advice"],
                    blocked_by=["OM-LED-001", "OM-LED-002"],
                    reason_codes=["LEDGER_EVIDENCE_UNAVAILABLE"],
                )
            )
        return out

    @staticmethod
    def _deduplicate_datasets(datasets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for item in datasets:
            scope_key = "|".join(
                f"{key}={value}" for key, value in sorted((item.get("scope") or {}).items())
            )
            out[(str(item.get("dataset_id") or ""), scope_key)] = item
        return list(out.values())

    @staticmethod
    def _onboarded() -> bool:
        return str(os.environ.get("OM_QUALITY_ONBOARDED") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }


__all__ = [
    "OMQualityService",
    "default_quality_artifact_path",
    "default_quality_control_path",
]
