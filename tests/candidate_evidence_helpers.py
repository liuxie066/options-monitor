from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from domain.domain.engine import (
    EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
    EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
    build_candidate_decision,
    build_candidate_reject,
)
from src.application.candidate_snapshot_manifest import (
    publish_candidate_snapshot_manifest,
)
from src.application.opening_candidate_snapshot import (
    dependency_from_hash,
    seal_opening_candidate_snapshot,
)
from src.application.strategy_scan_status import (
    publish_strategy_scan_status,
    publish_strategy_scan_status_index_v2,
)


CONFIG_HASH = "a" * 64
POLICY_HASH = "b" * 64


def earnings_evidence(
    *, expiration: str, market_date: str, event_date: str | None = None
) -> dict[str, Any]:
    expiration_day = date.fromisoformat(expiration)
    events = []
    if event_date is not None:
        days_before_expiration = (expiration_day - date.fromisoformat(event_date)).days
        blocking = days_before_expiration <= EARNINGS_NEAR_EXPIRY_WINDOW_DAYS
        events.append(
            {
                "earnings_date": event_date,
                "days_before_expiration": days_before_expiration,
                "classification": "blocking" if blocking else "nonblocking",
                "blocking": blocking,
            }
        )
    blocking_events = [event for event in events if event["blocking"]]
    return {
        "earnings_evidence_status": "ready",
        "earnings_reason_code": None,
        "earnings_policy_version": EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
        "earnings_window_days": EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
        "earnings_market_date": market_date,
        "earnings_hard_window_start": (
            expiration_day - timedelta(days=EARNINGS_NEAR_EXPIRY_WINDOW_DAYS)
        ).isoformat(),
        "earnings_hard_window_end": expiration,
        "earnings_hard_coverage_status": "complete",
        "earnings_soft_coverage_status": "complete",
        "earnings_has_event": bool(events),
        "earnings_blocking_has_event": bool(blocking_events),
        "earnings_events": events,
        "earnings_blocking_events": blocking_events,
        "earnings_nonblocking_events": [event for event in events if not event["blocking"]],
    }




def seal_opening_candidate_fixture(
    base: Path,
    *,
    run_id: str,
    account: str = "lx",
    market: str = "US",
    accepted_rows: Iterable[Mapping[str, Any]] = (),
    rejected_rows: Iterable[Mapping[str, Any]] = (),
    sealed_at: str = "2026-06-01T00:00:00Z",
    manifest_sealed_at: str = "2026-06-01T00:00:01Z",
) -> dict[str, Any]:
    """Publish a current manifest-bound opening snapshot for test facts."""

    accepted = [_normalized_row(row, accepted=True) for row in accepted_rows]
    rejected = [_normalized_row(row, accepted=False) for row in rejected_rows]
    rows = [*accepted, *rejected]
    modes = sorted({_mode(row) for row in rows}) or ["put"]
    symbols_by_mode = {
        mode: sorted({str(row["symbol"]).upper() for row in rows if _mode(row) == mode})
        for mode in modes
    }
    if not rows:
        symbols_by_mode = {"put": ["NVDA"]}

    account_dir = base / "output_runs" / run_id / "accounts" / account
    account_dir.mkdir(parents=True, exist_ok=True)
    scan_statuses: list[dict[str, Any]] = []
    expected: list[dict[str, str]] = []
    for mode, symbols in symbols_by_mode.items():
        family = "sell_put" if mode == "put" else "covered_call"
        for symbol in symbols:
            candidate_count = sum(
                _mode(row) == mode and str(row["symbol"]).upper() == symbol
                for row in accepted
            )
            publish_strategy_scan_status(
                report_dir=account_dir,
                run_id=run_id,
                account=account,
                market=market,
                symbol=symbol,
                strategy_family=family,
                status="completed",
                candidate_count=candidate_count,
                reason="no_candidate" if candidate_count == 0 else None,
                snapshot_id=f"fixture-{symbol}-{mode}",
                receipt_relpath=f"quotes/{symbol}/{mode}/receipt.json",
            )
            scan_statuses.append(
                {
                    "symbol": symbol,
                    "strategy_mode": mode,
                    "status": "completed",
                    "reason": "no_candidate" if candidate_count == 0 else None,
                    "quote_snapshot_id": f"fixture-{symbol}-{mode}",
                    "quote_receipt_relpath": f"quotes/{symbol}/{mode}/receipt.json",
                }
            )
            expected.append(
                {
                    "market": market.upper(),
                    "symbol": symbol,
                    "strategy_family": family,
                    "strategy_mode": mode,
                    "candidate_owner": "opening",
                    "account_config_sha256": CONFIG_HASH,
                }
            )

    final_candidates = {
        mode: [row for row in accepted if _mode(row) == mode]
        for mode in modes
    }
    evaluations: dict[str, list[dict[str, Any]]] = {mode: [] for mode in modes}
    for row in accepted:
        evaluations[_mode(row)].append(
            {
                "normalized_input": row,
                "opening_decision": build_candidate_decision(
                    mode=_mode(row),
                    symbol=str(row["symbol"]),
                    contract_symbol=str(row["contract_symbol"]),
                    accepted=True,
                    normalized_input=row,
                ),
            }
        )
    for row in rejected:
        reject = build_candidate_reject(
            stage=str(row.get("stage") or "stage3_risk_filter"),
            reason=str(row.get("rule") or "risk_spread"),
            message=str(row.get("message") or "fixture rejection"),
            metric_value=row.get("metric_value", row.get("spread_ratio")),
            threshold=row.get("threshold", 0.30),
        )
        evaluations[_mode(row)].append(
            {
                "normalized_input": row,
                "opening_decision": build_candidate_decision(
                    mode=_mode(row),
                    symbol=str(row["symbol"]),
                    contract_symbol=str(row["contract_symbol"]),
                    accepted=False,
                    rejects=[reject],
                    normalized_input=row,
                ),
            }
        )

    seal_opening_candidate_snapshot(
        base=base,
        run_id=run_id,
        account=account,
        market=market,
        physical_account={
            "status": "available",
            "logical_account": account,
            "futu_account_id": "fixture-account",
            "trd_env": "REAL",
            "market": market,
            "source": "opend",
        },
        account_config_sha256=CONFIG_HASH,
        strategy_policy_sha256=POLICY_HASH,
        dependencies=[
            dependency_from_hash(kind=kind, sha256=char * 64)
            for kind, char in (
                ("required_data", "1"),
                ("portfolio", "2"),
                ("ledger", "3"),
                ("fx", "4"),
                ("earnings_rv", "5"),
            )
        ],
        scan_statuses=scan_statuses,
        final_candidates=final_candidates,
        candidate_evaluations=evaluations,
        sealed_at=sealed_at,
    )
    publish_strategy_scan_status_index_v2(
        report_dir=account_dir,
        run_id=run_id,
        account=account,
        account_config_sha256=CONFIG_HASH,
        expected=expected,
    )
    return publish_candidate_snapshot_manifest(
        base=base,
        run_id=run_id,
        account=account,
        strategy_policy_sha256=POLICY_HASH,
        sealed_at=manifest_sealed_at,
    )






def _normalized_row(raw: Mapping[str, Any], *, accepted: bool) -> dict[str, Any]:
    row = dict(raw)
    mode = _mode(row)
    expiration = str(row.get("expiration") or "2026-06-19")
    expiration_day = date.fromisoformat(expiration)
    market_day = expiration_day - timedelta(days=max(7, int(row.get("dte") or 30)))
    hard_start = max(
        market_day,
        expiration_day - timedelta(days=EARNINGS_NEAR_EXPIRY_WINDOW_DAYS),
    )
    symbol = str(row.get("symbol") or "NVDA").upper()
    contract = str(
        row.get("contract_symbol")
        or f"{symbol.replace('.', '')}-{mode}-{int(float(row.get('strike') or 100))}"
    )
    normalized = {
        "symbol": symbol,
        "contract_symbol": contract,
        "expiration": expiration,
        "option_type": mode,
        "strike": float(row.get("strike") or 100),
        "spot": float(row.get("spot") or (110 if mode == "put" else 90)),
        "dte": int(row.get("dte") or 30),
        "bid": float(row.get("bid") or 1.0),
        "ask": float(row.get("ask") or 1.2),
        "mid": float(row.get("mid") or 1.1),
        "multiplier": int(row.get("multiplier") or 100),
        "currency": str(row.get("currency") or "USD"),
        "net_income": float(row.get("net_income") or 100),
        "net_income_cny": float(row.get("net_income_cny") or 700),
        "spread_ratio": float(row.get("spread_ratio") or 0.10),
        "iv_rv_ratio": float(row.get("iv_rv_ratio") or 1.25),
        "iv_minus_rv": float(row.get("iv_minus_rv") or 0.08),
        "annualized_net_return_on_cash_basis": float(
            row.get("annualized_net_return_on_cash_basis") or 0.12
        ),
        "annualized_net_premium_return": float(
            row.get("annualized_net_premium_return")
            or row.get("annualized_net_return_on_cash_basis")
            or 0.12
        ),
        "max_new_contracts": int(row.get("max_new_contracts") or 1),
        "covered_contracts_available": int(
            row.get("covered_contracts_available") or 1
        ),
        "earnings_evidence_status": "ready",
        "earnings_reason_code": None,
        "earnings_policy_version": EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
        "earnings_window_days": EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
        "earnings_market_date": market_day.isoformat(),
        "earnings_hard_window_start": hard_start.isoformat(),
        "earnings_hard_window_end": expiration,
        "earnings_hard_coverage_status": "complete",
        "earnings_soft_coverage_status": "complete",
        "earnings_has_event": False,
        "earnings_blocking_has_event": False,
        "earnings_events": [],
        "earnings_blocking_events": [],
        "earnings_nonblocking_events": [],
        **row,
        "symbol": symbol,
        "contract_symbol": contract,
        "expiration": expiration,
        "option_type": mode,
    }
    normalized["fixture_expected_status"] = "accepted" if accepted else "rejected"
    return normalized


def _mode(row: Mapping[str, Any]) -> str:
    raw = str(row.get("mode") or row.get("option_type") or "put").lower()
    return "call" if raw in {"call", "covered_call", "sell_call"} else "put"
