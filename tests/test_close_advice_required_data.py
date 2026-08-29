from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pandas as pd


def _config(
    *,
    account: str,
    enabled: bool = True,
    host: str = "127.0.0.1",
    port: int = 11111,
) -> dict:
    return {
        "portfolio": {
            "broker": "富途",
            "account": account,
        },
        "close_advice": {"enabled": enabled},
        "symbols": [
            {
                "symbol": "NVDA",
                "broker": "US",
                "fetch": {
                    "source": "opend",
                    "host": host,
                    "port": port,
                },
                "sell_put": {"enabled": True},
                "sell_call": {"enabled": False},
            }
        ],
    }


def _position(
    *,
    account: str,
    lot_id: str,
    expiration: str = "2026-08-28",
) -> dict:
    return {
        "record_id": lot_id,
        "fields": {
            "broker": "富途",
            "account": account,
            "symbol": "NVDA",
            "status": "open",
            "side": "short",
            "option_type": "put",
            "contracts": 1,
            "contracts_open": 1,
            "multiplier": 100,
            "strike": 100,
            "expiration_ymd": expiration,
            "currency": "USD",
            "premium": 2.0,
            "opened_at": int(
                datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp()
                * 1000
            ),
        },
    }


def test_requirements_plan_is_order_independent_and_skips_disabled_account() -> None:
    from src.application.close_advice_required_data import (
        build_close_advice_required_data_plan,
    )

    started = datetime(2026, 7, 29, 1, 40, tzinfo=timezone.utc)
    configs = {
        "lx": _config(account="lx"),
        "sy": _config(account="sy", enabled=False),
    }
    positions = {
        "lx": [_position(account="lx", lot_id="lot-lx")],
        "sy": [_position(account="sy", lot_id="lot-sy")],
    }
    forward = build_close_advice_required_data_plan(
        run_id="run-1",
        run_started_at_utc=started,
        business_date=date(2026, 7, 29),
        account_configs=configs,
        base_config=configs["lx"],
        markets_to_run=["US"],
        position_records_by_account=positions,
    )
    reverse = build_close_advice_required_data_plan(
        run_id="run-1",
        run_started_at_utc=started,
        business_date=date(2026, 7, 29),
        account_configs={"sy": configs["sy"], "lx": configs["lx"]},
        base_config=configs["lx"],
        markets_to_run=["US"],
        position_records_by_account={
            "sy": positions["sy"],
            "lx": positions["lx"],
        },
    )

    assert forward == reverse
    assert forward["status"] == "complete"
    assert forward["accounts"]["sy"] == {
        "close_advice_enabled": False,
        "status": "not_applicable",
        "requirements": [],
        "planning_errors": [],
    }
    requirement = forward["accounts"]["lx"]["requirements"][0]
    assert (
        requirement["quote_key"]
        == "NVDA|put|2026-08-28|100.000000"
    )
    assert requirement["fetch_binding"]["binding_id"]
    assert forward["summary"]["requirements_total"] == 1


def test_candidate_route_wins_and_only_conflicting_position_is_rejected() -> None:
    from src.application.close_advice_required_data import (
        build_close_advice_required_data_plan,
    )
    from src.application.required_data_prefetch_planning import (
        merge_close_advice_requirements_into_prefetch_config,
    )

    candidate = _config(account="lx", host="127.0.0.1", port=11111)
    position_config = _config(
        account="lx",
        host="127.0.0.1",
        port=11112,
    )
    plan = build_close_advice_required_data_plan(
        run_id="run-1",
        run_started_at_utc=datetime(
            2026,
            7,
            29,
            1,
            40,
            tzinfo=timezone.utc,
        ),
        business_date=date(2026, 7, 29),
        account_configs={"lx": position_config},
        base_config=candidate,
        markets_to_run=["US"],
        position_records_by_account={
            "lx": [_position(account="lx", lot_id="lot-lx")]
        },
    )

    merged, resolved_plan = (
        merge_close_advice_requirements_into_prefetch_config(
            candidate_config=candidate,
            requirements_plan=plan,
        )
    )

    assert len(merged["symbols"]) == 1
    assert merged["symbols"][0]["fetch"]["port"] == 11111
    assert "_close_advice_position_requirements" not in merged["symbols"][0]
    requirement = resolved_plan["accounts"]["lx"]["requirements"][0]
    assert requirement["planning_status"] == "unavailable"
    assert requirement["planning_reason"] == "required_data_route_conflict"
    assert resolved_plan["accounts"]["lx"]["status"] == "partial"
    diagnostic = merged["_close_advice_required_data_diagnostics"][0]
    assert diagnostic["preserved_candidate_binding"]["port"] == 11111
    assert diagnostic["rejected_requirement_ids"] == [
        requirement["requirement_id"]
    ]


def test_ambiguous_candidate_routes_are_preserved_and_position_is_rejected() -> None:
    from src.application.close_advice_required_data import (
        build_close_advice_required_data_plan,
    )
    from src.application.required_data_prefetch_planning import (
        merge_close_advice_requirements_into_prefetch_config,
    )

    position_config = _config(account="lx", port=11111)
    candidate_config = dict(position_config)
    candidate_config["symbols"] = [
        _config(account="lx", port=port)["symbols"][0]
        for port in (11111, 11112)
    ]
    plan = build_close_advice_required_data_plan(
        run_id="run-1",
        run_started_at_utc=datetime(
            2026,
            7,
            29,
            1,
            40,
            tzinfo=timezone.utc,
        ),
        business_date=date(2026, 7, 29),
        account_configs={"lx": position_config},
        base_config=candidate_config,
        markets_to_run=["US"],
        position_records_by_account={
            "lx": [_position(account="lx", lot_id="lot-lx")]
        },
    )

    merged, resolved_plan = (
        merge_close_advice_requirements_into_prefetch_config(
            candidate_config=candidate_config,
            requirements_plan=plan,
        )
    )

    assert len(merged["symbols"]) == 2
    assert {
        item["fetch"]["port"] for item in merged["symbols"]
    } == {11111, 11112}
    assert all(
        "_close_advice_position_requirements" not in item
        for item in merged["symbols"]
    )
    diagnostic = merged["_close_advice_required_data_diagnostics"][0]
    assert diagnostic["candidate_route_ambiguous"] is True
    assert len(diagnostic["rejected_requirement_ids"]) == 1
    assert (
        resolved_plan["accounts"]["lx"]["requirements"][0][
            "planning_reason"
        ]
        == "required_data_route_conflict"
    )


def test_position_binding_uses_base_fallback_without_defaulting() -> None:
    from src.application.close_advice_required_data import (
        build_close_advice_required_data_plan,
    )

    account_config = _config(account="lx")
    account_config["symbols"] = []
    base_config = _config(account="lx", host="10.0.0.8", port=22222)
    plan = build_close_advice_required_data_plan(
        run_id="run-1",
        run_started_at_utc=datetime(
            2026,
            7,
            29,
            1,
            40,
            tzinfo=timezone.utc,
        ),
        business_date=date(2026, 7, 29),
        account_configs={"lx": account_config},
        base_config=base_config,
        markets_to_run=["US"],
        position_records_by_account={
            "lx": [_position(account="lx", lot_id="lot-lx")]
        },
    )

    binding = plan["accounts"]["lx"]["requirements"][0][
        "fetch_binding"
    ]
    assert binding["config_scope"] == "base"
    assert binding["host"] == "10.0.0.8"
    assert binding["port"] == 22222


def test_missing_or_unsupported_position_binding_is_typed_unavailable() -> None:
    from src.application.close_advice_required_data import (
        build_close_advice_required_data_plan,
    )

    missing = _config(account="lx")
    missing["symbols"] = []
    unsupported = _config(account="sy")
    unsupported["symbols"][0]["fetch"]["source"] = "http"
    plan = build_close_advice_required_data_plan(
        run_id="run-1",
        run_started_at_utc=datetime(
            2026,
            7,
            29,
            1,
            40,
            tzinfo=timezone.utc,
        ),
        business_date=date(2026, 7, 29),
        account_configs={"lx": missing, "sy": unsupported},
        base_config=missing,
        markets_to_run=["US"],
        position_records_by_account={
            "lx": [_position(account="lx", lot_id="lot-lx")],
            "sy": [_position(account="sy", lot_id="lot-sy")],
        },
    )

    assert (
        plan["accounts"]["lx"]["requirements"][0]["planning_reason"]
        == "required_data_symbol_config_missing"
    )
    assert (
        plan["accounts"]["sy"]["requirements"][0]["planning_reason"]
        == "required_data_symbol_source_unsupported"
    )
    assert all(
        "fetch_binding" not in plan["accounts"][account]["requirements"][0]
        for account in ("lx", "sy")
    )


def test_position_only_requirement_creates_one_exact_prefetch_plan(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_planning as planning
    from src.application.close_advice_required_data import (
        build_close_advice_required_data_plan,
    )
    from src.application.required_data_prefetch_planning import (
        merge_close_advice_requirements_into_prefetch_config,
    )

    config = _config(account="lx")
    plan = build_close_advice_required_data_plan(
        run_id="run-1",
        run_started_at_utc=datetime(
            2026,
            7,
            29,
            1,
            40,
            tzinfo=timezone.utc,
        ),
        business_date=date(2026, 7, 29),
        account_configs={"lx": config},
        base_config=config,
        markets_to_run=["US"],
        position_records_by_account={
            "lx": [_position(account="lx", lot_id="lot-lx")]
        },
    )
    candidate_config = dict(config)
    candidate_config["symbols"] = []
    merged, resolved_plan = (
        merge_close_advice_requirements_into_prefetch_config(
            candidate_config=candidate_config,
            requirements_plan=plan,
        )
    )
    symbol_cfg = merged["symbols"][0]
    monkeypatch.setattr(
        planning,
        "get_underlier_spot",
        lambda *_args, **_kwargs: 110.0,
    )
    monkeypatch.setattr(
        planning,
        "list_option_expirations",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        planning.opend_utils,
        "get_trading_date",
        lambda _market: date(2026, 7, 29),
    )
    bundle = planning.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path / "required_data",
        symbol="NVDA",
        limit_expirations=0,
        want_put=False,
        want_call=False,
        sell_put_cfg={"enabled": False},
        sell_call_cfg={"enabled": False},
        position_requirements=symbol_cfg[
            "_close_advice_position_requirements"
        ],
        symbol_cfg=symbol_cfg,
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    assert len(merged["symbols"]) == 1
    assert resolved_plan["accounts"]["lx"]["status"] == "ready"
    assert len(bundle.side_plans) == 1
    side_plan = bundle.side_plans[0]
    assert side_plan.option_type == "put"
    assert side_plan.explicit_expirations == ["2026-08-28"]
    assert side_plan.strike_window.min_strike == 100
    assert side_plan.strike_window.max_strike == 100
    assert side_plan.source_fields == [
        "close_advice.position_requirements"
    ]


def test_position_only_route_conflict_rejects_all_affected_requirements() -> None:
    from src.application.close_advice_required_data import (
        build_close_advice_required_data_plan,
    )
    from src.application.required_data_prefetch_planning import (
        merge_close_advice_requirements_into_prefetch_config,
    )

    configs = {
        "lx": _config(account="lx", port=11111),
        "sy": _config(account="sy", port=11112),
    }
    plan = build_close_advice_required_data_plan(
        run_id="run-1",
        run_started_at_utc=datetime(
            2026,
            7,
            29,
            1,
            40,
            tzinfo=timezone.utc,
        ),
        business_date=date(2026, 7, 29),
        account_configs=configs,
        base_config=configs["lx"],
        markets_to_run=["US"],
        position_records_by_account={
            account: [
                _position(
                    account=account,
                    lot_id=f"lot-{account}",
                )
            ]
            for account in configs
        },
    )
    candidate_config = dict(configs["lx"])
    candidate_config["symbols"] = []

    merged, resolved_plan = (
        merge_close_advice_requirements_into_prefetch_config(
            candidate_config=candidate_config,
            requirements_plan=plan,
        )
    )

    assert merged["symbols"] == []
    diagnostic = merged["_close_advice_required_data_diagnostics"][0]
    assert diagnostic["position_only_conflict"] is True
    assert len(diagnostic["rejected_requirement_ids"]) == 2
    for account in ("lx", "sy"):
        account_plan = resolved_plan["accounts"][account]
        assert account_plan["status"] == "partial"
        assert (
            account_plan["requirements"][0]["planning_reason"]
            == "required_data_route_conflict"
        )


def test_candidate_and_position_routes_normalize_host_case() -> None:
    from src.application.close_advice_required_data import (
        build_close_advice_required_data_plan,
    )
    from src.application.required_data_prefetch_planning import (
        merge_close_advice_requirements_into_prefetch_config,
    )

    position_config = _config(account="lx", host="opend.example")
    plan = build_close_advice_required_data_plan(
        run_id="run-host-case",
        run_started_at_utc=datetime(
            2026,
            7,
            29,
            1,
            40,
            tzinfo=timezone.utc,
        ),
        business_date=date(2026, 7, 29),
        account_configs={"lx": position_config},
        base_config=position_config,
        markets_to_run=["US"],
        position_records_by_account={
            "lx": [_position(account="lx", lot_id="lot-lx")]
        },
    )
    candidate_config = _config(account="lx", host="OpenD.EXAMPLE")

    merged, resolved_plan = (
        merge_close_advice_requirements_into_prefetch_config(
            candidate_config=candidate_config,
            requirements_plan=plan,
        )
    )

    requirement = resolved_plan["accounts"]["lx"]["requirements"][0]
    diagnostic = merged["_close_advice_required_data_diagnostics"][0]
    assert resolved_plan["accounts"]["lx"]["status"] == "ready"
    assert requirement["planning_status"] == "ready"
    assert diagnostic["rejected_requirement_ids"] == []
    assert diagnostic["accepted_requirement_ids"] == [
        requirement["requirement_id"]
    ]


def _frozen_workspace(
    tmp_path: Path,
    *,
    quote_strike: float = 100,
) -> tuple[dict, Path, Path, Path, Path]:
    from src.application.close_advice_required_data import (
        PLAN_FILE_NAME,
        build_close_advice_required_data_plan,
        publish_close_advice_required_data_plan,
    )
    from src.application.ledger.api import position_lot_risk_view
    from src.application.opend_symbol_outputs import (
        publish_required_data_quote_snapshot,
        save_outputs,
    )
    from src.application.required_data_plan_identity import (
        build_required_data_expected_fetch_contract,
        required_data_plan_id,
    )
    from src.application.required_data_snapshot import (
        seal_required_data_snapshot,
    )

    run_id = "run-1"
    run_dir = tmp_path / "output_runs" / run_id
    required_root = run_dir / "required_data"
    (required_root / "raw").mkdir(parents=True)
    (required_root / "parsed").mkdir(parents=True)
    state_dir = run_dir / "state"
    state_dir.mkdir()
    account_state = run_dir / "accounts" / "lx" / "state"
    account_state.mkdir(parents=True)
    output_dir = run_dir / "accounts" / "lx"
    config = _config(account="lx")
    record = _position(account="lx", lot_id="lot-lx")
    plan = build_close_advice_required_data_plan(
        run_id=run_id,
        run_started_at_utc=datetime(
            2026,
            7,
            29,
            1,
            40,
            tzinfo=timezone.utc,
        ),
        business_date=date(2026, 7, 29),
        account_configs={"lx": config},
        base_config=config,
        markets_to_run=["US"],
        position_records_by_account={"lx": [record]},
    )
    plan_path = state_dir / PLAN_FILE_NAME
    publish_close_advice_required_data_plan(
        path=plan_path,
        payload=plan,
    )
    observed_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    completed_at = observed_at + timedelta(seconds=1)
    quote_expiration = "2026-08-28"
    discovery_trading_date = date(2026, 7, 29)
    quote_dte = (
        date.fromisoformat(quote_expiration) - discovery_trading_date
    ).days
    quote_spot = 110.0
    term_lookback = max(20, quote_dte)
    term_input_hash = "a" * 64
    term_entry = {
        "schema_version": "term_matched_rv.v1",
        "expiration": quote_expiration,
        "status": "ok",
        "reason": None,
        "term_matched_rv": 0.2,
        "remaining_sessions": quote_dte,
        "lookback_sessions": term_lookback,
        "input_start": "2026-01-02",
        "input_end": discovery_trading_date.isoformat(),
        "input_close_session_count": term_lookback + 1,
        "input_return_count": term_lookback,
        "input_hash": term_input_hash,
    }
    side_plan = {
        "option_type": "put",
        "min_dte": quote_dte,
        "max_dte": quote_dte,
        "explicit_expirations": [quote_expiration],
        "strike_window": {
            "min_strike": quote_strike,
            "max_strike": quote_strike,
            "source": "close_advice_fixture",
            "buffer_applied": False,
            "buffer_pct": 0.0,
            "base_min_strike": quote_strike,
            "base_max_strike": quote_strike,
        },
        "planning_reason": "close_advice_fixture",
        "source_fields": ["open_position"],
        "spot_reference": quote_spot,
        "min_strike": quote_strike,
        "max_strike": quote_strike,
        "expiration_count": 1,
        "required_exact_strikes_by_expiration": {
            quote_expiration: [float(quote_strike)],
        },
    }
    fetch_plan = {
        "symbol": "NVDA",
        "spot_reference": quote_spot,
        "require_realized_volatility": True,
        "side_plans": [side_plan],
        "merged_requests": [
            {
                "symbol": "NVDA",
                "limit_expirations": 8,
                "host": "127.0.0.1",
                "port": 11111,
                "option_types": ["put"],
                "explicit_expirations": [quote_expiration],
                "trading_date": discovery_trading_date.isoformat(),
                "min_dte": quote_dte,
                "max_dte": quote_dte,
                "side_strike_windows": {
                    "put": {
                        "min_strike": quote_strike,
                        "max_strike": quote_strike,
                    }
                },
                "include_realized_volatility": True,
                "side_plans": [side_plan],
                "planning_reason": "close_advice_fixture",
            }
        ],
        "expiration_discovery_complete": True,
        "expiration_discovery_error": None,
        "expiration_discovery": {
            "outcome": "success_rows",
            "reason_code": None,
            "expirations": [quote_expiration],
            "observed_at_utc": observed_at.isoformat(),
            "completed_at_utc": completed_at.isoformat(),
            "request_identity": {
                "symbol": "NVDA",
                "underlier": "US.NVDA",
                "source": "opend",
                "host": "127.0.0.1",
                "port": 11111,
                "trading_date": discovery_trading_date.isoformat(),
            },
            "error": None,
        },
        "projection_outcome": "success_rows",
        "projected_expirations": [quote_expiration],
    }
    expected_contract = build_required_data_expected_fetch_contract(
        symbol="NVDA",
        fetch_plan=fetch_plan,
        source="opend",
        host="127.0.0.1",
        port=11111,
    )
    quote_payload = {
        "symbol": "NVDA",
        "underlier_code": "US.NVDA",
        "meta": {
            "status": "ok",
            "source": "opend",
            "host": "127.0.0.1",
            "port": 11111,
            "trading_date": discovery_trading_date.isoformat(),
            "source_outcome": "success_rows",
            "source_observed_at": observed_at.isoformat(),
            "completed_at_utc": completed_at.isoformat(),
            "snapshot_requested_codes": 1,
            "snapshot_returned_codes": 1,
            "snapshot_missing_codes": 0,
            "snapshot_unexpected_codes": 0,
            "snapshot_requested_code_set": ["NVDA-P"],
            "snapshot_returned_code_set": ["NVDA-P"],
            "snapshot_missing_code_set": [],
            "snapshot_unexpected_code_set": [],
            "snapshot_complete": True,
            "realized_volatility": {
                "status": "ok",
                "reason": None,
                "realized_volatility_20": 0.2,
                "realized_volatility_60": 0.2,
                "realized_volatility_120": 0.2,
                "realized_volatility_estimate": 0.2,
                "term_matched": {quote_expiration: term_entry},
                "qfq_history": {"status": "ok"},
                "trading_calendar": {"status": "ok"},
            },
        },
        "rows": [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": quote_expiration,
                "dte": quote_dte,
                "contract_symbol": "NVDA-P",
                "strike": quote_strike,
                "spot": quote_spot,
                "bid": 1.9,
                "ask": 2.1,
                "mid": 2.0,
                "last_price": 2.0,
                "implied_volatility": 0.3,
                "realized_volatility_20": 0.2,
                "realized_volatility_60": 0.2,
                "realized_volatility_120": 0.2,
                "realized_volatility_estimate": 0.2,
                "term_matched_rv": 0.2,
                "term_matched_rv_status": "ok",
                "term_matched_rv_reason": None,
                "term_matched_rv_remaining_sessions": quote_dte,
                "term_matched_rv_lookback_sessions": term_lookback,
                "term_matched_rv_input_start": "2026-01-02",
                "term_matched_rv_input_end": discovery_trading_date.isoformat(),
                "term_matched_rv_input_session_count": term_lookback + 1,
                "term_matched_rv_input_hash": term_input_hash,
                "delta": -0.3,
                "multiplier": 100,
            }
        ],
    }
    raw_path, csv_path = save_outputs(
        tmp_path,
        "NVDA",
        quote_payload,
        output_root=required_root,
    )
    publish_required_data_quote_snapshot(
        producer_root=required_root,
        producer_run_id=run_id,
        symbol="NVDA",
        raw_path=raw_path,
        csv_path=csv_path,
        fetch_plan=fetch_plan,
        fetch_policy={
            "source": "opend",
            "host": "127.0.0.1",
            "port": 11111,
        },
        expected_fetch_contract=expected_contract,
        source_observed_at=observed_at,
        completed_at=completed_at,
        now=completed_at,
    )
    plan_items = [
        {
            "symbol": "NVDA",
            "source": "opend",
            "fetch_plan": fetch_plan,
            "fetch_binding": expected_contract["fetch_binding"],
            "expected_fetch_contract": expected_contract,
            "projection_outcome": "success_rows",
            "discovery_status": "complete",
        }
    ]
    manifest_path = state_dir / "required_data_snapshot_manifest.json"
    seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=required_root,
        run_id=run_id,
        prefetch_summary={
            "global_required_data_plan": {
                "plan_id": required_data_plan_id(plan_items),
                "symbols": plan_items,
            },
            "symbols": [],
            "results": [],
        },
        close_advice_required_data_plan_path=plan_path,
    )
    position = position_lot_risk_view(
        record,
        as_of_date=date(2026, 7, 29),
    ).as_open_position_min(as_of_date=date(2026, 7, 29))
    context_path = account_state / "option_positions_context.json"
    context_path.write_text(
        json.dumps(
            {
                "context_status": "available",
                "filters": {"broker": "富途", "account": "lx"},
                "open_positions_min": [position],
            }
        ),
        encoding="utf-8",
    )
    return config, context_path, required_root, output_dir, manifest_path


def test_frozen_close_advice_reads_only_sealed_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import close_advice_runner as runner

    (
        config,
        context_path,
        required_root,
        output_dir,
        manifest_path,
    ) = _frozen_workspace(tmp_path)
    monkeypatch.setattr(
        runner,
        "_ensure_required_data_coverage_for_positions",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("frozen mode must not repair coverage")
        ),
    )
    monkeypatch.setattr(
        runner,
        "_fetch_missing_quotes_via_opend",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("frozen mode must not fetch fallback quotes")
        ),
    )
    (
        required_root / "parsed" / "NVDA_required_data.meta.json"
    ).write_text(
        json.dumps(
            {
                "symbol": "NVDA",
                "status": "stale",
                "csv_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    tracked = sorted(
        [
            *required_root.glob("raw/*"),
            *required_root.glob("parsed/*"),
            *required_root.glob("receipts/**/*"),
        ]
    )
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tracked
        if path.is_file()
    }

    result = runner.run_close_advice(
        config=config,
        context_path=context_path,
        required_data_root=required_root,
        output_dir=output_dir,
        base_dir=tmp_path,
        markets_to_run=["US"],
        required_data_snapshot_manifest=manifest_path,
        required_data_snapshot_run_id="run-1",
        close_advice_required_data_plan=(
            manifest_path.parent
            / "close_advice_required_data_plan.json"
        ),
        account="lx",
    )

    assert result["snapshot_authority"] == "valid"
    assert result["quote_mode"] == "frozen_snapshot"
    assert result["quote_fetch_diagnostics"]["network_fetch_attempts"] == 0
    assert (
        result["quote_fetch_diagnostics"][
            "required_data_write_attempts"
        ]
        == 0
    )
    assert (
        result["quote_fetch_diagnostics"][
            "position_requirements_validated"
        ]
        == 1
    )
    assert (
        result["quote_fetch_diagnostics"][
            "position_requirements_missing"
        ]
        == 0
    )
    assert len(result["quote_fetch_diagnostics"]["binding_ids"]) == 1
    assert result["business_date"] == "2026-07-29"
    assert result["report_manifest"]["status"] == "success"
    assert result["close_advice_required_data_plan_sha256"]
    row = pd.read_csv(output_dir / "close_advice.csv").iloc[0].to_dict()
    assert row["quote_mode"] == "frozen_snapshot"
    assert row["required_data_snapshot_plan_id"]
    assert row["required_data_snapshot_manifest_sha256"]
    assert row["close_advice_required_data_plan_sha256"]
    assert row["required_data_requirement_id"]
    assert row["required_data_binding_id"]
    assert row["required_data_snapshot_id"]
    assert row["required_data_receipt_hash"]
    assert row["required_data_payload_sha256"]
    assert row["required_data_source_observed_at"]
    assert row["required_data_expires_at"]
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in before
    } == before


def test_bound_plan_snapshot_returns_the_exact_validated_generation(
    tmp_path: Path,
) -> None:
    from src.application.close_advice_required_data import (
        resolve_bound_close_advice_required_data_plan_snapshot,
    )
    from src.application.required_data_snapshot import (
        load_required_data_snapshot_manifest_snapshot,
    )

    (
        _config_payload,
        _context_path,
        required_root,
        _output_dir,
        manifest_path,
    ) = _frozen_workspace(tmp_path)
    manifest, _root, _manifest_bytes = (
        load_required_data_snapshot_manifest_snapshot(
            manifest_path=manifest_path,
            expected_run_id="run-1",
            expected_required_data_root=required_root,
        )
    )
    snapshot = resolve_bound_close_advice_required_data_plan_snapshot(
        manifest_path=manifest_path,
        manifest=manifest,
        expected_run_id="run-1",
    )
    assert snapshot is not None
    payload, plan_path, plan_bytes = snapshot
    plan_path.write_text("{}\n", encoding="utf-8")

    assert json.loads(plan_bytes) == payload
    assert plan_path.read_bytes() != plan_bytes


def test_frozen_close_advice_rejects_parent_manifest_generation_mismatch(
    tmp_path: Path,
) -> None:
    from src.application.close_advice_runner import run_close_advice

    (
        config,
        context_path,
        required_root,
        output_dir,
        manifest_path,
    ) = _frozen_workspace(tmp_path)

    result = run_close_advice(
        config=config,
        context_path=context_path,
        required_data_root=required_root,
        output_dir=output_dir,
        base_dir=tmp_path,
        markets_to_run=["US"],
        required_data_snapshot_manifest=manifest_path,
        required_data_snapshot_manifest_sha256="0" * 64,
        required_data_snapshot_run_id="run-1",
        close_advice_required_data_plan=(
            manifest_path.parent / "close_advice_required_data_plan.json"
        ),
        account="lx",
    )

    assert result["status"] == "snapshot_integrity_failed"
    assert result["snapshot_authority"] == "invalid"
    assert "generation mismatch" in result["integrity_failure"]["evidence"][
        "message"
    ]


def test_frozen_integrity_failure_invalidates_old_success_report(
    tmp_path: Path,
) -> None:
    from src.application.close_advice_report_manifest import (
        validate_close_advice_report_manifest,
    )
    from src.application.close_advice_runner import run_close_advice

    (
        config,
        context_path,
        required_root,
        output_dir,
        manifest_path,
    ) = _frozen_workspace(tmp_path)
    kwargs = {
        "config": config,
        "context_path": context_path,
        "required_data_root": required_root,
        "output_dir": output_dir,
        "base_dir": tmp_path,
        "markets_to_run": ["US"],
        "required_data_snapshot_manifest": manifest_path,
        "required_data_snapshot_run_id": "run-1",
        "close_advice_required_data_plan": (
            manifest_path.parent
            / "close_advice_required_data_plan.json"
        ),
        "account": "lx",
    }
    first = run_close_advice(**kwargs)
    assert first["snapshot_authority"] == "valid"
    old_csv = (output_dir / "close_advice.csv").read_bytes()
    quote_csv = required_root / "parsed" / "NVDA_required_data.csv"
    quote_csv.write_bytes(quote_csv.read_bytes() + b"\n")

    second = run_close_advice(**kwargs)

    assert second["status"] == "snapshot_integrity_failed"
    assert second["snapshot_authority"] == "invalid"
    assert (output_dir / "close_advice.csv").read_bytes() == old_csv
    validation = validate_close_advice_report_manifest(
        csv_path=output_dir / "close_advice.csv",
        desired_market="US",
        account="lx",
    )
    assert validation["ok"] is False
    assert validation["reason"] == "close_advice_manifest_not_success"
    assert validation["status"] == "failed"


def test_close_report_manifest_binds_run_and_quote_mode(tmp_path: Path) -> None:
    from src.application.close_advice_report_manifest import (
        validate_close_advice_report_manifest,
    )
    from src.application.close_advice_runner import run_close_advice

    (
        config,
        context_path,
        required_root,
        output_dir,
        manifest_path,
    ) = _frozen_workspace(tmp_path)
    result = run_close_advice(
        config=config,
        context_path=context_path,
        required_data_root=required_root,
        output_dir=output_dir,
        base_dir=tmp_path,
        markets_to_run=["US"],
        required_data_snapshot_manifest=manifest_path,
        required_data_snapshot_run_id="run-1",
        close_advice_required_data_plan=(
            manifest_path.parent / "close_advice_required_data_plan.json"
        ),
        account="lx",
    )

    assert result["snapshot_authority"] == "valid"
    valid = validate_close_advice_report_manifest(
        csv_path=output_dir / "close_advice.csv",
        desired_market="US",
        account="lx",
        expected_run_id="run-1",
        expected_quote_mode="frozen_snapshot",
    )
    wrong_run = validate_close_advice_report_manifest(
        csv_path=output_dir / "close_advice.csv",
        expected_run_id="run-2",
    )
    wrong_mode = validate_close_advice_report_manifest(
        csv_path=output_dir / "close_advice.csv",
        expected_quote_mode="legacy_mutable",
    )

    assert valid["ok"] is True
    assert wrong_run["reason"] == "close_advice_report_run_mismatch"
    assert wrong_mode["reason"] == "close_advice_report_quote_mode_mismatch"


def test_frozen_missing_exact_contract_is_position_scoped_without_fetch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import close_advice_runner as runner

    (
        config,
        context_path,
        required_root,
        output_dir,
        manifest_path,
    ) = _frozen_workspace(tmp_path, quote_strike=105)
    monkeypatch.setattr(
        runner,
        "_ensure_required_data_coverage_for_positions",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("frozen mode must not repair coverage")
        ),
    )
    monkeypatch.setattr(
        runner,
        "_fetch_missing_quotes_via_opend",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("frozen mode must not fetch fallback quotes")
        ),
    )

    result = runner.run_close_advice(
        config=config,
        context_path=context_path,
        required_data_root=required_root,
        output_dir=output_dir,
        base_dir=tmp_path,
        markets_to_run=["US"],
        required_data_snapshot_manifest=manifest_path,
        required_data_snapshot_run_id="run-1",
        close_advice_required_data_plan=(
            manifest_path.parent
            / "close_advice_required_data_plan.json"
        ),
        account="lx",
    )

    assert result["snapshot_authority"] == "valid"
    assert result["status"] == "degraded"
    assert result["evaluation_gap_rows"] == 1
    assert result["flag_counts"]["required_data_missing_contract"] == 1
    assert result["notify_rows"] == 0
    assert result["quote_fetch_diagnostics"]["network_fetch_attempts"] == 0
    assert (
        result["quote_fetch_diagnostics"][
            "position_requirements_missing"
        ]
        == 1
    )


def test_frozen_evaluation_consumes_validated_receipt_bytes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import close_advice_runner as runner

    (
        config,
        context_path,
        required_root,
        output_dir,
        manifest_path,
    ) = _frozen_workspace(tmp_path)
    quote_csv = required_root / "parsed" / "NVDA_required_data.csv"
    original_bytes = quote_csv.read_bytes()
    original_resolve = runner.resolve_frozen_required_data_csv_bytes_batch
    original_load = runner._load_frozen_required_data_quotes
    resolve_calls = 0

    def _resolve_then_tamper(**kwargs):
        nonlocal resolve_calls
        resolved = original_resolve(**kwargs)
        resolve_calls += 1
        if resolve_calls == 1:
            quote_csv.write_text("tampered\n", encoding="utf-8")
        return resolved

    def _load_then_restore(**kwargs):
        quotes = original_load(**kwargs)
        quote_csv.write_bytes(original_bytes)
        return quotes

    monkeypatch.setattr(
        runner,
        "resolve_frozen_required_data_csv_bytes_batch",
        _resolve_then_tamper,
    )
    monkeypatch.setattr(
        runner,
        "_load_frozen_required_data_quotes",
        _load_then_restore,
    )

    result = runner.run_close_advice(
        config=config,
        context_path=context_path,
        required_data_root=required_root,
        output_dir=output_dir,
        base_dir=tmp_path,
        markets_to_run=["US"],
        required_data_snapshot_manifest=manifest_path,
        required_data_snapshot_run_id="run-1",
        close_advice_required_data_plan=(
            manifest_path.parent
            / "close_advice_required_data_plan.json"
        ),
        account="lx",
    )

    assert result["snapshot_authority"] == "valid"
    assert result["evaluable_rows"] == 1
    row = pd.read_csv(output_dir / "close_advice.csv").iloc[0]
    assert row["close_mid"] == 2.0
    assert quote_csv.read_bytes() == original_bytes
    assert resolve_calls == 2


def test_legacy_unbound_snapshot_degrades_positions_without_fetch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from domain.domain.decision_state_fingerprint import canonical_sha256
    from src.application import close_advice_runner as runner

    (
        config,
        context_path,
        required_root,
        output_dir,
        manifest_path,
    ) = _frozen_workspace(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("close_advice_required_data_plan_relpath")
    manifest.pop("close_advice_required_data_plan_sha256")
    manifest.pop("content_sha256")
    manifest["content_sha256"] = canonical_sha256(manifest)
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        runner,
        "expiration_business_today",
        lambda *_args, **_kwargs: date(2026, 7, 29),
    )
    monkeypatch.setattr(
        runner,
        "_ensure_required_data_coverage_for_positions",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("frozen mode must not repair coverage")
        ),
    )
    monkeypatch.setattr(
        runner,
        "_fetch_missing_quotes_via_opend",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("frozen mode must not fetch fallback quotes")
        ),
    )

    result = runner.run_close_advice(
        config=config,
        context_path=context_path,
        required_data_root=required_root,
        output_dir=output_dir,
        base_dir=tmp_path,
        markets_to_run=["US"],
        required_data_snapshot_manifest=manifest_path,
        required_data_snapshot_run_id="run-1",
        account="lx",
    )

    assert result["snapshot_authority"] == "valid"
    assert result["status"] == "degraded"
    assert result["evaluation_gap_rows"] == 1
    assert result["flag_counts"]["close_advice_plan_unavailable"] == 1
    assert result["notify_rows"] == 0
    assert result["quote_fetch_diagnostics"]["network_fetch_attempts"] == 0


def test_unsafe_bound_plan_path_fails_snapshot_authority(
    tmp_path: Path,
) -> None:
    from domain.domain.decision_state_fingerprint import canonical_sha256
    from src.application.close_advice_runner import run_close_advice

    (
        config,
        context_path,
        required_root,
        output_dir,
        manifest_path,
    ) = _frozen_workspace(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["close_advice_required_data_plan_relpath"] = "../outside.json"
    manifest.pop("content_sha256")
    manifest["content_sha256"] = canonical_sha256(manifest)
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )

    result = run_close_advice(
        config=config,
        context_path=context_path,
        required_data_root=required_root,
        output_dir=output_dir,
        base_dir=tmp_path,
        markets_to_run=["US"],
        required_data_snapshot_manifest=manifest_path,
        required_data_snapshot_run_id="run-1",
        account="lx",
    )

    assert result["status"] == "snapshot_integrity_failed"
    assert result["snapshot_authority"] == "invalid"
    assert (
        result["integrity_failure"]["evidence"]["error_type"]
        == "CloseAdviceRequiredDataPlanError"
    )
