from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.ledger.api import (
    validate_position_fact_snapshot_contract,
)
from src.application.position_advice_source_receipts import publish_source_receipt


PORTFOLIO_SOURCE_SCHEMA = "position_advice_portfolio_source.v1"
LEDGER_SOURCE_SCHEMA = "position_advice_ledger_source.v1"
CANDIDATE_SOURCE_SCHEMA = "position_advice_candidate_all_decisions.v1"
CASH_CAPACITY_SOURCE_SCHEMA = "position_advice_cash_capacity.v1"
SHARE_COVERAGE_SOURCE_SCHEMA = "position_advice_share_coverage.v1"
FX_SOURCE_SCHEMA = "position_advice_fx_source.v1"


def publish_portfolio_source_snapshot(
    *,
    producer_root: Path,
    account_run_id: str,
    account: str,
    broker: str,
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    included_markets: Iterable[str],
    portfolio_context: Mapping[str, Any],
    completed_at: datetime | str | None = None,
) -> tuple[Path, dict[str, Any]]:
    context = dict(portfolio_context or {})
    observation_status = str(
        context.get("source_observation_status") or ""
    ).strip().lower()
    if observation_status and observation_status != "trusted":
        raise ValueError(
            "portfolio source observation is not trusted"
        )
    observed_at = _required_text(
        context.get("source_observed_at"),
        "portfolio source_observed_at",
    )
    payload = {
        "schema_version": PORTFOLIO_SOURCE_SCHEMA,
        "normalized_portfolio_source": _required_text(
            normalized_portfolio_source,
            "normalized_portfolio_source",
        ),
        "portfolio_context": context,
    }
    return _publish_json(
        producer_root=producer_root,
        account_run_id=account_run_id,
        account=account,
        broker=broker,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
        included_markets=included_markets,
        source_kind="portfolio",
        producer_schema_version=PORTFOLIO_SOURCE_SCHEMA,
        source_observed_at=observed_at,
        producer_policy_hash=canonical_sha256(
            {
                "schema": PORTFOLIO_SOURCE_SCHEMA,
                "normalized_portfolio_source": normalized_portfolio_source,
            }
        ),
        payload=payload,
        dependencies=(),
        completed_at=completed_at,
    )


def publish_ledger_source_snapshot(
    *,
    producer_root: Path,
    account_run_id: str,
    account: str,
    broker: str,
    portfolio_account_identity_hash: str,
    included_markets: Iterable[str],
    decision_state_snapshot: Mapping[str, Any],
    completed_at: datetime | str | None = None,
) -> tuple[Path, dict[str, Any]]:
    snapshot = dict(decision_state_snapshot or {})
    if (
        snapshot.get("snapshot_status") != "trusted"
        or snapshot.get("actionable") is not True
        or len(str(snapshot.get("decision_state_fingerprint") or "")) != 64
    ):
        raise ValueError("ledger decision state snapshot is not trusted")
    position_fact_reasons = validate_position_fact_snapshot_contract(
        snapshot
    )
    if position_fact_reasons:
        raise ValueError(
            "ledger decision state position facts are invalid: "
            + ",".join(position_fact_reasons)
        )
    payload = {
        "schema_version": LEDGER_SOURCE_SCHEMA,
        "decision_state_snapshot": snapshot,
    }
    return _publish_json(
        producer_root=producer_root,
        account_run_id=account_run_id,
        account=account,
        broker=broker,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
        included_markets=included_markets,
        source_kind="ledger_decision_state",
        producer_schema_version=LEDGER_SOURCE_SCHEMA,
        source_observed_at=_required_text(
            snapshot.get("source_observed_at"),
            "ledger source_observed_at",
        ),
        producer_policy_hash=canonical_sha256(
            {
                "schema": LEDGER_SOURCE_SCHEMA,
                "fingerprint_schema_version": snapshot.get(
                    "fingerprint_schema_version"
                ),
            }
        ),
        payload=payload,
        dependencies=(),
        completed_at=completed_at,
    )


def publish_candidate_decisions_snapshot(
    *,
    producer_root: Path,
    account_run_id: str,
    account: str,
    broker: str,
    portfolio_account_identity_hash: str,
    included_markets: Iterable[str],
    decisions: Iterable[Mapping[str, Any]],
    quote_dependencies: Iterable[Mapping[str, Any]],
    source_observed_at: datetime | str,
    completed_at: datetime | str | None = None,
) -> tuple[Path, dict[str, Any]]:
    rows = [dict(item) for item in decisions]
    dependencies = [dict(item) for item in quote_dependencies]
    quote_ids = {
        str(item.get("snapshot_id") or "")
        for item in dependencies
        if item.get("source_kind") == "quotes"
    }
    if not quote_ids or any(
        str(row.get("quote_snapshot_id") or "") not in quote_ids for row in rows
    ):
        raise ValueError(
            "candidate decisions do not bind their quote dependencies"
        )
    policy_hashes = sorted(
        {str(row.get("risk_policy_hash") or "") for row in rows}
    )
    if rows and (
        any(len(item) != 64 for item in policy_hashes)
        or any(
            row.get("schema_version") != "candidate_all_decisions.v1"
            for row in rows
        )
    ):
        raise ValueError("candidate all-decisions payload is invalid")
    payload = {
        "schema_version": CANDIDATE_SOURCE_SCHEMA,
        "candidate_decisions": rows,
        "candidate_count": len(rows),
        "risk_policy_hashes": policy_hashes,
        "quote_snapshot_ids": sorted(quote_ids),
    }
    return _publish_json(
        producer_root=producer_root,
        account_run_id=account_run_id,
        account=account,
        broker=broker,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
        included_markets=included_markets,
        source_kind="candidate_decisions",
        producer_schema_version=CANDIDATE_SOURCE_SCHEMA,
        source_observed_at=source_observed_at,
        producer_policy_hash=canonical_sha256(
            {
                "schema": CANDIDATE_SOURCE_SCHEMA,
                "risk_policy_hashes": policy_hashes,
            }
        ),
        payload=payload,
        dependencies=dependencies,
        completed_at=completed_at,
    )


def publish_cash_capacity_snapshot(
    *,
    producer_root: Path,
    account_run_id: str,
    account: str,
    broker: str,
    portfolio_account_identity_hash: str,
    included_markets: Iterable[str],
    capacity_pool_authority_id: str,
    cash_capacity: Mapping[str, Any],
    dependencies: Iterable[Mapping[str, Any]],
    source_observed_at: datetime | str,
    completed_at: datetime | str | None = None,
) -> tuple[Path, dict[str, Any]]:
    payload = {
        "schema_version": CASH_CAPACITY_SOURCE_SCHEMA,
        "cash_capacity": dict(cash_capacity or {}),
        "capacity_pool_authority_id": capacity_pool_authority_id,
    }
    return _publish_json(
        producer_root=producer_root,
        account_run_id=account_run_id,
        account=account,
        broker=broker,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
        included_markets=included_markets,
        source_kind="cash_capacity",
        producer_schema_version=CASH_CAPACITY_SOURCE_SCHEMA,
        source_observed_at=source_observed_at,
        producer_policy_hash=canonical_sha256(
            {
                "schema": CASH_CAPACITY_SOURCE_SCHEMA,
                "cash_capacity_semantics": cash_capacity.get(
                    "cash_capacity_semantics"
                ),
            }
        ),
        payload=payload,
        dependencies=dependencies,
        capacity_pool_authority_id=capacity_pool_authority_id,
        completed_at=completed_at,
    )


def publish_share_coverage_snapshot(
    *,
    producer_root: Path,
    account_run_id: str,
    account: str,
    broker: str,
    portfolio_account_identity_hash: str,
    included_markets: Iterable[str],
    share_coverage: Mapping[str, Any],
    dependencies: Iterable[Mapping[str, Any]],
    source_observed_at: datetime | str,
    completed_at: datetime | str | None = None,
) -> tuple[Path, dict[str, Any]]:
    payload = {
        "schema_version": SHARE_COVERAGE_SOURCE_SCHEMA,
        "share_coverage": dict(share_coverage or {}),
    }
    return _publish_json(
        producer_root=producer_root,
        account_run_id=account_run_id,
        account=account,
        broker=broker,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
        included_markets=included_markets,
        source_kind="share_coverage",
        producer_schema_version=SHARE_COVERAGE_SOURCE_SCHEMA,
        source_observed_at=source_observed_at,
        producer_policy_hash=canonical_sha256(
            {
                "schema": SHARE_COVERAGE_SOURCE_SCHEMA,
                "share_coverage_semantics": share_coverage.get(
                    "share_coverage_semantics"
                ),
            }
        ),
        payload=payload,
        dependencies=dependencies,
        completed_at=completed_at,
    )


def publish_fx_source_snapshot(
    *,
    producer_root: Path,
    producer_run_id: str,
    included_markets: Iterable[str],
    fx_payload: Mapping[str, Any],
    source_observed_at: datetime | str,
    provider: str,
    completed_at: datetime | str | None = None,
) -> tuple[Path, dict[str, Any]]:
    fx = dict(fx_payload or {})
    if fx.get("freshness_status") == "stale_fallback":
        raise ValueError("stale fallback FX cannot be published for position advice")
    observed_at = _required_text(source_observed_at, "source_observed_at")
    payload_observed_at = _required_text(
        fx.get("timestamp"),
        "fx timestamp",
    )
    if payload_observed_at != observed_at:
        raise ValueError("FX source observation does not match provider timestamp")
    payload_provider = _required_text(fx.get("source"), "fx source")
    expected_provider = _required_text(provider, "provider")
    if payload_provider != expected_provider:
        raise ValueError("FX provider does not match payload source")
    rates = fx.get("rates")
    if not isinstance(rates, Mapping) or not rates:
        raise ValueError("FX rates are required")
    payload = {
        "schema_version": FX_SOURCE_SCHEMA,
        "provider": expected_provider,
        "fx": fx,
    }
    return _publish_json(
        producer_root=producer_root,
        account_run_id=producer_run_id,
        account=None,
        broker=None,
        portfolio_account_identity_hash=None,
        included_markets=included_markets,
        source_kind="fx",
        producer_schema_version=FX_SOURCE_SCHEMA,
        source_observed_at=observed_at,
        producer_policy_hash=canonical_sha256(
            {"schema": FX_SOURCE_SCHEMA, "provider": provider}
        ),
        payload=payload,
        dependencies=(),
        completed_at=completed_at,
        producer_scope="global",
    )


def _publish_json(
    *,
    producer_root: Path,
    account_run_id: str,
    account: str | None,
    broker: str | None,
    portfolio_account_identity_hash: str | None,
    included_markets: Iterable[str],
    source_kind: str,
    producer_schema_version: str,
    source_observed_at: datetime | str,
    producer_policy_hash: str,
    payload: Mapping[str, Any],
    dependencies: Iterable[Mapping[str, Any]],
    completed_at: datetime | str | None,
    capacity_pool_authority_id: str | None = None,
    producer_scope: str = "account",
) -> tuple[Path, dict[str, Any]]:
    value = dict(payload)
    run_id = _required_text(account_run_id, "account_run_id")
    payload_bytes = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    payload_hash = canonical_sha256(value)
    run_key = canonical_sha256({"producer_run_id": run_id})
    identity_key = (
        str(portfolio_account_identity_hash)
        if producer_scope == "account"
        else "shared"
    )
    prefix = (
        f"position_advice_producers/{source_kind}/{run_key}/{payload_hash}"
    )
    receipt = publish_source_receipt(
        producer_root=producer_root,
        receipt_relpath=f"{prefix}/receipt.json",
        payload_relpath=f"{prefix}/payload.json",
        payload_bytes=payload_bytes,
        source_kind=source_kind,
        producer_schema_version=producer_schema_version,
        producer_run_id=run_id,
        producer_scope=producer_scope,
        producer_account_run_id=(
            run_id
            if producer_scope == "account"
            else None
        ),
        broker=broker,
        account=account,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
        included_markets=included_markets,
        source_native_id=f"{source_kind}:{identity_key}:{payload_hash}",
        source_observed_at=source_observed_at,
        completed_at=completed_at or datetime.now(timezone.utc),
        producer_policy_hash=producer_policy_hash,
        dependencies=dependencies,
        capacity_pool_authority_id=capacity_pool_authority_id,
    )
    return Path(producer_root).resolve() / f"{prefix}/receipt.json", receipt


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


__all__ = [
    "CANDIDATE_SOURCE_SCHEMA",
    "CASH_CAPACITY_SOURCE_SCHEMA",
    "FX_SOURCE_SCHEMA",
    "LEDGER_SOURCE_SCHEMA",
    "PORTFOLIO_SOURCE_SCHEMA",
    "SHARE_COVERAGE_SOURCE_SCHEMA",
    "publish_candidate_decisions_snapshot",
    "publish_cash_capacity_snapshot",
    "publish_fx_source_snapshot",
    "publish_ledger_source_snapshot",
    "publish_portfolio_source_snapshot",
    "publish_share_coverage_snapshot",
]
