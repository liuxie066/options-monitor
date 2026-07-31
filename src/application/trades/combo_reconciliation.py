from __future__ import annotations

from pathlib import Path
from typing import Any

from src.application.daily_decision_brief_repository import (
    read_combo_candidate_exposures,
)
from src.application.ledger.api import (
    reconcile_combo_pair_inferences,
)


_ENABLED_MODES = {"observe", "confirm"}


def trade_combo_runtime_environment(*, host: str, port: int) -> str:
    """Return the stable runtime boundary used by one OpenD intake source."""

    host_value = str(host or "").strip().lower()
    port_value = int(port or 0)
    if not host_value or port_value <= 0:
        raise ValueError("combo reconciliation requires a valid OpenD host and port")
    return f"opend:{host_value}:{port_value}"


def reconcile_account_post_trade_combos(
    *,
    repo: Any,
    runtime_root: Path,
    account: str,
    runtime_environment: str,
    mode: str,
    effective_now_ms: int | None = None,
) -> dict[str, Any]:
    """Reconcile one account after trade commit without changing Combo membership."""

    account_value = str(account or "").strip().lower()
    mode_value = str(mode or "off").strip().lower()
    if mode_value not in {"off", *_ENABLED_MODES}:
        raise ValueError("combo reconciliation mode must be off, observe, or confirm")
    if not account_value:
        raise ValueError("combo reconciliation requires account")
    if mode_value == "off":
        return {
            "ok": True,
            "status": "off",
            "account": account_value,
            "mode": mode_value,
            "persisted": False,
        }

    preview = reconcile_combo_pair_inferences(
        repo=repo,
        account=account_value,
        runtime_environment=runtime_environment,
        persist=False,
        effective_now_ms=effective_now_ms,
    )
    scopes = {
        (
            str(item.get("market") or "").strip().upper(),
            str(item.get("market_date") or "").strip(),
        )
        for item in [
            *(preview.get("inferences") or []),
            *(preview.get("waiting_for_counterpart") or []),
        ]
        if str(item.get("market") or "").strip()
        and str(item.get("market_date") or "").strip()
    }
    exposures_by_id: dict[str, dict[str, Any]] = {}
    evidence_reads: list[dict[str, Any]] = []
    for market, market_date in sorted(scopes):
        result = read_combo_candidate_exposures(
            base=Path(runtime_root).resolve(),
            account=account_value,
            market=market,
            market_trading_date=market_date,
        )
        evidence_reads.append(
            {
                "market": market,
                "market_date": market_date,
                "available": bool(result.get("available")),
                "reason": result.get("reason"),
                "exposure_count": len(result.get("exposures") or []),
            }
        )
        for item in result.get("exposures") or []:
            exposure_id = str(item.get("candidate_exposure_id") or "").strip()
            if exposure_id:
                exposures_by_id[exposure_id] = dict(item)

    reconciled = reconcile_combo_pair_inferences(
        repo=repo,
        account=account_value,
        runtime_environment=runtime_environment,
        exposures=[exposures_by_id[key] for key in sorted(exposures_by_id)],
        persist=True,
        effective_now_ms=effective_now_ms,
    )
    return {
        **reconciled,
        "status": "reconciled",
        "mode": mode_value,
        "evidence_reads": evidence_reads,
    }


__all__ = [
    "reconcile_account_post_trade_combos",
    "trade_combo_runtime_environment",
]
