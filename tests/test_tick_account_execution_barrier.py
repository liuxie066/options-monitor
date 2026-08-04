from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application.multi_tick.misc import AccountResult


class _RunLog:
    def safe_event(self, *_args, **_kwargs):
        return None


class _Audit:
    def audit(self, *_args, **_kwargs):
        return None

    def fail_schema_validation(self, **_kwargs):
        return None


def _request(tmp_path: Path, *, accounts: list[str], workers: int, force: bool):
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
    )


def _fake_prepare(**kwargs):
    from src.infrastructure.io_utils import atomic_write_json

    out = {}
    for account, state_dir in kwargs["account_state_dirs"].items():
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        context = {
            "stocks_by_symbol": {
                "NVDA": {"avg_cost": 100, "shares": 100}
            }
        }
        context_path = state_dir / "portfolio_context.json"
        atomic_write_json(context_path, context)
        manifest_path = state_dir / "prepared_portfolio_context.v1.json"
        manifest = {
            "schema_version": "prepared_portfolio_context.v1",
            "run_id": kwargs["run_id"],
            "account": account,
            "status": "ready",
            "portfolio_context_relpath": context_path.name,
            "payload_sha256": hashlib.sha256(context_path.read_bytes()).hexdigest(),
        }
        atomic_write_json(manifest_path, manifest)
        out[account] = {**manifest, "manifest_path": str(manifest_path)}
    return out


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
    monkeypatch.setattr(mod, "run_one_account", fake_run_one_account)

    outcome = mod.run_tick_account_execution(
        _request(tmp_path, accounts=accounts, workers=workers, force=force)
    )

    assert len(prefetch_calls) == 1
    assert prefetch_calls[0]["force_refresh"] is force
    assert {item.acct for item in account_requests} == set(accounts)
    assert all(item.required_data_snapshot_manifest for item in account_requests)
    assert all(item.prepared_portfolio_context_manifest for item in account_requests)
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
                    "symbol": "NVDA",
                    "broker": "US",
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
                "symbol": "NVDA",
                "status": "open",
                "side": "short",
                "option_type": "put",
                "contracts": 1,
                "contracts_open": 1,
                "strike": 100,
                "expiration_ymd": "2026-08-28",
                "currency": "USD",
            },
        }
        for account in ("lx", "sy")
    ]
    ledger_reads: list[object] = []
    prefetch_calls: list[dict] = []
    account_requests = []

    monkeypatch.setattr(mod, "prepare_portfolio_contexts", _fake_prepare)
    monkeypatch.setattr(
        mod,
        "expiration_business_today",
        lambda _now: date(2026, 7, 29),
    )
    monkeypatch.setattr(
        mod,
        "resolve_position_data_config_path",
        lambda **_kwargs: tmp_path / "portfolio.runtime.json",
    )
    monkeypatch.setattr(
        mod,
        "open_position_ledger_from_data_config",
        lambda **_kwargs: (
            tmp_path / "portfolio.runtime.json",
            object(),
        ),
    )

    def _list_position_lot_snapshots(repo, **_kwargs):
        ledger_reads.append(repo)
        return records

    monkeypatch.setattr(
        mod,
        "list_position_lot_snapshots",
        _list_position_lot_snapshots,
    )

    def _prefetch(**kwargs):
        prefetch_calls.append(kwargs)
        return {
            "global_required_data_plan": {
                "plan_id": "a" * 64,
                "symbols": [
                    {
                        "symbol": "NVDA",
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

    assert len(ledger_reads) == 1
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
    from src.application.position_advice_source_receipts import sha256_bytes
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
    account_requests = []
    monkeypatch.setattr(
        mod,
        "load_required_data_snapshot_manifest",
        lambda **_kwargs: (manifest, request.shared_required.resolve()),
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
    assert len(account_requests) == 1
    assert (
        account_requests[0].close_advice_required_data_plan
        == plan_path.resolve()
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
