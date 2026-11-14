"""D1 planning failures retain their identity and independently consumable evidence."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import socket

import pytest

from tests import test_required_data_snapshot as frozen
from tests.cash_evidence_helpers import cash_portfolio
from src.application.required_data_plan_identity import (
    build_required_data_expected_fetch_contract,
    required_data_plan_id,
)
from src.application.required_data_snapshot import FrozenRequiredDataUnavailable


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(frozen, "_OBSERVED_AT", datetime.now(timezone.utc).isoformat())
    def denied(*args, **kwargs):
        pytest.fail("D1 regressions must not contact a provider")
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def failed_plan(symbol, *, exact=False):
    plan = deepcopy(frozen._fetch_plan(symbol))
    discovery = plan["expiration_discovery"]
    discovery.update(outcome="provider_error", reason_code="provider_call_failed",
                     expirations=[], error="TimeoutError: directory timed out")
    plan.update(expiration_discovery_complete=False,
                expiration_discovery_error=discovery["error"])
    plan["failed_planning_scopes"] = [{
        "source_fields": ["sell_put", "position_advice.roll_scope"],
        "outcome": discovery["outcome"], "reason_code": discovery["reason_code"],
        "error": discovery["error"], "request_identity": deepcopy(discovery["request_identity"]),
    }]
    if exact:
        for side in plan["side_plans"]:
            side["source_fields"] = ["close_advice.position_requirements"]
            side["strike_window"]["source"] = "close_advice.position_requirements"
        plan["merged_requests"][0]["side_plans"] = deepcopy(plan["side_plans"])
    else:
        plan.update(projection_outcome="provider_error", projected_expirations=[],
                    side_plans=[], merged_requests=[], require_realized_volatility=False)
    return plan


def summary_with_failure(*, exact=False):
    summary = frozen._summary("3690.HK", "0700.HK")
    item = summary["global_required_data_plan"]["symbols"][1]
    plan = failed_plan("0700.HK", exact=exact)
    item.update(fetch_plan=plan, projection_outcome=plan["projection_outcome"],
                discovery_status="incomplete", expected_fetch_contract=None)
    if exact:
        item["expected_fetch_contract"] = build_required_data_expected_fetch_contract(
            symbol="0700.HK", fetch_plan=plan, source="opend", host="127.0.0.1", port=11111)
    global_plan = summary["global_required_data_plan"]
    global_plan.update(plan_id=required_data_plan_id(global_plan["symbols"]),
                       planning_complete=False, discovery_complete=False)
    return summary


def test_d1_discovery_failure_remains_in_plan_and_healthy_symbol_is_consumable(tmp_path):
    root, path = frozen._workspace(tmp_path)
    frozen._publish_quote(root, run_id="run-1", symbol="3690.HK")
    summary = summary_with_failure()
    manifest = frozen._seal(root, path, summary)
    assert manifest["schema_version"] == "required_data_snapshot_manifest.v1"
    assert manifest["plan_id"] == summary["global_required_data_plan"]["plan_id"]
    assert manifest["summary"] == {"symbols_total": 2, "ready": 1, "failed": 1}
    assert manifest["status"] == "partial"
    batch = frozen._batch(root, path)
    assert batch.resolve("3690.HK")[1]
    with pytest.raises(FrozenRequiredDataUnavailable) as failed:
        batch.resolve("0700.HK")
    assert failed.value.reason == "provider_error"
    assert "directory timed out" in failed.value.detail


def test_d1_failed_planning_never_promotes_a_preexisting_receipt(tmp_path):
    root, path = frozen._workspace(tmp_path)
    for symbol in ("3690.HK", "0700.HK"):
        frozen._publish_quote(root, run_id="run-1", symbol=symbol)
    manifest = frozen._seal(root, path, summary_with_failure())
    assert manifest["symbols"]["0700.HK"]["status"] == "failed"
    assert manifest["symbols"]["3690.HK"]["status"] == "ready"


def test_d1_exact_contract_keeps_failed_directory_and_blocks_discovery_consumers(tmp_path, monkeypatch):
    root, path = frozen._workspace(tmp_path)
    summary = summary_with_failure(exact=True)
    frozen._publish_quote(root, run_id="run-1", symbol="3690.HK")
    exact_plan = failed_plan("0700.HK", exact=True)
    original_plan = frozen._fetch_plan
    monkeypatch.setattr(frozen, "_fetch_plan", lambda symbol, **kwargs: (
        deepcopy(exact_plan) if symbol == "0700.HK" else original_plan(symbol, **kwargs)))
    frozen._publish_quote(root, run_id="run-1", symbol="0700.HK")
    manifest = frozen._seal(root, path, summary)
    assert manifest["status"] == "partial"
    assert manifest["summary"] == {"symbols_total": 2, "ready": 2, "failed": 0}
    batch = frozen._batch(root, path)
    evidence, csv_bytes = batch.resolve("0700.HK")
    assert csv_bytes
    assert evidence["expected_fetch_contract"]["fetch_plan"]["expiration_discovery"]["outcome"] == "provider_error"
    from src.application.close_advice_runner import _load_frozen_required_data_quotes
    quotes = _load_frozen_required_data_quotes(
        csv_bytes_by_symbol={"0700.HK": csv_bytes}, base_dir=root,
        provenance_by_symbol={"0700.HK": evidence},
    )
    assert quotes and all(key[0] == "0700.HK" for key in quotes)
    exact_strikes = evidence["expected_fetch_contract"]["fetch_plan"]["side_plans"][0]["required_exact_strikes_by_expiration"]
    assert all(float(key[3]) in exact_strikes[key[2]] for key in quotes)
    with pytest.raises(FrozenRequiredDataUnavailable) as failed:
        batch.resolve("0700.HK", require_expiration_discovery=True)
    assert failed.value.reason == "provider_error"


@pytest.mark.parametrize("mutation", ["binding", "identity", "opening", "hash"])
def test_d1_failed_scope_cannot_bypass_common_authority(tmp_path, mutation):
    root, path = frozen._workspace(tmp_path)
    summary = summary_with_failure()
    item = summary["global_required_data_plan"]["symbols"][1]
    if mutation == "binding":
        item["fetch_binding"]["port"] += 1
    elif mutation == "identity":
        item["fetch_plan"]["expiration_discovery"]["request_identity"]["symbol"] = "3690.HK"
    elif mutation == "opening":
        item["fetch_plan"]["merged_requests"] = frozen._fetch_plan("0700.HK")["merged_requests"]
    if mutation != "hash":
        summary["global_required_data_plan"]["plan_id"] = required_data_plan_id(
            summary["global_required_data_plan"]["symbols"])
    else:
        summary["global_required_data_plan"]["plan_id"] = "0" * 64
    with pytest.raises(frozen.RequiredDataSnapshotError):
        frozen._seal(root, path, summary)
    assert not path.exists()


@pytest.mark.parametrize("exact", [False, True])
def test_d1_real_planning_prefetch_seal_and_frozen_consumption(tmp_path, monkeypatch, exact):
    from datetime import date
    from collections import Counter
    from tests import test_required_data_prefetch_inprocess as fixture
    from src.application import required_data_planning as planning, opend_utils
    from src.application.multi_tick import required_data_prefetch as prefetch
    calls = Counter()
    fetched = []
    watchlist = [
        {"symbol": "0700.HK", "sell_put": {"enabled": True, "min_dte": 7, "max_dte": 50, "max_strike": 200}},
        {"symbol": "AAPL", "sell_put": {"enabled": True, "min_dte": 7, "max_dte": 30, "max_strike": 200}},
        {"symbol": "AAPL", "sell_put": {"enabled": True, "min_dte": 31, "max_dte": 50, "max_strike": 200}},
    ]
    if exact:
        watchlist[0]["_close_advice_position_requirements"] = [{
            "option_type": "put", "expiration": "2026-08-28", "strike": 100,
            "requires_realized_volatility": True,
        }]
    def discover(symbol, **kwargs):
        calls[symbol] += 1
        if symbol == "0700.HK":
            raise TimeoutError("directory timed out")
        return ["2026-08-28", "2026-09-18"]
    def fetch(symbol, **kwargs):
        fetched.append(symbol)
        return fixture._strict_success_rows_for_fetch(symbol, kwargs)
    fixture._patch_prefetch_sources(monkeypatch, watchlist=watchlist)
    fixture._silence_source_snapshot_events(monkeypatch)
    monkeypatch.setattr(prefetch, "_prefill_spot_observation_cache", lambda **kwargs: ({}, set()))
    monkeypatch.setattr(prefetch, "prefetch_market_earnings_calendars", lambda **kwargs: {})
    monkeypatch.setattr(planning, "get_underlier_spot", lambda *args, **kwargs: 100.0)
    monkeypatch.setattr(planning, "list_option_expirations", discover)
    monkeypatch.setattr(opend_utils, "get_trading_date", lambda market: date(2026, 8, 4))
    monkeypatch.setattr(prefetch, "fetch_symbol", fetch)
    root, path = frozen._workspace(tmp_path)
    summary = fixture._prefetch(tmp_path, shared_required=root, producer_run_id="run-1")
    plan = summary["global_required_data_plan"]
    assert calls == {"0700.HK": 1, "AAPL": 1}
    assert plan["symbols_count"] == 2
    assert plan["planning_complete"] is False and plan["discovery_complete"] is False
    items = {item["symbol"]: item for item in plan["symbols"]}
    assert items["AAPL"]["fetch_plan"]["projected_expirations"] == ["2026-08-28", "2026-09-18"]
    assert items["0700.HK"]["fetch_plan"]["expiration_discovery"]["outcome"] == "provider_error"
    assert sorted(fetched) == (["0700.HK", "AAPL"] if exact else ["AAPL"])
    manifest = frozen._seal(root, path, summary)
    assert manifest["status"] == "partial"
    batch = frozen._batch(root, path)
    assert batch.resolve("AAPL")[1]
    if exact:
        evidence, data = batch.resolve("0700.HK")
        assert data and evidence["expected_fetch_contract"]["fetch_plan"]["side_plans"][0]["source_fields"] == ["close_advice.position_requirements"]
    for _account in ("lx", "sy"):
        with pytest.raises(FrozenRequiredDataUnavailable) as failure:
            batch.resolve("0700.HK", require_expiration_discovery=True)
        assert failure.value.reason == "provider_error"


def test_d1_failed_directory_retains_blocked_rv_demand_without_widening_exact_fetch(tmp_path, monkeypatch):
    from datetime import date
    from tests import test_required_data_fetch_planning as fixture
    from src.application import required_data_planning as planning, opend_utils
    monkeypatch.setattr(planning, "get_underlier_spot", lambda *args, **kwargs: 100.0)
    monkeypatch.setattr(opend_utils, "get_trading_date", lambda market: date(2026, 8, 4))
    def failed(*args, **kwargs):
        raise TimeoutError("directory timed out")
    monkeypatch.setattr(planning, "list_option_expirations", failed)
    plan = fixture._plan(
        tmp_path, wheel_put_cfg={"enabled": True, "min_dte": 7, "max_dte": 50,
                                  "requires_realized_volatility": True},
        position_requirements=[{"option_type": "put", "expiration": "2026-08-28", "strike": 100}],
    )
    assert plan.projection_outcome == "success_rows"
    assert plan.expiration_discovery.outcome == "provider_error"
    assert plan.failed_planning_scopes[0]["require_realized_volatility"] is True
    assert plan.require_realized_volatility is False
    assert plan.merged_specs and not any(spec.include_realized_volatility for spec in plan.merged_specs)
    assert all(side.source_fields == ["close_advice.position_requirements"] for side in plan.side_plans)


def test_d1_malformed_exact_scope_is_a_contract_validation_failure():
    plan = failed_plan("0700.HK", exact=True)
    plan["side_plans"][0]["strike_window"] = "invalid-window"
    with pytest.raises(ValueError, match="cannot authorize opening or roll"):
        build_required_data_expected_fetch_contract(
            symbol="0700.HK", fetch_plan=plan, source="opend", host="127.0.0.1", port=11111)


@pytest.mark.parametrize("arm", ["known", "known_currency_gap", "unknown_currency", "shared_bad", "other_account"])
def test_d1_a14_cash_candidate_and_brief_dependency_evidence(tmp_path, monkeypatch, arm):
    """Trace current currency guards through real projection, capacity, sealing and Brief."""
    import pandas as pd
    from conftest import phase2_opening_row
    from tests import test_wheel_scanning as wheel
    from tests.test_positions_context_builder_partial_close import _lot
    from src.application.positions.context_builder import build_context
    from src.application.ledger.wheel_capacity import build_shared_cash_capacity_fact
    from src.application.wheel.capacity import finalize_wheel_put_capacity
    from src.application.required_data_snapshot import FrozenRequiredDataBatch
    from src.application.candidate_snapshot_manifest import publish_candidate_snapshot_manifest
    from src.application.strategy_scan_status import publish_strategy_scan_status, publish_strategy_scan_status_index
    from src.application.daily_decision_brief_service import assemble_daily_decision_brief
    from src.application.multi_tick.misc import AccountResult
    from src.infrastructure.io_utils import atomic_write_json

    now, symbol = wheel.AS_OF, "0700.HK"
    account = "sy" if arm == "other_account" else "lx"
    raw = _lot(symbol="NVDA", strike=40, multiplier=100, currency="USD")
    if arm == "known_currency_gap":
        raw["fields"].pop("multiplier")
    elif arm == "unknown_currency":
        raw["fields"].pop("currency")
    snapshot = {"snapshot_status": "untrusted" if arm == "shared_bad" else "trusted",
                "normalized_account": account, "account_position_lots": [raw]}
    option = build_context([raw], broker="富途", account=account, decision_snapshot=snapshot, observed_at=now)
    expected_gap = {"USD": ["NVDA"]} if arm == "known_currency_gap" else {"UNKNOWN": ["NVDA"]}
    if arm in {"known_currency_gap", "unknown_currency"}:
        assert option["cash_secured_unavailable_by_ccy"] == expected_gap
    if arm == "known":
        assert option["cash_secured_total_by_ccy"] == {"USD": 4000}
    if arm == "other_account":
        assert option["open_positions_min"] == [] and option["cash_secured_unavailable_by_symbol"] == {}
    portfolio = cash_portfolio({"cash_by_currency": {"USD": 4000, "HKD": 22000},
        "source_observed_at": now.isoformat()}, account=account, evaluated_at=now)
    reservation = {"intent_id": "pending-1", "account": account, "currency": "HKD", "reserved_cash_amount": 3000}
    branch = {"account": account, "symbol": symbol, "wheel_branch_id": "hk-put", "direction": "put",
        "lifecycle_status": "active", "integrity_status": "trusted", "phase": "ready", "monitoring_gate": "enabled",
        "remaining_contracts": 2, "multiplier": 100, "principal_anchor": 20020, "currency": "HKD",
        "realized_put_net_pnl_in_current_stage": 0, "batch_generation_hash": "a" * 64, "projection_hash": "b" * 64,
        "active_intent_reservations": [reservation]}
    model = {"account": account, "wheel_branches": [branch]}
    fact = build_shared_cash_capacity_fact(account=account, portfolio_context=portfolio,
        option_context=option, wheel_read_model=model, fx_snapshot={})
    healthy = arm in {"known", "other_account"}
    assert fact["status"] == ("available" if healthy else "unavailable")
    assert fact["wheel_intent_reservations"] == [reservation]
    assert fact["cash_secured_by_currency"] == option["cash_secured_total_by_ccy"]
    if arm == "shared_bad":
        assert fact["reason"] == "option_decision_snapshot_unavailable"
    if arm in {"known_currency_gap", "unknown_currency"}:
        assert fact["reason"] == "option_cash_secured_unavailable"
        assert "cash_secured_unavailable_by_ccy" not in fact

    row = phase2_opening_row(wheel._call_row(symbol=symbol, option_type="put", currency="HKD",
        contract_symbol="HK.00700-PUT-99", strike=99, delta=-0.3))
    batch = FrozenRequiredDataBatch(manifest={}, manifest_bytes=b"{}", require_fresh=False,
        entries={symbol: ({}, pd.DataFrame([row]).to_csv(index=False).encode("utf-8"))},
        unavailable={"NVDA": FrozenRequiredDataUnavailable(symbol="NVDA", reason="provider_error", detail="directory timed out")})
    with pytest.raises(FrozenRequiredDataUnavailable):
        batch.resolve("NVDA")
    converter = wheel._converter()
    scan = wheel.run_wheel_put_scan(model, wheel._policy(), batch,
        {"exchange_rate_converter": converter, "stock_assignment_fee_fact_fn": lambda *_: {"basis": "estimated", "amount": 10}},
        decision_time_ms=int(now.timestamp() * 1000))
    assert scan["raw_candidates"]["hk-put"] and scan["capacity_claims"]
    def finalize(cash_fact):
        return finalize_wheel_put_capacity(account=account, wheel_read_model=model, wheel_scan=scan,
            opening_put_candidates=[], cash_capacity_fact=cash_fact, exchange_rate_converter=converter)
    captured = finalize(fact)
    allocation = captured["allocations"][0]
    assert allocation["granted_contracts"] == (1 if healthy else 0)
    if healthy:
        unreserved_model = {"account": account, "wheel_branches": [{**branch, "active_intent_reservations": []}]}
        unreserved = build_shared_cash_capacity_fact(account=account, portfolio_context=portfolio,
            option_context=option, wheel_read_model=unreserved_model, fx_snapshot={})
        assert finalize(unreserved)["allocations"][0]["granted_contracts"] == 2
        assert allocation["capacity_before"] < finalize(unreserved)["allocations"][0]["capacity_before"]
    else:
        assert allocation["allocation_reason"] == "cash_capacity_fact_unavailable"
    if arm == "known_currency_gap":
        from src.infrastructure import exchange_rates
        # Quote creation and use-time eligibility share the fixed trading session.
        monkeypatch.setattr(exchange_rates, "_utc_now", lambda: now)
        quoted = now
        fx = exchange_rates.project_exchange_rate_snapshot({"schema_version": 2, "pairs": {
            name: {"rate": rate, "source": "tencent_quote", "quote_at_utc": quoted.isoformat(),
                   "observed_at_utc": quoted.isoformat()}
            for name, rate in (("USDCNY", 7.2), ("HKDCNY", 0.92))
        }}, purpose="capacity", now=quoted)
        # A currency label cannot bound the unknown liability. Both completions
        # are accepted by the existing owner; the larger USD deficit consumes HKD.
        for multiplier, expected_grant in ((100, 1), (500, 0)):
            completed = deepcopy(raw)
            completed["fields"]["multiplier"] = multiplier
            completed_option = build_context([completed], broker="富途", account=account, observed_at=now,
                decision_snapshot={**snapshot, "account_position_lots": [completed]})
            completed_fact = build_shared_cash_capacity_fact(account=account, portfolio_context=portfolio,
                option_context=completed_option, wheel_read_model=model, fx_snapshot=fx)
            assert completed_fact["status"] == "available"
            completed_allocation = finalize(completed_fact)["allocations"][0]
            assert completed_allocation["granted_contracts"] == expected_grant
            if not expected_grant:
                assert completed_option["cash_secured_total_by_ccy"] == {"USD": 20000}
                assert completed_allocation["capacity_before"] == 0
                assert completed_allocation["allocation_reason"] == "effective_native_cash_insufficient"

    run_id = "d1-a14-" + arm
    sealed = wheel.seal_wheel_candidate_snapshot(base=tmp_path, run_id=run_id, account=account, market="HK",
        account_config_sha256="a" * 64, strategy_policy_sha256="b" * 64,
        dependencies=wheel._seal_dependencies(), run_mode={"scan_mode": "standard", "executable": True},
        scope_results=captured["scope_results"], batches=captured["batches"],
        capacity_allocations=captured["allocations"], sealed_at=now)
    report_dir = tmp_path / "output_runs" / run_id / "accounts" / account
    for scope in sealed["scope_results"]:
        publish_strategy_scan_status(report_dir=report_dir, run_id=run_id, account=account, market="HK",
            symbol=symbol, strategy_family="wheel", direction="put", status=scope["status"],
            candidate_count=scope.get("candidate_count"), reason=scope.get("reason_code"))
    publish_strategy_scan_status_index(report_dir=report_dir, run_id=run_id, account=account,
        account_config_sha256="a" * 64, run_mode={"scan_mode": "standard", "executable": True}, expected=[{
            "market": "HK", "symbol": symbol, "strategy_family": "wheel", "strategy_mode": "wheel",
            "candidate_owner": "wheel", "direction": "put", "account_config_sha256": "a" * 64}])
    publish_candidate_snapshot_manifest(base=tmp_path, run_id=run_id, account=account,
        strategy_policy_sha256="b" * 64, sealed_at=now)
    atomic_write_json(report_dir / "state" / "portfolio_context.json", portfolio)
    atomic_write_json(report_dir / "state" / "option_positions_context.json", option)
    brief = assemble_daily_decision_brief(base=tmp_path, run_id=run_id, account=account, market="HK",
        scheduler_decision={"in_run_window": True}, account_result=AccountResult(account, True, True, "ok", ""),
        pipeline_succeeded=True, config={"schedule": {"timezone": "Asia/Hong_Kong",
            "run_window": {"start": "00:00", "end": "23:59", "breaks": []}}}, now_utc=now)
    candidates = brief["candidates"]["wheel"]
    assert len(candidates) == (1 if healthy else 0)
    if healthy:
        assert candidates[0]["symbol"] == symbol and candidates[0]["option_type"] == "put"
        assert candidates[0]["capacity"]["contracts_available"] == 1
    else:
        assert brief["wheel_batches"][0]["recommended_contracts"] == 0

    # Re-run the actual capture boundary in every arm: unavailable scans retain
    # the reserved NVDA shares while an independent MSFT action reaches Brief.
    from tests.test_pipeline_capture_status_routing import _run_wheel_scan_failure_capture
    with monkeypatch.context() as capture_patch:
        wheel_snapshot, _, share_brief, _ = _run_wheel_scan_failure_capture(
            capture_patch, tmp_path / "share-proof", reserved_by_symbol={"NVDA": 100, "MSFT": 0})
    by_symbol = {item["symbol"]: item for item in wheel_snapshot["capacity_allocations"]}
    assert by_symbol["NVDA"]["granted_contracts"] == 0 and by_symbol["MSFT"]["granted_contracts"] == 1
    assert {item["symbol"] for item in share_brief["actions"] if item.get("strategy_family") == "covered_call"} == {"MSFT"}
