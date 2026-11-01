"""Offline public fetch-plan/receipt/candidate/Brief integration for HK RV."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from test_hk_cross_year_rv import CalendarQuoteFixture, NEAR, JAN, MAR


NOW = datetime(2026, 10, 30, 5, 45, tzinfo=timezone.utc)
SYMBOL = "0123.HK"


def _public_fetch(monkeypatch, base, expirations, *, conflict=False):
    from src.application import opend_symbol_fetching as symbols
    from src.application.opend_market_snapshot_fetching import MarketSnapshotFetchResult
    from src.application.required_data_fetching import build_fetch_request_from_spec, execute_required_data_opend
    from src.application.required_data_planning import RequiredDataFetchSpec, OptionSideFetchPlan, StrikeWindowPlan
    from src.infrastructure import futu_gateway

    fixture = CalendarQuoteFixture()
    if conflict:
        fixture.future_rows = [{"time": "2027-01-04", "trade_date_type": "NON_TRADING"}]
    forbidden = {"broker": 0, "network": 0}

    def deny(kind):
        def blocked(*args, **kwargs):
            forbidden[kind] += 1
            raise AssertionError(f"forbidden {kind}")

        return blocked

    class QuoteSDK:
        def request_trading_days(self, **kwargs):
            return 0, pd.DataFrame(fixture.get_trading_days(**kwargs))

        def request_history_kline(self, **kwargs):
            result = fixture.request_history_kline(**kwargs)
            return 0, pd.DataFrame(result["data"]), None

        def close(self):
            pass

    gateway = futu_gateway.build_futu_gateway()
    gateway.backend._quote_client = QuoteSDK()
    monkeypatch.setattr(futu_gateway, "build_futu_trade_context", deny("broker"))
    monkeypatch.setattr(futu_gateway, "build_futu_quote_context", deny("network"))
    monkeypatch.setattr(symbols, "build_ready_futu_quote_gateway", lambda **_: gateway)
    monkeypatch.setattr("src.application.opend_utils.load_runtime_symbol_aliases", lambda *_: {})

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(symbols, "datetime", FrozenDateTime)
    monkeypatch.setattr(symbols, "_utc_now_iso", lambda: NOW.isoformat())
    stamp = NOW.isoformat()
    codes = [f"HK.TST{expiry.replace('-', '')[2:]}P100000" for expiry in expirations]
    chain = pd.DataFrame(
        [
            {
                "code": code,
                "strike_time": expiry,
                "strike_price": 100,
                "option_type": "PUT",
                "lot_size": 100,
                "stock_type": "DRVT",
                "stock_owner": "HK.00123",
                "option_standard_type": "STANDARD",
                "suspension": False,
            }
            for code, expiry in zip(codes, expirations, strict=True)
        ]
    )
    monkeypatch.setattr(
        symbols,
        "fetch_symbol_option_chain",
        lambda **_: SimpleNamespace(
            frame=chain,
            fetch_meta={
                "status": "ok",
                "source_outcome": "success_rows",
                "source_observed_at": stamp,
                "expiration_statuses": {expiry: "fetched" for expiry in expirations},
            },
        ),
    )
    snapshots = {
        code: {
            "code": code,
            "last_price": 3,
            "bid_price": 3,
            "ask_price": 3.1,
            "price_spread": 0.01,
            "option_implied_volatility": 0.40,
            "option_delta": -0.3,
            "option_contract_multiplier": 100,
            "stock_owner": "HK.00123",
            "stock_type": "DRVT",
            "sec_status": "NORMAL",
            "suspension": False,
            "update_time": stamp,
            "snapshot_requested_at_utc": stamp,
            "snapshot_received_at_utc": stamp,
        }
        for code in codes
    }
    monkeypatch.setattr(
        symbols,
        "fetch_option_snapshots",
        lambda **_: MarketSnapshotFetchResult(
            snap_map=snapshots,
            errors=[],
            requested_codes=frozenset(codes),
            returned_codes=frozenset(codes),
            missing_codes=frozenset(),
            unexpected_codes=frozenset(),
            complete=True,
            requested_at_utc=stamp,
            received_at_utc=stamp,
        ),
    )
    from test_short_vol_provider_evidence import _fixed_option_clock

    monkeypatch.setattr(symbols, "normalize_option_observation", _fixed_option_clock(stamp))
    spec = RequiredDataFetchSpec(
        symbol=SYMBOL,
        limit_expirations=0,
        host="127.0.0.1",
        port=11111,
        option_types=("put",),
        explicit_expirations=list(expirations),
        min_dte=0,
        max_dte=1000,
        side_strike_windows={"put": {"min_strike": 100, "max_strike": 100}},
        include_realized_volatility=True,
        trading_date="2026-10-30",
        side_plans=[
            OptionSideFetchPlan(
                option_type="put",
                min_dte=0,
                max_dte=1000,
                explicit_expirations=list(expirations),
                strike_window=StrikeWindowPlan(
                    min_strike=100, max_strike=100, source="position_exact", base_min_strike=100, base_max_strike=100
                ),
                planning_reason="synthetic exact contract",
                source_fields=["position.strike"],
                required_exact_strikes_by_expiration={expiry: [100.0] for expiry in expirations},
            )
        ],
    )
    request = build_fetch_request_from_spec(
        spec=spec,
        spot_override=100,
        underlier_observation={
            "schema_version": "opening_underlier_observation.v1",
            "code": "HK.00123",
            "market": "HK",
            "last_price": 100,
            "update_time": stamp,
            "observed_at_utc": stamp,
            "age_seconds": 0,
            "market_state": "AFTERNOON",
            "sec_status": "NORMAL",
            "suspension": False,
            "status": "ready",
            "reason_code": None,
        },
    )
    payload = execute_required_data_opend(base=base, request=request)
    assert forbidden == {"broker": 0, "network": 0}
    return payload, spec.to_debug_dict()


@pytest.mark.parametrize("conflict", [False, True])
def test_public_plan_fetch_publish_validate_candidate_brief_chain(tmp_path, monkeypatch, conflict):
    from domain.domain.engine import build_candidate_decision
    from src.application.candidate_scanning import calculation_rejection
    from src.application.scan_sell_put import compute_metrics, explain_metrics_rejection
    from src.application.opend_symbol_outputs import save_outputs, publish_required_data_quote_snapshot
    from src.application.required_data_plan_identity import (
        build_required_data_expected_fetch_contract,
        required_data_plan_id,
    )
    from src.application.required_data_snapshot import seal_required_data_snapshot, resolve_frozen_required_data
    from src.application.opening_candidate_snapshot import (
        seal_opening_candidate_snapshot,
        dependency_from_file,
        dependency_from_hash,
    )
    from src.application.daily_decision_brief_service import assemble_daily_decision_brief
    from src.application.daily_decision_brief_renderer import render_full_brief, render_fixed_report
    from test_opening_candidate_snapshot import _dependencies, _physical_account, _publish_opening_manifest
    from test_daily_decision_brief_service import _account_dir, _config, _result, _write_close_report
    from test_candidate_engine_phase2_contract import _opening_row
    from test_required_data_snapshot import _fetch_plan

    run_id = "run-1"
    dependencies = _dependencies(tmp_path, run_id, "lx")
    # Discard this helper's synthetic placeholder before the real producer seals its manifest.
    (tmp_path / "output_runs" / run_id / "state" / "required_data_snapshot_manifest.json").unlink()
    account_dir = _account_dir(tmp_path)
    # Synthetic portfolio and reconciliation are unrelated to this quote/RV integration.
    for name in ("portfolio_context.json", "option_positions_context.json"):
        path = account_dir / "state" / name
        context = json.loads(path.read_text())
        context["as_of_utc"] = NOW.isoformat()
        if isinstance(context.get("exchange_rates"), dict):
            context["exchange_rates"]["timestamp"] = NOW.isoformat()
        path.write_text(json.dumps(context))
    monkeypatch.setattr(
        "src.application.daily_decision_brief_service._pending_attribution_for_brief", lambda **_: ([], None)
    )
    _write_close_report(account_dir, [], market="HK")
    expirations = (NEAR, JAN, "2028-01-28")
    payload, planned_request = _public_fetch(monkeypatch, tmp_path, expirations, conflict=conflict)
    assert payload["meta"]["status"] == "ok"
    rows = {row["expiration"]: row for row in payload["rows"]}
    assert rows[NEAR]["term_matched_rv_status"] == "ok"
    assert rows[JAN]["term_matched_rv_status"] == ("data_unavailable" if conflict else "ok")
    assert rows["2028-01-28"]["term_matched_rv_reason"] == "calendar_coverage_incomplete"

    plan = _fetch_plan(SYMBOL, observed_at=NOW.isoformat())
    plan["merged_requests"] = [planned_request]
    plan["projected_expirations"] = list(expirations)
    plan["expiration_discovery"]["expirations"] = list(expirations)
    plan["expiration_discovery"]["request_identity"]["trading_date"] = "2026-10-30"
    plan["side_plans"] = deepcopy(planned_request["side_plans"])
    contract = build_required_data_expected_fetch_contract(
        symbol=SYMBOL, fetch_plan=plan, source="opend", host="127.0.0.1", port=11111
    )
    required_root = tmp_path / "output_runs" / run_id / "required_data"
    raw, csv = save_outputs(tmp_path, SYMBOL, payload, output_root=required_root)
    receipt_path, _ = publish_required_data_quote_snapshot(
        runtime_root=tmp_path,
        producer_root=required_root,
        producer_run_id=run_id,
        symbol=SYMBOL,
        raw_path=raw,
        csv_path=csv,
        fetch_plan=plan,
        fetch_policy={"source": "opend", "host": "127.0.0.1", "port": 11111},
        expected_fetch_contract=contract,
        source_observed_at=NOW,
        completed_at=NOW,
        now=NOW,
    )
    plan_items = [
        {
            "symbol": SYMBOL,
            "source": "opend",
            "fetch_plan": plan,
            "fetch_binding": contract["fetch_binding"],
            "expected_fetch_contract": contract,
            "projection_outcome": "success_rows",
            "discovery_status": "complete",
        }
    ]
    manifest = tmp_path / "output_runs" / run_id / "state" / "required_data_snapshot_manifest.json"
    seal_required_data_snapshot(
        manifest_path=manifest,
        required_data_root=required_root,
        run_id=run_id,
        prefetch_summary={
            "schema_version": "1.0",
            "errors": 0,
            "global_required_data_plan": {
                "plan_id": required_data_plan_id(plan_items),
                "symbols": plan_items,
                "symbols_count": 1,
                "discovery_complete": True,
            },
            "symbols": [],
            "results": [],
        },
        sealed_at=NOW,
    )
    evidence = resolve_frozen_required_data(
        manifest_path=manifest, expected_run_id=run_id, symbol=SYMBOL, required_data_root=required_root, now=NOW
    )
    original_manifest = manifest.read_bytes()
    frame = pd.read_csv(csv)
    evaluations, accepted = [], []
    for raw_row in frame.astype(object).where(pd.notna(frame), None).to_dict("records"):
        merged = _opening_row(
            **{
                **raw_row,
                "symbol": SYMBOL,
                "stock_owner": "HK.00123",
                "currency": "HKD",
                "cny_per_hkd": 0.9,
                "decision_now_utc": NOW.isoformat(),
                "snapshot_received_at_utc": NOW.isoformat(),
                "cash_free_effective_native": 100000.0,
                "cash_required_native": 10000.0,
                "cash_native_currency": "HKD",
                "max_new_contracts": 10,
            }
        )
        metric = compute_metrics(pd.Series(merged), now_utc=NOW)
        rejects = (
            []
            if metric is not None
            else [calculation_rejection(merged, explain_metrics_rejection(pd.Series(merged), now_utc=NOW))]
        )
        decision = build_candidate_decision(
            mode="put",
            symbol=SYMBOL,
            contract_symbol=merged["contract_symbol"],
            accepted=metric is not None,
            rejects=rejects,
            normalized_input=merged,
        )
        evaluations.append({"normalized_input": merged, "opening_decision": decision})
        if metric is not None:
            accepted.append({**merged, **metric})
    assert len(accepted) == (1 if conflict else 2)
    dependencies = [dep for dep in dependencies if dep["kind"] not in {"required_data", "earnings_rv"}]
    dependencies.extend(
        [
            dependency_from_file(kind="required_data", path=manifest, base=tmp_path),
            dependency_from_hash(kind="earnings_rv", sha256=hashlib.sha256(original_manifest).hexdigest()),
        ]
    )
    snapshot = seal_opening_candidate_snapshot(
        base=tmp_path,
        run_id=run_id,
        account="lx",
        market="HK",
        physical_account=_physical_account("lx", "HK"),
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=dependencies,
        scan_statuses=[
            {
                "symbol": SYMBOL,
                "strategy_mode": "put",
                "status": "completed",
                "reason": "partial_data",
                "quote_snapshot_id": evidence["snapshot_id"],
                "quote_receipt_relpath": str(receipt_path.relative_to(required_root)),
            }
        ],
        final_candidates={"put": accepted},
        candidate_evaluations={"put": evaluations},
        run_mode={"scan_mode": "standard", "executable": True},
        sealed_at=NOW,
    )
    _publish_opening_manifest(tmp_path, snapshot)
    brief = assemble_daily_decision_brief(
        base=tmp_path,
        run_id=run_id,
        account="lx",
        market="HK",
        scheduler_decision={"in_run_window": True},
        account_result=_result(),
        pipeline_succeeded=True,
        config=_config(timezone_name="Asia/Hong_Kong"),
        now_utc=NOW,
        opening_candidate_snapshot=snapshot,
    )
    assert brief["actionability"] != "blocked", json.dumps(
        {"gaps": brief["data_gaps"], "actions": brief["actions"]}, indent=2
    )
    rv_gaps = [gap for gap in brief["data_gaps"] if gap.get("reason_code") == "term_matched_rv_unavailable"]
    assert any(
        gap.get("expiration") == "2028-01-28" and gap.get("rv_reason") == "calendar_coverage_incomplete"
        for gap in rv_gaps
    )
    if conflict:
        assert any(
            gap.get("expiration") == JAN and gap.get("rv_reason") == "calendar_source_conflict" for gap in rv_gaps
        )
    for text in (render_full_brief(brief), render_fixed_report(brief)):
        assert "2028-01-28" in text and "交易日历覆盖不足" in text
        if conflict:
            assert JAN in text and "交易日历来源冲突" in text
    assert manifest.read_bytes() == original_manifest


def test_separate_rv_request_binding_preserves_healthy_terms_and_each_source(tmp_path):
    from src.application.required_data_fetching import _merged_realized_volatility_meta
    from test_hk_cross_year_rv import fetch

    cache = {}
    fixture = CalendarQuoteFixture()
    requests = []
    for expiry in (NEAR, JAN, MAR, "2028-01-28"):
        snapshot = fetch(fixture, tmp_path, (expiry,), facts_cache=cache)
        requests.append({"snapshot_requested_code_set": [expiry], "realized_volatility": snapshot.to_meta()})
    # The all-unsupported old path has no history; bind a mixed unsupported request as the actual coordinator does.
    requests[-1]["realized_volatility"] = fetch(fixture, tmp_path, (NEAR, "2028-01-28"), facts_cache=cache).to_meta()
    merged, okay = _merged_realized_volatility_meta(requests)
    assert okay and merged["status"] == "partial"
    assert all(merged["term_matched"][expiry]["status"] == "ok" for expiry in (NEAR, JAN, MAR))
    assert merged["term_matched"]["2028-01-28"]["reason"] == "calendar_coverage_incomplete"
    assert len(merged["trading_calendar"]["requests"]) == 4
