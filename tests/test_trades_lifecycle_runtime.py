from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
from threading import Event
from typing import Any
import uuid

import pytest

from src.application.ledger.api import (
    LegacySettlementSemanticUnavailable,
    SettlementAdmissionStateIncoherent,
    SettlementSemanticUnavailable,
    attach_settlement_semantics,
    lifecycle_attempt_diagnostic_sha256,
    record_lifecycle_attempt_audit_atomically,
)
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades.inbox import (
    SettlementAttemptClaimOwnershipLost,
    claim_settlement_attempt,
    claim_settlement_provider_batch,
    enqueue_trade_payload,
    finish_settlement_attempt_provider_invocation,
    get_settlement_attempt_state,
    mark_settlement_attempt_provider_started,
    reserve_settlement_attempt_invocation,
    upsert_settlement_attempt_state,
)
from src.application.trades.lifecycle_runtime import (
    _registry_contract_metadata,
)
from src.application.trades.settlement_attempts import (
    SettlementAttemptOutcome,
    SettlementCapabilitySnapshot,
    SettlementCollectorContract,
    case_scope_fingerprint,
    prepare_provider_required_state,
    provider_input_scope_fingerprint,
)


@pytest.mark.parametrize(
    ("code", "symbol", "market"),
    [
        ("HK.TCH260731P440000", "0700.HK", "HK"),
        ("HK.POP260828P145000", "9992.HK", "HK"),
        ("US.NVDA260821P100000", "NVDA", "US"),
    ],
)
def test_registry_contract_metadata_compares_canonical_underlier_identity(
    code: str,
    symbol: str,
    market: str,
) -> None:
    metadata = _registry_contract_metadata(
        {"code": code},
        lifecycle_case={"symbol": symbol},
    )

    assert metadata["market"] == market
    assert metadata["contract_class"] == "standard_equity_option"


def test_registry_contract_metadata_rejects_real_underlier_conflict() -> None:
    with pytest.raises(
        ValueError,
        match="broker option code conflicts with lifecycle contract",
    ):
        _registry_contract_metadata(
            {"code": "HK.TCH260731P440000"},
            lifecycle_case={"symbol": "9992.HK"},
        )


def test_due_reconciliation_keeps_complete_source_account_id_set(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    captured: dict = {}

    def build_collector(**kwargs):
        captured.update(kwargs)
        return _Collector(supported=False)

    _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )

    monkeypatch.setattr(mod, "build_settlement_observation_collector", build_collector)

    mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source={
            **_runtime_source(tmp_path),
            "futu_account_ids": ["1001", "1002"],
        },
        broker_gateway=object(),
        quote_gateway=object(),
        now_ms=1_000,
        apply_changes=True,
    )

    assert captured["futu_account_ids"] == ["1001", "1002"]


def test_due_reconciliation_preview_does_not_build_collector(monkeypatch) -> None:
    import src.application.trades.lifecycle_runtime as mod

    captured: dict = {}

    def reconcile(_repo, **kwargs):
        captured.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(
        mod,
        "build_settlement_observation_collector",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError(f"collector built during preview: {kwargs}")
        ),
    )
    monkeypatch.setattr(mod, "reconcile_due_lifecycle_cases", reconcile)

    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source={"account": "lx", "futu_account_ids": ["1001", "1002"]},
        broker_gateway=object(),
        quote_gateway=object(),
        now_ms=123,
        apply_changes=False,
    )

    assert result == {"status": "ok"}
    assert captured["account"] == "lx"
    assert captured["observation_collector"] is None


def test_applied_due_requires_seal_sink_before_provider() -> None:
    import src.application.trades.lifecycle_runtime as mod

    factory_calls = 0

    def collector_factory():
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("collector must not be built without a seal sink")

    with pytest.raises(ValueError, match="requires a seal sink"):
        mod.reconcile_due_lifecycle_cases_for_source(
            object(),
            source={"account": "lx", "futu_account_ids": ["1001"]},
            now_ms=1_000,
            apply_changes=True,
            settlement_collector_factory=collector_factory,
        )

    assert factory_calls == 0


def test_timing_after_intake_propagates_wheel_start_policy(monkeypatch) -> None:
    import src.application.trades.lifecycle_runtime as mod

    captured: dict[str, Any] = {}

    class Repo:
        def get_trade_lifecycle_timing_policy(self, _case_id: str) -> dict[str, Any]:
            return {"policy_schema": "lifecycle_timing_policy.v1"}

    def reconcile(_repo: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "applied"}

    monkeypatch.setattr(mod, "reconcile_lifecycle_close_reason", reconcile)

    result = mod.ensure_lifecycle_timing_after_intake(
        Repo(),
        payload={},
        result={
            "diagnostics": {
                "lifecycle_adoption": {
                    "lifecycle_case": {
                        "case_id": "case-wheel-start",
                        "status": "waiting_settlement_evidence",
                    }
                }
            }
        },
        now_ms=1_000,
        apply_changes=True,
        wheel_start_enabled=True,
    )

    assert result is not None
    assert captured["case_id"] == "case-wheel-start"
    assert captured["wheel_start_enabled"] is True


def _candidate(case_id: str) -> dict:
    return {
        "lifecycle_case": {
            "schema_version": "lifecycle_case.v2",
            "case_id": case_id,
            "account": "lx",
            "futu_account_id": "1001",
            "status": "waiting_settlement_evidence",
            "contract_key": f"contract:{case_id}",
            "target_contracts_by_lot": {f"lot:{case_id}": 1},
            "observation_start_ms": 100,
            "pending_until_ms": 200,
            "derived_summary": {"reason_state": "cause_pending"},
        },
        "case_updated_at_ms": 150,
        "timing_policy": {
            "policy_schema": "lifecycle_timing_policy.v1",
            "settlement_deadline_ms": 200,
            "calendar_hash": "calendar-1",
        },
        "evidence_revision": 1,
    }


def _read_model(case_id: str) -> dict:
    return {
        "lifecycle_case_id": case_id,
        "reason_state": "cause_pending",
        "pairing_until_ms": 180,
        "pending_until_ms": 200,
        "first_option_close_received_at_ms": 110,
        "remaining_contracts_by_lot": {f"lot:{case_id}": 1},
        "reserved_contracts_by_lot": {f"lot:{case_id}": 1},
        "reservation_evidence_ids": [f"anchor:{case_id}"],
        "terminal_event_ids": [],
        "timing_policy_hash": "timing-1",
        "lifecycle_generation_token": f"generation:{case_id}",
    }


def _observation(case_id: str) -> dict:
    return attach_settlement_semantics(
        {
            "schema_version": "broker_settlement_observation.v2",
            "case_id": case_id,
            "account": "lx",
            "futu_account_id": "1001",
            "market": "US",
            "contract_identity": {
                "symbol": "NVDA",
                "option_contract_code": "US.NVDA260821P100000",
                "option_type": "put",
                "position_side": "short",
                "strike": "100.00",
                "expiration_ymd": "2026-08-21",
                "multiplier": 100,
            },
            "target_contracts_by_lot": {f"lot:{case_id}": 1},
            "frozen_preterminal_remaining_by_lot": {
                f"lot:{case_id}": 1
            },
            "anchor_option_deal_key": f"anchor:{case_id}",
            "anchor_execution_time_ms": 1_000,
            "observed_at_ms": 2_000,
            "settlement_deadline_ms": 1_500,
            "required_sources": ["anchor_option_close"],
            "source_receipts": {
                "anchor_option_close": {
                    "status": "complete",
                    "coverage_complete": True,
                    "pagination_complete": True,
                    "rows": [],
                }
            },
            "stock_settlement_candidates": [],
            "broker_option_position_absent": False,
            "projection_matches_frozen_remaining": True,
            "reservation_exclusive": True,
            "competing_effective_consumption": False,
            "stock_settlement_present": False,
            "normal_order_present": False,
            "complete": False,
            "incomplete_reason_codes": [
                "broker_option_position_present"
            ],
        },
        evidence_kind="expire_close",
    )


class _Collector:
    def __init__(self, *, supported: bool, outcome_kind: str = "unknown_error") -> None:
        capability_state = "supported" if supported else "missing_static"
        self.contract = SettlementCollectorContract(
            required_capability_keys=("synthetic.capability",)
        )
        self.capability = SettlementCapabilitySnapshot(
            contract_version=self.contract.contract_version,
            gateway_adapter_version="test-adapter.v1",
            provider_sdk_version="test-sdk.v1",
            capability_fingerprint=(
                f"capability:{capability_state}"
            ),
            capabilities={"synthetic.capability": capability_state},
        )
        self.outcome_kind = outcome_kind
        self.calls = 0

    def collect_outcome(
        self,
        lifecycle_case: dict,
        read_model: dict,
        *,
        before_first_provider_io=None,
    ) -> SettlementAttemptOutcome:
        if before_first_provider_io is not None:
            before_first_provider_io()
        self.calls += 1
        return SettlementAttemptOutcome(
            kind=self.outcome_kind,
            source_id="lx",
            account="lx",
            case_id=str(lifecycle_case["case_id"]),
            contract_version=self.contract.contract_version,
            capability_fingerprint=self.capability.capability_fingerprint,
            reason_code="test_outcome",
            error_class="unknown",
        )


def _patch_due_planner(monkeypatch, *, candidates: list[dict]) -> dict[str, Any]:
    import src.application.trades.lifecycle_runtime as mod

    counts: dict[str, Any] = {
        "account_reads": 0,
        "reconciliations": 0,
        "control_now_ms": 1_000,
        "seals": [],
    }
    candidate_by_id = {
        str(item["lifecycle_case"]["case_id"]): item
        for item in candidates
    }

    monkeypatch.setattr(
        mod,
        "list_trade_lifecycle_due_candidates",
        lambda *_args, **_kwargs: list(candidate_by_id.values()),
    )

    def read_models(*_args, **_kwargs):
        counts["account_reads"] += 1
        return {
            case_id: _read_model(case_id)
            for case_id in candidate_by_id
        }

    def reconcile(*_args, **kwargs):
        counts["reconciliations"] += 1
        assert kwargs["observation_collector"] is None
        selected = list(kwargs.get("case_ids") or candidate_by_id)
        results = []
        for case_id in selected:
            if case_id.startswith("local"):
                results.append(
                    {
                        "case_id": case_id,
                        "status": "needs_review",
                        "lifecycle_read_model": _read_model(case_id),
                    }
                )
            else:
                results.append(
                    {
                        "case_id": case_id,
                        "status": "observation_required",
                    }
                )
        return {
            "schema_version": "due_lifecycle_reconciliation.v2",
            "account": "lx",
            "now_ms": kwargs["now_ms"],
            "apply_changes": kwargs["apply_changes"],
            "case_count": len(results),
            "results": results,
        }

    monkeypatch.setattr(
        mod,
        "lifecycle_case_read_models_for_account",
        read_models,
    )
    monkeypatch.setattr(mod, "reconcile_due_lifecycle_cases", reconcile)
    monkeypatch.setattr(
        mod,
        "_settlement_control_wall_clock_ms",
        lambda: counts["control_now_ms"],
    )
    monkeypatch.setattr(
        mod,
        "record_lifecycle_attempt_audit_atomically",
        lambda repo, *, attempt_audit: repo.record_attempt(
            attempt_audit
        ),
    )
    reconcile_runtime = mod.reconcile_due_lifecycle_cases_for_source

    def reconcile_with_test_sink(*args, **kwargs):
        if kwargs.get("apply_changes"):
            kwargs.setdefault("seal_sink", counts["seals"].append)
        return reconcile_runtime(*args, **kwargs)

    monkeypatch.setattr(
        mod,
        "reconcile_due_lifecycle_cases_for_source",
        reconcile_with_test_sink,
    )
    return counts


def _runtime_source(tmp_path: Path, *, enabled: bool = True) -> dict:
    return {
        "id": "lx",
        "account": "lx",
        "futu_account_ids": ["1001"],
        "state_path": tmp_path / "state.json",
        "inbox_path": tmp_path / "inbox.sqlite3",
        "settlement_observation": {"enabled": enabled},
    }


def _expired_runtime_invocation(
    path: Path,
    *,
    collector: _Collector,
    finished: bool,
) -> dict:
    candidate = _candidate("provider-1")
    read_model = _read_model("provider-1")
    lifecycle_case = dict(candidate["lifecycle_case"])
    state = prepare_provider_required_state(
        None,
        source_id="lx",
        account="lx",
        case_id="provider-1",
        case_scope_fingerprint_value=case_scope_fingerprint(candidate),
        provider_input_scope_fingerprint_value=(
            provider_input_scope_fingerprint(
                lifecycle_case=lifecycle_case,
                read_model=read_model,
            )
        ),
        contract_version=collector.contract.contract_version,
        capability_fingerprint=(
            collector.capability.capability_fingerprint
        ),
        now_ms=1_000,
    )
    upsert_settlement_attempt_state(path, state=state)
    reserved = reserve_settlement_attempt_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="provider-1",
        case_scope_fingerprint=str(state["case_scope_fingerprint"]),
        claim_id="claim-1",
        now_ms=1_000,
        lease_ms=120_000,
    )
    assert reserved is not None
    started = mark_settlement_attempt_provider_started(
        path,
        source_id="lx",
        account="lx",
        case_id="provider-1",
        claim_id="claim-1",
        invocation_id=str(reserved["invocation_id"]),
        attempted_at_ms=1_500,
    )
    if not finished:
        return started
    outcome = SettlementAttemptOutcome(
        kind="unknown_error",
        source_id="lx",
        account="lx",
        case_id="provider-1",
        contract_version=collector.contract.contract_version,
        capability_fingerprint=(
            collector.capability.capability_fingerprint
        ),
        reason_code="provider_failed",
        error_class="ProviderError",
    )
    return finish_settlement_attempt_provider_invocation(
        path,
        source_id="lx",
        account="lx",
        case_id="provider-1",
        claim_id="claim-1",
        invocation_id=str(started["invocation_id"]),
        outcome=outcome,
        outcome_code=4,
        semantic_fingerprint=None,
        receipt_sha256=None,
        diagnostic_sha256=lifecycle_attempt_diagnostic_sha256(
            reason_code=outcome.reason_code,
            provider_code=outcome.provider_code,
            error_class=outcome.error_class,
        ),
        control_now_ms=2_000,
    )


def _runtime_audit(state: dict) -> dict:
    invocation = uuid.UUID(str(state["invocation_id"])).bytes
    return {
        "account": "lx",
        "case_id": "provider-1",
        "invocation_id": invocation,
        "attempted_at_ms": 1_500,
        "outcome_code": state["pending_outcome_code"],
        "semantic_fingerprint": state["pending_semantic_fingerprint"],
        "receipt_sha256": state["pending_receipt_sha256"],
        "diagnostic_sha256": state["pending_diagnostic_sha256"],
        "span_ordinal": None,
        "ordinal": 1,
        "last_ordinal": 1,
        "last_invocation_id": invocation,
        "chain_sha256": b"c" * 32,
    }


class _RuntimeAuditRepo:
    def __init__(self, audit: dict | None = None) -> None:
        self.audits: dict[tuple[str, str], dict] = {}
        self.fallback_audit = audit
        if audit is not None and "invocation_id" in audit:
            invocation_id = str(
                uuid.UUID(bytes=audit["invocation_id"])
            )
            self.audits[(str(audit["case_id"]), invocation_id)] = audit
        self.lookups: list[tuple[str, str]] = []
        self.writes: list[object] = []

    def record_attempt(self, envelope) -> dict:
        self.writes.append(envelope)
        invocation_id = str(uuid.UUID(bytes=envelope.invocation_id))
        ordinal = sum(
            1
            for case_id, _invocation_id in self.audits
            if case_id == envelope.case_id
        ) + 1
        chain = hashlib.sha256(
            envelope.invocation_id + ordinal.to_bytes(8, "big")
        ).digest()
        audit = {
            "account": "lx",
            "case_id": envelope.case_id,
            "invocation_id": envelope.invocation_id,
            "attempted_at_ms": envelope.attempted_at_ms,
            "outcome_code": envelope.outcome_code,
            "semantic_fingerprint": envelope.semantic_fingerprint,
            "receipt_sha256": envelope.receipt_sha256,
            "diagnostic_sha256": envelope.diagnostic_sha256,
            "span_ordinal": ordinal if envelope.outcome_code in {1, 2} else None,
            "ordinal": ordinal,
            "last_ordinal": ordinal,
            "last_invocation_id": envelope.invocation_id,
            "chain_sha256": chain,
        }
        self.audits[(envelope.case_id, invocation_id)] = audit
        return {
            "audit_ordinal": ordinal,
            "audit_chain_sha256": chain.hex(),
        }

    def get_trade_lifecycle_attempt_audit_by_invocation(
        self,
        *,
        case_id: str,
        invocation_id: str,
    ) -> dict | None:
        self.lookups.append((case_id, invocation_id))
        return self.audits.get(
            (case_id, str(uuid.UUID(str(invocation_id))))
        ) or self.fallback_audit


@pytest.mark.parametrize(
    (
        "finished",
        "audit_mode",
        "control_now_ms",
        "expected_state",
        "expected_lookup_count",
    ),
    [
        (True, "exact", 200_000, "ledger_committed", 1),
        (True, "none", 200_000, "ambiguous_provider_result", 1),
        (False, "none", 200_000, "ambiguous_provider_result", 1),
        (False, "present", 200_000, "ambiguous_provider_result", 1),
        (True, "exact", 1_000, "provider_finished", 0),
    ],
)
def test_runtime_recovers_only_expired_invocations_before_provider(
    tmp_path: Path,
    monkeypatch,
    finished: bool,
    audit_mode: str,
    control_now_ms: int,
    expected_state: str,
    expected_lookup_count: int,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    counts["control_now_ms"] = control_now_ms
    source = _runtime_source(tmp_path)
    collector = _Collector(supported=True)
    state = _expired_runtime_invocation(
        source["inbox_path"],
        collector=collector,
        finished=finished,
    )
    audit = (
        _runtime_audit(state)
        if audit_mode == "exact"
        else ({"case_id": "wrong-case"} if audit_mode == "present" else None)
    )
    repo = _RuntimeAuditRepo(audit)

    result = mod.reconcile_due_lifecycle_cases_for_source(
        repo,
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )

    stored = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )
    assert stored is not None
    assert stored["invocation_state"] == expected_state
    assert len(repo.lookups) == expected_lookup_count
    assert collector.calls == 0
    assert result["provider_attempt_count"] == 0
    assert len(counts["seals"]) == (
        1 if expected_state == "ledger_committed" else 0
    )


def test_runtime_scopes_control_reads_to_current_candidates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    captured: dict[str, tuple[str, ...] | str] = {}

    def list_states(*_args, **kwargs):
        captured["list_account"] = str(kwargs["account"])
        captured["list_case_ids"] = tuple(kwargs["case_ids"])
        return {}

    def summarize(*_args, **kwargs):
        captured["summary_account"] = str(kwargs["account"])
        captured["summary_case_ids"] = tuple(kwargs["case_ids"])
        return {
            "source_id": "lx",
            "provider_required_count": 1,
            "blocked_count": 1,
            "disabled_count": 0,
            "backoff_count": 0,
            "claimed_count": 0,
            "eligible_count": 0,
            "earliest_next_attempt_at_ms": None,
            "last_state_change": None,
        }

    monkeypatch.setattr(mod, "list_settlement_attempt_states", list_states)
    monkeypatch.setattr(mod, "settlement_attempt_summary", summarize)
    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=_runtime_source(tmp_path),
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=_Collector(supported=False),
    )

    assert result["provider_attempt_count"] == 0
    assert captured == {
        "list_account": "lx",
        "list_case_ids": ("provider-1",),
        "summary_account": "lx",
        "summary_case_ids": ("provider-1",),
    }


def test_static_block_is_cached_before_account_wide_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    collector = _Collector(supported=False)
    source = _runtime_source(tmp_path)

    first = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    later = first
    for tick in range(1, 11):
        later = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
            source=source,
            now_ms=1_000 + tick * 60_000,
            apply_changes=True,
            settlement_collector=collector,
        )

    assert first["planned_case_count"] == 1
    assert counts["account_reads"] == 1
    assert collector.calls == 0
    assert later["planned_case_count"] == 0
    assert later["skipped_counts"]["blocked"] == 1
    assert later["control_summary"]["blocked_count"] == 1
    assert later["control_summary"]["last_state_change"] == {
        "case_id": "provider-1",
        "outcome_kind": "blocked_static",
        "reason_code": "missing_static_capability",
        "provider_code": None,
        "error_class": "missing_static",
        "updated_at_ms": 1_000,
    }
    state = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )
    assert state is not None
    assert state["updated_at_ms"] == 1_000


def test_disabled_provider_branch_keeps_local_due_planning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[
            _candidate("local-1"),
            _candidate("provider-1"),
        ],
    )
    collector = _Collector(supported=True)

    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=_runtime_source(tmp_path, enabled=False),
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )

    assert counts["account_reads"] == 1
    assert collector.calls == 0
    assert result["local_reconciliation"]["case_count"] == 2
    assert result["skipped_counts"]["disabled"] == 1
    assert result["control_summary"]["disabled_count"] == 1


@pytest.mark.parametrize(
    ("case_id", "enabled"),
    [("local-1", True), ("provider-1", False)],
)
def test_local_or_disabled_branch_does_not_construct_collector(
    tmp_path: Path,
    monkeypatch,
    case_id: str,
    enabled: bool,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[_candidate(case_id)],
    )
    factory_calls = 0

    def collector_factory():
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("collector must remain lazy")

    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=_runtime_source(tmp_path, enabled=enabled),
        now_ms=1_000,
        apply_changes=True,
        settlement_collector_factory=collector_factory,
    )

    assert factory_calls == 0
    assert counts["account_reads"] == 1
    assert result["provider_attempt_count"] == 0
    assert result["capability"]["inspection_status"] == "not_required"


def test_unknown_error_uses_bounded_backoff_without_replanning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    collector = _Collector(supported=True, outcome_kind="unknown_error")
    source = _runtime_source(tmp_path)

    first = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    counts["control_now_ms"] = 61_000
    second = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=61_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    counts["control_now_ms"] = 301_000
    third = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=301_000,
        apply_changes=True,
        settlement_collector=collector,
    )

    assert first["provider_results"][0]["outcome"]["kind"] == "unknown_error"
    assert second["skipped_counts"]["backoff"] == 1
    assert collector.calls == 2
    assert counts["account_reads"] == 3
    assert third["control_summary"]["backoff_count"] == 1


def test_provider_failure_commits_real_audit_before_inbox_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    monkeypatch.setattr(
        mod,
        "record_lifecycle_attempt_audit_atomically",
        record_lifecycle_attempt_audit_atomically,
    )
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    repo.upsert_trade_lifecycle_case(
        {
            "case_id": "provider-1",
            "case_key": "provider-1",
            "account": "lx",
            "broker": "futu",
            "symbol": "NVDA",
            "status": "waiting_settlement_evidence",
        }
    )
    source = _runtime_source(tmp_path)

    result = mod.reconcile_due_lifecycle_cases_for_source(
        repo,
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=_Collector(supported=True),
    )
    state = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )
    assert state is not None
    audit = repo.get_trade_lifecycle_attempt_audit_by_invocation(
        case_id="provider-1",
        invocation_id=str(state["invocation_id"]),
    )

    assert result["control_status"] == "ok"
    assert result["provider_attempt_count"] == 1
    assert audit is not None
    assert audit["outcome_code"] == 4
    assert audit["ordinal"] == 1
    assert state["invocation_state"] == "ledger_committed"
    assert state["committed_audit_ordinal"] == audit["ordinal"]
    assert state["committed_chain_sha256"] == audit["chain_sha256"]
    assert result["seal_status"] == "sealed"
    assert result["run_seal"] == counts["seals"][0]
    assert result["run_seal"]["head_count"] == 1
    assert result["provider_results"][0]["audit_ordinal"] == 1


def test_seal_failure_does_not_recall_provider_or_reseal_stable_head(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    repo = _RuntimeAuditRepo()
    collector = _Collector(supported=True)
    source = _runtime_source(tmp_path)

    def fail_seal(_seal):
        raise OSError("disk full")

    failed = mod.reconcile_due_lifecycle_cases_for_source(
        repo,
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
        seal_sink=fail_seal,
    )
    later_seals: list[dict] = []
    second = mod.reconcile_due_lifecycle_cases_for_source(
        repo,
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
        seal_sink=later_seals.append,
    )
    third = mod.reconcile_due_lifecycle_cases_for_source(
        repo,
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
        seal_sink=later_seals.append,
    )

    assert failed["seal_status"] == "seal_persist_failed"
    assert failed["seal_error_class"] == "OSError"
    assert second["seal_status"] == "not_required"
    assert third["seal_status"] == "not_required"
    assert second["provider_attempt_count"] == 0
    assert third["provider_attempt_count"] == 0
    assert later_seals == []
    assert collector.calls == 1
    assert len(repo.writes) == 1


def test_control_clock_is_independent_from_business_observation_time(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    source = _runtime_source(tmp_path)
    observed_claim_until: list[int] = []

    class _ClaimInspectingCollector(_Collector):
        def collect_outcome(
            self,
            lifecycle_case: dict,
            read_model: dict,
            *,
            before_first_provider_io=None,
        ) -> SettlementAttemptOutcome:
            if before_first_provider_io is not None:
                before_first_provider_io()
            state = get_settlement_attempt_state(
                source["inbox_path"],
                source_id="lx",
                account="lx",
                case_id="provider-1",
            )
            assert state is not None
            observed_claim_until.append(int(state["claim_until_ms"]))
            return super().collect_outcome(
                lifecycle_case,
                read_model,
            )

    collector = _ClaimInspectingCollector(supported=True)
    first = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=9_000_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    counts["control_now_ms"] = 61_000
    future_business_tick = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=99_000_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    counts["control_now_ms"] = 301_000
    historical_business_tick = (
        mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
            source=source,
            now_ms=1,
            apply_changes=True,
            settlement_collector=collector,
        )
    )

    assert len(observed_claim_until) == 2
    assert 121_000 <= observed_claim_until[0] < 122_000
    assert 421_000 <= observed_claim_until[1] < 422_000
    assert first["provider_attempt_count"] == 1
    assert future_business_tick["skipped_counts"]["backoff"] == 1
    assert historical_business_tick["provider_attempt_count"] == 1
    state = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )
    assert state is not None
    assert state["last_attempt_at_ms"] == 301_000


def test_each_provider_case_is_claimed_immediately_before_its_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    _patch_due_planner(
        monkeypatch,
        candidates=[
            _candidate("provider-1"),
            _candidate("provider-2"),
        ],
    )
    source = _runtime_source(tmp_path)
    clock_ms = 1_000
    competing_claim_acquired = False

    class _SlowFirstCollector(_Collector):
        def __init__(self) -> None:
            super().__init__(supported=True)
            self.case_calls: list[str] = []
            self.now_ms_fn = lambda: clock_ms

        def collect_outcome(
            self,
            lifecycle_case: dict,
            read_model: dict,
            *,
            before_first_provider_io=None,
        ) -> SettlementAttemptOutcome:
            if before_first_provider_io is not None:
                before_first_provider_io()
            nonlocal clock_ms, competing_claim_acquired
            case_id = str(lifecycle_case["case_id"])
            self.case_calls.append(case_id)
            if case_id == "provider-1":
                clock_ms = 122_000
                second_state = get_settlement_attempt_state(
                    source["inbox_path"],
                    source_id="lx",
                    account="lx",
                    case_id="provider-2",
                )
                assert second_state is not None
                competing_claim_acquired = claim_settlement_attempt(
                    source["inbox_path"],
                    source_id="lx",
                    account="lx",
                    case_id="provider-2",
                    case_scope_fingerprint=str(
                        second_state["case_scope_fingerprint"]
                    ),
                    claim_id="competing-worker",
                    now_ms=clock_ms,
                    lease_ms=120_000,
                )
            return super().collect_outcome(
                lifecycle_case,
                read_model,
            )

    collector = _SlowFirstCollector()
    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    assert competing_claim_acquired is True
    assert collector.case_calls == ["provider-1"]
    assert result["provider_claim_count"] == 1
    assert result["provider_attempt_count"] == 1
    assert result["skipped_counts"]["claimed"] == 1


def test_active_batch_leader_blocks_fallthrough_account_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[
            _candidate("provider-1"),
            _candidate("provider-2"),
        ],
    )
    source = _runtime_source(tmp_path)
    collector = _Collector(supported=True)
    initial = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    reads_before_overlap = counts["account_reads"]
    calls_before_overlap = collector.calls
    leader_state = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )
    assert initial["provider_attempt_count"] == 2
    assert leader_state is not None

    counts["control_now_ms"] = 301_000
    assert reserve_settlement_attempt_invocation(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
        case_scope_fingerprint=str(
            leader_state["case_scope_fingerprint"]
        ),
        claim_id="overlapping-worker",
        now_ms=301_000,
        lease_ms=120_000,
    )
    overlapped = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=301_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    second_state = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-2",
    )

    assert overlapped["provider_attempt_count"] == 0
    assert overlapped["skipped_counts"]["claimed"] == 1
    assert counts["account_reads"] == reads_before_overlap
    assert collector.calls == calls_before_overlap
    assert second_state is not None
    assert second_state["claim_id"] is None


def test_failed_batch_leader_claim_does_not_fall_through_to_next_case(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[
            _candidate("provider-1"),
            _candidate("provider-2"),
        ],
    )
    source = _runtime_source(tmp_path)
    collector = _Collector(supported=True)
    mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    reads_before_overlap = counts["account_reads"]
    calls_before_overlap = collector.calls
    attempted_claims: list[str] = []

    def reject_leader_claim(*_args, **kwargs):
        attempted_claims.append(str(kwargs["case_id"]))
        return None

    counts["control_now_ms"] = 301_000
    monkeypatch.setattr(
        mod,
        "reserve_settlement_attempt_invocation",
        reject_leader_claim,
    )
    overlapped = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=301_000,
        apply_changes=True,
        settlement_collector=collector,
    )

    assert attempted_claims == ["provider-1"]
    assert overlapped["provider_attempt_count"] == 0
    assert overlapped["skipped_counts"]["claimed"] == 1
    assert counts["account_reads"] == reads_before_overlap
    assert collector.calls == calls_before_overlap


def test_batch_lease_survives_leader_completion_until_all_cases_finish(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[
            _candidate("provider-1"),
            _candidate("provider-2"),
        ],
    )
    source = _runtime_source(tmp_path)
    outer_collector = _Collector(supported=True)
    overlapping_collector = _Collector(supported=True)
    original_reconcile = mod.reconcile_settlement_attempt_invocation
    overlap_started = False
    overlap_result: dict | None = None

    def reconcile_then_overlap(*args, **kwargs):
        nonlocal overlap_started, overlap_result
        completed = original_reconcile(*args, **kwargs)
        if kwargs["case_id"] == "provider-1" and not overlap_started:
            overlap_started = True
            overlap_result = mod.reconcile_due_lifecycle_cases_for_source(
                _RuntimeAuditRepo(),
                source=source,
                now_ms=1_000,
                apply_changes=True,
                settlement_collector=overlapping_collector,
            )
        return completed

    monkeypatch.setattr(
        mod,
        "reconcile_settlement_attempt_invocation",
        reconcile_then_overlap,
    )
    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=outer_collector,
    )

    assert overlap_started is True
    assert overlap_result is not None
    assert overlap_result["provider_attempt_count"] == 0
    assert overlap_result["skipped_counts"]["claimed"] == 1
    assert overlapping_collector.calls == 0
    assert result["provider_attempt_count"] == 2
    assert outer_collector.calls == 2
    assert counts["account_reads"] == 2


def test_running_provider_batch_renews_before_expiry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    source = _runtime_source(tmp_path)
    monotonic_seconds = 0.0
    batch_renewed_after_original_expiry = Event()
    competing_batch_acquired = False
    original_batch_renew = mod.renew_settlement_provider_batch_claim

    def observed_batch_renew(*args, **kwargs):
        renewed = original_batch_renew(*args, **kwargs)
        if renewed and int(kwargs["now_ms"]) >= 122_000:
            batch_renewed_after_original_expiry.set()
        return renewed

    monkeypatch.setattr(
        mod,
        "_SETTLEMENT_CLAIM_RENEW_INTERVAL_SEC",
        0.001,
    )
    monkeypatch.setattr(
        mod,
        "_SETTLEMENT_CLAIM_MONOTONIC_FN",
        lambda: monotonic_seconds,
    )
    monkeypatch.setattr(
        mod,
        "renew_settlement_provider_batch_claim",
        observed_batch_renew,
    )

    class _SlowCollector(_Collector):
        def collect_outcome(
            self,
            lifecycle_case: dict,
            read_model: dict,
            *,
            before_first_provider_io=None,
        ) -> SettlementAttemptOutcome:
            if before_first_provider_io is not None:
                before_first_provider_io()
            nonlocal monotonic_seconds, competing_batch_acquired
            monotonic_seconds = 121.0
            assert batch_renewed_after_original_expiry.wait(timeout=2)
            competing_batch_acquired = claim_settlement_provider_batch(
                source["inbox_path"],
                source_id="lx",
                account="lx",
                claim_id="competing-batch",
                now_ms=122_000,
                lease_ms=120_000,
            )
            return super().collect_outcome(lifecycle_case, read_model)

    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=_SlowCollector(supported=True),
    )

    assert competing_batch_acquired is False
    assert result["control_status"] == "ok"
    assert result["provider_attempt_count"] == 1


def test_batch_release_ownership_loss_does_not_delete_new_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    source = _runtime_source(tmp_path)
    original_release = mod.release_settlement_provider_batch_claim
    ownership_changed = False

    def steal_then_release(*args, **kwargs):
        nonlocal ownership_changed
        if not ownership_changed:
            assert claim_settlement_provider_batch(
                source["inbox_path"],
                source_id="lx",
                account="lx",
                claim_id="competing-batch",
                now_ms=200_000,
                lease_ms=120_000,
            )
            ownership_changed = True
        return original_release(*args, **kwargs)

    monkeypatch.setattr(
        mod,
        "release_settlement_provider_batch_claim",
        steal_then_release,
    )
    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=_Collector(supported=True),
    )

    assert ownership_changed is True
    assert result["control_status"] == "claim_ownership_lost"
    assert result["provider_attempt_count"] == 1
    assert not claim_settlement_provider_batch(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        claim_id="probe-batch",
        now_ms=200_001,
        lease_ms=120_000,
    )


def test_preparation_guard_renews_leader_before_snapshot_handoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    source = _runtime_source(tmp_path)
    monotonic_seconds = 0.0
    renewed_after_original_expiry = Event()
    competing_claim_acquired = False
    claim_until_at_provider = 0
    original_read = mod.lifecycle_case_read_models_for_account
    original_renew = mod.renew_settlement_attempt_claim

    def observed_renew(*args, **kwargs):
        renewed = original_renew(*args, **kwargs)
        if renewed and int(kwargs["now_ms"]) >= 122_000:
            renewed_after_original_expiry.set()
        return renewed

    def slow_provider_preparation(*args, **kwargs):
        nonlocal monotonic_seconds, competing_claim_acquired
        read_models = original_read(*args, **kwargs)
        if counts["account_reads"] == 2:
            monotonic_seconds = 121.0
            assert renewed_after_original_expiry.wait(timeout=2)
            current = get_settlement_attempt_state(
                source["inbox_path"],
                source_id="lx",
                account="lx",
                case_id="provider-1",
            )
            assert current is not None
            competing_claim_acquired = claim_settlement_attempt(
                source["inbox_path"],
                source_id="lx",
                account="lx",
                case_id="provider-1",
                case_scope_fingerprint=str(
                    current["case_scope_fingerprint"]
                ),
                claim_id="competing-worker",
                now_ms=122_000,
                lease_ms=120_000,
            )
        return read_models

    monkeypatch.setattr(
        mod,
        "_SETTLEMENT_CLAIM_RENEW_INTERVAL_SEC",
        0.001,
    )
    monkeypatch.setattr(
        mod,
        "_SETTLEMENT_CLAIM_MONOTONIC_FN",
        lambda: monotonic_seconds,
    )
    monkeypatch.setattr(
        mod,
        "renew_settlement_attempt_claim",
        observed_renew,
    )
    monkeypatch.setattr(
        mod,
        "lifecycle_case_read_models_for_account",
        slow_provider_preparation,
    )

    class _InspectingCollector(_Collector):
        def collect_outcome(
            self,
            lifecycle_case: dict,
            read_model: dict,
            *,
            before_first_provider_io=None,
        ) -> SettlementAttemptOutcome:
            if before_first_provider_io is not None:
                before_first_provider_io()
            nonlocal claim_until_at_provider
            state = get_settlement_attempt_state(
                source["inbox_path"],
                source_id="lx",
                account="lx",
                case_id="provider-1",
            )
            assert state is not None
            claim_until_at_provider = int(state["claim_until_ms"])
            return super().collect_outcome(lifecycle_case, read_model)

    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=_InspectingCollector(supported=True),
    )

    assert competing_claim_acquired is False
    assert claim_until_at_provider >= 242_000
    assert result["control_status"] == "ok"
    assert result["provider_attempt_count"] == 1


def test_preparation_ownership_loss_skips_snapshot_and_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    source = _runtime_source(tmp_path)
    ownership_changed = False
    lost_claim_id: str | None = None

    def lose_before_preparation(*_args, **kwargs):
        nonlocal lost_claim_id, ownership_changed
        if not ownership_changed:
            ownership_changed = True
            lost_claim_id = str(kwargs["claim_id"])
        return False

    monkeypatch.setattr(
        mod,
        "renew_settlement_attempt_claim",
        lose_before_preparation,
    )
    collector = _Collector(supported=True)
    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    state = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )

    assert ownership_changed is True
    assert result["control_status"] == "claim_ownership_lost"
    assert result["provider_attempt_count"] == 0
    assert collector.calls == 0
    assert counts["account_reads"] == 1
    assert state is not None
    assert state["claim_id"] == lost_claim_id
    assert state["invocation_state"] == "reserved"


def test_running_provider_call_with_frozen_clock_renews_before_expiry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    source = _runtime_source(tmp_path)
    collector_clock_ms = 1_000
    monotonic_seconds = 0.0
    renewed_after_original_expiry = Event()
    competing_claim_acquired = False
    original_renew = mod.renew_settlement_attempt_claim

    def observed_renew(*args, **kwargs):
        renewed = original_renew(*args, **kwargs)
        if renewed and int(kwargs["now_ms"]) >= 122_000:
            renewed_after_original_expiry.set()
        return renewed

    monkeypatch.setattr(
        mod,
        "_SETTLEMENT_CLAIM_RENEW_INTERVAL_SEC",
        0.001,
    )
    monkeypatch.setattr(
        mod,
        "_SETTLEMENT_CLAIM_MONOTONIC_FN",
        lambda: monotonic_seconds,
    )
    monkeypatch.setattr(
        mod,
        "renew_settlement_attempt_claim",
        observed_renew,
    )

    class _SlowCollector(_Collector):
        def __init__(self) -> None:
            super().__init__(supported=True)
            self.now_ms_fn = lambda: collector_clock_ms

        def collect_outcome(
            self,
            lifecycle_case: dict,
            read_model: dict,
            *,
            before_first_provider_io=None,
        ) -> SettlementAttemptOutcome:
            if before_first_provider_io is not None:
                before_first_provider_io()
            nonlocal monotonic_seconds, competing_claim_acquired
            monotonic_seconds = 121.0
            assert renewed_after_original_expiry.wait(timeout=2)
            current = get_settlement_attempt_state(
                source["inbox_path"],
                source_id="lx",
                account="lx",
                case_id="provider-1",
            )
            assert current is not None
            competing_claim_acquired = claim_settlement_attempt(
                source["inbox_path"],
                source_id="lx",
                account="lx",
                case_id="provider-1",
                case_scope_fingerprint=str(
                    current["case_scope_fingerprint"]
                ),
                claim_id="competing-worker",
                now_ms=122_000,
                lease_ms=120_000,
            )
            return super().collect_outcome(
                lifecycle_case,
                read_model,
            )

    collector = _SlowCollector()
    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    completed = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )

    assert competing_claim_acquired is False
    assert result["control_status"] == "ok"
    assert result["provider_attempt_count"] == 1
    assert completed is not None
    assert completed["claim_id"] is None


def test_lost_lease_skips_canonical_write_and_reports_typed_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    source = _runtime_source(tmp_path)
    clock_ms = 1_000
    provider_started = Event()
    ownership_changed = Event()
    original_renew = mod.renew_settlement_attempt_claim
    lost_claim_id: str | None = None

    def steal_then_renew(*args, **kwargs):
        nonlocal lost_claim_id
        if provider_started.is_set() and not ownership_changed.is_set():
            lost_claim_id = str(kwargs["claim_id"])
            ownership_changed.set()
            return False
        return original_renew(*args, **kwargs)

    monkeypatch.setattr(
        mod,
        "_SETTLEMENT_CLAIM_RENEW_INTERVAL_SEC",
        0.001,
    )
    monkeypatch.setattr(
        mod,
        "renew_settlement_attempt_claim",
        steal_then_renew,
    )
    monkeypatch.setattr(
        mod,
        "reconcile_lifecycle_close_reason",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("lost claim must not write canonical evidence")
        ),
    )

    class _ObservedCollector(_Collector):
        def __init__(self) -> None:
            super().__init__(supported=True)
            self.now_ms_fn = lambda: clock_ms

        def collect_outcome(
            self,
            lifecycle_case: dict,
            read_model: dict,
            *,
            before_first_provider_io=None,
        ) -> SettlementAttemptOutcome:
            if before_first_provider_io is not None:
                before_first_provider_io()
            nonlocal clock_ms
            clock_ms = 122_000
            provider_started.set()
            assert ownership_changed.wait(timeout=2)
            self.calls += 1
            return SettlementAttemptOutcome(
                kind="observed_incomplete",
                source_id="lx",
                account="lx",
                case_id=str(lifecycle_case["case_id"]),
                contract_version=self.contract.contract_version,
                capability_fingerprint=(
                    self.capability.capability_fingerprint
                ),
                observation=_observation(str(lifecycle_case["case_id"])),
            )

    collector = _ObservedCollector()
    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    current = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )

    assert result["control_status"] == "claim_ownership_lost"
    assert result["control_error_class"] == (
        "SettlementAttemptClaimOwnershipLost"
    )
    assert result["provider_attempt_count"] == 1
    assert result["provider_results"][0]["admission_status"] is None
    assert current is not None
    assert current["claim_id"] == lost_claim_id
    assert current["invocation_state"] == "provider_started"


def test_inbox_reconcile_failure_recovers_exact_without_duplicate_audit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    source = _runtime_source(tmp_path)
    repo = _RuntimeAuditRepo()
    canonical_writes = 0

    class _ObservedCollector(_Collector):
        def collect_outcome(
            self,
            lifecycle_case: dict,
            read_model: dict,
            *,
            before_first_provider_io=None,
        ) -> SettlementAttemptOutcome:
            if before_first_provider_io is not None:
                before_first_provider_io()
            self.calls += 1
            return SettlementAttemptOutcome(
                kind="observed_incomplete",
                source_id="lx",
                account="lx",
                case_id=str(lifecycle_case["case_id"]),
                contract_version=self.contract.contract_version,
                capability_fingerprint=(
                    self.capability.capability_fingerprint
                ),
                reason_code="settlement_observation_incomplete",
                observation=_observation(str(lifecycle_case["case_id"])),
            )

    def committed_write(*_args, **_kwargs):
        nonlocal canonical_writes
        canonical_writes += 1
        _args[0].record_attempt(_kwargs["attempt_audit"])
        return {"admission_status": "admitted_semantic"}

    monkeypatch.setattr(
        mod,
        "reconcile_lifecycle_close_reason",
        committed_write,
    )
    original_reconcile = mod.reconcile_settlement_attempt_invocation
    reconcile_failed = False

    def fail_first_reconcile(*args, **kwargs):
        nonlocal reconcile_failed
        if not reconcile_failed:
            reconcile_failed = True
            raise SettlementAttemptClaimOwnershipLost(
                "claim ownership changed after canonical commit"
            )
        return original_reconcile(*args, **kwargs)

    monkeypatch.setattr(
        mod,
        "reconcile_settlement_attempt_invocation",
        fail_first_reconcile,
    )

    collector = _ObservedCollector(supported=True)
    first = mod.reconcile_due_lifecycle_cases_for_source(
        repo,
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    pending = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )

    counts["control_now_ms"] = 200_000
    restarted = mod.reconcile_due_lifecycle_cases_for_source(
        repo,
        source=source,
        now_ms=200_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    committed = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )

    assert canonical_writes == 1
    assert len(repo.writes) == 1
    assert first["control_status"] == "claim_ownership_lost"
    assert first["control_error_class"] == (
        "SettlementAttemptClaimOwnershipLost"
    )
    assert pending is not None
    assert pending["invocation_state"] == "provider_finished"
    assert restarted["provider_attempt_count"] == 0
    assert committed is not None
    assert committed["invocation_state"] == "ledger_committed"
    assert committed["committed_audit_ordinal"] == 1
    assert collector.calls == 1


def test_terminal_summary_failure_returns_control_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    _patch_due_planner(monkeypatch, candidates=[])
    source = _runtime_source(tmp_path)
    enqueue_trade_payload(
        source["inbox_path"],
        payload={"deal_id": "health-fixture"},
        source="test",
    )
    monkeypatch.setattr(
        mod,
        "settlement_attempt_summary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("summary unavailable")
        ),
    )

    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=_Collector(supported=True),
    )

    assert result["control_status"] == "control_store_unavailable"
    assert result["control_error_class"] == "OperationalError"
    assert result["provider_attempt_count"] == 0


@pytest.mark.parametrize(
    (
        "semantic_error",
        "expected_kind",
        "expected_next_attempt",
        "expected_reason",
        "expected_error_class",
    ),
    [
        (
            SettlementSemanticUnavailable("current payload invalid"),
            "unknown_error",
            301_000,
            "current_semantic_unavailable",
            "semantic_contract",
        ),
        (
            LegacySettlementSemanticUnavailable(
                "legacy_semantic_unavailable"
            ),
            "legacy_semantic_unavailable",
            None,
            "legacy_semantic_unavailable",
            "canonical_evidence_unavailable",
        ),
        (
            SettlementAdmissionStateIncoherent(
                "duplicate canonical state is incomplete"
            ),
            "unknown_error",
            301_000,
            "settlement_admission_state_incoherent",
            "canonical_state",
        ),
    ],
)
def test_runtime_only_permanently_blocks_legacy_semantic_failure(
    tmp_path: Path,
    monkeypatch,
    semantic_error: SettlementSemanticUnavailable,
    expected_kind: str,
    expected_next_attempt: int | None,
    expected_reason: str,
    expected_error_class: str,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )

    class _ObservedCollector(_Collector):
        def collect_outcome(
            self,
            lifecycle_case: dict,
            read_model: dict,
            *,
            before_first_provider_io=None,
        ) -> SettlementAttemptOutcome:
            if before_first_provider_io is not None:
                before_first_provider_io()
            self.calls += 1
            return SettlementAttemptOutcome(
                kind="observed_incomplete",
                source_id="lx",
                account="lx",
                case_id=str(lifecycle_case["case_id"]),
                contract_version=self.contract.contract_version,
                capability_fingerprint=(
                    self.capability.capability_fingerprint
                ),
                reason_code="settlement_observation_incomplete",
                observation=_observation(str(lifecycle_case["case_id"])),
            )

    def fail_semantic_admission(*_args, **_kwargs):
        raise semantic_error

    monkeypatch.setattr(
        mod,
        "reconcile_lifecycle_close_reason",
        fail_semantic_admission,
    )
    replacements: list[dict] = []
    original_replace = (
        mod.replace_finished_settlement_attempt_provider_invocation
    )

    def capture_replacement(*args, **kwargs):
        replacements.append(dict(kwargs))
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(
        mod,
        "replace_finished_settlement_attempt_provider_invocation",
        capture_replacement,
    )
    source = _runtime_source(tmp_path)
    repo = _RuntimeAuditRepo()
    result = mod.reconcile_due_lifecycle_cases_for_source(
        repo,
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=_ObservedCollector(supported=True),
    )
    state = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )

    assert state is not None
    assert result["provider_results"][0]["outcome"]["kind"] == (
        expected_kind
    )
    assert state["outcome_kind"] == expected_kind
    assert state["next_attempt_at_ms"] == expected_next_attempt
    assert state["claim_id"] is None
    assert state["claim_until_ms"] is None
    assert result["provider_attempt_count"] == 1
    assert result["provider_results"][0]["outcome"]["reason_code"] == (
        expected_reason
    )
    assert result["provider_results"][0]["outcome"]["error_class"] == (
        expected_error_class
    )
    expected_audit_kind = (
        "legacy_semantic_unavailable_after_call"
        if expected_kind == "legacy_semantic_unavailable"
        else "processing_failure_after_call"
    )
    assert len(replacements) == 1
    assert replacements[0]["outcome_code"] == (
        8 if expected_kind == "legacy_semantic_unavailable" else 7
    )
    assert replacements[0]["semantic_fingerprint"] is None
    assert replacements[0]["receipt_sha256"] is None
    assert len(repo.writes) == 1
    assert repo.writes[0].outcome_kind == expected_audit_kind
    assert state["invocation_state"] == "ledger_committed"


def test_preclaimed_preparation_failure_completes_claim_without_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    original_read = mod.lifecycle_case_read_models_for_account

    def fail_provider_preparation(*args, **kwargs):
        if counts["account_reads"] >= 1:
            raise RuntimeError("provider preparation failed")
        return original_read(*args, **kwargs)

    monkeypatch.setattr(
        mod,
        "lifecycle_case_read_models_for_account",
        fail_provider_preparation,
    )
    source = _runtime_source(tmp_path)
    collector = _Collector(supported=True)

    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    repeated = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=2_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    state = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )

    assert result["control_status"] == "ok"
    assert result["provider_attempt_count"] == 0
    assert repeated["provider_attempt_count"] == 0
    assert collector.calls == 0
    assert counts["account_reads"] == 1
    assert state is not None
    assert state["outcome_kind"] == "unknown_error"
    assert state["reason_code"] == "settlement_attempt_preparation_failed"
    assert state["error_class"] == "RuntimeError"
    assert state["attempt_count"] == 0
    assert state["next_attempt_at_ms"] == 301_000
    assert state["claim_id"] is None
    assert state["claim_until_ms"] is None


def test_collector_contract_exception_completes_claim_with_backoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )

    class _FailingCollector(_Collector):
        def collect_outcome(
            self,
            lifecycle_case: dict,
            read_model: dict,
            *,
            before_first_provider_io=None,
        ) -> SettlementAttemptOutcome:
            if before_first_provider_io is not None:
                before_first_provider_io()
            self.calls += 1
            raise TypeError("collector contract failure")

    source = _runtime_source(tmp_path)
    collector = _FailingCollector(supported=True)
    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    state = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )

    assert result["control_status"] == "ok"
    assert result["provider_attempt_count"] == 1
    assert collector.calls == 1
    assert state is not None
    assert state["outcome_kind"] == "unknown_error"
    assert state["reason_code"] == "settlement_attempt_processing_failed"
    assert state["error_class"] == "TypeError"
    assert state["attempt_count"] == 1
    assert state["next_attempt_at_ms"] == 301_000
    assert state["claim_id"] is None
    assert state["claim_until_ms"] is None


def test_unexpected_reconciliation_exception_completes_claim_with_backoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )

    class _ObservedCollector(_Collector):
        def collect_outcome(
            self,
            lifecycle_case: dict,
            read_model: dict,
            *,
            before_first_provider_io=None,
        ) -> SettlementAttemptOutcome:
            if before_first_provider_io is not None:
                before_first_provider_io()
            self.calls += 1
            return SettlementAttemptOutcome(
                kind="observed_incomplete",
                source_id="lx",
                account="lx",
                case_id=str(lifecycle_case["case_id"]),
                contract_version=self.contract.contract_version,
                capability_fingerprint=(
                    self.capability.capability_fingerprint
                ),
                observation=_observation(str(lifecycle_case["case_id"])),
            )

    monkeypatch.setattr(
        mod,
        "reconcile_lifecycle_close_reason",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("unexpected canonical writer failure")
        ),
    )
    source = _runtime_source(tmp_path)
    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=_ObservedCollector(supported=True),
    )
    state = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )

    assert result["provider_attempt_count"] == 1
    assert state is not None
    assert state["outcome_kind"] == "unknown_error"
    assert state["reason_code"] == "settlement_attempt_processing_failed"
    assert state["error_class"] == "RuntimeError"
    assert state["next_attempt_at_ms"] == 301_000
    assert state["claim_id"] is None
    assert state["claim_until_ms"] is None


def test_post_write_refresh_exception_completes_claim_with_backoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    original_candidates = mod.list_trade_lifecycle_due_candidates
    candidate_reads = 0

    def fail_post_write_refresh(*args, **kwargs):
        nonlocal candidate_reads
        candidate_reads += 1
        if candidate_reads == 4:
            raise RuntimeError("post-write refresh failed")
        return original_candidates(*args, **kwargs)

    monkeypatch.setattr(
        mod,
        "list_trade_lifecycle_due_candidates",
        fail_post_write_refresh,
    )

    class _ObservedCollector(_Collector):
        def collect_outcome(
            self,
            lifecycle_case: dict,
            read_model: dict,
            *,
            before_first_provider_io=None,
        ) -> SettlementAttemptOutcome:
            if before_first_provider_io is not None:
                before_first_provider_io()
            self.calls += 1
            return SettlementAttemptOutcome(
                kind="observed_incomplete",
                source_id="lx",
                account="lx",
                case_id=str(lifecycle_case["case_id"]),
                contract_version=self.contract.contract_version,
                capability_fingerprint=(
                    self.capability.capability_fingerprint
                ),
                observation=_observation(str(lifecycle_case["case_id"])),
            )

    monkeypatch.setattr(
        mod,
        "reconcile_lifecycle_close_reason",
        lambda *_args, **_kwargs: {
            "admission_status": "admitted_semantic"
        },
    )
    source = _runtime_source(tmp_path)
    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=_ObservedCollector(supported=True),
    )
    state = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )

    assert candidate_reads == 4
    assert result["provider_attempt_count"] == 1
    assert result["provider_results"][0]["admission_status"] is None
    assert state is not None
    assert state["outcome_kind"] == "unknown_error"
    assert state["reason_code"] == "settlement_attempt_refresh_failed"
    assert state["error_class"] == "RuntimeError"
    assert state["next_attempt_at_ms"] == 301_000
    assert state["claim_id"] is None
    assert state["claim_until_ms"] is None


def test_initial_lease_guard_start_failure_completes_without_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    monkeypatch.setattr(
        mod.threading.Thread,
        "start",
        lambda _thread: (_ for _ in ()).throw(
            RuntimeError("cannot start renewal thread")
        ),
    )
    source = _runtime_source(tmp_path)
    collector = _Collector(supported=True)

    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    state = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )

    assert result["provider_attempt_count"] == 0
    assert collector.calls == 0
    assert state is not None
    assert state["outcome_kind"] == "unknown_error"
    assert state["reason_code"] == "settlement_attempt_lease_guard_failed"
    assert state["error_class"] == "RuntimeError"
    assert state["attempt_count"] == 0
    assert state["next_attempt_at_ms"] == 301_000
    assert state["claim_id"] is None
    assert state["claim_until_ms"] is None


def test_post_refresh_guard_start_failure_completes_owned_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    original_start = mod.threading.Thread.start
    thread_starts = 0

    def fail_fourth_start(thread):
        nonlocal thread_starts
        thread_starts += 1
        if thread_starts == 4:
            raise RuntimeError("cannot start post-refresh renewal thread")
        return original_start(thread)

    monkeypatch.setattr(
        mod.threading.Thread,
        "start",
        fail_fourth_start,
    )
    source = _runtime_source(tmp_path)
    collector = _Collector(supported=True)
    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )
    state = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )

    assert thread_starts == 4
    assert result["provider_attempt_count"] == 1
    assert collector.calls == 1
    assert state is not None
    assert state["outcome_kind"] == "unknown_error"
    assert state["reason_code"] == "settlement_attempt_refresh_failed"
    assert state["error_class"] == "RuntimeError"
    assert state["next_attempt_at_ms"] == 301_000
    assert state["claim_id"] is None
    assert state["claim_until_ms"] is None


def test_completion_update_failure_uses_minimal_owned_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    monkeypatch.setattr(
        mod,
        "settlement_attempt_updates_after_outcome",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("malformed control counters")
        ),
    )

    class _PreCallCollector(_Collector):
        def collect_outcome(
            self,
            lifecycle_case: dict,
            read_model: dict,
            *,
            before_first_provider_io=None,
        ) -> SettlementAttemptOutcome:
            self.calls += 1
            return SettlementAttemptOutcome(
                kind="unknown_error",
                source_id="lx",
                account="lx",
                case_id=str(lifecycle_case["case_id"]),
                contract_version=self.contract.contract_version,
                capability_fingerprint=(
                    self.capability.capability_fingerprint
                ),
                reason_code="pre_call_cancelled",
                error_class="local",
            )

    source = _runtime_source(tmp_path)
    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=_PreCallCollector(supported=True),
    )
    state = get_settlement_attempt_state(
        source["inbox_path"],
        source_id="lx",
        account="lx",
        case_id="provider-1",
    )

    assert result["provider_attempt_count"] == 0
    assert result["provider_results"][0]["outcome"]["reason_code"] == (
        "settlement_attempt_completion_failed"
    )
    assert state is not None
    assert state["outcome_kind"] == "unknown_error"
    assert state["reason_code"] == "settlement_attempt_completion_failed"
    assert state["error_class"] == "ValueError"
    assert state["attempt_count"] == 0
    assert state["next_attempt_at_ms"] == 301_000
    assert state["claim_id"] is None
    assert state["claim_until_ms"] is None


def test_restart_preserves_backoff_and_control_row_loss_allows_one_extra_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    source = _runtime_source(tmp_path)

    first_collector = _Collector(supported=True)
    first = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=first_collector,
    )
    restarted_collector = _Collector(supported=True)
    restarted = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=61_000,
        apply_changes=True,
        settlement_collector=restarted_collector,
    )

    with sqlite3.connect(source["inbox_path"]) as conn:
        conn.execute(
            """
            DELETE FROM lifecycle_settlement_attempt_state
            WHERE source_id = ? AND account = ? AND case_id = ?
            """,
            ("lx", "lx", "provider-1"),
        )

    recreated_collector = _Collector(supported=True)
    recreated = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=121_000,
        apply_changes=True,
        settlement_collector=recreated_collector,
    )
    bounded = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=181_000,
        apply_changes=True,
        settlement_collector=recreated_collector,
    )

    assert first["provider_attempt_count"] == 1
    assert restarted["provider_attempt_count"] == 0
    assert recreated["provider_attempt_count"] == 1
    assert bounded["provider_attempt_count"] == 0
    assert first_collector.calls == 1
    assert restarted_collector.calls == 0
    assert recreated_collector.calls == 1
    assert counts["account_reads"] == 4


def test_control_store_failure_fails_provider_closed_but_runs_local_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[
            _candidate("local-1"),
            _candidate("provider-1"),
        ],
    )
    monkeypatch.setattr(
        mod,
        "list_settlement_attempt_states",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("control table unavailable")
        ),
    )
    collector = _Collector(supported=True)
    source = _runtime_source(tmp_path)
    enqueue_trade_payload(
        source["inbox_path"],
        payload={"deal_id": "health-fixture"},
        source="test",
    )

    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )

    assert result["control_status"] == "control_store_unavailable"
    assert result["local_reconciliation"]["case_count"] == 2
    assert result["provider_attempt_count"] == 0
    assert collector.calls == 0
    assert counts["account_reads"] == 1


@pytest.mark.parametrize(
    "failing_operation",
    [
        "upsert_settlement_attempt_state",
        "reserve_settlement_attempt_invocation",
    ],
)
def test_pre_provider_control_write_failure_fails_closed_after_local_plan(
    tmp_path: Path,
    monkeypatch,
    failing_operation: str,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    counts = _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    collector = _Collector(supported=True)
    source = _runtime_source(tmp_path)
    enqueue_trade_payload(
        source["inbox_path"],
        payload={"deal_id": "health-fixture"},
        source="test",
    )
    monkeypatch.setattr(
        mod,
        failing_operation,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("control operation unavailable")
        ),
    )

    result = mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
        source=source,
        now_ms=1_000,
        apply_changes=True,
        settlement_collector=collector,
    )

    assert result["control_status"] == "control_store_unavailable"
    assert result["local_reconciliation"]["case_count"] == 1
    assert result["provider_claim_count"] == 0
    assert result["provider_attempt_count"] == 0
    assert collector.calls == 0
    assert counts["account_reads"] == 1


def test_whole_inbox_corruption_remains_service_fatal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.trades.lifecycle_runtime as mod

    _patch_due_planner(
        monkeypatch,
        candidates=[_candidate("provider-1")],
    )
    source = _runtime_source(tmp_path)
    source["inbox_path"].write_text("not a sqlite database")

    with pytest.raises(sqlite3.DatabaseError):
        mod.reconcile_due_lifecycle_cases_for_source(
        _RuntimeAuditRepo(),
            source=source,
            now_ms=1_000,
            apply_changes=True,
            settlement_collector=_Collector(supported=True),
        )
