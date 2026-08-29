from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.multi_tick.misc import AccountResult


class _RunLog:
    def __init__(self):
        self.events = []

    def safe_event(self, *_args, **kwargs):
        self.events.append(kwargs)
        return None


class _Audit:
    def __init__(self):
        self.events = []

    def audit(self, *_args, **kwargs):
        self.events.append(kwargs)
        return None

    def fail_schema_validation(self, **_kwargs):
        return None


def _portfolio_context(account: str) -> dict:
    return {
        "filters": {"account": account},
        "portfolio_source_name": "futu",
        "source_account_identifiers": [account],
        "source_observed_at": datetime.now(timezone.utc).isoformat(),
        "source_observation_status": "trusted",
        "stocks_by_symbol": {
            "NVDA": {"avg_cost": 100, "shares": 100, "account": account}
        },
    }


def _request(
    tmp_path: Path,
    *,
    accounts: list[str],
    workers: int,
    force: bool,
    trigger_kind: str = "manual",
):
    from src.application.tick_account_execution import TickAccountExecutionRequest

    run_dir = tmp_path / "output_runs" / "run-1"
    shared = run_dir / "required_data"
    shared.mkdir(parents=True, exist_ok=True)
    config = {
        "runtime": {"portfolio_timeout_sec": 1},
        "symbols": [
            {
                "symbol": "NVDA",
                "broker": "US",
                "sell_put": {"enabled": True},
                "sell_call": {"enabled": False},
            }
        ],
    }
    return TickAccountExecutionRequest(
        account_ids=accounts,
        account_workers=workers,
        base=tmp_path,
        base_cfg=config,
        cfg_path=tmp_path / "config.us.json",
        vpy=Path("/usr/bin/python3"),
        markets_to_run=["US"],
        scheduler_ms=3,
        scheduler_view={},
        notify_decision_by_account={account: True for account in accounts},
        should_run_global=True,
        reason_global="due",
        run_id="run-1",
        run_dir=run_dir,
        shared_required=shared,
        accounts_root=tmp_path / "output_accounts",
        prefetch_done=False,
        force_mode=force,
        smoke=False,
        no_send=True,
        scan_decision_by_account={
            account: {
                "should_run": True,
                "scheduler_decision": {
                    "scheduled_scan_target_market": (
                        f"2026-07-28T{10 + idx:02d}:00:00-04:00"
                    )
                },
            }
            for idx, account in enumerate(accounts)
        },
        state_path=tmp_path / "scheduler_state.json",
        scheduler_schedule_key="schedule",
        runlog=_RunLog(),
        audit_helper=_Audit(),
        trigger_kind=trigger_kind,
    )


def _fake_prepare(**kwargs):
    from src.infrastructure.io_utils import atomic_write_json

    out = {}
    for account, state_dir in kwargs["account_state_dirs"].items():
        authority = kwargs["account_config_authorities"][account]
        assert authority.state_path.is_file()
        assert authority.compatibility_path.is_file()
        assert authority.state_path.read_bytes() == authority.canonical_bytes
        assert authority.compatibility_path.read_bytes() == authority.canonical_bytes
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        context = _portfolio_context(account)
        context_path = state_dir / "portfolio_context.json"
        atomic_write_json(context_path, context)
        manifest_path = state_dir / "prepared_portfolio_context.v1.json"
        manifest = {
            "schema_version": "prepared_portfolio_context.v1",
            "run_id": kwargs["run_id"],
            "account": account,
            "status": "ready",
            "account_config_sha256": authority.account_config_sha256,
            "portfolio_context_relpath": context_path.name,
            "payload_sha256": hashlib.sha256(context_path.read_bytes()).hexdigest(),
            "portfolio_source_name": "futu",
            "portfolio_source_account": account,
        }
        atomic_write_json(manifest_path, manifest)
        out[account] = {
            **manifest,
            "manifest_path": str(manifest_path),
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        }
    return out


def _fake_prepare_options(**kwargs):
    from src.application.prepared_option_positions_context import (
        PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA,
        PREPARED_OPTION_POSITIONS_MANIFEST_NAME,
        PreparedOptionPositionsBatch,
    )
    from src.infrastructure.io_utils import atomic_write_json

    manifests = {}
    for account, authority in kwargs[
        "account_config_authorities"
    ].items():
        state_dir = (
            Path(kwargs["base"])
            / "output_runs"
            / kwargs["run_id"]
            / "accounts"
            / account
            / "state"
        )
        state_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = state_dir / PREPARED_OPTION_POSITIONS_MANIFEST_NAME
        manifest = {
            "schema_version": PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA,
            "run_id": kwargs["run_id"],
            "account": account,
            "status": "ready",
            "account_config_sha256": authority.account_config_sha256,
        }
        atomic_write_json(manifest_path, manifest)
        manifests[account] = {
            **manifest,
            "manifest_path": str(manifest_path),
            "manifest_sha256": hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
        }
    return PreparedOptionPositionsBatch(
        manifests=manifests,
        position_records_by_account={
            account: [] for account in manifests
        },
        unavailable_by_account={},
        observed_at_utc="2026-07-29T01:40:00+00:00",
        ledger_read_count=1 if manifests else 0,
        fx_observation_count=1 if manifests else 0,
    )


def test_record_tick_latency_preserves_event_and_best_effort_behavior(
    monkeypatch,
) -> None:
    from src.application import multi_tick_audit as mod

    clock = Mock(side_effect=(1.5, 2.0))
    safe_event = Mock(side_effect=(None, OSError("run log unavailable")))
    monkeypatch.setattr(mod, "monotonic", clock)
    runlog = SimpleNamespace(safe_event=safe_event)

    assert mod.record_tick_latency(
        runlog=runlog,
        stage="account_outcomes",
        started=1.0,
        data={"account_count": 2},
    ) == 500
    assert mod.record_tick_latency(
        runlog=runlog,
        stage="provider_delivery",
        started=3.0,
    ) == 0
    assert clock.call_count == 2
    assert safe_event.call_count == 2
    safe_event.assert_any_call(
        "tick_latency",
        "ok",
        duration_ms=500,
        data={"stage": "account_outcomes", "account_count": 2},
    )


@pytest.fixture(autouse=True)
def _stub_prepared_option_context(monkeypatch):
    from src.application import tick_account_execution as mod

    monkeypatch.setattr(
        mod,
        "prepare_option_positions_contexts",
        _fake_prepare_options,
    )
    monkeypatch.setattr(
        mod,
        "load_prepared_option_positions_context",
        lambda **kwargs: {
            "prepared_authority": {
                "run_id": kwargs["expected_run_id"],
                "account": kwargs["expected_account"],
            },
            "filters": {
                "account": kwargs["expected_account"],
                "broker": "futu",
            },
            "context_status": "available",
            "decision_snapshot_status": "trusted",
            "open_positions_min": [],
        },
    )


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("accounts", [["lx", "sy"], ["sy", "lx"]])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("snapshot_status", ["complete", "partial"])
def test_barrier_prefetches_once_and_seals_before_account_submission(
    monkeypatch,
    tmp_path: Path,
    workers: int,
    accounts: list[str],
    force: bool,
    snapshot_status: str,
) -> None:
    from src.application import tick_account_execution as mod
    from src.infrastructure.io_utils import atomic_write_json

    prefetch_calls: list[dict] = []
    account_requests = []
    cleanup_calls: list[dict] = []

    def fake_prefetch(**kwargs):
        prefetch_calls.append(kwargs)
        return {
            "schema_version": "1.0",
            "errors": 1 if snapshot_status == "partial" else 0,
            "symbols": [],
            "results": {},
            "global_required_data_plan": {
                "plan_id": "a" * 64,
                "symbols": [{"symbol": "NVDA", "fetch_plan": {}}],
            },
            "quote_receipts": {},
        }

    def fake_seal(**kwargs):
        payload = {
            "schema_version": "required_data_snapshot_manifest.v1",
            "run_id": kwargs["run_id"],
            "status": snapshot_status,
            "plan_id": "a" * 64,
            "symbols": {},
            "summary": {},
        }
        atomic_write_json(kwargs["manifest_path"], payload)
        return payload

    def fake_run_one_account(*, request, **_kwargs):
        assert (
            request.run_dir / "state" / "required_data_snapshot_manifest.json"
        ).is_file()
        assert request.allow_notifications is False
        account_requests.append(request)
        return mod.AccountRunOutcome(
            result=AccountResult(
                request.acct,
                True,
                False,
                "ok",
                "",
            ),
            acct_metrics={"account": request.acct},
            prefetch_done=True,
            ran_pipeline=True,
        )

    monkeypatch.setattr(mod, "prepare_portfolio_contexts", _fake_prepare)
    monkeypatch.setattr(mod, "prefetch_required_data", fake_prefetch)
    monkeypatch.setattr(mod, "seal_required_data_snapshot", fake_seal)
    monkeypatch.setattr(
        mod,
        "_retire_required_data_shadows_after_manifest",
        lambda **kwargs: cleanup_calls.append(kwargs),
    )
    monkeypatch.setattr(mod, "run_one_account", fake_run_one_account)

    outcome = mod.run_tick_account_execution(
        _request(tmp_path, accounts=accounts, workers=workers, force=force)
    )

    assert len(prefetch_calls) == 1
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0]["trigger"] == "new_seal"
    assert cleanup_calls[0]["manifest_bytes"] == cleanup_calls[0][
        "manifest_path"
    ].read_bytes()
    assert prefetch_calls[0]["force_refresh"] is force
    assert {item.acct for item in account_requests} == set(accounts)
    assert all(item.required_data_snapshot_manifest for item in account_requests)
    assert all(item.prepared_portfolio_context_manifest for item in account_requests)
    assert all(
        item.prepared_option_positions_context_manifest
        for item in account_requests
    )
    assert outcome.prefetch_done is True
    assert set(outcome.ran_pipeline_accounts) == set(accounts)
    summaries = [
        (
            tmp_path
            / "output_runs"
            / "run-1"
            / "accounts"
            / account
            / "state"
            / "required_data_prefetch_summary.json"
        ).read_bytes()
        for account in accounts
    ]
    assert len(set(summaries)) == 1


def test_experience_barrier_skips_account_authority_wheel_and_runtime_shadow(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import tick_account_execution as mod
    from src.infrastructure.io_utils import atomic_write_json

    request = replace(
        _request(
            tmp_path,
            accounts=["paper"],
            workers=1,
            force=False,
        ),
        experience=True,
    )
    captured = []

    def _forbidden(name):
        return lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError(f"{name} must not run")
        )

    monkeypatch.setattr(
        mod,
        "resolve_experience_account_display_name",
        lambda **_kwargs: "美股模拟期权账户",
    )
    monkeypatch.setattr(
        mod, "prepare_portfolio_contexts", _forbidden("portfolio preparation")
    )
    monkeypatch.setattr(
        mod,
        "prepare_option_positions_contexts",
        _forbidden("option-position preparation"),
    )
    monkeypatch.setattr(
        mod,
        "merge_wheel_requirements_into_prefetch_config",
        _forbidden("Wheel planning"),
    )
    monkeypatch.setattr(
        mod,
        "_build_close_advice_barrier_plan",
        _forbidden("Close Advice planning"),
    )
    monkeypatch.setattr(
        mod,
        "_publish_runtime_portfolio_snapshot_shadow",
        _forbidden("runtime portfolio shadow"),
    )
    monkeypatch.setattr(
        mod,
        "prefetch_required_data",
        lambda **_kwargs: {
            "schema_version": "1.0",
            "errors": 0,
            "symbols": [],
            "results": {},
            "global_required_data_plan": {
                "plan_id": "a" * 64,
                "symbols": [],
            },
            "quote_receipts": {},
        },
    )

    def _seal(**kwargs):
        payload = {
            "schema_version": "required_data_snapshot_manifest.v1",
            "run_id": kwargs["run_id"],
            "status": "complete",
            "plan_id": "a" * 64,
            "symbols": {},
            "summary": {},
        }
        atomic_write_json(kwargs["manifest_path"], payload)
        return payload

    monkeypatch.setattr(mod, "seal_required_data_snapshot", _seal)

    def _run_one_account(*, request, **_kwargs):
        captured.append(request)
        return mod.AccountRunOutcome(
            result=AccountResult(request.acct, True, False, "experience", ""),
            acct_metrics={"account": request.acct},
            prefetch_done=True,
            ran_pipeline=True,
        )

    monkeypatch.setattr(mod, "run_one_account", _run_one_account)

    outcome = mod.run_tick_account_execution(request)

    assert outcome.ran_pipeline_accounts == ["paper"]
    assert len(captured) == 1
    child = captured[0]
    assert child.experience is True
    assert child.account_display_name == "美股模拟期权账户"
    assert child.prepared_portfolio_context_manifest is None
    assert child.prepared_option_positions_context_manifest is None
    assert child.has_wheel_scope is False
    assert child.allow_mutations is False
    assert child.allow_notifications is False


def test_barrier_reads_shared_ledger_once_and_plans_close_advice_before_prefetch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from datetime import date

    from src.application import tick_account_execution as mod
    from src.infrastructure.io_utils import atomic_write_json

    request = _request(
        tmp_path,
        accounts=["lx", "sy"],
        workers=2,
        force=False,
        trigger_kind="scheduled",
    )
    request = replace(
        request,
        markets_to_run=["HK"],
        cfg_path=tmp_path / "config.hk.json",
    )
    request.base_cfg.update(
        {
            "portfolio": {
                "broker": "富途",
                "data_config": "portfolio.runtime.json",
            },
            "close_advice": {"enabled": True},
            "symbols": [
                {
                    "symbol": "0700.HK",
                    "broker": "HK",
                    "fetch": {
                        "source": "opend",
                        "host": "127.0.0.1",
                        "port": 11111,
                    },
                    "sell_put": {"enabled": True},
                    "sell_call": {"enabled": False},
                }
            ],
        }
    )
    records = [
        {
            "record_id": f"lot-{account}",
            "fields": {
                "broker": "富途",
                "account": account,
                "symbol": "0700.HK",
                "status": "open",
                "side": "short",
                "option_type": "put",
                "contracts": 1,
                "contracts_open": 1,
                "strike": 100,
                "expiration_ymd": "2026-08-28",
                "currency": "HKD",
            },
        }
        for account in ("lx", "sy")
    ]
    prepared_option_calls: list[dict] = []
    prefetch_calls: list[dict] = []
    account_requests = []
    quality_gate_calls: list[tuple[str, str | None, str | None]] = []

    monkeypatch.setattr(mod, "prepare_portfolio_contexts", _fake_prepare)
    monkeypatch.setattr(
        mod,
        "expiration_business_today",
        lambda _now: date(2026, 7, 29),
    )
    def _prepare_options(**kwargs):
        prepared_option_calls.append(kwargs)
        baseline = _fake_prepare_options(**kwargs)
        return mod.PreparedOptionPositionsBatch(
            manifests=baseline.manifests,
            position_records_by_account={
                account: records for account in ("lx", "sy")
            },
            unavailable_by_account={},
            observed_at_utc=baseline.observed_at_utc,
            ledger_read_count=1,
            fx_observation_count=1,
        )

    monkeypatch.setattr(
        mod,
        "prepare_option_positions_contexts",
        _prepare_options,
    )
    monkeypatch.setattr(
        mod,
        "list_position_lot_snapshots",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("close-advice planning must reuse prepared rows")
        ),
    )
    monkeypatch.setattr(
        mod,
        "assert_quality_allows",
        lambda consumer, *, account=None, market=None: quality_gate_calls.append(
            (consumer, account, market)
        ),
    )

    def _prefetch(**kwargs):
        prefetch_calls.append(kwargs)
        return {
            "global_required_data_plan": {
                "plan_id": "a" * 64,
                "symbols": [
                    {
                        "symbol": "0700.HK",
                        "fetch_plan": {},
                    }
                ],
            },
            "symbols": [],
            "results": [],
        }

    def _seal(**kwargs):
        plan_path = kwargs["close_advice_required_data_plan_path"]
        assert plan_path is not None and plan_path.is_file()
        payload = {
            "schema_version": "required_data_snapshot_manifest.v1",
            "run_id": kwargs["run_id"],
            "status": "complete",
            "plan_id": "a" * 64,
            "symbols": {},
            "summary": {},
        }
        atomic_write_json(kwargs["manifest_path"], payload)
        return payload

    def _run_one_account(*, request, **_kwargs):
        account_requests.append(request)
        return mod.AccountRunOutcome(
            result=AccountResult(
                request.acct,
                True,
                False,
                "ok",
                "",
            ),
            acct_metrics={"account": request.acct},
            prefetch_done=True,
            ran_pipeline=True,
        )

    monkeypatch.setattr(mod, "prefetch_required_data", _prefetch)
    monkeypatch.setattr(mod, "seal_required_data_snapshot", _seal)
    monkeypatch.setattr(mod, "run_one_account", _run_one_account)

    outcome = mod.run_tick_account_execution(request)

    assert len(prepared_option_calls) == 1
    assert prepared_option_calls[0]["mark_evidence_accounts"] == ("lx",)
    assert len(prefetch_calls) == 1
    merged_requirements = [
        requirement
        for item in prefetch_calls[0]["cfg"]["symbols"]
        for requirement in item.get(
            "_close_advice_position_requirements",
            [],
        )
    ]
    assert {
        requirement["position_lot_id"]
        for requirement in merged_requirements
    } == {"lot-lx", "lot-sy"}
    assert quality_gate_calls == [
        ("close_advice", "lx", "hk"),
        ("close_advice", "sy", "hk"),
    ]
    plan_path = (
        request.run_dir
        / "state"
        / "close_advice_required_data_plan.json"
    )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["status"] == "complete"
    assert plan["summary"]["requirements_ready"] == 2
    assert all(
        item.close_advice_required_data_plan == plan_path
        for item in account_requests
    )
    assert outcome.prefetch_invocation_count == 1


def test_close_advice_quality_gate_blocks_only_affected_account_market(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import tick_account_execution as mod
    from src.application.quality.gate import QualityGateBlocked

    request = _request(
        tmp_path,
        accounts=["lx", "sy"],
        workers=1,
        force=False,
    )
    account_config = {
        "portfolio": {"broker": "富途"},
        "close_advice": {"enabled": True},
        "symbols": [
            {
                "symbol": "NVDA",
                "fetch": {
                    "source": "opend",
                    "host": "127.0.0.1",
                    "port": 11111,
                },
            }
        ],
    }
    records = {
        account: [
            {
                "record_id": f"lot-{account}",
                "fields": {
                    "broker": "富途",
                    "account": account,
                    "symbol": "NVDA",
                    "status": "open",
                    "side": "short",
                    "option_type": "put",
                    "contracts_open": 1,
                    "strike": 100,
                    "expiration_ymd": "2026-08-28",
                },
            }
        ]
        for account in ("lx", "sy")
    }
    gate_calls: list[tuple[str, str]] = []

    def _quality_gate(
        _consumer: str,
        *,
        account: str | None = None,
        market: str | None = None,
    ) -> None:
        gate_calls.append((str(account), str(market)))
        if account == "lx":
            raise QualityGateBlocked(
                "close_advice",
                "POSITION_CONTRACT_TERMS_DRIFT",
                ("OM-POS-002",),
            )

    monkeypatch.setattr(mod, "assert_quality_allows", _quality_gate)
    state_dir = request.run_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    merged, plan_path = mod._build_close_advice_barrier_plan(
        request=request,
        scanning_configs={
            "lx": account_config,
            "sy": account_config,
        },
        candidate_config=request.base_cfg,
        run_state_dir=state_dir,
        run_started_at_utc=datetime(2026, 7, 29, 1, 40, tzinfo=timezone.utc),
        position_records_by_account=records,
    )

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert gate_calls == [("lx", "us"), ("sy", "us")]
    assert plan["accounts"]["lx"]["status"] == "partial"
    assert plan["accounts"]["lx"]["requirements"] == []
    assert plan["accounts"]["lx"]["planning_errors"] == [
        {
            "reason": (
                "quality_gate_blocked:POSITION_CONTRACT_TERMS_DRIFT"
            ),
            "position_lot_id": "lot-lx",
            "quote_key": "NVDA|put|2026-08-28|100.000000",
        }
    ]
    assert plan["accounts"]["sy"]["status"] == "ready"
    merged_requirements = [
        requirement
        for item in merged.get("symbols") or []
        for requirement in item.get(
            "_close_advice_position_requirements",
            [],
        )
    ]
    assert [item["position_lot_id"] for item in merged_requirements] == [
        "lot-sy"
    ]


def test_close_advice_barrier_fails_closed_without_target_quality_dataset(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import tick_account_execution as mod
    from src.application.quality import gate as quality_gate_module
    from src.infrastructure.quality.artifact_repository import (
        QualityArtifactRepository,
    )

    request = _request(
        tmp_path,
        accounts=["lx"],
        workers=1,
        force=False,
    )
    account_config = {
        "portfolio": {"broker": "富途"},
        "close_advice": {"enabled": True},
        "symbols": [
            {
                "symbol": "NVDA",
                "fetch": {
                    "source": "opend",
                    "host": "127.0.0.1",
                    "port": 11111,
                },
            }
        ],
    }
    records = {
        "lx": [
            {
                "record_id": "lot-lx",
                "fields": {
                    "broker": "富途",
                    "account": "lx",
                    "symbol": "NVDA",
                    "status": "open",
                    "side": "short",
                    "option_type": "put",
                    "contracts_open": 1,
                    "strike": 100,
                    "expiration_ymd": "2026-08-28",
                },
            }
        ]
    }
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    artifact_path = tmp_path / "quality" / "status.json"
    QualityArtifactRepository(artifact_path).write_atomic(
        {
            "schema_version": "investment.quality_status.v1",
            "observed_at_utc": observed_at,
            "datasets": [
                {
                    "dataset_id": "om.option_positions",
                    "scope": {"account": "sy", "market": "us"},
                    "freshness": {"observed_at_utc": observed_at},
                    "usable_for": ["close_advice"],
                    "blocked_consumers": [],
                }
            ],
        }
    )
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "true")
    monkeypatch.setattr(
        quality_gate_module,
        "default_quality_artifact_path",
        lambda: artifact_path,
    )
    state_dir = request.run_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    merged, plan_path = mod._build_close_advice_barrier_plan(
        request=request,
        scanning_configs={"lx": account_config},
        candidate_config=request.base_cfg,
        run_state_dir=state_dir,
        run_started_at_utc=datetime(2026, 7, 29, 1, 40, tzinfo=timezone.utc),
        position_records_by_account=records,
    )

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["accounts"]["lx"]["status"] == "partial"
    assert plan["accounts"]["lx"]["requirements"] == []
    assert plan["accounts"]["lx"]["planning_errors"] == [
        {
            "reason": "quality_gate_blocked:QUALITY_DATASET_UNAVAILABLE",
            "position_lot_id": "lot-lx",
            "quote_key": "NVDA|put|2026-08-28|100.000000",
        }
    ]
    assert all(
        not item.get("_close_advice_position_requirements")
        for item in merged.get("symbols") or []
    )


def test_reentry_restores_manifest_bound_close_advice_plan_without_replanning(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from datetime import date, datetime, timezone

    from domain.domain.decision_state_fingerprint import canonical_sha256
    from src.application import tick_account_execution as mod
    from src.application.close_advice_required_data import (
        PLAN_FILE_NAME,
        build_close_advice_required_data_plan,
        publish_close_advice_required_data_plan,
    )
    from src.application.prepared_option_positions_context import (
        PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA,
        PREPARED_OPTION_POSITIONS_MANIFEST_NAME,
    )
    from src.application.source_receipts import sha256_bytes
    from src.infrastructure.io_utils import atomic_write_json

    request = replace(
        _request(
            tmp_path,
            accounts=["lx"],
            workers=1,
            force=False,
        ),
        prefetch_done=True,
    )
    state_dir = request.run_dir / "state"
    state_dir.mkdir(parents=True)
    plan_path = state_dir / PLAN_FILE_NAME
    plan = build_close_advice_required_data_plan(
        run_id=request.run_id,
        run_started_at_utc=datetime(
            2026,
            7,
            29,
            1,
            40,
            tzinfo=timezone.utc,
        ),
        business_date=date(2026, 7, 29),
        account_configs={
            "lx": {"close_advice": {"enabled": False}}
        },
        base_config=request.base_cfg,
        markets_to_run=["US"],
        position_records_by_account={},
    )
    publish_close_advice_required_data_plan(
        path=plan_path,
        payload=plan,
    )
    manifest_path = state_dir / "required_data_snapshot_manifest.json"
    manifest = {
        "schema_version": "required_data_snapshot_manifest.v1",
        "run_id": request.run_id,
        "status": "complete",
        "plan_id": "a" * 64,
        "sealed_at_utc": datetime.now(timezone.utc).isoformat(),
        "required_data_root_relpath": "../required_data",
        "symbols": {},
        "summary": {"symbols_total": 0, "ready": 0, "failed": 0},
        "close_advice_required_data_plan_relpath": PLAN_FILE_NAME,
        "close_advice_required_data_plan_sha256": sha256_bytes(
            plan_path.read_bytes()
        ),
    }
    manifest["content_sha256"] = canonical_sha256(manifest)
    atomic_write_json(manifest_path, manifest)
    option_manifest_path = (
        request.run_dir
        / "accounts"
        / "lx"
        / "state"
        / PREPARED_OPTION_POSITIONS_MANIFEST_NAME
    )
    option_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        option_manifest_path.parent / "prepared_portfolio_context.v1.json",
        {"schema_version": "prepared_portfolio_context.v1"},
    )
    atomic_write_json(
        option_manifest_path,
        {
            "schema_version": PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA,
            "run_id": request.run_id,
            "account": "lx",
            "status": "ready",
        },
    )
    account_requests = []
    cleanup_calls: list[dict] = []
    monkeypatch.setattr(
        mod,
        "load_required_data_snapshot_manifest_snapshot",
        lambda **_kwargs: (
            manifest,
            request.shared_required.resolve(),
            manifest_path.read_bytes(),
        ),
    )
    monkeypatch.setattr(
        mod,
        "load_prepared_portfolio_context",
        lambda **_kwargs: _portfolio_context("lx"),
    )
    monkeypatch.setattr(
        mod,
        "load_prepared_option_positions_context",
        lambda **_kwargs: {"wheel_read_model": {"batches": []}},
    )
    monkeypatch.setattr(
        mod,
        "prepare_portfolio_contexts",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("re-entry must not prepare contexts")
        ),
    )
    monkeypatch.setattr(
        mod,
        "_build_close_advice_barrier_plan",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("re-entry must not rebuild the plan")
        ),
    )
    monkeypatch.setattr(
        mod,
        "prefetch_required_data",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("re-entry must not prefetch again")
        ),
    )
    monkeypatch.setattr(
        mod,
        "_retire_required_data_shadows_after_manifest",
        lambda **kwargs: cleanup_calls.append(kwargs),
    )

    def _run_one_account(*, request, **_kwargs):
        account_requests.append(request)
        return mod.AccountRunOutcome(
            result=AccountResult(
                request.acct,
                True,
                False,
                "ok",
                "",
            ),
            acct_metrics={"account": request.acct},
            prefetch_done=True,
            ran_pipeline=True,
        )

    monkeypatch.setattr(mod, "run_one_account", _run_one_account)

    outcome = mod.run_tick_account_execution(request)

    assert outcome.prefetch_invocation_count == 0
    assert outcome.snapshot_status == "complete"
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0]["trigger"] == "recovery"
    assert cleanup_calls[0]["manifest_bytes"] == manifest_path.read_bytes()
    assert len(account_requests) == 1
    assert (
        account_requests[0].close_advice_required_data_plan
        == plan_path.resolve()
    )
    assert (
        account_requests[0].prepared_option_positions_context_manifest
        == option_manifest_path.resolve()
    )


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_required_data_shadow_cleanup_is_observable_and_nonfatal(
    monkeypatch,
    tmp_path: Path,
    cleanup_fails: bool,
) -> None:
    from src.application import tick_account_execution as mod

    request = _request(
        tmp_path,
        accounts=["lx"],
        workers=1,
        force=False,
    )
    request.audit_helper.audit = Mock()
    request.runlog.safe_event = Mock()
    expected = {
        "removed_files": 2,
        "removed_bytes": 123,
        "absent_files": 0,
        "failed_files": 0,
    }

    if cleanup_fails:
        monkeypatch.setattr(
            mod,
            "retire_required_data_snapshot_shadows",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("disk busy")),
        )
        expected = {
            "removed_files": 0,
            "removed_bytes": 0,
            "absent_files": 0,
            "failed_files": 1,
        }
    else:
        monkeypatch.setattr(
            mod,
            "retire_required_data_snapshot_shadows",
            lambda **_kwargs: expected,
        )

    summary = mod._retire_required_data_shadows_after_manifest(
        request=request,
        manifest_path=request.run_dir / "state/manifest.json",
        manifest={},
        manifest_bytes=b"{}\n",
        trigger="new_seal",
    )

    status = "degraded" if cleanup_fails else "ok"
    event = {"trigger": "new_seal", **expected}
    assert summary == expected
    request.audit_helper.audit.assert_called_once_with(
        "cleanup",
        "required_data_shadow_cleanup",
        run_id=request.run_id,
        status=status,
        extra=event,
    )
    request.runlog.safe_event.assert_called_once_with(
        "required_data_shadow_cleanup",
        status,
        data=event,
    )


@pytest.mark.parametrize(
    ("seal_behavior", "reason", "prefetch_done"),
    [
        ("failed", "required_data_snapshot_failed", True),
        (
            "raise",
            "required_data_snapshot_manifest_unavailable",
            False,
        ),
    ],
)
def test_terminal_barrier_failure_returns_typed_account_outcomes_without_pipeline(
    monkeypatch,
    tmp_path: Path,
    seal_behavior: str,
    reason: str,
    prefetch_done: bool,
) -> None:
    from src.application import tick_account_execution as mod
    from src.infrastructure.io_utils import atomic_write_json

    monkeypatch.setattr(mod, "prepare_portfolio_contexts", _fake_prepare)
    monkeypatch.setattr(
        mod,
        "prefetch_required_data",
        lambda **_kwargs: {
            "errors": 2,
            "symbols": [],
            "results": {},
            "global_required_data_plan": {
                "plan_id": "a" * 64,
                "symbols": [{"symbol": "NVDA"}],
            },
        },
    )

    def fake_seal(**kwargs):
        if seal_behavior == "raise":
            raise RuntimeError("atomic publish failed")
        payload = {
            "schema_version": "required_data_snapshot_manifest.v1",
            "run_id": kwargs["run_id"],
            "status": "failed",
            "plan_id": "a" * 64,
            "symbols": {},
            "summary": {"ready": 0, "failed": 1},
        }
        atomic_write_json(kwargs["manifest_path"], payload)
        return payload

    monkeypatch.setattr(mod, "seal_required_data_snapshot", fake_seal)
    monkeypatch.setattr(
        mod,
        "run_one_account",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("account pipeline must not start")
        ),
    )

    outcome = mod.run_tick_account_execution(
        _request(
            tmp_path,
            accounts=["lx", "sy"],
            workers=2,
            force=False,
        )
    )

    assert outcome.ran_pipeline_accounts == []
    assert outcome.prefetch_done is prefetch_done
    assert [item.decision_reason for item in outcome.results] == [reason, reason]
    assert all(item.should_notify is True for item in outcome.results)
    assert set(outcome.scheduled_scan_targets_by_account) == {"lx", "sy"}
    assert all(
        item["ran_pipeline"] is False
        and item["snapshot_status"] in {"failed", "unavailable"}
        for item in outcome.account_metrics
    )


def test_quote_drift_is_frozen_once_while_account_capacity_can_differ(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import tick_account_execution as mod
    from src.infrastructure.io_utils import atomic_write_json

    provider_calls = {
        "spot": 0,
        "expiration": 0,
        "chain": 0,
        "snapshot": 0,
    }
    observed_by_account: dict[str, dict] = {}

    def fake_prefetch(**_kwargs):
        for method in provider_calls:
            provider_calls[method] += 1
        invocation = provider_calls["snapshot"]
        iv_rv_ratio = 1.09 if invocation == 1 else 1.06
        market_fact = {
            "symbol": "3690.HK",
            "contract_symbol": "3690.HK-P-100",
            "spot": 110.0,
            "expiration": "2026-08-28",
            "iv_rv_ratio": iv_rv_ratio,
            "candidate": iv_rv_ratio > 1.08,
            "rejection_reason": None if iv_rv_ratio > 1.08 else "iv_rv_below_min",
        }
        return {
            "errors": 0,
            "symbols": [{"symbol": "3690.HK", "status": "ok"}],
            "results": {"3690.HK": market_fact},
            "global_required_data_plan": {
                "plan_id": "a" * 64,
                "symbols": [{"symbol": "3690.HK", "fetch_plan": {}}],
            },
        }

    def fake_seal(**kwargs):
        fact = kwargs["prefetch_summary"]["results"]["3690.HK"]
        payload = {
            "schema_version": "required_data_snapshot_manifest.v1",
            "run_id": kwargs["run_id"],
            "status": "complete",
            "plan_id": "a" * 64,
            "symbols": {
                "3690.HK": {
                    "status": "ready",
                    "market_fact": fact,
                }
            },
            "summary": {"ready": 1, "failed": 0},
        }
        atomic_write_json(kwargs["manifest_path"], payload)
        return payload

    capacities = {
        "lx": {"contracts": 2, "headroom": 20_000},
        "sy": {"contracts": 1, "headroom": 10_000},
    }

    def fake_run_one_account(*, request, **_kwargs):
        manifest = json.loads(
            request.required_data_snapshot_manifest.read_text(encoding="utf-8")
        )
        observed_by_account[request.acct] = {
            "market_fact": manifest["symbols"]["3690.HK"]["market_fact"],
            "capacity": capacities[request.acct],
        }
        return mod.AccountRunOutcome(
            result=AccountResult(request.acct, True, False, "ok", ""),
            acct_metrics={"account": request.acct},
            prefetch_done=True,
            ran_pipeline=True,
        )

    request = _request(
        tmp_path,
        accounts=["lx", "sy"],
        workers=2,
        force=False,
    )
    request.base_cfg["symbols"][0]["symbol"] = "3690.HK"
    monkeypatch.setattr(mod, "prepare_portfolio_contexts", _fake_prepare)
    monkeypatch.setattr(mod, "prefetch_required_data", fake_prefetch)
    monkeypatch.setattr(mod, "seal_required_data_snapshot", fake_seal)
    monkeypatch.setattr(mod, "run_one_account", fake_run_one_account)

    outcome = mod.run_tick_account_execution(request)

    assert outcome.prefetch_invocation_count == 1
    assert provider_calls == {
        "spot": 1,
        "expiration": 1,
        "chain": 1,
        "snapshot": 1,
    }
    assert observed_by_account["lx"]["market_fact"] == observed_by_account["sy"][
        "market_fact"
    ]
    assert observed_by_account["lx"]["market_fact"]["iv_rv_ratio"] == 1.09
    assert observed_by_account["lx"]["market_fact"]["candidate"] is True
    assert observed_by_account["lx"]["capacity"] != observed_by_account["sy"][
        "capacity"
    ]


def test_config_archive_conflict_fails_closed_before_any_account_child(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import tick_account_execution as mod
    from src.application.tick_run_workspace import account_run_config_paths

    request = _request(
        tmp_path,
        accounts=["lx"],
        workers=1,
        force=False,
    )
    historical = (
        tmp_path
        / "output_accounts"
        / "lx"
        / "state"
        / "config.override.json"
    )
    historical.parent.mkdir(parents=True)
    historical.write_text("historical\n", encoding="utf-8")
    state_path, compatibility_path = account_run_config_paths(
        base=tmp_path,
        run_id=request.run_id,
        account="lx",
    )
    compatibility_path.parent.mkdir(parents=True, exist_ok=True)
    compatibility_path.write_text("conflicting archive\n", encoding="utf-8")

    monkeypatch.setattr(
        mod,
        "prepare_portfolio_contexts",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("prepared worker must not start")
        ),
    )
    monkeypatch.setattr(
        mod,
        "prefetch_required_data",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("required-data prefetch must not start")
        ),
    )
    monkeypatch.setattr(
        mod,
        "run_one_account",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("pipeline and Close Advice must not start")
        ),
    )

    outcome = mod.run_tick_account_execution(request)

    assert outcome.ran_pipeline_accounts == []
    assert [item.decision_reason for item in outcome.results] == [
        "account_config_compatibility_conflict"
    ]
    assert outcome.account_metrics[0]["error_code"] == (
        "ACCOUNT_CONFIG_COMPATIBILITY_CONFLICT"
    )
    assert state_path.is_file()
    assert compatibility_path.read_text(encoding="utf-8") == (
        "conflicting archive\n"
    )
    assert historical.read_text(encoding="utf-8") == "historical\n"


def test_config_hash_drift_returns_typed_failure_before_pipeline_and_close_advice(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import account_run as account_run_mod
    from src.application import tick_account_execution as mod
    from src.application.tick_run_workspace import account_run_config_paths

    request = _request(
        tmp_path,
        accounts=["lx"],
        workers=1,
        force=False,
    )

    def _tamper_after_publication(**_kwargs):
        state_path, _compatibility_path = account_run_config_paths(
            base=tmp_path,
            run_id=request.run_id,
            account="lx",
        )
        state_path.write_text("{}\n", encoding="utf-8")
        return False

    monkeypatch.setattr(mod, "_account_pipeline_is_required", _tamper_after_publication)
    monkeypatch.setattr(
        account_run_mod,
        "ensure_account_output_dir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("account workspace must not be touched")
        ),
    )
    monkeypatch.setattr(
        account_run_mod,
        "run_pipeline_script",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("pipeline must not start")
        ),
    )
    monkeypatch.setattr(
        account_run_mod,
        "run_close_advice",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("Close Advice must not start")
        ),
    )

    outcome = mod.run_tick_account_execution(request)

    assert [item.decision_reason for item in outcome.results] == [
        "account_config_artifact_mismatch"
    ]
    assert outcome.account_metrics[0]["error_code"] == (
        "ACCOUNT_CONFIG_ARTIFACT_MISMATCH"
    )
    assert outcome.ran_pipeline_accounts == []


def test_config_failure_projection_does_not_follow_output_runs_symlink(
    tmp_path: Path,
) -> None:
    from src.application import tick_account_execution as mod
    from src.application.tick_run_workspace import AccountRunConfigError

    request = _request(
        tmp_path,
        accounts=["lx"],
        workers=1,
        force=False,
    )
    output_runs = tmp_path / "output_runs"
    preserved = tmp_path / "output_runs-preserved"
    output_runs.rename(preserved)
    outside = tmp_path / "outside"
    outside.mkdir()
    output_runs.symlink_to(outside, target_is_directory=True)

    outcome = mod._account_config_failure_outcome(
        request=request,
        account="lx",
        error=AccountRunConfigError(
            "ACCOUNT_CONFIG_PATH_UNSAFE",
            "unsafe run path",
        ),
        prefetch_done=False,
    )

    assert outcome.result.decision_reason == "account_config_path_unsafe"
    assert outcome.acct_metrics["error_code"] == "ACCOUNT_CONFIG_PATH_UNSAFE"
    assert list(outside.iterdir()) == []


def test_config_drift_isolated_to_one_account_before_shared_prefetch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import tick_account_execution as mod
    from src.application.tick_run_workspace import canonical_account_run_config_bytes
    from src.infrastructure.io_utils import atomic_write_json

    request = _request(
        tmp_path,
        accounts=["lx", "sy"],
        workers=2,
        force=False,
    )
    prepared_scopes: list[set[str]] = []
    prefetch_scopes: list[set[str]] = []
    account_children: list[str] = []

    def _scan_gate(**_kwargs):
        return True

    def _prepare(**kwargs):
        prepared_scopes.append(set(kwargs["account_config_authorities"]))
        prepared = _fake_prepare(**kwargs)
        authority = kwargs["account_config_authorities"]["lx"]
        replacement = json.loads(authority.canonical_bytes.decode("utf-8"))
        replacement.setdefault("runtime", {})["generation"] = "replacement"
        replacement_bytes = canonical_account_run_config_bytes(replacement)
        authority.state_path.write_bytes(replacement_bytes)
        authority.compatibility_path.write_bytes(replacement_bytes)
        return prepared

    def _prefetch(**kwargs):
        accounts = set(kwargs["cfg"].get("accounts") or [])
        prefetch_scopes.append(accounts)
        return {
            "global_required_data_plan": {
                "plan_id": "a" * 64,
                "symbols": [],
            },
            "symbols": [],
            "results": {},
        }

    def _seal(**kwargs):
        payload = {
            "schema_version": "required_data_snapshot_manifest.v1",
            "run_id": kwargs["run_id"],
            "status": "complete",
            "plan_id": "a" * 64,
            "symbols": {},
            "summary": {},
        }
        atomic_write_json(kwargs["manifest_path"], payload)
        return payload

    def _run_one_account(*, request, **_kwargs):
        account_children.append(request.acct)
        return mod.AccountRunOutcome(
            result=AccountResult(request.acct, True, False, "ok", ""),
            acct_metrics={"account": request.acct},
            prefetch_done=True,
            ran_pipeline=True,
        )

    monkeypatch.setattr(mod, "_account_pipeline_is_required", _scan_gate)
    monkeypatch.setattr(mod, "prepare_portfolio_contexts", _prepare)
    monkeypatch.setattr(
        mod,
        "_build_close_advice_barrier_plan",
        lambda **kwargs: (kwargs["candidate_config"], None),
    )
    monkeypatch.setattr(mod, "prefetch_required_data", _prefetch)
    monkeypatch.setattr(mod, "seal_required_data_snapshot", _seal)
    monkeypatch.setattr(mod, "run_one_account", _run_one_account)

    outcome = mod.run_tick_account_execution(request)

    assert prepared_scopes == [{"lx", "sy"}]
    assert len(prefetch_scopes) == 1
    assert account_children == ["sy"]
    assert [item.decision_reason for item in outcome.results] == [
        "account_config_parent_bytes_mismatch",
        "ok",
    ]
    assert outcome.ran_pipeline_accounts == ["sy"]


def test_runtime_snapshot_shadow_is_account_scoped_and_legacy_neutral(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import tick_account_execution as mod

    assembly_calls: list[dict] = []
    forbidden_reads = dict.fromkeys(
        ("ledger_prepare", "portfolio_prepare", "ledger_open", "ledger_list"),
        0,
    )
    audit = _Audit()
    runlog = _RunLog()
    request = SimpleNamespace(
        base=tmp_path,
        run_id="run-1",
        audit_helper=audit,
        runlog=runlog,
    )
    legacy = AccountResult("lx", True, True, "ok", "unchanged")
    legacy_before = vars(legacy).copy()
    required_path = tmp_path / "required.json"
    required_path.write_bytes(b"required")

    def _candidate_bundle(*, account: str, **_kwargs):
        account_dir = tmp_path / "output_runs" / "run-1" / "accounts" / account
        status_path = account_dir / "status.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_bytes(f"status:{account}".encode())
        return {
            "manifest": {
                "status_index": {"relpath": "status.json"},
                "owner_snapshots": [
                    {
                        "candidate_owner": "opening",
                        "relpath": "state/opening.json",
                    }
                ],
            }
        }

    def _assemble(**kwargs):
        assembly_calls.append(kwargs)
        account = kwargs["account"]
        return (
            {
                "account": account,
                "status": "trusted",
                "reason_codes": [],
                "seal": {"content_sha256": "a" * 64},
            },
            {"state/config.override.json": kwargs["account_config_bytes"]},
        )

    def _publish(*, snapshot: dict, **_kwargs):
        if snapshot["account"] == "sy":
            raise mod.RuntimePortfolioSnapshotError(
                "ACCOUNT_RUN_STATE_CONFLICT",
                "injected immutable conflict",
            )
        return tmp_path / "runtime_portfolio_snapshot.v1.json"

    def _forbidden(name):
        def _call(*_args, **_kwargs):
            forbidden_reads[name] += 1

        return _call

    for attribute, counter in {
        "prepare_option_positions_contexts": "ledger_prepare",
        "prepare_portfolio_contexts": "portfolio_prepare",
        "open_position_ledger_from_data_config": "ledger_open",
        "list_position_lot_snapshots": "ledger_list",
    }.items():
        monkeypatch.setattr(mod, attribute, _forbidden(counter))
    monkeypatch.setattr(
        mod,
        "load_account_run_config",
        lambda **kwargs: {"portfolio": {"account": kwargs["account"]}},
    )

    def _receipt(kind):
        return lambda **kwargs: {
            key: f"{kind}-{part}:{kwargs['expected_account']}".encode()
            for key, part in (
                ("manifest_bytes", "manifest"),
                ("payload_bytes", "payload"),
            )
        }

    monkeypatch.setattr(
        mod, "load_prepared_portfolio_context_receipt", _receipt("portfolio")
    )
    monkeypatch.setattr(
        mod, "load_prepared_option_positions_context_receipt", _receipt("option")
    )
    monkeypatch.setattr(mod, "load_candidate_snapshot_bundle", _candidate_bundle)
    monkeypatch.setattr(
        mod,
        "read_account_run_state_bytes_safely",
        lambda *, account, name, **_kwargs: f"{name}:{account}".encode(),
    )
    monkeypatch.setattr(mod, "assemble_runtime_portfolio_snapshot", _assemble)
    monkeypatch.setattr(mod, "publish_runtime_portfolio_snapshot", _publish)

    for account in ("lx", "sy"):
        authority = SimpleNamespace(
            account_config_sha256=hashlib.sha256(account.encode()).hexdigest(),
            canonical_bytes=f"config:{account}".encode(),
        )
        mod._publish_runtime_portfolio_snapshot_shadow(
            request=request,
            account=account,
            account_config_authority=authority,
            prepared_portfolio_manifest_path=tmp_path / f"portfolio-{account}",
            prepared_portfolio_manifest_sha256="b" * 64,
            prepared_option_manifest_path=tmp_path / f"option-{account}",
            prepared_option_manifest_sha256="c" * 64,
            required_data_manifest_path=required_path,
        )

    assert [call["account"] for call in assembly_calls] == ["lx", "sy"]
    assert [call["account_config_bytes"] for call in assembly_calls] == [
        b"config:lx",
        b"config:sy",
    ]
    assert not any(forbidden_reads.values())
    assert vars(legacy) == legacy_before
    assert [event["account"] for event in audit.events] == ["lx", "sy"]
    assert [event["status"] for event in audit.events] == ["ok", "error"]
    assert all(len(event["extra"]) <= 5 for event in audit.events)
    assert all(
        set(event["extra"])
        <= {
            "account",
            "snapshot_status",
            "reason_count",
            "content_sha256",
            "artifact_name",
            "error_type",
            "error_code",
        }
        for event in audit.events
    )
    assert [event["data"]["account"] for event in runlog.events] == ["lx", "sy"]
    assert runlog.events[1]["data"]["error_code"] == "ACCOUNT_RUN_STATE_CONFLICT"
