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
        self.events.append({"step": _args[0] if _args else None, "status": _args[1] if len(_args) > 1 else None, **kwargs})
        return None


class _Audit:
    def __init__(self):
        self.events = []

    def audit(self, *_args, **kwargs):
        self.events.append(kwargs)
        return None

    def fail_schema_validation(self, **_kwargs):
        return None


def _manifest_payload(*, run_id: str, status: str = "complete", symbols=None, summary=None) -> dict:
    """The minimal `required_data_snapshot_manifest.v1` payload the seal stubs write."""
    return {
        "schema_version": "required_data_snapshot_manifest.v1",
        "run_id": run_id,
        "status": status,
        "plan_id": "a" * 64,
        "symbols": {} if symbols is None else symbols,
        "summary": {} if summary is None else summary,
    }


def _seal_stub(*, status: str = "complete", symbols=None, summary=None):
    """A `seal_required_data_snapshot` stub that writes and returns the minimal manifest."""

    def _seal(**kwargs):
        from src.infrastructure.io_utils import atomic_write_json

        payload = _manifest_payload(
            run_id=kwargs["run_id"], status=status, symbols=symbols, summary=summary
        )
        atomic_write_json(kwargs["manifest_path"], payload)
        return payload

    return _seal


def _outcome(acct: str, outcome: str = "ok"):
    """The successful per-account outcome the `run_one_account` stubs return."""
    from src.application import tick_account_execution as mod

    return mod.AccountRunOutcome(
        result=AccountResult(acct, True, False, outcome, ""),
        acct_metrics={"account": acct},
        prefetch_done=True,
        ran_pipeline=True,
    )


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


def _lx_request(tmp_path: Path):
    """The single-account `lx` tick request shared by the barrier tests."""
    return _request(tmp_path, accounts=["lx"], workers=1, force=False)


def _fake_prepare(**kwargs):
    from src.infrastructure.io_utils import atomic_write_json

    out = {}
    for account, state_dir in kwargs["account_state_dirs"].items():
        authority = kwargs["account_config_authorities"][account]
        assert authority.state_path.is_file()
        assert authority.state_path.read_bytes() == authority.canonical_bytes
        assert not (authority.state_path.parent.parent / "config.override.json").exists()
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        context = _portfolio_context(account)
        if kwargs.get("fx_snapshot_sha256") is not None:
            context["fx_snapshot_sha256"] = kwargs["fx_snapshot_sha256"]
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
        if kwargs.get("fx_snapshot_sha256") is not None:
            manifest["run_fx_snapshot_sha256"] = kwargs["fx_snapshot_sha256"]
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


@pytest.mark.parametrize("smoke", [False, True])
def test_real_option_preparation_replays_fx_and_reports_tick_audit(monkeypatch, tmp_path, smoke):
    import subprocess

    from src.application import current_fx_run
    from src.application import prepared_option_positions_context as prepared
    from src.application import prepared_portfolio_context as portfolio
    from src.application import tick_account_execution as mod
    from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository

    env_file = tmp_path / "isolated.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("OM_ENV_FILE", str(env_file))
    monkeypatch.setenv("OM_RUNTIME_ROOT", "")
    monkeypatch.setenv("OM_DATA_CONFIG", "")
    request = replace(_request(tmp_path, accounts=["lx", "sy"], workers=1, force=False), smoke=smoke)
    request.cfg_path.write_text("{}\n", encoding="utf-8")
    paths = {}
    data_paths = {}
    for account in request.account_ids:
        data_path = tmp_path / account / "portfolio.runtime.json"
        data_path.parent.mkdir(parents=True)
        data_path.write_text("{}\n", encoding="utf-8")
        data_paths[account] = data_path
        paths[account] = data_path.parent / "output_shared" / "state" / "option_positions.sqlite3"
    real_config = mod.build_account_runtime_config
    real_prepare = prepared.prepare_option_positions_contexts
    real_import = PerformanceEvidenceSQLiteRepository.import_envelope
    batches = []
    children = []
    forbidden_calls = []
    imports = []
    fetches = []
    failed = False
    quoted = "2026-08-10T03:00:00+00:00"

    def forbidden(*_args, **_kwargs):
        forbidden_calls.append(True)
        pytest.fail("unexpected provider, process, bootstrap or shadow boundary")

    def account_config(**kwargs):
        config = real_config(**kwargs)
        config["portfolio"]["data_config"] = str(data_paths[kwargs["account"]])
        return config

    def fetch_fx(**kwargs):
        assert kwargs["cache_path"].is_relative_to(tmp_path)
        fetches.append(True)
        assert len(fetches) == 1
        return {"schema_version": 2, "pairs": {
            pair: {"rate": rate, "source": "tencent_quote",
                   "quote_at_utc": quoted, "observed_at_utc": quoted}
            for pair, rate in (("USDCNY", 7.2), ("HKDCNY", 0.92))
        }}

    def prepare_options(**kwargs):
        assert kwargs["base"] == tmp_path
        assert kwargs["persist_fx_evidence"] is (not smoke)
        batch = real_prepare(**kwargs)
        batches.append(batch)
        return batch

    def import_once(repo, *args, **kwargs):
        nonlocal failed
        assert repo.db_path in paths.values()
        imports.append(repo.db_path)
        if repo.db_path == paths["sy"] and not failed:
            failed = True
            raise OSError("synthetic first import unavailable")
        return real_import(repo, *args, **kwargs)

    def run_account(*, request, **_kwargs):
        assert request.allow_notifications is False
        assert request.allow_mutations is (not smoke)
        assert request.prepared_option_positions_context_manifest.is_relative_to(tmp_path)
        prepared.load_prepared_option_positions_context_receipt(
            manifest_path=request.prepared_option_positions_context_manifest,
            expected_base=tmp_path,
            expected_run_id=request.run_id,
            expected_account=request.acct,
            expected_account_config_sha256=request.account_config_authority.account_config_sha256,
            expected_manifest_sha256=request.prepared_option_positions_context_manifest_sha256,
            expected_runtime_config=json.loads(request.account_config_authority.canonical_bytes),
        )
        children.append(request.acct)
        return _outcome(request.acct)

    monkeypatch.setattr(mod, "build_account_runtime_config", account_config)
    monkeypatch.setattr(mod, "_account_pipeline_is_required", lambda **_kwargs: True)
    monkeypatch.setattr(current_fx_run, "current_exchange_rate_snapshot", fetch_fx)
    monkeypatch.setattr(prepared, "get_exchange_rates_or_fetch_latest", forbidden)
    monkeypatch.setattr(portfolio, "fetch_futu_portfolio_context", forbidden)
    monkeypatch.setattr(portfolio, "load_account_portfolio_context", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(mod, "prepare_portfolio_contexts", _fake_prepare)
    # Override the autouse fake with the actual owner and actual temporary evidence.
    monkeypatch.setattr(mod, "prepare_option_positions_contexts", prepare_options)
    monkeypatch.setattr(PerformanceEvidenceSQLiteRepository, "import_envelope", import_once)
    monkeypatch.setattr(mod, "_build_close_advice_barrier_plan", lambda **kw: (kw["candidate_config"], None))
    monkeypatch.setattr(mod, "open_position_ledger_from_data_config", forbidden)
    monkeypatch.setattr(mod, "decision_state_snapshot", forbidden)
    monkeypatch.setattr(mod, "prefetch_required_data", lambda **_kwargs: {
        "global_required_data_plan": {"plan_id": "a" * 64, "symbols": []},
        "symbols": [], "results": {},
    })
    monkeypatch.setattr(mod, "seal_required_data_snapshot", _seal_stub())
    monkeypatch.setattr(mod, "_retire_required_data_shadows_after_manifest", lambda **_kwargs: None)
    monkeypatch.setattr(mod, "_publish_runtime_portfolio_snapshot_shadow", forbidden)
    monkeypatch.setattr(mod, "run_one_account", run_account)
    first = mod.run_tick_account_execution(request)
    assert first.ran_pipeline_accounts == ["lx", "sy"]
    assert len(batches) == 1 and batches[0].unavailable_by_account == {}
    frozen = {
        account: (Path(manifest["manifest_path"]).read_bytes(),
                  (Path(manifest["manifest_path"]).parent / "option_positions_context.json").read_bytes())
        for account, manifest in batches[0].manifests.items()
    }
    assert PerformanceEvidenceSQLiteRepository(paths["sy"]).read_all().fx_rates == ()
    monkeypatch.setattr(prepared, "open_position_ledger_from_data_config", forbidden)
    monkeypatch.setattr(prepared, "read_decision_state_rows_many", forbidden)
    second = mod.run_tick_account_execution(request)
    assert second.ran_pipeline_accounts == ["lx", "sy"]
    assert len(batches) == 2 and batches[1].unavailable_by_account == {}
    assert children == ["lx", "sy", "lx", "sy"]
    assert forbidden_calls == [] and fetches == [True]
    assert batches[1].manifests == batches[0].manifests
    assert batches[1].observed_at_utc == batches[0].observed_at_utc
    assert batches[1].ledger_read_count == 0
    for account, manifest in batches[1].manifests.items():
        path = Path(manifest["manifest_path"])
        assert (path.read_bytes(), (path.parent / "option_positions_context.json").read_bytes()) == frozen[account]
        facts = PerformanceEvidenceSQLiteRepository(paths[account]).read_all().fx_rates
        assert len(facts) == (0 if smoke else 2)
        assert all(fact.source_id.startswith("verified_quote:tencent_quote:") for fact in facts)
    events = [event for event in request.runlog.events if event["step"] == "option_performance_fx_evidence"]
    if smoke:
        assert imports == [] and events == []
        assert [batch.fx_evidence_status for batch in batches] == ["disabled", "not_attempted"]
    else:
        assert imports == [paths[account] for _ in range(2) for account in ("lx", "sy")]
        assert [event["status"] for event in events] == ["degraded", "ok"]
        assert [event["data"] for event in events] == [
            {"status": "partial", "ledger_count": 1, "inserted_count": 2, "idempotent_count": 0, "error_count": 1},
            {"status": "persisted", "ledger_count": 2, "inserted_count": 2, "idempotent_count": 2, "error_count": 0},
        ]


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

    is_partial = snapshot_status == "partial"
    prefetch_payload = {
        "schema_version": "1.0",
        "errors": 1 if is_partial else 0,
        "symbols": [
            {"symbol": "NVDA", "status": "error" if is_partial else "ok"}
        ],
        "results": {"NVDA": "error" if is_partial else "ok"},
        "global_required_data_plan": {
            "plan_id": "a" * 64,
            "symbols": [{"symbol": "NVDA", "fetch_plan": {}}],
        },
        "quote_receipts": {"NVDA": "receipts/NVDA.json"},
        "audit": [
            {
                "symbol": "NVDA",
                "source": "opend",
                "status": "error" if is_partial else "fetched",
                "message": "typed provider failure" if is_partial else "fetched",
                "duration_sec": 0.125,
                "quote_source_receipt": "receipts/NVDA.json",
                **({"error_code": "RATE_LIMIT"} if is_partial else {}),
                "payload": {
                    "meta": {
                        "status": "error" if is_partial else "ok",
                        "error_code": "SNAPSHOT_COVERAGE_INCOMPLETE",
                        "errors": ["expiration coverage incomplete"],
                    },
                    "option_chain": [
                        {
                            "contract_code": "NVDA260918P00100000",
                            "raw": "x" * 8192,
                        }
                    ],
                },
            }
        ],
    }
    prefetch_payload_before = json.loads(json.dumps(prefetch_payload))
    prefetch_calls: list[dict] = []
    account_requests = []
    cleanup_calls: list[dict] = []
    fx_calls: list[str] = []
    fx_hash = "f" * 64

    def fake_seal_fx(**kwargs):
        fx_calls.append(kwargs["run_id"])
        return {"pairs": {}}, fx_hash

    def fake_prepare_with_fx(**kwargs):
        assert fx_calls == ["run-1"]
        assert kwargs["fx_snapshot_sha256"] == fx_hash
        return _fake_prepare(**kwargs)

    def fake_options_with_fx(**kwargs):
        from src.application.multi_tick_audit import prepared_context_phase
        assert kwargs["fx_snapshot_sha256"] == fx_hash
        with prepared_context_phase("ledger_read_a"):
            return _fake_prepare_options(**kwargs)

    def fake_prefetch(**kwargs):
        from src.application.multi_tick_audit import prepared_context_phase
        assert any(e.get("step") == "prepared_context_phase" and e.get("status") == "ok" and e["data"]["phase"] == "post_portfolio_prepare" for e in request.runlog.events)
        with prepared_context_phase("outside_scope"):
            pass
        prefetch_calls.append(kwargs)
        return prefetch_payload

    def fake_seal(**kwargs):
        assert kwargs["prefetch_summary"] is prefetch_payload
        assert kwargs["prefetch_summary"]["audit"][0]["payload"]["meta"][
            "error_code"
        ] == "SNAPSHOT_COVERAGE_INCOMPLETE"
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
        return _outcome(request.acct)

    monkeypatch.setattr(mod, "seal_run_fx_snapshot", fake_seal_fx)
    monkeypatch.setattr(mod, "prepare_portfolio_contexts", fake_prepare_with_fx)
    monkeypatch.setattr(mod, "prepare_option_positions_contexts", fake_options_with_fx)
    monkeypatch.setattr(mod, "prefetch_required_data", fake_prefetch)
    monkeypatch.setattr(mod, "seal_required_data_snapshot", fake_seal)
    monkeypatch.setattr(
        mod,
        "_retire_required_data_shadows_after_manifest",
        lambda **kwargs: cleanup_calls.append(kwargs),
    )
    monkeypatch.setattr(mod, "run_one_account", fake_run_one_account)

    request = _request(tmp_path, accounts=accounts, workers=workers, force=force)
    outcome = mod.run_tick_account_execution(request)
    phases = [e for e in request.runlog.events if e.get("step") == "prepared_context_phase"]
    completed = [e for e in phases if e["status"] == "ok"]
    assert phases[0]["data"]["phase"] == phases[-1]["data"]["phase"] == "post_portfolio_prepare"
    assert len([e for e in completed if e["data"]["phase"] == "ledger_read_a"]) == 1
    assert "outside_scope" not in {e["data"]["phase"] for e in phases}
    for phase in ("portfolio_authority", "portfolio_config", "portfolio_context", "prepared_option_authority"):
        assert {e["data"]["account"] for e in completed if e["data"]["phase"] == phase} == set(accounts)
    assert next(i for i, e in enumerate(request.runlog.events) if e.get("step") == "prepared_option_positions_context") < max(i for i, e in enumerate(request.runlog.events) if e.get("step") == "prepared_context_phase")

    assert len(prefetch_calls) == 1
    assert fx_calls == ["run-1"]
    assert prefetch_payload == prefetch_payload_before
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0]["trigger"] == "new_seal"
    assert cleanup_calls[0]["manifest_bytes"] == cleanup_calls[0][
        "manifest_path"
    ].read_bytes()
    assert prefetch_calls[0]["force_refresh"] is force
    assert {item.acct for item in account_requests} == set(accounts)
    assert all(item.required_data_snapshot_manifest for item in account_requests)
    assert all(item.prefetch_done is True for item in account_requests)
    assert all(item.prepared_portfolio_context_manifest for item in account_requests)
    assert all(
        item.prepared_option_positions_context_manifest
        for item in account_requests
    )
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
    persisted = json.loads(summaries[0])
    assert persisted["snapshot_manifest_sha256"] == hashlib.sha256(
        cleanup_calls[0]["manifest_bytes"]
    ).hexdigest()
    full_persisted = dict(prefetch_payload)
    for field in (
        "snapshot_manifest_relpath",
        "snapshot_manifest_sha256",
        "snapshot_status",
    ):
        full_persisted[field] = persisted[field]
    expected_persisted = dict(full_persisted)
    expected_persisted["audit"] = [
        {key: value for key, value in item.items() if key != "payload"}
        for item in full_persisted["audit"]
    ]
    assert persisted == expected_persisted
    expected_bytes = (
        json.dumps(expected_persisted, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    full_pretty_bytes = (
        json.dumps(full_persisted, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    assert summaries[0] == expected_bytes
    assert len(summaries[0]) * 2 < len(full_pretty_bytes)


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

    monkeypatch.setattr(mod, "seal_required_data_snapshot", _seal_stub())

    def _run_one_account(*, request, **_kwargs):
        captured.append(request)
        return _outcome(request.acct, "experience")

    monkeypatch.setattr(mod, "run_one_account", _run_one_account)

    outcome = mod.run_tick_account_execution(request)

    assert not any(e.get("step") == "prepared_context_phase" for e in request.runlog.events)
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
        "src.application.close_advice_required_data.close_advice_market_date",
        lambda _now, _market: date(2026, 7, 29),
    )
    monkeypatch.setattr(mod, "enrich_close_advice_required_data_plan_bounded", lambda **_kwargs: None)
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
        "decision_state_snapshot",
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
        payload = _manifest_payload(run_id=kwargs["run_id"])
        atomic_write_json(kwargs["manifest_path"], payload)
        return payload

    def _run_one_account(*, request, **_kwargs):
        account_requests.append(request)
        return _outcome(request.acct)

    monkeypatch.setattr(mod, "prefetch_required_data", _prefetch)
    monkeypatch.setattr(mod, "seal_required_data_snapshot", _seal)
    monkeypatch.setattr(mod, "run_one_account", _run_one_account)

    outcome = mod.run_tick_account_execution(request)

    assert len(prepared_option_calls) == 1
    assert "mark_evidence_accounts" not in prepared_option_calls[0]
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

    request = _lx_request(tmp_path)
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


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_required_data_shadow_cleanup_is_observable_and_nonfatal(
    monkeypatch,
    tmp_path: Path,
    cleanup_fails: bool,
) -> None:
    from src.application import tick_account_execution as mod

    request = _lx_request(tmp_path)
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


@pytest.mark.parametrize("write_failure_account", [None, "lx"])
@pytest.mark.parametrize(
    ("seal_behavior", "reason"),
    [
        ("failed", "required_data_snapshot_failed"),
        ("raise", "required_data_snapshot_manifest_unavailable"),
    ],
)
def test_terminal_barrier_failure_returns_typed_account_outcomes_without_pipeline(
    monkeypatch,
    tmp_path: Path,
    write_failure_account: str | None,
    seal_behavior: str,
    reason: str,
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
        payload = _manifest_payload(
            run_id=kwargs["run_id"], status="failed", summary={"ready": 0, "failed": 1}
        )
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

    if write_failure_account is not None:
        write_state = mod.state_repo.write_account_run_state

        def fail_one_metrics(base, run_id, account, name, payload):
            if account == write_failure_account and name == "account_metrics.json":
                raise OSError("metrics disk unavailable")
            return write_state(base, run_id, account, name, payload)

        monkeypatch.setattr(mod.state_repo, "write_account_run_state", fail_one_metrics)

    request = _request(
        tmp_path,
        accounts=["lx", "sy"],
        workers=2,
        force=False,
    )
    outcome = mod.run_tick_account_execution(request)

    assert outcome.ran_pipeline_accounts == []
    assert [item.decision_reason for item in outcome.results] == [reason, reason]
    assert all(item.should_notify is True for item in outcome.results)
    assert set(outcome.scheduled_scan_targets_by_account) == {"lx", "sy"}
    assert all(
        item["ran_pipeline"] is False
        and item["snapshot_status"] in {"failed", "unavailable"}
        for item in outcome.account_metrics
    )
    if write_failure_account is not None:
        assert not (
            tmp_path / "output_runs/run-1/accounts/lx/state/account_metrics.json"
        ).exists()
        assert (
            tmp_path / "output_runs/run-1/accounts/sy/state/account_metrics.json"
        ).is_file()
        assert any(
            item.get("account") == "lx" and item.get("status") == "degraded"
            for item in request.audit_helper.events
        )
        assert any(
            item.get("error_code") == "ACCOUNT_METRICS_WRITE_FAILED"
            for item in request.runlog.events
        )


def test_barrier_and_prefetch_writers_do_not_follow_output_runs_symlink(
    tmp_path: Path,
) -> None:
    from domain.storage.no_follow import UnsafePathError
    from domain.storage.repositories import state_repo
    from src.application import tick_account_execution as mod
    from src.application.multi_tick_audit import MultiTickAuditHelper

    request = _lx_request(tmp_path)
    request = replace(
        request,
        audit_helper=MultiTickAuditHelper(
            base=tmp_path,
            base_cfg={},
            runlog=request.runlog,
            safe_data_fn=dict,
            append_audit_event=state_repo.append_audit_event,
            record_project_failure=lambda *_args, **_kwargs: {},
            record_project_success=lambda *_args, **_kwargs: {},
            build_failure_audit_fields=lambda *_args, **_kwargs: {},
            run_id=request.run_id,
            idempotency_key="issue-460",
            write_run_artifacts=True,
        ),
    )
    output_runs = tmp_path / "output_runs"
    output_runs.rename(tmp_path / "output_runs-preserved")
    outside = tmp_path / "outside"
    outside.mkdir()
    output_runs.symlink_to(outside, target_is_directory=True)

    outcomes = mod._terminal_barrier_outcomes(
        request=request,
        scanning_accounts={"lx"},
        barrier_reason="required_data_snapshot_failed",
        snapshot_status="failed",
        run_account_fn=lambda _account: (_ for _ in ()).throw(
            AssertionError("account pipeline must not start")
        ),
    )
    assert [item.result.account for item in outcomes] == ["lx"]
    assert outcomes[0].result.should_notify is True
    with pytest.raises(UnsafePathError):
        mod._publish_prefetch_summary_to_accounts(
            request=request, accounts=["lx"], payload={"status": "failed"}
        )
    assert list(outside.iterdir()) == []
    shared_audit = tmp_path / "output_shared/state/audit_events.jsonl"
    assert any(
        item.get("action") == "account_metrics_write_failed"
        for item in (
            json.loads(line)
            for line in shared_audit.read_text(encoding="utf-8").splitlines()
        )
    )


def test_barrier_metrics_and_telemetry_failure_keeps_account_outcome(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import tick_account_execution as mod

    request = _lx_request(tmp_path)
    monkeypatch.setattr(
        mod.state_repo,
        "write_account_run_state",
        lambda *_args: (_ for _ in ()).throw(OSError("metrics disk unavailable")),
    )
    request.audit_helper.audit = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError("audit unavailable")
    )
    request.runlog.safe_event = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError("runlog unavailable")
    )

    outcomes = mod._terminal_barrier_outcomes(
        request=request,
        scanning_accounts={"lx"},
        barrier_reason="required_data_snapshot_failed",
        snapshot_status="failed",
        run_account_fn=lambda _account: (_ for _ in ()).throw(
            AssertionError("account pipeline must not start")
        ),
    )
    assert len(outcomes) == 1
    assert outcomes[0].result.decision_reason == "required_data_snapshot_failed"
    assert outcomes[0].result.should_notify is True


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
        payload = _manifest_payload(
            run_id=kwargs["run_id"],
            symbols={"3690.HK": {"status": "ready", "market_fact": fact}},
            summary={"ready": 1, "failed": 0},
        )
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
        return _outcome(request.acct)

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


def test_config_hash_drift_returns_typed_failure_before_pipeline_and_close_advice(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import account_run as account_run_mod
    from src.application import tick_account_execution as mod
    from src.application.tick_run_workspace import account_run_config_path

    request = _lx_request(tmp_path)

    def _tamper_after_publication(**_kwargs):
        state_path = account_run_config_path(
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
        "account_config_parent_bytes_mismatch"
    ]
    assert outcome.account_metrics[0]["error_code"] == (
        "ACCOUNT_CONFIG_PARENT_BYTES_MISMATCH"
    )
    assert outcome.ran_pipeline_accounts == []


def test_config_failure_projection_does_not_follow_output_runs_symlink(
    tmp_path: Path,
) -> None:
    from src.application import tick_account_execution as mod
    from src.application.tick_run_workspace import AccountRunConfigError

    request = _lx_request(tmp_path)
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


@pytest.mark.parametrize(
    ("accounts", "expected_healthy"),
    [(["lx"], []), (["lx", "sy"], ["sy"])],
)
def test_unavailable_prepared_context_fails_closed_before_shared_prefetch(
    monkeypatch,
    tmp_path: Path,
    accounts: list[str],
    expected_healthy: list[str],
) -> None:
    from src.application import tick_account_execution as mod
    from src.infrastructure.io_utils import atomic_write_json

    request = _request(
        tmp_path,
        accounts=accounts,
        workers=len(accounts),
        force=False,
    )
    build_prefetch_config = Mock(wraps=mod.build_cross_account_prefetch_config)
    prepare_options = Mock(wraps=_fake_prepare_options)
    prefetch = Mock(
        return_value={
            "global_required_data_plan": {
                "plan_id": "a" * 64,
                "symbols": [],
            },
            "symbols": [],
            "results": {},
        }
    )
    account_children: list[str] = []

    def _prepare(**kwargs):
        prepared = _fake_prepare(**kwargs)
        authority = kwargs["account_config_authorities"]["lx"]
        manifest_path = Path(prepared["lx"]["manifest_path"])
        manifest = {
            "schema_version": "prepared_portfolio_context.v1",
            "run_id": kwargs["run_id"],
            "account": "lx",
            "status": "unavailable",
            "account_config_sha256": authority.account_config_sha256,
            "reason": "portfolio_context_unavailable",
        }
        atomic_write_json(manifest_path, manifest)
        prepared["lx"] = {
            **manifest,
            "manifest_path": str(manifest_path),
            "manifest_sha256": hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
        }
        return prepared

    def _seal(**kwargs):
        payload = _manifest_payload(run_id=kwargs["run_id"])
        atomic_write_json(kwargs["manifest_path"], payload)
        return payload

    def _run_one_account(*, request, **_kwargs):
        account_children.append(request.acct)
        return _outcome(request.acct)

    monkeypatch.setattr(mod, "_account_pipeline_is_required", lambda **_kwargs: True)
    monkeypatch.setattr(mod, "prepare_portfolio_contexts", _prepare)
    monkeypatch.setattr(mod, "prepare_option_positions_contexts", prepare_options)
    monkeypatch.setattr(
        mod,
        "build_cross_account_prefetch_config",
        build_prefetch_config,
    )
    monkeypatch.setattr(
        mod,
        "_build_close_advice_barrier_plan",
        lambda **kwargs: (kwargs["candidate_config"], None),
    )
    monkeypatch.setattr(mod, "prefetch_required_data", prefetch)
    monkeypatch.setattr(mod, "seal_required_data_snapshot", _seal)
    monkeypatch.setattr(mod, "run_one_account", _run_one_account)

    outcome = mod.run_tick_account_execution(request)

    expected_scope = set(expected_healthy)
    assert build_prefetch_config.call_count == 1
    planner_kwargs = build_prefetch_config.call_args.kwargs
    assert set(planner_kwargs["account_configs"]) == expected_scope
    assert set(planner_kwargs["prepared_portfolio_contexts"]) == expected_scope
    assert prepare_options.call_count == 1
    assert (
        set(prepare_options.call_args.kwargs["account_config_authorities"])
        == expected_scope
    )
    assert prefetch.call_count == (1 if expected_healthy else 0)
    assert account_children == expected_healthy
    assert outcome.ran_pipeline_accounts == expected_healthy
    results_by_account = {result.account: result for result in outcome.results}
    metrics_by_account = {
        str(metrics["account"]): metrics for metrics in outcome.account_metrics
    }
    assert results_by_account["lx"].decision_reason == (
        "account_config_prepared_context_invalid"
    )
    assert results_by_account["lx"].should_notify is False
    assert metrics_by_account["lx"]["error_code"] == (
        "ACCOUNT_CONFIG_PREPARED_CONTEXT_INVALID"
    )
    assert metrics_by_account["lx"]["error"] == "portfolio_context_unavailable"
    assert metrics_by_account["lx"]["ran_scan"] is False
    assert metrics_by_account["lx"]["ran_pipeline"] is False
    assert metrics_by_account["lx"]["should_notify"] is False


@pytest.mark.parametrize("healthy_account", [None, "lx"], ids=["all-overdue", "healthy-peer"])
def test_real_preparation_deadline_excludes_accounts_from_tick(
    monkeypatch, tmp_path: Path, healthy_account,
) -> None:
    import subprocess

    from src.application import prepared_portfolio_context as prepared
    from src.application import tick_account_execution as mod

    request = _request(tmp_path, accounts=["lx", "sy"], workers=1, force=False)
    clock = SimpleNamespace(now=0.0)
    forbidden_calls = []
    workers = {}
    result_reads = []
    source_events = []
    account_children = []
    real_prepare = prepared.prepare_portfolio_contexts
    real_read_result = prepared._read_worker_result
    build_prefetch_config = Mock(wraps=mod.build_cross_account_prefetch_config)
    prepare_options = Mock(wraps=_fake_prepare_options)
    prefetch = Mock(return_value={
        "global_required_data_plan": {"plan_id": "a" * 64, "symbols": []},
        "symbols": [], "results": {},
    })

    def _forbidden(*_args, **_kwargs):
        forbidden_calls.append(True)
        raise AssertionError("unexpected provider, subprocess or sleeping boundary")

    class _Worker:
        returncode = 0

        def __init__(self, command, **_kwargs):
            worker_request = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
            self.account = worker_request["account"]
            self.terminated = False
            self.waited = 0
            workers[self.account] = self
            context = {
                **_portfolio_context(self.account),
                "cash_by_currency": {},
                "fx_snapshot_sha256": worker_request["fx_snapshot_sha256"],
            }
            payload_digest = hashlib.sha256(json.dumps(
                context, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            Path(worker_request["result_path"]).write_text(json.dumps({
                "schema_version": "prepared_portfolio_context_worker_result.v1",
                "token": worker_request["token"],
                "run_id": worker_request["run_id"],
                "account": self.account,
                "account_config_sha256": worker_request["account_config_sha256"],
                "status": "ready",
                "portfolio_context": context,
                "payload_sha256": payload_digest,
                "portfolio_source_name": "futu",
                "portfolio_source_account": self.account,
            }), encoding="utf-8")

        def poll(self):
            if self.account != healthy_account:
                clock.now = 1.0
            return self.returncode

        def terminate(self):
            self.terminated = True
            raise ProcessLookupError("synthetic worker already exited")

        def wait(self, timeout=None):
            self.waited += 1
            return self.returncode

        def kill(self):
            _forbidden()

    def _prepare(**kwargs):
        assert kwargs["base"] == tmp_path
        assert kwargs["timeout_sec"] == 1
        return real_prepare(**kwargs, popen_factory=_Worker)

    def _read_result(**kwargs):
        result_reads.append(kwargs["request"]["account"])
        return real_read_result(**kwargs)

    def _run_one_account(*, request, **_kwargs):
        account_children.append(request.acct)
        assert request.acct == healthy_account
        assert request.allow_notifications is False
        loaded = prepared.load_prepared_portfolio_context(
            manifest_path=request.prepared_portfolio_context_manifest,
            expected_base=tmp_path,
            expected_run_id=request.run_id,
            expected_account=request.acct,
            expected_account_config_sha256=request.account_config_authority.account_config_sha256,
            expected_manifest_sha256=request.prepared_portfolio_context_manifest_sha256,
            expected_runtime_config=json.loads(request.account_config_authority.canonical_bytes),
        )
        assert loaded["filters"]["account"] == request.acct
        assert loaded["fx_snapshot_sha256"] == "f" * 64
        return _outcome(request.acct)

    monkeypatch.setattr(prepared, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=_forbidden))
    monkeypatch.setattr(prepared, "_read_worker_result", _read_result)
    monkeypatch.setattr(prepared, "run_worker", _forbidden)
    monkeypatch.setattr(prepared, "fetch_futu_portfolio_context", _forbidden)
    monkeypatch.setattr(prepared, "load_account_portfolio_context", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(prepared.state_repo, "append_source_snapshot_event",
                        lambda _base, snapshot: source_events.append(snapshot))
    monkeypatch.setattr(mod, "open_position_ledger_from_data_config", _forbidden)
    monkeypatch.setattr(mod, "decision_state_snapshot", _forbidden)
    monkeypatch.setattr(mod, "seal_run_fx_snapshot", lambda **_kwargs: ({}, "f" * 64))
    monkeypatch.setattr(mod, "_account_pipeline_is_required", lambda **_kwargs: True)
    monkeypatch.setattr(mod, "prepare_portfolio_contexts", _prepare)
    monkeypatch.setattr(mod, "prepare_option_positions_contexts", prepare_options)
    monkeypatch.setattr(mod, "build_cross_account_prefetch_config", build_prefetch_config)
    monkeypatch.setattr(mod, "_build_close_advice_barrier_plan",
                        lambda **kwargs: (kwargs["candidate_config"], None))
    monkeypatch.setattr(mod, "prefetch_required_data", prefetch)
    monkeypatch.setattr(mod, "seal_required_data_snapshot", _seal_stub())
    monkeypatch.setattr(mod, "_retire_required_data_shadows_after_manifest", lambda **_kwargs: None)
    monkeypatch.setattr(mod, "run_one_account", _run_one_account)

    outcome = mod.run_tick_account_execution(request)

    expected_healthy = [healthy_account] if healthy_account else []
    assert forbidden_calls == []
    assert set(workers) == {"lx", "sy"}
    assert result_reads == expected_healthy
    assert [item["payload"]["filters"]["account"] for item in source_events] == expected_healthy
    assert build_prefetch_config.call_count == 1
    planner_kwargs = build_prefetch_config.call_args.kwargs
    assert set(planner_kwargs["account_configs"]) == set(expected_healthy)
    assert set(planner_kwargs["prepared_portfolio_contexts"]) == set(expected_healthy)
    assert prepare_options.call_count == 1
    assert set(prepare_options.call_args.kwargs["account_config_authorities"]) == set(expected_healthy)
    assert prefetch.call_count == len(expected_healthy)
    assert account_children == expected_healthy
    assert outcome.ran_pipeline_accounts == expected_healthy
    results = {item.account: item for item in outcome.results}
    metrics = {item["account"]: item for item in outcome.account_metrics}
    prepared_metrics = {item["account"]: item for item in outcome.prepared_context_metrics}
    for account in {"lx", "sy"} - set(expected_healthy):
        state = tmp_path / "output_runs" / request.run_id / "accounts" / account / "state"
        manifest = json.loads((state / "prepared_portfolio_context.v1.json").read_bytes())
        assert manifest["status"] == "unavailable"
        assert manifest["reason"] == "portfolio_context_deadline_exceeded"
        assert manifest["worker_returncode"] == 0
        assert not list(state.glob("portfolio_context.*.json"))
        assert workers[account].terminated and workers[account].waited == 1
        assert prepared_metrics[account]["reason"] == "portfolio_context_deadline_exceeded"
        assert results[account].decision_reason == "account_config_prepared_context_invalid"
        assert results[account].should_notify is False
        assert metrics[account]["error_code"] == "ACCOUNT_CONFIG_PREPARED_CONTEXT_INVALID"
        assert metrics[account]["error"] == "portfolio_context_deadline_exceeded"
        assert metrics[account]["ran_scan"] is False
        assert metrics[account]["ran_pipeline"] is False
        assert metrics[account]["should_notify"] is False


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
        payload = _manifest_payload(run_id=kwargs["run_id"])
        atomic_write_json(kwargs["manifest_path"], payload)
        return payload

    def _run_one_account(*, request, **_kwargs):
        account_children.append(request.acct)
        return _outcome(request.acct)

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


def test_runtime_snapshot_shadow_is_account_scoped_and_current_only(
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
    manifest_schema = "candidate_snapshot_manifest.v4"
    manifest_name = "candidate_snapshot_manifest.v4.json"
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
                "schema_version": manifest_schema,
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
        return tmp_path / "runtime_portfolio_snapshot.v2.json"

    def _forbidden(name):
        def _call(*_args, **_kwargs):
            forbidden_reads[name] += 1

        return _call

    for attribute, counter in {
        "prepare_option_positions_contexts": "ledger_prepare",
        "prepare_portfolio_contexts": "portfolio_prepare",
        "open_position_ledger_from_data_config": "ledger_open",
        "decision_state_snapshot": "ledger_list",
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
    assert [call["candidate_manifest_bytes"] for call in assembly_calls] == [
        f"{manifest_name}:lx".encode(),
        f"{manifest_name}:sy".encode(),
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


def test_real_holdings_fallback_shadow_publishes_replays_and_audits_each_account(monkeypatch, tmp_path):
    from scripts import benchmark_runtime_portfolio_snapshot as fixture_owner
    from tests import test_runtime_portfolio_snapshot as snapshot_fixture
    from tests.test_prepared_portfolio_context import _prepare_cached_holdings_receipts
    from src.application import runtime_portfolio_snapshot as snapshot_owner
    from src.application import tick_account_execution as mod

    run_id = fixture_owner.RUN_ID
    prepared = _prepare_cached_holdings_receipts(tmp_path, monkeypatch, run_id=run_id, ages={"lx": 400, "sy": 0})
    audit, runlog = _Audit(), _RunLog()
    request = SimpleNamespace(base=tmp_path, run_id=run_id, audit_helper=audit, runlog=runlog)
    snapshots, references_by_account, option_receipts, candidates = {}, {}, {}, {}
    sealed_files, tasks = {}, []
    forbidden_calls = []

    def forbidden(*args, **kwargs):
        forbidden_calls.append((args, kwargs))
        raise AssertionError("shadow must only consume sealed owners")

    for name in ("prepare_portfolio_contexts", "prepare_option_positions_contexts", "open_position_ledger_from_data_config",
                 "decision_state_snapshot", "run_one_account", "prefetch_required_data", "seal_run_fx_snapshot",
                 "retire_required_data_snapshot_shadows"):
        monkeypatch.setattr(mod, name, forbidden)
    for account in ("lx", "sy"):
        authority = prepared.authorities[account]
        # Customize only these synthetic accounts/configs. Default golden generation remains hash checked.
        with monkeypatch.context() as fixture_patch:
            fixture_patch.setattr(fixture_owner, "ACCOUNT", account)
            fixture_patch.setattr(fixture_owner, "canonical_account_run_config_bytes", lambda _config: authority.canonical_bytes)
            assembly = snapshot_fixture._owner_assembly_kwargs(verify_fixture_hash=False)
        receipt = prepared.receipts[account]
        assembly.update(prepared_portfolio_manifest_bytes=receipt["manifest_bytes"],
                        prepared_portfolio_payload_bytes=receipt["payload_bytes"])
        snapshot, references = snapshot_owner.assemble_runtime_portfolio_snapshot(**assembly)
        snapshots[account], references_by_account[account] = snapshot, references
        account_dir = tmp_path / "output_runs" / run_id / "accounts" / account
        # Portfolio/config already exist through the real parent; assert bytes instead of replacing them.
        required_binding = next(row for row in snapshot["replay_bindings"] if row["role"] == "required_data_snapshot")
        for relpath, raw in references.items():
            root = tmp_path / "output_runs" / run_id if relpath == required_binding["relpath"] else account_dir
            path = root / relpath
            assert path.is_relative_to(tmp_path)
            if path.exists():
                assert path.read_bytes() == raw
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            sealed_files[path] = raw
        option_manifest = json.loads(assembly["prepared_option_manifest_bytes"])
        option_path = account_dir / "state" / option_manifest["payload_relpath"]
        option_path.write_bytes(assembly["prepared_option_payload_bytes"])
        sealed_files[option_path] = assembly["prepared_option_payload_bytes"]
        option_receipts[account] = {"manifest_bytes": assembly["prepared_option_manifest_bytes"],
                                    "payload_bytes": assembly["prepared_option_payload_bytes"]}
        candidates[account] = {"manifest": json.loads(assembly["candidate_manifest_bytes"])}
        option_binding = next(row for row in snapshot["replay_bindings"] if row["role"] == "prepared_option_positions_context")
        tasks.append(SimpleNamespace(account=account, account_config_authority=authority,
            prepared_portfolio_manifest_path=Path(prepared.manifests[account]["manifest_path"]),
            prepared_portfolio_manifest_sha256=prepared.manifests[account]["manifest_sha256"],
            prepared_option_manifest_path=account_dir / option_binding["relpath"],
            prepared_option_manifest_sha256=option_binding["sha256"],
            required_data_manifest_path=tmp_path / "output_runs" / run_id / required_binding["relpath"]))

    # Unchanged ledger/candidate producers are isolated at their read facade; all supplied bytes
    # still pass the real assembler's canonical owner/hash/config validation.
    def option_receipt(**kwargs):
        account = kwargs["expected_account"]
        assert kwargs["expected_account_config_sha256"] == prepared.authorities[account].account_config_sha256
        return option_receipts[account]

    monkeypatch.setattr(mod, "load_prepared_option_positions_context_receipt", option_receipt)
    monkeypatch.setattr(mod, "load_candidate_snapshot_bundle", lambda *, account, **kwargs: candidates[account])
    legacy = [AccountResult(account, True, True, "ok", "unchanged") for account in ("lx", "sy")]
    legacy_before = [vars(result).copy() for result in legacy]
    artifact_bytes = {}
    for _ in range(2):
        mod.publish_runtime_portfolio_snapshot_shadows(request=request, tasks=tuple(tasks))
        for account in ("lx", "sy"):
            loaded = snapshot_owner.load_runtime_portfolio_snapshot(base=tmp_path, run_id=run_id, account=account,
                reference_payloads=references_by_account[account])
            assert loaded == snapshots[account]
            assert loaded["status"] == ("data_unavailable" if account == "lx" else "trusted")
            assert loaded["sections"]["broker_positions"]["completeness"] == (
                {"status": "unavailable", "reason_codes": ["position_snapshot_refresh_unavailable"]} if account == "lx"
                else {"status": "complete", "reason_codes": []})
            cash = loaded["sections"]["broker_cash"]["facts"]
            assert cash["cash_snapshot"] == prepared.cached[account]["cash_snapshot"]
            assert cash["cash_by_currency"] == prepared.cached[account]["cash_by_currency"]
            assert cash["cash_by_currency"]["CNY"] == 1000
            assert cash["cash_snapshot"]["status"] == "fresh"
            path = tmp_path / "output_runs" / run_id / "accounts" / account / "state" / snapshot_owner.ARTIFACT_NAME
            encoded = path.read_bytes()
            assert encoded == snapshot_owner.canonical_json_bytes(loaded)
            assert encoded == artifact_bytes.setdefault(account, encoded)
            cache = prepared.authorities[account].state_path.parent / "portfolio_context.json"
            assert cache.read_bytes() == prepared.cache_bytes[account]
        assert all(path.read_bytes() == raw for path, raw in sealed_files.items())
    assert [call["account"] for call in prepared.provider_calls] == ["lx"]
    assert not forbidden_calls
    assert [vars(result) for result in legacy] == legacy_before
    assert [event["account"] for event in audit.events] == ["lx", "sy", "lx", "sy"]
    assert [event["status"] for event in audit.events] == ["error", "ok", "error", "ok"]
    assert all("error_type" not in event["extra"] for event in audit.events)
    assert [event["status"] for event in runlog.events if event["step"] == "runtime_portfolio_snapshot"] == ["degraded", "ok", "degraded", "ok"]


def test_scheduler_skip_does_not_emit_preparation_timings(monkeypatch, tmp_path):
    from src.application import tick_account_execution as mod
    request = _lx_request(tmp_path)
    monkeypatch.setattr(mod, "_account_pipeline_is_required", lambda **_kwargs: False)
    monkeypatch.setattr(mod, "run_one_account", lambda **_kwargs: replace(_outcome("lx", "skipped"), ran_pipeline=False))
    prepare = Mock(side_effect=AssertionError("must not prepare"))
    monkeypatch.setattr(mod, "prepare_portfolio_contexts", prepare)
    outcome = mod.run_tick_account_execution(request)
    prepare.assert_not_called()
    assert outcome.ran_pipeline_accounts == []
    assert not any(e.get("step") == "prepared_context_phase" for e in request.runlog.events)


@pytest.mark.parametrize("failed_phase", ["portfolio_config", "portfolio_context", "option_prepare", "logger"])
def test_preparation_timing_failure_keeps_tick_account_outcomes(monkeypatch, tmp_path, failed_phase):
    from src.application import tick_account_execution as mod
    request = _request(tmp_path, accounts=["lx", "sy"], workers=1, force=False)
    promoted = False
    original_config = mod.load_account_run_config
    original_portfolio = mod.load_prepared_portfolio_context
    def _prepare(**kwargs):
        nonlocal promoted
        result = _fake_prepare(**kwargs)
        promoted = True
        return result
    def _config(**kwargs):
        if failed_phase == "portfolio_config" and promoted and kwargs["account"] == "lx":
            raise mod.AccountRunConfigError("ACCOUNT_CONFIG_READ_FAILED", "private detail")
        return original_config(**kwargs)
    def _portfolio(**kwargs):
        if failed_phase == "portfolio_context" and kwargs["expected_account"] == "lx":
            raise mod.PreparedPortfolioContextError("private detail")
        return original_portfolio(**kwargs)
    if failed_phase == "logger":
        original_event = request.runlog.safe_event
        def _event(*args, **kwargs):
            if args[0] == "prepared_context_phase":
                raise OSError("log failure")
            return original_event(*args, **kwargs)
        request.runlog.safe_event = _event
    monkeypatch.setattr(mod, "seal_run_fx_snapshot", lambda **_kwargs: ({}, "f" * 64))
    monkeypatch.setattr(mod, "prepare_portfolio_contexts", _prepare)
    monkeypatch.setattr(mod, "load_account_run_config", _config)
    monkeypatch.setattr(mod, "load_prepared_portfolio_context", _portfolio)
    if failed_phase == "option_prepare":
        monkeypatch.setattr(mod, "prepare_option_positions_contexts", Mock(side_effect=OSError("private detail")))
    monkeypatch.setattr(mod, "_build_close_advice_barrier_plan", lambda **kwargs: (kwargs["candidate_config"], None))
    monkeypatch.setattr(mod, "prefetch_required_data", lambda **_kwargs: {
        "global_required_data_plan": {"plan_id": "a" * 64, "symbols": []}, "symbols": [], "results": {},
    })
    monkeypatch.setattr(mod, "seal_required_data_snapshot", _seal_stub())
    children = []
    def _run(*, request, **_kwargs):
        children.append(request.acct)
        return _outcome(request.acct)
    monkeypatch.setattr(mod, "run_one_account", _run)
    outcome = mod.run_tick_account_execution(request)
    expected = ["lx", "sy"] if failed_phase == "logger" else [] if failed_phase == "option_prepare" else ["sy"]
    assert children == outcome.ran_pipeline_accounts == expected
    phases = [e for e in request.runlog.events if e.get("step") == "prepared_context_phase"]
    if failed_phase == "logger":
        assert phases == []
    else:
        assert any(e["status"] == "error" and e["data"]["phase"] == failed_phase for e in phases)
        assert "private detail" not in str(phases)
