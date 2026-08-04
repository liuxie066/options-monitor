from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from domain.domain.symbol_identity import OPTION_CODE_RE, canonical_symbol
from src.application.ledger.api import advance_lifecycle_case_state
from src.application.trades.close_reason_reconciliation import (
    reconcile_due_lifecycle_cases,
    reconcile_lifecycle_close_reason,
)
from src.application.trades.lifecycle_timing import (
    bind_lifecycle_timing_policy,
)
from src.application.trades.settlement_observation import (
    build_settlement_observation_collector,
)


def ensure_lifecycle_timing_after_intake(
    repo: Any,
    *,
    payload: dict[str, Any],
    result: dict[str, Any],
    gateway: Any | None = None,
    quote_gateway: Any | None = None,
    quote_dependency_error: str | None = None,
    now_ms: int,
    apply_changes: bool,
) -> dict[str, Any] | None:
    quote_gateway = quote_gateway or gateway
    adoption = _lifecycle_adoption(result)
    lifecycle_case = (
        dict(adoption.get("lifecycle_case") or {})
        if isinstance(adoption, dict)
        else {}
    )
    case_id = str(lifecycle_case.get("case_id") or "").strip()
    if not case_id:
        return None
    existing = repo.get_trade_lifecycle_timing_policy(case_id)
    if isinstance(existing, dict):
        binding = {
            "schema_version": "lifecycle_timing_binding_result.v1",
            "case_id": case_id,
            "apply_changes": bool(apply_changes),
            "created": False,
            "existing": True,
            "policy": existing,
        }
    else:
        try:
            if quote_gateway is None:
                raise RuntimeError(
                    str(quote_dependency_error or "").strip()
                    or "Futu quote dependency is unavailable"
                )
            contract_metadata = _registry_contract_metadata(
                payload,
                lifecycle_case=lifecycle_case,
            )
            expiration = date.fromisoformat(
                str(lifecycle_case.get("expiration_ymd") or "")
            )
            calendar_start = (
                expiration - timedelta(days=1)
            ).isoformat()
            calendar_end = (
                expiration + timedelta(days=14)
            ).isoformat()
            market = str(
                lifecycle_case.get("market")
                or contract_metadata.get("market")
                or ""
            ).strip().upper()
            calendar_result = (
                quote_gateway.get_trading_days_with_receipt(
                    market=market,
                    start=calendar_start,
                    end=calendar_end,
                )
            )
            if (
                not isinstance(calendar_result, dict)
                or not bool(
                    calendar_result.get("coverage_complete")
                )
                or not bool(
                    calendar_result.get("pagination_complete")
                )
                or not isinstance(
                    calendar_result.get("rows"),
                    list,
                )
            ):
                raise ValueError(
                    "Futu quote trading calendar coverage is incomplete"
                )
            binding = bind_lifecycle_timing_policy(
                repo,
                lifecycle_case={
                    **lifecycle_case,
                    "market": market,
                },
                contract_metadata=contract_metadata,
                trading_days=[
                    dict(item)
                    for item in calendar_result.get("rows") or []
                    if isinstance(item, dict)
                ],
                calendar_source="futu_request_trading_days",
                calendar_observed_at_ms=int(now_ms),
                apply_changes=apply_changes,
            )
        except Exception as exc:
            failure = {
                "schema_version": "lifecycle_timing_binding_result.v1",
                "case_id": case_id,
                "apply_changes": bool(apply_changes),
                "created": False,
                "existing": False,
                "status": "needs_review",
                "reason_codes": [
                    "lifecycle_timing_policy_unavailable"
                ],
                "error": f"{type(exc).__name__}: {exc}",
            }
            if apply_changes:
                failure["write_result"] = (
                    advance_lifecycle_case_state(
                        repo,
                        case_id=case_id,
                        status="needs_review",
                        derived_summary={
                            "reason_state": "needs_review",
                            "close_reason": None,
                            "lifecycle_reason_codes": [
                                "lifecycle_timing_policy_unavailable"
                            ],
                            "timing_error": failure["error"],
                        },
                        public_transition="needs_review",
                    )
                )
            return failure

    if not apply_changes:
        return binding
    case_status = str(
        lifecycle_case.get("status") or ""
    ).strip().lower()
    if case_status in {"ledger_written", "conflict"}:
        return binding
    reconciliation = reconcile_lifecycle_close_reason(
        repo,
        case_id=case_id,
        now_ms=int(now_ms),
        apply_changes=True,
    )
    return {**binding, "reconciliation": reconciliation}


def reconcile_due_lifecycle_cases_for_source(
    repo: Any,
    *,
    source: dict[str, Any],
    gateway: Any | None = None,
    broker_gateway: Any | None = None,
    quote_gateway: Any | None = None,
    quote_dependency_error: str | None = None,
    trd_env: str = "REAL",
    now_ms: int,
    apply_changes: bool,
) -> dict[str, Any]:
    account = str(source.get("account") or "").strip().lower()
    account_ids = [
        str(item or "").strip()
        for item in list(source.get("futu_account_ids") or [])
        if str(item or "").strip()
    ]
    if not account or not account_ids:
        raise ValueError(
            "due lifecycle source requires one account and at least one Futu account id"
        )
    collector = build_settlement_observation_collector(
        repo=repo,
        gateway=gateway,
        broker_gateway=broker_gateway,
        quote_gateway=quote_gateway,
        quote_dependency_error=quote_dependency_error,
        futu_account_ids=account_ids,
        trd_env=trd_env,
        now_ms_fn=lambda: int(now_ms),
    )
    return reconcile_due_lifecycle_cases(
        repo,
        account=account,
        now_ms=int(now_ms),
        apply_changes=apply_changes,
        observation_collector=collector,
    )


def _lifecycle_adoption(
    result: dict[str, Any],
) -> dict[str, Any] | None:
    diagnostics = (
        dict(result.get("diagnostics") or {})
        if isinstance(result.get("diagnostics"), dict)
        else {}
    )
    adoption = diagnostics.get("lifecycle_adoption")
    return dict(adoption) if isinstance(adoption, dict) else None


def _registry_contract_metadata(
    payload: dict[str, Any],
    *,
    lifecycle_case: dict[str, Any],
) -> dict[str, Any]:
    raw_code = str(
        payload.get("code")
        or payload.get("stock_code")
        or payload.get("broker_symbol")
        or ""
    ).strip().upper()
    match = OPTION_CODE_RE.match(raw_code)
    if match is None:
        raise ValueError(
            "standard broker option contract class is unproven"
        )
    market = str(match.group("market") or "").strip().upper()
    broker_symbol = canonical_symbol(raw_code)
    case_symbol = canonical_symbol(lifecycle_case.get("symbol"))
    if not broker_symbol or not case_symbol or broker_symbol != case_symbol:
        raise ValueError(
            "broker option code conflicts with lifecycle contract"
        )
    return {
        "market": market,
        "settlement_style": "physical",
        "underlying_security_type": "equity",
        "contract_class": "standard_equity_option",
    }


__all__ = [
    "ensure_lifecycle_timing_after_intake",
    "reconcile_due_lifecycle_cases_for_source",
]
