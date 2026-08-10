from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domain.domain.combo_identity import build_combo_identity
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger.identity import ContractKey
from src.application.ai_decision_advice.contexts import (
    build_frozen_inputs,
    freeze_candidates,
    freeze_external_evidence,
    freeze_option_positions,
    freeze_portfolio_distribution,
)
from src.application.ai_decision_advice.evidence_store import (
    EvidenceIndex,
    SymbolEvidenceView,
)
from src.application.prepared_option_positions_context import (
    PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA,
)
from src.application.prepared_portfolio_distribution import (
    PREPARED_PORTFOLIO_DISTRIBUTION_SCHEMA,
    PreparedPortfolioDistribution,
)


CONFIG_HASH = "a" * 64
OBSERVED = "2026-08-09T11:59:00+00:00"


def _snapshot(*, run_id: str = "run-1", account: str = "lx") -> dict:
    return {
        "run_id": run_id,
        "account": account,
        "account_config_sha256": CONFIG_HASH,
        "ranked_candidates": [
            {
                "candidate_id": "sp1",
                "strategy_mode": "put",
                "rank": 1,
                "facts": {
                    "symbol": "NVDA",
                    "option_type": "put",
                    "strike": 100,
                    "expiration": "2026-09-18",
                    "multiplier": 100,
                    "currency": "USD",
                    "dte": 40,
                    "delta": -0.2,
                    "period_net_return_on_cash_basis": 0.03,
                    "annualized_net_return_on_cash_basis": 0.27,
                    "net_income": 295,
                    "premium": 3,
                },
            },
            {
                "candidate_id": "cc1",
                "strategy_mode": "call",
                "rank": 1,
                "facts": {
                    "symbol": "AAPL",
                    "option_type": "call",
                    "strike": 200,
                    "expiration": "2026-10-16",
                    "multiplier": 100,
                    "currency": "USD",
                    "annualized_net_premium_return": 0.15,
                },
            },
        ],
    }


def _portfolio(
    *,
    run_id: str = "run-1",
    account: str = "lx",
    status: str = "ready",
    reason: str = "portfolio_ready",
    assets: list[dict[str, Any]] | None = None,
    observed_at: str = OBSERVED,
) -> PreparedPortfolioDistribution:
    rows = assets if assets is not None else [
        {
            "code": "NVDA",
            "normalized_type": "stock",
            "currency": "USD",
            "quantity": 300.0,
            "value": 300_000.0,
        },
        {
            "code": "AAPL",
            "normalized_type": "stock",
            "currency": "USD",
            "quantity": 200.0,
            "value": 400_000.0,
        },
        {
            "code": "USD-MMF",
            "normalized_type": "cash",
            "currency": "USD",
            "quantity": 300_000.0,
            "value": 300_000.0,
        },
    ]
    total = sum(float(row["value"]) for row in rows)
    asset_weights = (
        {str(row["code"]): float(row["value"]) / total for row in rows}
        if total
        else {}
    )
    currency_values: dict[str, float] = {}
    for row in rows:
        currency = str(row["currency"])
        currency_values[currency] = currency_values.get(currency, 0) + float(
            row["value"]
        )
    payload = {
        "observed_at_utc": observed_at,
        "retrieved_at_utc": "2026-08-09T12:00:00+00:00",
        "freshness_status": "fresh" if status == "ready" else "stale",
        "trust_status": "trusted",
        "dataset_ids": [],
        "reason_codes": [],
        "valuation_currency": "CNY",
        "assets": rows,
        "derived": {
            "total_value": total,
            "asset_weights": asset_weights,
            "currency_weights": {
                key: value / total for key, value in currency_values.items()
            }
            if total
            else {},
            "cash_and_mmf_weight": (
                sum(
                    float(row["value"])
                    for row in rows
                    if row["normalized_type"] == "cash"
                )
                / total
            )
            if total
            else 0.0,
        },
    }
    return PreparedPortfolioDistribution(
        envelope={
            "authority": {
                "schema_version": PREPARED_PORTFOLIO_DISTRIBUTION_SCHEMA,
                "run_id": run_id,
                "account": account,
                "mapped_pm_account": account,
                "provider": "portfolio_management",
                "account_config_sha256": CONFIG_HASH,
                "status": status,
                "reason": reason,
                "fetched_at_utc": "2026-08-09T12:00:00+00:00",
                "validation": {"status": "passed"},
            },
            "payload": payload,
            "integrity": {},
        },
        artifact_path=None,
        artifact_sha256=None,
    )


def _position(
    record_id: str,
    *,
    account: str = "lx",
    symbol: str = "NVDA",
    option_type: str = "put",
    side: str = "short",
    strike: float = 95,
    expiry: str = "2026-09-18",
    multiplier: int = 100,
    contracts: int = 1,
    group_id: str | None = None,
    broker: str = "futu",
) -> dict:
    return {
        "record_id": record_id,
        "broker": broker,
        "account": account,
        "status": "open",
        "symbol": symbol,
        "option_type": option_type,
        "side": side,
        "strike": strike,
        "expiration_ymd": expiry,
        "multiplier": multiplier,
        "contracts_open": contracts,
        "strategy_group_id": group_id,
        "premium": 999,
        "note": "private",
        "raw_payload": {"account": account},
    }


def _option_context(
    *,
    run_id: str = "run-1",
    account: str = "lx",
    rows: list[dict] | None = None,
    identities: list[dict] | None = None,
    memberships: list[dict] | None = None,
    fx_status: str = "ready",
    observed_at: str = OBSERVED,
) -> dict:
    return {
        "context_source": "prepared",
        "context_status": "available",
        "decision_snapshot_status": "trusted",
        "filters": {"account": account, "broker": "futu"},
        "prepared_authority": {
            "schema_version": PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA,
            "run_id": run_id,
            "account": account,
            "account_config_sha256": CONFIG_HASH,
            "source_observed_at": observed_at,
            "fx_status": fx_status,
        },
        "exchange_rates": {
            "rates": {"USDCNY": 7.2, "HKDCNY": 0.92}
        },
        "open_positions_min": rows if rows is not None else [],
        "decision_state_snapshot": {
            "account_combo_identities": identities or [],
            "account_combo_group_memberships": memberships or [],
        },
    }


def _evidence_index(
    *,
    frozen_at: str = "2026-08-09T12:00:00+00:00",
    checked_at: str = OBSERVED,
    claim: str = "public fact",
) -> EvidenceIndex:
    return EvidenceIndex(
        frozen_at=frozen_at,
        views={
            "NVDA": SymbolEvidenceView(
                symbol="NVDA",
                coverage="completed",
                last_checked_at=checked_at,
                last_success_at=checked_at,
                evidence=(
                    {
                        "content_fingerprint": "b" * 64,
                        "topic": "regulatory",
                        "claim": claim,
                        "event_status": "developing",
                        "source": {
                            "title": "Public title",
                            "publisher": "Publisher",
                            "url": "https://example.com/fact",
                        },
                        "account_id": "must-not-leak",
                        "local_path": "/private/path",
                    },
                ),
            )
        },
    )


def _contract_key(
    *,
    option_type: str,
    side: str,
    strike: float,
    expiry: str = "2026-09-18",
    symbol: str = "NVDA",
    account: str = "lx",
) -> dict[str, Any]:
    return ContractKey.from_values(
        broker="futu",
        account=account,
        underlying_symbol=symbol,
        option_type=option_type,
        position_side=side,
        strike=strike,
        expiration_ymd=expiry,
    ).to_dict()


def _combo_identity(
    *,
    group_id: str,
    put_strike: float = 95,
    call_strike: float = 120,
) -> dict[str, Any]:
    return build_combo_identity(
        {
            "group_id": group_id,
            "strategy": "combo_yield",
            "account": "lx",
            "symbol": "NVDA",
            "funding_put_record_id": "put-1",
            "funding_put_open_event_id": "event-put",
            "funding_put_contract_key": _contract_key(
                option_type="put",
                side="short",
                strike=put_strike,
            ),
            "participation_call_record_id": "call-1",
            "participation_call_open_event_id": "event-call",
            "participation_call_contract_key": _contract_key(
                option_type="call",
                side="long",
                strike=call_strike,
            ),
            "original_contracts": 2,
        }
    )


def _combo_membership(
    *,
    group_id: str,
    status: str = "exact",
) -> dict[str, Any]:
    reason_codes = (
        []
        if status == "exact"
        else ["combo_group_retag_history_present"]
    )
    fact: dict[str, Any] = {
        "membership_schema_version": "account_combo_group_membership.v1",
        "group_id": group_id,
        "status": status,
        "current_account_member_record_ids": ["call-1", "put-1"],
        "global_current_member_count": 2,
        "global_historical_member_count": 2,
        "external_member_count": 0,
        "external_membership_hash": canonical_sha256([]),
        "retag_event_count": 0,
        "retag_history_hash": canonical_sha256([]),
        "cross_account_member_present": False,
        "cross_symbol_member_present": False,
        "member_bindings_for_current_account": [
            {
                "record_id": "call-1",
                "role": "participation_call",
                "open_event_id": "event-call",
                "strategy": "combo_yield",
                "account": "lx",
                "symbol": "NVDA",
            },
            {
                "record_id": "put-1",
                "role": "funding_put",
                "open_event_id": "event-put",
                "strategy": "combo_yield",
                "account": "lx",
                "symbol": "NVDA",
            },
        ],
        "reason_codes": reason_codes,
    }
    fact["membership_hash"] = canonical_sha256(fact)
    return fact


def test_freeze_candidates_preserves_pool_and_omits_absolute_premium() -> None:
    out = freeze_candidates(_snapshot(), market="US")

    assert [row["candidate_id"] for row in out["sell_put"]] == ["sp1"]
    assert [row["candidate_id"] for row in out["covered_call"]] == ["cc1"]
    assert out["sell_put"][0]["period_net_return"] == 0.03
    assert out["covered_call"][0]["annualized_gate"] == 0.15
    assert "net_income" not in str(out)
    assert "premium" not in str(out)


def test_freeze_portfolio_uses_pm_weights_and_never_exposes_absolutes() -> None:
    out = freeze_portfolio_distribution(
        _portfolio(),
        expected_run_id="run-1",
        expected_account="lx",
        expected_account_config_sha256=CONFIG_HASH,
    )

    assert out == {
        "status": "ready",
        "quality": {
            "freshness_status": "fresh",
            "trust_status": "trusted",
            "observed_at_utc": OBSERVED,
        },
        "asset_weights": {"AAPL": 0.4, "NVDA": 0.3, "USD-MMF": 0.3},
        "currency_weights": {"USD": 1.0},
        "cash_and_mmf_weight": 0.3,
        "gaps": [],
    }
    serialized = str(out)
    assert "quantity" not in serialized
    assert "total_value" not in serialized
    assert "300000" not in serialized


def test_fresh_trusted_zero_portfolio_is_complete_but_has_no_total() -> None:
    out = freeze_portfolio_distribution(_portfolio(assets=[]))

    assert out["status"] == "ready"
    assert out["asset_weights"] == {}
    assert out["cash_and_mmf_weight"] == 0.0
    assert out["gaps"] == []


def test_cross_account_portfolio_is_rejected_without_foreign_rows() -> None:
    out = freeze_portfolio_distribution(
        _portfolio(account="sy"),
        expected_run_id="run-1",
        expected_account="lx",
        expected_account_config_sha256=CONFIG_HASH,
    )

    assert out["status"] == "unavailable"
    assert out["asset_weights"] == {}
    assert out["gaps"] == [
        "portfolio_unavailable:portfolio_authority_mismatch"
    ]


def test_available_portfolio_requires_prepared_pm_source_authority() -> None:
    invalid: list[tuple[PreparedPortfolioDistribution, str]] = []

    missing_schema = _portfolio()
    missing_schema.envelope["authority"].pop("schema_version")
    invalid.append(
        (missing_schema, "portfolio_prepared_schema_invalid")
    )

    wrong_provider = _portfolio()
    wrong_provider.envelope["authority"]["provider"] = "futu"
    invalid.append((wrong_provider, "portfolio_provider_invalid"))

    missing_mapping = _portfolio()
    missing_mapping.envelope["authority"]["mapped_pm_account"] = ""
    invalid.append((missing_mapping, "portfolio_mapped_account_missing"))

    failed_validation = _portfolio()
    failed_validation.envelope["authority"]["validation"] = {
        "status": "failed"
    }
    invalid.append((failed_validation, "portfolio_validation_invalid"))

    for prepared, reason in invalid:
        out = freeze_portfolio_distribution(
            prepared,
            expected_run_id="run-1",
            expected_account="lx",
            expected_account_config_sha256=CONFIG_HASH,
        )
        assert out["status"] == "unavailable"
        assert out["asset_weights"] == {}
        assert out["gaps"] == [f"portfolio_unavailable:{reason}"]


def test_formal_unavailable_portfolio_preserves_soft_dependency_reason() -> None:
    prepared = _portfolio(
        status="unavailable",
        reason="provider_none",
        assets=[],
    )
    prepared.envelope["authority"].update(
        {
            "provider": "none",
            "mapped_pm_account": "",
            "validation": {"status": "not_applicable"},
        }
    )

    out = freeze_portfolio_distribution(
        prepared,
        expected_run_id="run-1",
        expected_account="lx",
        expected_account_config_sha256=CONFIG_HASH,
    )

    assert out["status"] == "unavailable"
    assert out["gaps"] == ["portfolio_unavailable:provider_none"]


def test_option_positions_aggregate_all_but_detail_candidate_symbols_only() -> None:
    rows = [
        _position("put-1", contracts=1),
        _position("put-2", contracts=2),
        _position(
            "other-1",
            symbol="MSFT",
            option_type="call",
            side="long",
            strike=450,
            expiry="2026-10-16",
            contracts=4,
        ),
    ]

    out = freeze_option_positions(
        _option_context(rows=rows),
        candidate_symbols=["NVDA"],
        expected_run_id="run-1",
        expected_account="lx",
        expected_account_config_sha256=CONFIG_HASH,
    )

    assert out["status"] == "ready"
    assert out["summary"]["total_open_contracts"] == 7
    assert out["candidate_contracts"] == [
        {
            "symbol": "NVDA",
            "option_type": "put",
            "side": "short",
            "strike": 95,
            "expiry": "2026-09-18",
            "multiplier": 100,
            "contracts": 3,
        }
    ]
    assert "MSFT" not in str(out["candidate_contracts"])
    assert "MSFT" not in str(out["summary"])
    assert "private" not in str(out)
    assert "record_id" not in str(out)


def test_candidate_contracts_keep_adjusted_multipliers_separate() -> None:
    out = freeze_option_positions(
        _option_context(
            rows=[
                _position("standard", multiplier=100, contracts=2),
                _position("adjusted", multiplier=50, contracts=1),
            ]
        ),
        candidate_symbols=["NVDA"],
        expected_account="lx",
    )

    assert out["candidate_contracts"] == [
        {
            "symbol": "NVDA",
            "option_type": "put",
            "side": "short",
            "strike": 95,
            "expiry": "2026-09-18",
            "multiplier": 100,
            "contracts": 2,
        },
        {
            "symbol": "NVDA",
            "option_type": "put",
            "side": "short",
            "strike": 95,
            "expiry": "2026-09-18",
            "multiplier": 50,
            "contracts": 1,
        },
    ]


def test_invalid_option_row_makes_the_entire_input_unavailable() -> None:
    row = _position("bad")
    row["multiplier"] = None

    out = freeze_option_positions(
        _option_context(rows=[_position("good"), row]),
        candidate_symbols=["NVDA"],
        expected_account="lx",
    )

    assert out["status"] == "unavailable"
    assert out["summary"]["total_open_contracts"] is None
    assert out["candidate_contracts"] == []


def test_cross_account_option_context_is_rejected_not_emptied() -> None:
    out = freeze_option_positions(
        _option_context(account="sy", rows=[_position("foreign", account="sy")]),
        candidate_symbols=["NVDA"],
        expected_run_id="run-1",
        expected_account="lx",
        expected_account_config_sha256=CONFIG_HASH,
    )

    assert out["status"] == "unavailable"
    assert out["summary"]["total_open_contracts"] is None
    assert "NVDA" not in str(out)


def test_option_positions_require_prepared_source_authority() -> None:
    missing_schema = _option_context(rows=[_position("put-1")])
    missing_schema["prepared_authority"].pop("schema_version")
    legacy_source = _option_context(rows=[_position("put-1")])
    legacy_source["context_source"] = "legacy"

    for context, reason in (
        (missing_schema, "option_prepared_schema_invalid"),
        (legacy_source, "option_source_invalid"),
    ):
        out = freeze_option_positions(
            context,
            candidate_symbols=["NVDA"],
            expected_run_id="run-1",
            expected_account="lx",
            expected_account_config_sha256=CONFIG_HASH,
        )
        assert out["status"] == "unavailable"
        assert out["summary"]["total_open_contracts"] is None
        assert out["candidate_contracts"] == []
        assert out["gaps"] == [f"option_positions_unavailable:{reason}"]


def test_only_valid_identity_produces_simplified_combo_structure() -> None:
    group_id = "combo_yield:lx:one"
    identity = _combo_identity(group_id=group_id)
    membership = _combo_membership(group_id=group_id)
    rows = [
        _position("put-1", contracts=2, group_id=group_id),
        _position(
            "call-1",
            option_type="call",
            side="long",
            strike=120,
            contracts=1,
            group_id=group_id,
        ),
    ]

    ready = freeze_option_positions(
        _option_context(
            rows=rows,
            identities=[identity],
            memberships=[membership],
        ),
        candidate_symbols=["NVDA"],
        expected_account="lx",
    )
    invalid_identity = dict(identity)
    invalid_identity["identity_hash"] = "0" * 64
    untrusted = freeze_option_positions(
        _option_context(
            rows=rows,
            identities=[invalid_identity],
            memberships=[membership],
        ),
        candidate_symbols=["NVDA"],
        expected_account="lx",
    )

    assert ready["verified_structures"] == [
        {
            "label": "SP+LC",
            "symbol": "NVDA",
            "funding_contracts": 2,
            "expression_contracts": 1,
            "expression_to_funding_ratio": 0.5,
        }
    ]
    assert group_id not in str(ready)
    assert untrusted["verified_structures"] == []


def test_combo_structure_requires_exact_membership_and_matching_contract_keys() -> None:
    group_id = "combo_yield:lx:one"
    rows = [
        _position("put-1", contracts=2, group_id=group_id),
        _position(
            "call-1",
            option_type="call",
            side="long",
            strike=120,
            contracts=1,
            group_id=group_id,
        ),
    ]
    identity = _combo_identity(group_id=group_id)
    conflict = _combo_membership(group_id=group_id, status="conflict")

    conflict_out = freeze_option_positions(
        _option_context(
            rows=rows,
            identities=[identity],
            memberships=[conflict],
        ),
        candidate_symbols=["NVDA"],
        expected_account="lx",
    )
    missing_out = freeze_option_positions(
        _option_context(rows=rows, identities=[identity]),
        candidate_symbols=["NVDA"],
        expected_account="lx",
    )
    mismatch_out = freeze_option_positions(
        _option_context(
            rows=rows,
            identities=[_combo_identity(group_id=group_id, put_strike=96)],
            memberships=[_combo_membership(group_id=group_id)],
        ),
        candidate_symbols=["NVDA"],
        expected_account="lx",
    )

    assert conflict_out["verified_structures"] == []
    assert missing_out["verified_structures"] == []
    assert mismatch_out["verified_structures"] == []


def test_freeze_external_evidence_rewrites_refs_and_strips_private_fields() -> None:
    out = freeze_external_evidence(_evidence_index(), symbols=["NVDA", "AAPL"])
    by_symbol = {row["symbol"]: row for row in out["symbols"]}

    assert out["evidence_as_of"] == OBSERVED
    assert by_symbol["NVDA"]["coverage"] == "completed"
    assert by_symbol["NVDA"]["evidence"][0]["ref"].startswith("evidence:")
    assert by_symbol["AAPL"]["coverage"] == "no_evidence"
    assert "must-not-leak" not in str(out)
    assert "local_path" not in str(out)


def test_build_frozen_inputs_projects_registers_and_hashes_content() -> None:
    rows = [
        _position("put-1", contracts=2),
        _position(
            "call-1",
            symbol="AAPL",
            option_type="call",
            side="short",
            strike=210,
            expiry="2026-10-16",
        ),
    ]
    frozen = build_frozen_inputs(
        snapshot=_snapshot(),
        portfolio_distribution=_portfolio(),
        option_positions_context=_option_context(rows=rows),
        evidence_index=_evidence_index(),
        market="US",
        evidence_run_id="evidence-run-1",
    )

    assert set(frozen.projections) == {"sp1", "cc1"}
    assert frozen.projections["sp1"]["assignment_exposure_ratio"] == 0.072
    assert frozen.projections["cc1"]["call_away_fraction"] == 0.5
    prefixes = {
        row["id"].split(":", 1)[0]
        for row in frozen.fact_registry["facts"]
    }
    assert {
        "candidate",
        "projection",
        "portfolio",
        "position",
        "coverage",
        "evidence",
        "gap",
    } <= prefixes
    bindings = frozen.input_bindings()
    assert set(bindings) == {
        "candidate_snapshot_hash",
        "portfolio_distribution_hash",
        "option_positions_hash",
        "fact_registry_hash",
        "external_evidence_hash",
        "external_evidence_run_id",
    }
    assert all(
        len(value) == 64
        for key, value in bindings.items()
        if key != "external_evidence_run_id"
    )

    model_payload = {
        "candidates": frozen.candidates,
        "portfolio_distribution": frozen.portfolio_distribution,
        "option_positions": frozen.option_positions,
        "projections": frozen.projections,
        "fact_registry": frozen.fact_registry,
        "external_evidence": frozen.external_evidence,
    }
    forbidden_keys = {
        "account",
        "mapped_pm_account",
        "broker",
        "quantity",
        "value",
        "total_value",
        "exchange_rates",
        "record_id",
        "position_id",
        "order_id",
        "trade_id",
        "group_id",
        "strategy_group_id",
        "premium",
        "cost",
        "note",
        "raw_payload",
        "path",
    }
    assert not (_recursive_keys(model_payload) & forbidden_keys)
    assert "must-not-leak" not in str(model_payload)


def test_content_hashes_exclude_run_and_account_authority() -> None:
    first = build_frozen_inputs(
        snapshot=_snapshot(run_id="run-1", account="lx"),
        portfolio_distribution=_portfolio(run_id="run-1", account="lx"),
        option_positions_context=_option_context(run_id="run-1", account="lx"),
        evidence_index=_evidence_index(),
        market="US",
    )
    second = build_frozen_inputs(
        snapshot=_snapshot(run_id="run-2", account="sy"),
        portfolio_distribution=_portfolio(run_id="run-2", account="sy"),
        option_positions_context=_option_context(run_id="run-2", account="sy"),
        evidence_index=_evidence_index(),
        market="US",
    )

    assert first.candidate_snapshot_hash == second.candidate_snapshot_hash
    assert (
        first.portfolio_distribution_hash
        == second.portfolio_distribution_hash
    )
    assert first.option_positions_hash == second.option_positions_hash
    assert first.projection_hash == second.projection_hash
    assert first.fact_registry_hash == second.fact_registry_hash


def test_semantic_hashes_ignore_observation_times_but_track_fact_changes() -> None:
    first = build_frozen_inputs(
        snapshot=_snapshot(),
        portfolio_distribution=_portfolio(observed_at=OBSERVED),
        option_positions_context=_option_context(observed_at=OBSERVED),
        evidence_index=_evidence_index(
            frozen_at="2026-08-09T12:00:00+00:00",
            checked_at=OBSERVED,
        ),
        market="US",
        evidence_run_id="evidence-run-1",
    )
    later = build_frozen_inputs(
        snapshot=_snapshot(),
        portfolio_distribution=_portfolio(
            observed_at="2026-08-09T16:00:00+00:00"
        ),
        option_positions_context=_option_context(
            observed_at="2026-08-09T16:00:00+00:00"
        ),
        evidence_index=_evidence_index(
            frozen_at="2026-08-09T16:01:00+00:00",
            checked_at="2026-08-09T16:00:00+00:00",
        ),
        market="US",
        evidence_run_id="evidence-run-1",
    )

    assert first.portfolio_distribution != later.portfolio_distribution
    assert first.option_positions != later.option_positions
    assert first.external_evidence != later.external_evidence
    assert first.input_bindings() == later.input_bindings()
    assert first.projection_hash == later.projection_hash

    changed_evidence = build_frozen_inputs(
        snapshot=_snapshot(),
        portfolio_distribution=_portfolio(),
        option_positions_context=_option_context(),
        evidence_index=_evidence_index(claim="materially different fact"),
        market="US",
        evidence_run_id="evidence-run-1",
    )
    changed_positions = build_frozen_inputs(
        snapshot=_snapshot(),
        portfolio_distribution=_portfolio(),
        option_positions_context=_option_context(
            rows=[_position("put-1", contracts=2)]
        ),
        evidence_index=_evidence_index(),
        market="US",
        evidence_run_id="evidence-run-1",
    )
    changed_quality = build_frozen_inputs(
        snapshot=_snapshot(),
        portfolio_distribution=_portfolio(
            status="degraded",
            reason="portfolio_degraded",
        ),
        option_positions_context=_option_context(),
        evidence_index=_evidence_index(),
        market="US",
        evidence_run_id="evidence-run-1",
    )

    assert (
        first.external_evidence_hash
        != changed_evidence.external_evidence_hash
    )
    assert first.fact_registry_hash != changed_evidence.fact_registry_hash
    assert first.option_positions_hash != changed_positions.option_positions_hash
    assert first.projection_hash != changed_positions.projection_hash
    assert first.fact_registry_hash != changed_positions.fact_registry_hash
    assert (
        first.portfolio_distribution_hash
        != changed_quality.portfolio_distribution_hash
    )
    assert first.fact_registry_hash != changed_quality.fact_registry_hash


def _recursive_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key
            for item in value.values()
            for key in _recursive_keys(item)
        }
    if isinstance(value, list):
        return {
            key for item in value for key in _recursive_keys(item)
        }
    return set()
