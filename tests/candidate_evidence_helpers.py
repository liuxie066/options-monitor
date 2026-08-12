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
from src.application.combo_yield_candidate_snapshot import (
    seal_combo_yield_candidate_snapshot,
)
from src.application.opening_candidate_snapshot import (
    dependency_from_hash,
    seal_opening_candidate_snapshot,
)
from src.application.shadow_replay.common import (
    DATASET_FILES,
    refresh_dataset_manifest,
)
from src.application.strategy_scan_status import (
    publish_strategy_scan_status,
    publish_strategy_scan_status_index_v2,
)


CONFIG_HASH = "a" * 64
POLICY_HASH = "b" * 64


def seal_opening_candidate_fixture(
    base: Path,
    *,
    run_id: str,
    account: str = "lx",
    market: str = "US",
    accepted_rows: Iterable[Mapping[str, Any]] = (),
    rejected_rows: Iterable[Mapping[str, Any]] = (),
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
        sealed_at="2026-06-01T00:00:00Z",
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
        sealed_at="2026-06-01T00:00:01Z",
    )


def seal_strict_dataset_fixture(dataset_dir: Path) -> dict[str, Any]:
    """Attach strict candidate authority to an already-written test dataset."""

    for name in DATASET_FILES:
        path = dataset_dir / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
    manifest = refresh_dataset_manifest(dataset_dir)
    manifest["source"] = {
        **dict(manifest.get("source") or {}),
        "candidate_evidence_coverage": {
            "schema_version": "candidate_evidence_compatibility.v1",
            "accounts": [
                {
                    "schema_version": "candidate_evidence_compatibility.v1",
                    "run_id": "test-run",
                    "account": "lx",
                    "status": "supported",
                    "reason_code": "candidate_snapshot_manifest_valid",
                    "strict_replay_authority": True,
                    "markets": ["us"],
                }
            ],
            "counts": {
                "supported": 1,
                "supported_limited_legacy_snapshot": 0,
                "unsupported_legacy_csv_only": 0,
                "unsupported_snapshot_missing": 0,
                "unsupported_snapshot_schema": 0,
                "not_scanned": 0,
            },
            "strict_replay_authority": True,
            "reason_code": "test_manifest_supported",
        },
    }
    from src.application.shadow_replay.common import write_json

    write_json(dataset_dir / "manifest.json", manifest)
    return refresh_dataset_manifest(dataset_dir)


def seal_combo_candidate_fixture(
    base: Path,
    *,
    run_id: str,
    account: str = "lx",
    market: str = "US",
    ranked_pairs: Iterable[Mapping[str, Any]] = (),
    pair_evaluations: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Publish a manifest-bound Combo v2 snapshot for replay tests."""

    selected: list[dict[str, Any]] = []
    for raw in ranked_pairs:
        row = _normalized_combo_pair(raw, run_id=run_id, account=account)
        selected.append(row)

    if pair_evaluations is None:
        evaluations = [
            {
                **row,
                "diagnostic_scope": "pair",
                "diagnostic_stage": "pair_filter",
                "accepted": True,
                "reject_reasons": [],
            }
            for row in selected
        ]
    else:
        evaluations = [
            _normalized_combo_evaluation(raw, run_id=run_id, account=account)
            for raw in pair_evaluations
        ]

    eligible = [
        row
        for row in evaluations
        if row.get("diagnostic_scope") == "pair" and row.get("accepted") is True
    ]
    selected_ids = {str(row["candidate_pair_id"]) for row in selected}
    rank_records: list[dict[str, Any]] = []
    for rank, row in enumerate(eligible, start=1):
        pair_id = str(row["candidate_pair_id"])
        is_selected = pair_id in selected_ids
        rank_records.append(
            {
                "candidate_pair_id": pair_id,
                "run_id": run_id,
                "account": account,
                "symbol": row["symbol"],
                "put_contract_symbol": row["put_contract_symbol"],
                "call_contract_symbol": row["call_contract_symbol"],
                "baseline_rank": rank if is_selected else None,
                "shadow_rank": rank if is_selected else None,
                "baseline_selected": is_selected,
                "shadow_selected": is_selected,
                "rank_changed": False,
            }
        )

    symbols = sorted(
        {
            str(row.get("symbol") or "TSLA").strip().upper()
            for row in [*selected, *evaluations]
        }
    ) or ["TSLA"]
    account_dir = base / "output_runs" / run_id / "accounts" / account
    account_dir.mkdir(parents=True, exist_ok=True)
    scan_statuses: list[dict[str, Any]] = []
    expected: list[dict[str, str]] = []
    for symbol in symbols:
        candidate_count = sum(
            str(row.get("symbol") or "").upper() == symbol for row in selected
        )
        publish_strategy_scan_status(
            report_dir=account_dir,
            run_id=run_id,
            account=account,
            market=market,
            symbol=symbol,
            strategy_family="combo_yield",
            status="completed",
            candidate_count=candidate_count,
            reason="no_candidate" if candidate_count == 0 else None,
            snapshot_id=f"fixture-{symbol}-combo",
            receipt_relpath=f"quotes/{symbol}/combo/receipt.json",
        )
        scan_statuses.append(
            {
                "symbol": symbol,
                "strategy_mode": "combo_yield",
                "variant": "sp_lc",
                "status": "completed",
                "reason": "no_candidate" if candidate_count == 0 else None,
                "quote_snapshot_id": f"fixture-{symbol}-combo",
                "quote_receipt_relpath": f"quotes/{symbol}/combo/receipt.json",
            }
        )
        expected.append(
            {
                "market": market.upper(),
                "symbol": symbol,
                "strategy_family": "combo_yield",
                "strategy_mode": "combo_yield",
                "candidate_owner": "sp_lc",
                "account_config_sha256": CONFIG_HASH,
            }
        )

    seal_combo_yield_candidate_snapshot(
        base=base,
        run_id=run_id,
        account=account,
        market=market,
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
        pair_evaluations=evaluations,
        rank_records=rank_records,
        ranked_pairs=selected,
        sealed_at="2026-06-01T00:00:00Z",
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
        sealed_at="2026-06-01T00:00:01Z",
    )


def _normalized_combo_pair(
    raw: Mapping[str, Any],
    *,
    run_id: str,
    account: str,
) -> dict[str, Any]:
    row = dict(raw)
    symbol = str(row.get("symbol") or "TSLA").strip().upper()
    put_contract = str(row.get("put_contract_symbol") or "TSLA-P150").strip()
    call_contract = str(row.get("call_contract_symbol") or "TSLA-C220").strip()
    pair_id = f"combo_yield:{symbol}:{put_contract}:{call_contract}"
    expiration = str(
        row.get("expiration")
        or row.get("put_expiration")
        or row.get("call_expiration")
        or "2026-06-19"
    )
    dte = int(row.get("dte") or row.get("put_dte") or row.get("call_dte") or 30)
    return {
        "run_id": run_id,
        "account": account,
        "symbol": symbol,
        "structure_mode": str(row.get("structure_mode") or "same_expiry_pair"),
        "candidate_pair_id": pair_id,
        "expiration": expiration,
        "dte": dte,
        "put_expiration": str(row.get("put_expiration") or expiration),
        "put_dte": int(row.get("put_dte") or dte),
        "call_expiration": str(row.get("call_expiration") or expiration),
        "call_dte": int(row.get("call_dte") or dte),
        "spot": float(row.get("spot") or 180),
        "currency": str(row.get("currency") or "USD"),
        "multiplier": int(row.get("multiplier") or 100),
        "put_contracts": int(row.get("put_contracts") or row.get("contracts") or 1),
        "call_contracts": int(row.get("call_contracts") or row.get("contracts") or 1),
        "put_contract_symbol": put_contract,
        "put_strike": float(row.get("put_strike") or 150),
        "call_contract_symbol": call_contract,
        "call_strike": float(row.get("call_strike") or 220),
        **row,
        "run_id": run_id,
        "account": account,
        "symbol": symbol,
        "candidate_pair_id": pair_id,
        "put_contract_symbol": put_contract,
        "call_contract_symbol": call_contract,
    }


def _normalized_combo_evaluation(
    raw: Mapping[str, Any],
    *,
    run_id: str,
    account: str,
) -> dict[str, Any]:
    row = _normalized_combo_pair(raw, run_id=run_id, account=account)
    accepted = raw.get("accepted") is True
    return {
        **row,
        "diagnostic_scope": str(raw.get("diagnostic_scope") or "pair"),
        "diagnostic_stage": str(raw.get("diagnostic_stage") or "pair_filter"),
        "accepted": accepted,
        "reject_reasons": list(raw.get("reject_reasons") or ([] if accepted else ["fixture_rejected"])),
    }


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
