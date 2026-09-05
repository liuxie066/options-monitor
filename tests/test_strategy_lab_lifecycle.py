from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.candidate_snapshot_manifest import load_candidate_snapshot_bundle
from src.application.opend_fetch_config import OpenDEndpointRateLimit
from src.application.opend_market_snapshot_fetching import MarketSnapshotFetchResult
from src.application.opening_candidate_snapshot import ranked_opening_candidate_decisions
from src.application.strategy_lab.contracts import (
    HIDDEN_SNAPSHOT_BATCH_CEILING,
    TICK_PROTECTION_SECONDS,
    VALIDATION_WAKE_TOLERANCE_SECONDS,
    build_strategy_lab_timer_binding,
)
from src.application.strategy_lab.recipe import build_concentration_arms
from src.application.strategy_lab.service import (
    advance_experiment,
    confirm_research,
    confirm_validation,
    execute_research,
    read_receipt,
)
from src.infrastructure.strategy_lab.experiment_store import ExperimentStore
from tests.candidate_evidence_helpers import seal_opening_candidate_fixture


SOURCE = "b" * 40
EXPIRATION = "2026-10-30"
BASELINE_CONTRACT = "HK.TCH261030P300000"
CHALLENGER_CONTRACT = "HK.POP261030P145000"


def _trading_days(start: str, count: int) -> list[str]:
    current = date.fromisoformat(start)
    values: list[str] = []
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def _candidate(symbol: str, contract: str, strike: float, period_return: float) -> dict[str, object]:
    return {
        "symbol": symbol,
        "contract_symbol": contract,
        "option_type": "put",
        "expiration": EXPIRATION,
        "strike": strike,
        "currency": "HKD",
        "sell_limit": 1.0,
        "price_tick": 0.01,
        "multiplier": 100,
        "period_net_return_on_cash_basis": period_return,
        "net_assignment_discount_pct": 0.01,
        "spread_ratio": 0.1,
        "open_interest": 100,
        "net_income": 100 if symbol == "0700.HK" else 120,
    }


def _formal_point(tmp_path: Path, trading_day: str, account_config_sha256: str) -> dict[str, object]:
    compact_day = trading_day.replace("-", "")
    target = f"{trading_day}T01:50:00Z"
    opening = load_candidate_snapshot_bundle(
        base=tmp_path,
        run_id=f"lifecycle-{compact_day}",
        account="lx",
    )["owners"]["opening"]
    decisions = ranked_opening_candidate_decisions(opening)
    point_id = canonical_sha256({"trading_day": trading_day})
    fx_fact = {
        "fact_id": f"fx-{compact_day}",
        "base_currency": "HKD",
        "quote_currency": "CNY",
        "rate": "0.92",
        "source_fact_sha256": "e" * 64,
    }
    return {
        "market": "HK",
        "account": "lx",
        "trading_date": trading_day,
        "recommendation_point_id": point_id,
        "content_sha256": canonical_sha256({"point_id": point_id}),
        "captured_at_utc": f"{trading_day}T01:59:00Z",
        "source_binding": {
            "market": "HK",
            "account": "lx",
            "scheduled_scan_target_market": target,
        },
        "recommendation_point": {
            "recommendation_point_id": point_id,
            "opening_snapshot_sha256": opening["content_sha256"],
            "scheduled_scan_target_market": target,
            "decision_at_utc": f"{trading_day}T01:58:30Z",
            "formal_point_time_coherence": {
                "maximum_observed_at_utc": f"{trading_day}T01:58:50Z",
            },
            "producer_accepted_candidate_ids": [item["candidate_id"] for item in decisions],
            "prepared_context_manifest_ref": f"prepared/{compact_day}.json",
            "prepared_context_manifest_sha256": "c" * 64,
            "prepared_context_payload_sha256": "d" * 64,
            "source_commit_sha": SOURCE,
            "account_config_sha256": account_config_sha256,
        },
        "opening_snapshot": opening,
        "option_position_evidence_binding": {
            "open_option_positions": [
                {
                    "lot_id": "existing-baseline",
                    "instrument_key": BASELINE_CONTRACT,
                    "symbol": "0700.HK",
                    "currency": "HKD",
                    "multiplier": 100,
                    "contracts_open": 1,
                }
            ],
            "valuation_mark_facts": [
                {
                    "fact_id": "mark-existing-baseline",
                    "instrument_key": BASELINE_CONTRACT,
                    "price": 10,
                }
            ],
            "fx_rate_facts": [fx_fact],
        },
    }


def _seal_formal_point(tmp_path: Path, trading_day: str, account_config_sha256: str) -> dict[str, object]:
    compact_day = trading_day.replace("-", "")
    seal_opening_candidate_fixture(
        tmp_path,
        run_id=f"lifecycle-{compact_day}",
        account="lx",
        market="HK",
        accepted_rows=(
            _candidate("0700.HK", BASELINE_CONTRACT, 300.0, 0.020),
            _candidate("9992.HK", CHALLENGER_CONTRACT, 145.0, 0.019),
        ),
        sealed_at=f"{trading_day}T01:58:40Z",
        manifest_sealed_at=f"{trading_day}T01:58:41Z",
    )
    return _formal_point(tmp_path, trading_day, account_config_sha256)


def _fx_binding(kind: str) -> dict[str, object]:
    fact = {
        "fact_id": "terminal-hkd-cny",
        "base_currency": "HKD",
        "quote_currency": "CNY",
        "rate": "0.92",
    }
    return {
        "expiration": EXPIRATION,
        "currency": "HKD",
        "fact": fact,
        "fact_ref": {"kind": kind, "fact_id": fact["fact_id"]},
        "fact_sha256": canonical_sha256(fact),
    }


def test_public_20_plus_10_lifecycle_publishes_bound_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.application.strategy_lab.service as service

    context = {
        "repo_root": tmp_path,
        "runtime_root": tmp_path / "runtime",
        "store_path": tmp_path / "experiments.sqlite3",
        "artifact_root": tmp_path / "artifacts",
        "config_hk": tmp_path / "config.hk.json",
        "opend_binding": {"host": "127.0.0.1", "port": 11111},
        "opend_limiter_root": tmp_path,
        "tick_markets": ("hk",),
        "tick_lock_paths": (tmp_path / "tick.lock",),
    }
    manifest = [{"path": "owner.py", "sha256": "a" * 64}]
    behavior_sha256 = canonical_sha256(manifest)
    account_config_sha256 = "9" * 64
    research_dates = _trading_days("2026-08-03", 20)
    research_points = {
        trading_day: _seal_formal_point(tmp_path, trading_day, account_config_sha256) for trading_day in research_dates
    }
    research_sessions = []
    for trading_day in research_dates:
        projected = build_concentration_arms(research_points[trading_day])
        research_sessions.append(
            {
                "trading_date": trading_day,
                "market_calendar_binding": {"session": {"trade_date_type": "WHOLE"}},
                "points": [
                    {
                        "recommendation_point_id": projected["recommendation_point_id"],
                        "recommendation_available_at_utc": projected["recommendation_available_at_utc"],
                        "opening_fx_binding": projected["opening_fx_binding"],
                        "arms": projected["arms"],
                    }
                ],
            }
        )
    fee_plan = {
        "commission_free": True,
        "platform_fee": 0.0,
        "fee_plan_ref": "fixture-fee-plan",
    }
    terminal_fx = _fx_binding("fx_rate")
    spec = {
        "source_commit_sha": SOURCE,
        "behavior_manifest": manifest,
        "evaluator_behavior_sha256": behavior_sha256,
        "history_k_authority": {
            "probe_request": {"opend_binding": context["opend_binding"]},
        },
        "fee_plan": {"receipt": fee_plan},
        "terminal_fx_bindings": [terminal_fx],
        "research_window": {
            "status": "available",
            "selected_trading_dates": research_dates,
            "sessions": research_sessions,
        },
    }
    research_preview = {
        "status": "available",
        "blockers": [],
        "spec": spec,
        "spec_sha256": canonical_sha256(spec),
        "preview_sha256": "c" * 64,
    }
    monkeypatch.setattr(service, "preview_experiment", lambda *_args, **_kwargs: research_preview)
    monkeypatch.setattr(service, "source_commit_sha", lambda _root: SOURCE)
    monkeypatch.setattr(service, "build_evaluator_behavior_manifest", lambda _root: manifest)
    monkeypatch.setattr(service, "evaluator_behavior_sha256", canonical_sha256)
    monkeypatch.setattr(service, "_provider_guard", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "load_runtime_config", lambda **_kwargs: (Path("config"), {}))
    monkeypatch.setattr(
        service,
        "resolve_opend_fetch_limits",
        lambda _config: SimpleNamespace(
            history_kline=SimpleNamespace(window_sec=30, max_calls=100),
            market_snapshot=SimpleNamespace(window_sec=30, max_calls=100),
        ),
    )
    monkeypatch.setattr(service, "build_futu_gateway", lambda **_kwargs: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        service,
        "collect_research_fill_evidence",
        lambda _gateway, query, **_kwargs: {
            "status": "available",
            "bars": [{"time_utc": query["window_start_utc"], "high": 2.0, "volume": 1.0}],
        },
    )
    monkeypatch.setattr(
        service,
        "resolve_expiry_outcome",
        lambda _gateway, query, _fee_plan, terminal_fx_binding, **_kwargs: {
            "status": "available",
            "underlying_code": query["underlying_code"],
            "expiration": query["expiration"],
            "underlying_close": 400.0,
            "terminal_kind": "expired_worthless",
            "terminal_fee": {"amount": 0.0},
            "terminal_fx_binding": terminal_fx_binding,
        },
    )

    confirmed = confirm_research(
        context,
        {"recipe_id": "sell_put_option_position_concentration"},
        confirmed_preview_sha256=research_preview["preview_sha256"],
        actor="tester",
        idempotency_key="research-confirm",
        occurred_at_utc="2026-11-01T00:00:00Z",
    )
    experiment_id = confirmed["experiment"]["experiment_id"]
    for _ in range(80):
        research = execute_research(
            context,
            experiment_id,
            actor="tester",
            occurred_at_utc="2026-11-01T00:01:00Z",
        )
        if research["status"] == "complete":
            break
    else:  # pragma: no cover - makes a stalled public lifecycle explicit
        pytest.fail("research did not complete")
    research_receipt = read_receipt(context, experiment_id)["receipt"]
    assert research["experiment"]["state"] == "awaiting_validation_confirmation"
    assert research["conclusion"]["leader"]["variant_id"] == "challenger_0.002"
    assert research_receipt["research_window"]["selected_trading_dates"] == research_dates

    validation_dates = _trading_days("2026-09-01", 10)
    validation_points = {
        trading_day: _seal_formal_point(tmp_path, trading_day, account_config_sha256)
        for trading_day in validation_dates
    }
    schedule_sha256 = "8" * 64
    calendar_sha256 = "7" * 64
    validation_sessions = []
    for trading_day in validation_dates:
        point = validation_points[trading_day]
        slot = f"{trading_day}T02:00:00Z"
        validation_sessions.append(
            {
                "trading_date": trading_day,
                "scheduled_scan_targets_utc": [f"{trading_day}T01:50:00Z"],
                "expected_recommendation_point_ids": [point["recommendation_point_id"]],
                "minute_grid_utc": [slot],
                "session_endpoint_utc": f"{trading_day}T02:01:00Z",
            }
        )
    timer = build_strategy_lab_timer_binding()
    plan = {
        "experiment_id": experiment_id,
        "requested_start": validation_dates[0],
        "leader": research["conclusion"]["leader"],
        "provider_source": {
            "provider": "futu_opend",
            "opend_binding": context["opend_binding"],
        },
        "account_run_config_sha256": account_config_sha256,
        "schedule": {"schedule_config_sha256": schedule_sha256},
        "market_calendar": {
            "market_calendar_version": "hk.fixture",
            "snapshot_content_sha256": calendar_sha256,
            "sessions": validation_sessions,
        },
        "evaluator_behavior_sha256": behavior_sha256,
        "hidden_snapshot_batch_ceiling": HIDDEN_SNAPSHOT_BATCH_CEILING,
        "validation_wake_tolerance_seconds": VALIDATION_WAKE_TOLERANCE_SECONDS,
        "tick_protection_seconds": TICK_PROTECTION_SECONDS,
        "timer_binding": timer,
        "timer_binding_sha256": canonical_sha256(timer),
    }
    validation_preview = {
        "status": "available",
        "preview_sha256": "d" * 64,
        "validation_plan": plan,
        "validation_plan_sha256": canonical_sha256(plan),
    }
    monkeypatch.setattr(service, "preview_validation", lambda *_args, **_kwargs: validation_preview)
    current_day = validation_dates[0]

    def load_expectation(_root: object, *, trading_date: str, **_kwargs: object) -> dict[str, object]:
        if trading_date > current_day:
            return {"status": "missing"}
        session = next(item for item in validation_sessions if item["trading_date"] == trading_date)
        return {
            "status": "available",
            "expectation": {
                "market": "HK",
                "account": "lx",
                "trading_date": trading_date,
                "market_calendar_version": "hk.fixture",
                "market_calendar_sha256": calendar_sha256,
                "schedule_config_sha256": schedule_sha256,
                "scheduled_scan_targets_market": session["scheduled_scan_targets_utc"],
                "expected_recommendation_point_ids": session["expected_recommendation_point_ids"],
            },
        }

    def load_point(
        _root: object,
        *,
        trading_date: str,
        recommendation_point_id: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        point = validation_points[trading_date]
        assert recommendation_point_id == point["recommendation_point_id"]
        return {
            "status": "available",
            "artifact_ref": f"formal/{recommendation_point_id}.json.gz",
            "artifact_file_sha256": canonical_sha256({"file": recommendation_point_id}),
            "artifact_content_sha256": point["content_sha256"],
            "point": point,
        }

    monkeypatch.setattr(service, "load_formal_expectation", load_expectation)
    monkeypatch.setattr(service, "load_formal_point", load_point)
    monkeypatch.setattr(
        service,
        "_current_validation_config",
        lambda *_args: OpenDEndpointRateLimit(window_sec=30, max_calls=100, max_wait_sec=0),
    )
    monkeypatch.setattr(service, "try_low_priority_opend_call", lambda **kwargs: kwargs["call"]())
    snapshot_slot = f"{validation_dates[0]}T02:00:00Z"

    def fetch_snapshot(*, option_codes: list[str], **_kwargs: object) -> MarketSnapshotFetchResult:
        slot = datetime.fromisoformat(snapshot_slot.replace("Z", "+00:00"))
        requested = slot + timedelta(seconds=3)
        received = slot + timedelta(seconds=4)
        local_time = received.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
        codes = frozenset(option_codes)
        return MarketSnapshotFetchResult(
            snap_map={
                code: {
                    "code": code,
                    "bid_price": 2.0,
                    "bid_vol": 3,
                    "update_time": local_time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                for code in option_codes
            },
            errors=[],
            requested_codes=codes,
            returned_codes=codes,
            missing_codes=frozenset(),
            unexpected_codes=frozenset(),
            complete=True,
            opend_call_count=1,
            requested_at_utc=requested.isoformat(),
            received_at_utc=received.isoformat(),
        )

    monkeypatch.setattr(service, "fetch_option_snapshots", fetch_snapshot)
    validation = confirm_validation(
        context,
        experiment_id,
        validation_dates[0],
        confirmed_preview_sha256=validation_preview["preview_sha256"],
        actor="tester",
        idempotency_key="validation-confirm",
        occurred_at_utc="2026-08-31T00:00:00Z",
    )
    assert validation["experiment"]["state"] == "validation_collecting"

    for trading_day in validation_dates:
        current_day = trading_day
        snapshot_slot = f"{trading_day}T02:00:00Z"
        progressed = advance_experiment(
            context,
            experiment_id,
            occurred_at_utc=f"{trading_day}T02:00:05Z",
            provider_capable=True,
        )
        assert progressed["provider_logical_units"] == 1

    waiting = advance_experiment(
        context,
        experiment_id,
        occurred_at_utc=f"{validation_dates[-1]}T02:02:00Z",
    )
    assert waiting["experiment"]["state"] == "waiting_outcome"
    monkeypatch.setattr(
        service,
        "resolve_terminal_fx_binding",
        lambda *_args, **_kwargs: (terminal_fx, None),
    )
    for _ in range(30):
        final = advance_experiment(
            context,
            experiment_id,
            occurred_at_utc="2026-11-01T02:03:00Z",
            provider_capable=True,
        )
        if final.get("experiment", {}).get("state") == "completed":
            break
    else:  # pragma: no cover - makes a stalled public lifecycle explicit
        pytest.fail("validation outcome did not complete")
    final_receipt = read_receipt(context, experiment_id, kind="final")["receipt"]
    observations = ExperimentStore(context["store_path"]).list_observations(experiment_id)
    assert final["experiment"]["state"] == "completed"
    assert final_receipt["conclusion"] == "challenger_passed"
    assert len(final_receipt["validation_window"]["sessions"]) == 10
    assert len(final_receipt["comparison"]["daily_aggregates"]) == 10
    assert len([item for item in observations if item["kind"] == "validation_fill"]) == 20
    assert len([item for item in observations if item["kind"] == "single_result"]) == 100
    assert final["experiment"]["research_receipt_ref"] == research["experiment"]["research_receipt_ref"]
    assert final["experiment"]["final_receipt_ref"]
