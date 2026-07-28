from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from domain.domain.position_advice_authority import (
    AuthorityResolution,
    normalize_account_label,
    normalize_portfolio_source,
    scope_for,
)
from domain.domain.symbol_identity import symbol_market
from src.application.ledger.api import (
    decision_state_snapshot,
    open_position_ledger,
)
from src.application.position_advice_authority_service import (
    read_authority_resolution_under_lock,
)
from src.application.position_advice_current_repository import (
    PositionAdviceCurrentError,
    validate_current_artifacts_under_lock,
)
from src.application.position_advice_input_builder import (
    PositionAdviceInputError,
    validate_artifact_binding,
)
from src.application.position_advice_source_receipts import (
    PositionAdviceSourceError,
)
from src.infrastructure.position_advice_manifest_lock import (
    PositionAdviceLockError,
    position_advice_manifest_locks,
)


POSITION_ADVICE_READ_SCHEMA = "position_advice_read.output.v2"
FRESHNESS_STATUSES = frozenset(
    {
        "fresh",
        "stale_decision_state",
        "projection_untrusted",
        "superseded_portfolio_plan",
        "stale_market_data",
        "stale_capacity_or_holdings",
        "stale_fx",
        "input_snapshot_skew",
        "freshness_unknown",
    }
)


def read_position_advice_v2_from_ledger(
    *,
    base: Path,
    normalized_account: str,
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    data_config_path: Path,
    requested_portfolio_plan_id: str | None = None,
    requested_market: str | None = None,
    now: datetime | str | None = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Read the current v2 plan without refreshing facts or mutating state."""

    account = normalize_account_label(normalized_account)
    repo = open_position_ledger(Path(data_config_path))
    return read_position_advice_v2(
        base=base,
        normalized_account=account,
        normalized_portfolio_source=normalized_portfolio_source,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
        decision_snapshot_reader=lambda: decision_state_snapshot(
            repo,
            account=account,
            portfolio_scope_id=scope_for(account),
        ),
        requested_portfolio_plan_id=requested_portfolio_plan_id,
        requested_market=requested_market,
        now=now,
        timeout_seconds=timeout_seconds,
    )


def read_position_advice_v2(
    *,
    base: Path,
    normalized_account: str,
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    decision_snapshot_reader: Callable[[], Mapping[str, Any]],
    requested_portfolio_plan_id: str | None = None,
    requested_market: str | None = None,
    now: datetime | str | None = None,
    timeout_seconds: float = 5.0,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """Return one fail-closed current Position Advice response."""

    attempts = int(max_attempts)
    if attempts < 1 or attempts > 2:
        raise ValueError("reader supports one attempt plus at most one retry")
    checked_at = _timestamp(now or datetime.now(timezone.utc))
    account = normalize_account_label(normalized_account)
    source = normalize_portfolio_source(normalized_portfolio_source)
    identity_hash = str(portfolio_account_identity_hash or "").strip()
    scope_id = scope_for(account)
    requested_plan_id = (
        str(requested_portfolio_plan_id or "").strip() or None
    )
    market = _market(requested_market)

    for attempt in range(attempts):
        try:
            with position_advice_manifest_locks(
                base=base,
                portfolio_scope_id=scope_id,
                global_mode="shared",
                scope_mode="shared",
                timeout_seconds=timeout_seconds,
            ):
                resolution = read_authority_resolution_under_lock(
                    base=base,
                    normalized_account=account,
                    normalized_portfolio_source=source,
                    portfolio_account_identity_hash=identity_hash,
                )
                if (
                    resolution.resolution_status != "resolved"
                    or resolution.mode not in {"v2_shadow", "v2"}
                    or not resolution.policy_hash
                ):
                    return _unavailable_response(
                        checked_at=checked_at,
                        account=account,
                        scope_id=scope_id,
                        resolution=resolution,
                        status="freshness_unknown",
                        reason_codes=(
                            *resolution.reason_codes,
                            (
                                "position_advice_v2_inactive"
                                if resolution.mode == "v1"
                                else "authority_conflict"
                            ),
                        ),
                    )

                validated = validate_current_artifacts_under_lock(
                    base=base,
                    portfolio_scope_id=scope_id,
                    market=market,
                    now=checked_at,
                    require_fresh=False,
                )
                current = dict(validated["current"])
                advice = dict(validated["advice"])
                effective_market = market or _market(
                    current.get("current_market")
                )
                if effective_market:
                    advice = _advice_for_market(
                        advice,
                        market=effective_market,
                    )
                immutable_input = dict(validated["immutable_input"])
                source_manifest = dict(validated["source_manifest"])
                _validate_current_binding(
                    current=current,
                    advice=advice,
                    immutable_input=immutable_input,
                    source_manifest=source_manifest,
                    resolution=resolution,
                    account=account,
                    source=source,
                    identity_hash=identity_hash,
                    scope_id=scope_id,
                )

                current_plan_id = str(
                    advice.get("portfolio_plan_id") or ""
                ).strip()
                if (
                    requested_plan_id is not None
                    and requested_plan_id != current_plan_id
                ):
                    return _response(
                        advice=advice,
                        checked_at=checked_at,
                        status="superseded_portfolio_plan",
                        reason_codes=("superseded_portfolio_plan",),
                        resolution=resolution,
                        current=current,
                    )

                source_status = _source_freshness_status(
                    source_manifest,
                    now=checked_at,
                )
                if source_status != "fresh":
                    return _response(
                        advice=advice,
                        checked_at=checked_at,
                        status=source_status,
                        reason_codes=(source_status,),
                        resolution=resolution,
                        current=current,
                    )

                state_a = dict(decision_snapshot_reader() or {})
                state_status = _snapshot_status(state_a)
                if state_status != "fresh":
                    return _response(
                        advice=advice,
                        checked_at=checked_at,
                        status=state_status,
                        reason_codes=(state_status,),
                        resolution=resolution,
                        current=current,
                    )
                fingerprint_a = str(
                    state_a.get("decision_state_fingerprint") or ""
                )
                if fingerprint_a != current.get(
                    "decision_state_fingerprint"
                ):
                    return _response(
                        advice=advice,
                        checked_at=checked_at,
                        status="stale_decision_state",
                        reason_codes=("decision_state_fingerprint_mismatch",),
                        resolution=resolution,
                        current=current,
                    )

                state_b = dict(decision_snapshot_reader() or {})
                state_b_status = _snapshot_status(state_b)
                if state_b_status != "fresh":
                    return _response(
                        advice=advice,
                        checked_at=checked_at,
                        status=state_b_status,
                        reason_codes=(state_b_status,),
                        resolution=resolution,
                        current=current,
                    )
                fingerprint_b = str(
                    state_b.get("decision_state_fingerprint") or ""
                )
                if fingerprint_a != fingerprint_b:
                    if attempt + 1 < attempts:
                        continue
                    return _response(
                        advice=advice,
                        checked_at=checked_at,
                        status="stale_decision_state",
                        reason_codes=("decision_state_changed_during_read",),
                        resolution=resolution,
                        current=current,
                    )
                final_resolution = read_authority_resolution_under_lock(
                    base=base,
                    normalized_account=account,
                    normalized_portfolio_source=source,
                    portfolio_account_identity_hash=identity_hash,
                )
                if (
                    final_resolution.resolution_status != "resolved"
                    or final_resolution.mode != resolution.mode
                    or final_resolution.generation != resolution.generation
                    or final_resolution.policy_hash != resolution.policy_hash
                ):
                    return _response(
                        advice=advice,
                        checked_at=checked_at,
                        status="freshness_unknown",
                        reason_codes=("authority_changed_during_read",),
                        resolution=final_resolution,
                        current=current,
                    )
                return _response(
                    advice=advice,
                    checked_at=checked_at,
                    status="fresh",
                    reason_codes=(),
                    resolution=final_resolution,
                    current=current,
                )
        except (
            OSError,
            TypeError,
            ValueError,
            PositionAdviceCurrentError,
            PositionAdviceInputError,
            PositionAdviceLockError,
            PositionAdviceSourceError,
        ) as exc:
            return _unavailable_response(
                checked_at=checked_at,
                account=account,
                scope_id=scope_id,
                resolution=None,
                status=(
                    "input_snapshot_skew"
                    if "skew" in str(exc).lower()
                    else "freshness_unknown"
                ),
                reason_codes=(
                    "input_snapshot_skew"
                    if "skew" in str(exc).lower()
                    else "freshness_validation_failed",
                ),
            )

    return _unavailable_response(
        checked_at=checked_at,
        account=account,
        scope_id=scope_id,
        resolution=None,
        status="stale_decision_state",
        reason_codes=("decision_state_changed_during_read",),
    )


def _advice_for_market(
    advice: Mapping[str, Any],
    *,
    market: str,
) -> dict[str, Any]:
    out = dict(advice)
    out["rows"] = [
        dict(item)
        for item in advice.get("rows") or []
        if isinstance(item, Mapping)
        and str(symbol_market(item.get("symbol")) or "").upper() == market
    ]
    return out


def _market(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    if text not in {"US", "HK"}:
        raise ValueError("requested_market must be US or HK")
    return text


def _validate_current_binding(
    *,
    current: Mapping[str, Any],
    advice: Mapping[str, Any],
    immutable_input: Mapping[str, Any],
    source_manifest: Mapping[str, Any],
    resolution: AuthorityResolution,
    account: str,
    source: str,
    identity_hash: str,
    scope_id: str,
) -> None:
    expected = {
        "account": account,
        "portfolio_scope_id": scope_id,
        "normalized_portfolio_source": source,
        "portfolio_account_identity_hash": identity_hash,
        "authority_mode": resolution.mode,
        "authority_generation": resolution.generation,
        "authority_policy_hash": resolution.policy_hash,
    }
    for field, value in expected.items():
        if current.get(field) != value:
            raise PositionAdviceInputError(
                f"current binding mismatch: {field}"
            )
    if current.get("source_manifest_hash") != source_manifest.get(
        "source_manifest_hash"
    ):
        raise PositionAdviceInputError("current source manifest mismatch")
    if current.get("account_run_id") != source_manifest.get("account_run_id"):
        raise PositionAdviceInputError("current source run mismatch")
    validate_artifact_binding(
        advice=advice,
        immutable_input=immutable_input,
        account_run_id=str(current["account_run_id"]),
        normalized_account=account,
        portfolio_scope_id=scope_id,
        portfolio_account_identity_hash=identity_hash,
        source_manifest_hash=str(current["source_manifest_hash"]),
        authority_resolution=resolution,
        expected_decision_state_fingerprint=str(
            current["decision_state_fingerprint"]
        ),
    )
    for payload in (advice, immutable_input):
        if payload.get("normalized_portfolio_source") != source:
            raise PositionAdviceInputError(
                "artifact portfolio source binding mismatch"
            )
        if payload.get("included_markets") != current.get(
            "included_markets"
        ):
            raise PositionAdviceInputError(
                "artifact market binding mismatch"
            )


def _source_freshness_status(
    source_manifest: Mapping[str, Any],
    *,
    now: datetime | str,
) -> str:
    now_dt = _parse_timestamp(now)
    expired: set[str] = set()
    for raw in source_manifest.get("source_manifest") or []:
        item = dict(raw)
        if now_dt >= _parse_timestamp(item.get("expires_at")):
            expired.add(str(item.get("source_kind") or ""))
    if "fx" in expired:
        return "stale_fx"
    if expired & {"quotes", "candidate_decisions"}:
        return "stale_market_data"
    if expired:
        return "stale_capacity_or_holdings"
    return "fresh"


def _snapshot_status(snapshot: Mapping[str, Any]) -> str:
    if snapshot.get("snapshot_status") == "projection_untrusted":
        return "projection_untrusted"
    if (
        snapshot.get("snapshot_status") != "trusted"
        or snapshot.get("actionable") is not True
        or len(str(snapshot.get("decision_state_fingerprint") or "")) != 64
    ):
        return "freshness_unknown"
    return "fresh"


def _response(
    *,
    advice: Mapping[str, Any],
    checked_at: str,
    status: str,
    reason_codes: tuple[str, ...],
    resolution: AuthorityResolution,
    current: Mapping[str, Any],
) -> dict[str, Any]:
    if status not in FRESHNESS_STATUSES:
        raise ValueError("unsupported freshness status")
    rows = [
        dict(item)
        for item in advice.get("rows") or []
        if isinstance(item, Mapping)
    ]
    if status != "fresh" or resolution.mode != "v2":
        zero_reasons = reason_codes
        if status == "fresh" and resolution.mode == "v2_shadow":
            zero_reasons = (*reason_codes, "v2_not_authoritative")
        rows = _zero_actionability(rows, zero_reasons)
    return {
        "schema_version": POSITION_ADVICE_READ_SCHEMA,
        "availability_status": "available",
        "freshness": {
            "status": status,
            "reason_codes": list(reason_codes),
            "checked_at": checked_at,
        },
        "account": advice.get("account"),
        "portfolio_scope_id": advice.get("portfolio_scope_id"),
        "authority_mode": resolution.mode,
        "authority_generation": resolution.generation,
        "authority_policy_hash": resolution.policy_hash,
        "portfolio_plan_id": advice.get("portfolio_plan_id"),
        "account_run_id": advice.get("account_run_id"),
        "current_manifest_hash": current.get("current_manifest_hash"),
        "advice_checked_at": checked_at,
        "economic_model": advice.get("economic_model"),
        "allocator_version": advice.get("allocator_version"),
        "rows": rows,
        "row_count": len(rows),
        "actionable_count": sum(
            1 for item in rows if item.get("actionable") is True
        ),
        "model_actionable_count": sum(
            1 for item in rows if item.get("model_actionable") is True
        ),
        "model_trade_actionable_count": sum(
            1
            for item in rows
            if item.get("model_trade_actionable") is True
        ),
        "human_review_required_count": sum(
            1
            for item in rows
            if item.get("human_review_required") is True
        ),
        "source_manifest": [
            dict(item)
            for item in advice.get("source_manifest") or []
            if isinstance(item, Mapping)
        ],
        "advisory_only": True,
        "execution_authorized": False,
    }


def _unavailable_response(
    *,
    checked_at: str,
    account: str,
    scope_id: str,
    resolution: AuthorityResolution | None,
    status: str,
    reason_codes: tuple[str, ...],
) -> dict[str, Any]:
    if status not in FRESHNESS_STATUSES:
        raise ValueError("unsupported freshness status")
    return {
        "schema_version": POSITION_ADVICE_READ_SCHEMA,
        "availability_status": "unavailable",
        "freshness": {
            "status": status,
            "reason_codes": sorted(
                {str(item) for item in reason_codes if str(item)}
            ),
            "checked_at": checked_at,
        },
        "account": account,
        "portfolio_scope_id": scope_id,
        "authority_mode": resolution.mode if resolution else None,
        "authority_generation": (
            resolution.generation if resolution else None
        ),
        "authority_policy_hash": (
            resolution.policy_hash if resolution else None
        ),
        "portfolio_plan_id": None,
        "account_run_id": None,
        "advice_checked_at": checked_at,
        "rows": [],
        "row_count": 0,
        "actionable_count": 0,
        "model_actionable_count": 0,
        "model_trade_actionable_count": 0,
        "human_review_required_count": 0,
        "source_manifest": [],
        "advisory_only": True,
        "execution_authorized": False,
    }


def _zero_actionability(
    rows: list[dict[str, Any]],
    reason_codes: tuple[str, ...],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for raw in rows:
        item = dict(raw)
        item["actionable"] = False
        item["action_scope"] = (
            str(item.get("action_scope") or "human_fact_review")
            if item.get("human_review_required") is True
            else "none"
        )
        item["reason_codes"] = sorted(
            {
                *(
                    str(value)
                    for value in item.get("reason_codes") or []
                    if str(value)
                ),
                *(str(value) for value in reason_codes if str(value)),
            }
        )
        output.append(item)
    return output


def _timestamp(value: datetime | str) -> str:
    return _parse_timestamp(value).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: datetime | str | Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return parsed.astimezone(timezone.utc)


__all__ = [
    "FRESHNESS_STATUSES",
    "POSITION_ADVICE_READ_SCHEMA",
    "read_position_advice_v2",
    "read_position_advice_v2_from_ledger",
]
