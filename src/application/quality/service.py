from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.account_config import accounts_from_config
from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.ledger.api import open_trade_reconciliation_evidence_repo
from src.application.quality.intake_checks import build_trade_intake_datasets
from src.application.quality.ledger_checks import build_ledger_datasets
from src.application.quality.lifecycle_checks import build_lifecycle_datasets, next_trading_day
from src.application.quality.model import (
    POLICY_VERSION,
    SCHEMA_VERSION,
    check_result,
    dataset_status,
    summarize,
    utc_iso,
    validate_payload,
)
from src.application.quality.position_checks import (
    build_opend_runtime_check,
    build_position_dataset,
)
from src.application.quality.paths import (
    default_quality_artifact_path,
    default_quality_control_path,
)
from src.application.quality.runtime_checks import build_runtime_checks, runtime_verdict
from src.application.quality.runtime_status_facade import read_runtime_status
from src.application.runtime_config_freshness import infer_runtime_config_market
from src.infrastructure.quality.artifact_repository import QualityArtifactRepository
from src.infrastructure.quality.control_state_repository import QualityControlStateRepository
from src.infrastructure.quality.opend_position_adapter import (
    OpenDOptionPositionAdapter,
    OpenDOptionSnapshot,
)


_ROOT = Path(__file__).resolve().parents[3]
_SCHEMA = _ROOT / "contracts" / "quality-monitoring" / "quality_status.v1.schema.json"
_VERSION = _ROOT / "VERSION"


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

    def read_published(self) -> dict[str, Any] | None:
        return self.artifact_repository.read()

    def refresh(
        self,
        *,
        config_keys: list[str] | None = None,
        deep: bool = True,
        day_end_strict: bool = False,
    ) -> dict[str, Any]:
        now = self.now_fn().astimezone(timezone.utc)
        observed_at = utc_iso(now)
        configs = self._load_configs(config_keys or ["us", "hk"])
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
                datasets.extend(
                    build_ledger_datasets(
                        repo=repo,
                        accounts=accounts,
                        market=market,
                        observed_at_utc=observed_at,
                    )
                )
            else:
                datasets.extend(
                    self._unavailable_ledger_datasets(
                        accounts=accounts,
                        market=market,
                        observed_at=observed_at,
                    )
                )

            cases = repo.list_trade_lifecycle_cases() if repo is not None else []
            evidence_rows = repo.list_trade_lifecycle_evidence() if repo is not None else []
            local_lots = repo.list_position_lots() if repo is not None else []
            calendar_start = self._calendar_start(cases, now=now)
            for account in accounts:
                snapshot = (
                    self.opend_adapter.fetch(
                        cfg=cfg,
                        account=account,
                        market=market,
                        calendar_start=calendar_start,
                        calendar_end=now.date() + timedelta(days=14),
                    )
                    if deep
                    else self._unavailable_snapshot(
                        account=account,
                        market=market,
                        observed_at=observed_at,
                        reason="DEEP_REFRESH_NOT_REQUESTED",
                    )
                )
                runtime_checks.append(
                    build_opend_runtime_check(
                        snapshot=snapshot,
                        observed_at_utc=observed_at,
                    )
                )
                position_dataset, control_state = build_position_dataset(
                    snapshot=snapshot,
                    local_lots=local_lots,
                    account=account,
                    market=market,
                    observed_at_utc=observed_at,
                    now=now,
                    control_state=control_state,
                    day_end_strict=day_end_strict,
                )
                datasets.append(position_dataset)
                self._record_first_deep_reconcile(
                    control_state=control_state,
                    cases=cases,
                    account=account,
                    snapshot=snapshot,
                    now=now,
                )
                datasets.extend(
                    build_lifecycle_datasets(
                        cases=cases,
                        evidence_rows=evidence_rows,
                        account=account,
                        market=market,
                        observed_at_utc=observed_at,
                        now=now,
                        trading_days=snapshot.trading_days,
                        first_deep_by_case=dict(
                            control_state.get("lifecycle_first_deep_reconcile") or {}
                        ),
                    )
                )
                datasets.append(
                    self._holdings_sync_dataset(
                        runtime_for_config=runtime_for_config,
                        account=account,
                        market=market,
                        observed_at=observed_at,
                    )
                )

        control_state["updated_at_utc"] = observed_at
        self.control_repository.write(control_state)
        runtime_status = runtime_verdict(runtime_checks)
        if runtime_errors and runtime_status == "healthy":
            runtime_status = "unknown"
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
            "datasets": self._deduplicate_datasets(datasets),
            "incidents": [],
            "extensions": {
                "onboarded": self._onboarded(),
                "deep_refresh": deep,
                "day_end_strict": day_end_strict,
            },
        }
        payload["summary"] = summarize(payload)
        validate_payload(payload, schema_path=_SCHEMA)
        self.artifact_repository.write_atomic(payload)
        return payload

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
    def _record_first_deep_reconcile(
        *,
        control_state: dict[str, Any],
        cases: list[dict[str, Any]],
        account: str,
        snapshot: OpenDOptionSnapshot,
        now: datetime,
    ) -> None:
        if not snapshot.complete:
            return
        first_deep = control_state.setdefault("lifecycle_first_deep_reconcile", {})
        for case in cases:
            if str(case.get("account") or "").strip().lower() != account:
                continue
            case_id = str(case.get("case_id") or "").strip()
            status = str(case.get("status") or "").strip().lower()
            try:
                expiration = date.fromisoformat(str(case.get("expiration_ymd") or "")[:10])
            except ValueError:
                continue
            next_day = next_trading_day(expiration, snapshot.trading_days)
            if case_id and status not in {"ledger_written"} and next_day and now.date() >= next_day:
                first_deep.setdefault(case_id, utc_iso(now))

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
    def _holdings_sync_dataset(
        *,
        runtime_for_config: list[dict[str, Any]],
        account: str,
        market: str,
        observed_at: str,
    ) -> dict[str, Any]:
        intents: list[dict[str, Any]] = []
        enabled = False
        for runtime in runtime_for_config:
            intake = runtime.get("trade_intake") if isinstance(runtime.get("trade_intake"), dict) else {}
            for source in intake.get("sources") or []:
                if not isinstance(source, dict):
                    continue
                source_account = str(source.get("account") or "").strip().lower()
                if source_account and source_account != account:
                    continue
                summary = source.get("summary") if isinstance(source.get("summary"), dict) else {}
                intent = summary.get("last_stock_holdings_sync_intent")
                if isinstance(intent, dict):
                    intents.append(intent)
                enabled = enabled or bool(intake.get("holdings_sync", {}).get("enabled"))
        if not enabled and not intents:
            status, reason, message = "pass", "STOCK_REFRESH_INTENT_NOT_APPLICABLE", "PM stock-refresh intent is not enabled for this source."
            verdict = "trusted"
        elif not intents:
            status, reason, message = "unknown", "STOCK_REFRESH_INTENT_EVIDENCE_MISSING", "Stock-refresh intent is enabled but no result evidence is available."
            verdict = "unavailable"
        else:
            latest = intents[-1]
            result_status = str(latest.get("status") or "").strip().lower()
            ok = result_status in {"succeeded", "success", "queued", "debounced", "scheduled"}
            status, reason, message = (
                ("pass", "STOCK_REFRESH_INTENT_CONFIRMED", "Stock-refresh intent has a PM handoff result.")
                if ok
                else ("warn", "STOCK_REFRESH_INTENT_DELAYED", "Stock-refresh intent has not reached a successful PM handoff.")
            )
            verdict = "trusted" if ok else "partial"
        check = check_result(
            check_id="OM-HSYNC-001",
            status=status,
            scope={"account": account, "market": market},
            observed_at_utc=observed_at,
            reason_code=reason,
            message=message,
            observed={"intent_count": len(intents)},
            expected={"latest_intent_result": "successful"},
            evidence_refs=[],
        )
        return dataset_status(
            dataset_id="om.stock_refresh_intent",
            scope={"account": account, "market": market},
            status=verdict,
            as_of_utc=observed_at,
            checks=[check],
            usable_for=["stock_refresh_timeliness"] if verdict == "trusted" else [],
            blocked_consumers=[],
            blocked_by=[],
            reason_codes=[] if status == "pass" else [reason],
        )

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
