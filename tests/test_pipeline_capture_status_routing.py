from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


RUN_ID = "20260810T020017Z-test"
ACCOUNT_CONFIG_SHA256 = "a" * 64
RETIRED_CANDIDATE_CSV_FRAGMENTS = (
    "_candidates.csv",
    "_candidates_labeled.csv",
    "_candidates_reject_log.csv",
    "_reject_log.csv",
    "_pair_diagnostics.csv",
    "_rank_shadow.csv",
    "_put_universe.csv",
    "_put_universe_labeled.csv",
    "_put_universe_cash_filtered.csv",
    "_put_universe_underwritten.csv",
    "sell_put_linked_calls.csv",
)


def _symbol_config(
    symbol: str,
    *,
    variant: str = "sp_lc",
    market: str = "HK",
    sell_put_enabled: bool = True,
    sell_call_enabled: bool = False,
    combo_enabled: bool = True,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "broker": market,
        "sell_put": {"enabled": sell_put_enabled},
        "sell_call": {"enabled": sell_call_enabled},
        "combo_yield": {"enabled": combo_enabled, "variant": variant},
    }


def _status(
    symbol: str,
    mode: str,
    status: str,
    *,
    variant: str | None = None,
    quote: str = "quote-1",
    reason: str | None = None,
    source_outcome: str | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    resolved_reason = reason or reason_code
    if status != "completed" and resolved_reason is None:
        resolved_reason = f"{status}_for_test"
    payload: dict[str, Any] = {
        "symbol": symbol,
        "strategy_mode": mode,
        "status": status,
        "reason": resolved_reason,
        "quote_snapshot_id": quote,
        "quote_receipt_relpath": f"quotes/{quote}/receipt.json",
    }
    if variant is not None:
        payload["variant"] = variant
    if source_outcome is not None:
        payload["source_outcome"] = source_outcome
    if reason_code is not None:
        payload["reason_code"] = reason_code
    return payload


def _run_default_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    symbols: list[dict[str, Any]],
    statuses: list[dict[str, Any]],
    pairs: list[dict[str, Any]] | None = None,
    emit_combo_evidence: bool = True,
    emit_status_capture: bool = True,
) -> None:
    from src.application import pipeline_watchlist as mod

    required_manifest = tmp_path / "required_data_manifest.json"
    portfolio_manifest = tmp_path / "prepared_portfolio_context.json"
    ledger_manifest = tmp_path / "prepared_option_positions_context.json"
    account_dir = tmp_path / "output_runs" / RUN_ID / "accounts" / "lx"
    account_dir.mkdir(parents=True)
    for path in (required_manifest, portfolio_manifest, ledger_manifest):
        path.write_text("{}\n", encoding="utf-8")
    report_dir = account_dir

    monkeypatch.setattr(
        mod,
        "resolve_watchlist_item_runtime_config",
        lambda *, item, **_kwargs: dict(item),
    )

    def _fake_pipeline(**kwargs: Any) -> list[dict[str, Any]]:
        from src.application.strategy_scan_status import (
            publish_strategy_scan_status,
            publish_strategy_scan_status_index_v2,
        )

        selected_pairs = [dict(item) for item in (pairs or [])]
        combo_counts: dict[str, int] = {}
        for item in selected_pairs:
            symbol = str(item.get("symbol") or "").strip().upper()
            combo_counts[symbol] = combo_counts.get(symbol, 0) + 1

        expected: list[dict[str, str]] = []
        for configured in symbols:
            symbol = str(configured["symbol"]).upper()
            market = str(configured.get("broker") or "").strip().upper()
            if bool((configured.get("sell_put") or {}).get("enabled")):
                expected.append(
                    {
                        "market": market,
                        "symbol": symbol,
                        "strategy_family": "sell_put",
                        "strategy_mode": "put",
                        "candidate_owner": "opening",
                        "account_config_sha256": ACCOUNT_CONFIG_SHA256,
                    }
                )
            if bool((configured.get("sell_call") or {}).get("enabled")):
                expected.append(
                    {
                        "market": market,
                        "symbol": symbol,
                        "strategy_family": "covered_call",
                        "strategy_mode": "call",
                        "candidate_owner": "opening",
                        "account_config_sha256": ACCOUNT_CONFIG_SHA256,
                    }
                )
            combo_cfg = dict(configured.get("combo_yield") or {})
            if combo_cfg.get("enabled"):
                variant = str(combo_cfg.get("variant") or "sp_lc")
                expected.append(
                    {
                        "market": market,
                        "symbol": symbol,
                        "strategy_family": "combo_yield",
                        "strategy_mode": "combo_yield",
                        "candidate_owner": variant,
                        "account_config_sha256": ACCOUNT_CONFIG_SHA256,
                    }
                )

        expected_keys = {
            (str(item["symbol"]), str(item["strategy_family"]))
            for item in expected
        }
        published: set[tuple[str, str]] = set()
        for item in statuses:
            if emit_status_capture:
                kwargs["candidate_capture_status_sink_fn"](item)
            mode = str(item.get("strategy_mode") or "").strip().lower()
            family = {
                "put": "sell_put",
                "call": "covered_call",
                "combo_yield": "combo_yield",
            }.get(mode)
            symbol = str(item.get("symbol") or "").strip().upper()
            key = (symbol, str(family or ""))
            if family is None or key not in expected_keys or key in published:
                continue
            published.add(key)
            status = str(item.get("status") or "").strip().lower()
            expected_market = next(
                str(scope["market"])
                for scope in expected
                if (str(scope["symbol"]), str(scope["strategy_family"])) == key
            )
            publish_strategy_scan_status(
                report_dir=report_dir,
                run_id=RUN_ID,
                account="lx",
                market=expected_market,
                symbol=symbol,
                strategy_family=family,
                status=status,
                candidate_count=(
                    combo_counts.get(symbol, 0)
                    if family == "combo_yield" and status == "completed"
                    else 0 if status == "completed" else None
                ),
                reason=str(item.get("reason") or "").strip() or None,
                snapshot_id=str(item.get("quote_snapshot_id") or "").strip() or None,
                receipt_relpath=str(item.get("quote_receipt_relpath") or "").strip() or None,
                source_outcome=str(item.get("source_outcome") or "").strip() or None,
                reason_code=str(item.get("reason_code") or "").strip() or None,
            )
        for item in expected:
            key = (str(item["symbol"]), str(item["strategy_family"]))
            if key in published:
                continue
            publish_strategy_scan_status(
                report_dir=report_dir,
                run_id=RUN_ID,
                account="lx",
                market=str(item["market"]),
                symbol=str(item["symbol"]),
                strategy_family=str(item["strategy_family"]),
                status="failed",
                reason="strategy_scan_status_missing",
            )
        publish_strategy_scan_status_index_v2(
            report_dir=report_dir,
            run_id=RUN_ID,
            account="lx",
            account_config_sha256=ACCOUNT_CONFIG_SHA256,
            expected=expected,
        )

        evidence_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for pair in selected_pairs:
            evidence_by_symbol.setdefault(
                str(pair.get("symbol") or "").strip().upper(), []
            ).append(pair)
        for item in statuses:
            if str(item.get("strategy_mode") or "").strip().lower() != "combo_yield":
                continue
            if not emit_combo_evidence:
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            pair_rows = evidence_by_symbol.get(symbol, [])
            variant = str(item.get("variant") or "").strip().lower()
            if pair_rows and str(pair_rows[0].get("variant") or "").strip():
                variant = str(pair_rows[0].get("variant") or "").strip().lower()
            rank_records = [
                {
                    **pair,
                    "baseline_rank": rank,
                    "shadow_rank": rank,
                    "baseline_selected": True,
                    "shadow_selected": True,
                    "rank_changed": False,
                }
                for rank, pair in enumerate(pair_rows, start=1)
            ]
            kwargs["combo_evidence_sink_fn"](
                {
                    "schema_version": "combo_yield_scan_evidence.v1",
                    "variant": variant,
                    "symbol": symbol,
                    "funding_put_decisions": [],
                    "pair_evaluations": [
                        {
                            **pair,
                            "diagnostic_scope": "pair",
                            "diagnostic_stage": "pair_filter",
                            "accepted": True,
                            "reject_reasons": "",
                        }
                        for pair in pair_rows
                    ],
                    "rank_records": rank_records,
                    "ranked_pairs": pair_rows,
                }
            )
        completed_combo_statuses = [
            item
            for item in statuses
            if str(item.get("strategy_mode") or "").strip().lower()
            == "combo_yield"
            and str(item.get("status") or "").strip().lower() == "completed"
        ]
        if completed_combo_statuses:
            from src.application.candidate_filter_trace import (
                append_candidate_filter_trace_rows,
                build_candidate_filter_trace_row,
            )
            from src.application.combo_yield_candidate_snapshot import (
                COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
            )
            from src.application.cc_lp_candidate_snapshot import (
                CC_LP_CANDIDATE_SNAPSHOT_FILE,
            )

            append_candidate_filter_trace_rows(
                report_dir / "candidate_filter_trace.jsonl",
                [
                    build_candidate_filter_trace_row(
                        run_id=RUN_ID,
                        account="lx",
                        symbol=str(item["symbol"]),
                        function="combo_yield",
                        mode="sp_lc",
                        strategy_family="combo_yield",
                        strategy_profile="sp_lc",
                        status=(
                            "accepted"
                            if combo_counts.get(
                                str(item.get("symbol") or "").strip().upper(),
                                0,
                            )
                            else "post_filtered"
                        ),
                        stage="post_filter",
                        rule=(
                            "combo_yield_pair_accepted"
                            if combo_counts.get(
                                str(item.get("symbol") or "").strip().upper(),
                                0,
                            )
                            else "combo_yield_no_pair"
                        ),
                        metric_value=combo_counts.get(
                            str(item.get("symbol") or "").strip().upper(),
                            0,
                        ),
                        threshold=1,
                        evidence_path=(
                            "state/"
                            + (
                                CC_LP_CANDIDATE_SNAPSHOT_FILE
                                if str(item.get("variant") or "").strip().lower()
                                == "cc_lp"
                                else COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE
                            )
                        ),
                    )
                    for item in completed_combo_statuses
                ],
            )
        kwargs["opening_runtime_context_sink_fn"](
            {
                "capacity_authority": {
                    "status": "available",
                    "logical_account": "lx",
                    "futu_account_id": "10001",
                    "trd_env": "REAL",
                    "market": str(symbols[0].get("broker") or "").strip().upper(),
                    "source": "opend",
                },
                "exchange_rates": {},
            },
            {"exchange_rates": {}},
        )
        return []

    monkeypatch.setattr(mod, "run_watchlist_pipeline", _fake_pipeline)
    mod.run_watchlist_pipeline_default(
        py="python3",
        base=tmp_path,
        cfg={"portfolio": {"account": "lx"}, "symbols": symbols},
        report_dir=report_dir,
        state_dir=tmp_path / "state",
        shared_state_dir=tmp_path / "shared_state",
        required_data_dir=tmp_path / "required_data",
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=10,
        portfolio_timeout_sec=10,
        want_scan=True,
        no_context=False,
        symbols_arg=None,
        log=lambda _message: None,
        want_fn=lambda name: name == "scan",
        source_account_run_id=RUN_ID,
        required_data_snapshot_manifest=required_manifest,
        prepared_portfolio_context_manifest=portfolio_manifest,
        prepared_option_positions_context_manifest=ledger_manifest,
        account_config_sha256=ACCOUNT_CONFIG_SHA256,
    )


def _run_full_symbol_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    market: str,
    symbol: str,
    variant: str,
    opening_mode: str,
    scenario: str,
) -> dict[str, list[str] | int]:
    """Run the real watchlist/symbol/status/sealing chain with frozen adapters."""

    from src.application import pipeline_context, pipeline_symbol, pipeline_watchlist
    from src.application.candidate_filter_trace import (
        append_candidate_filter_trace_rows,
        build_candidate_filter_trace_row,
    )
    from src.application.combo_yield_candidate_snapshot import (
        COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
    )

    required_manifest = tmp_path / "required_data_manifest.json"
    portfolio_manifest = tmp_path / "prepared_portfolio_context.json"
    ledger_manifest = tmp_path / "prepared_option_positions_context.json"
    account_dir = tmp_path / "output_runs" / RUN_ID / "accounts" / "lx"
    required_data_dir = tmp_path / "required_data"
    account_dir.mkdir(parents=True)
    required_data_dir.mkdir(parents=True)
    for path in (required_manifest, portfolio_manifest, ledger_manifest):
        path.write_text("{}\n", encoding="utf-8")

    observed: dict[str, list[str] | int] = {
        "context_builds": 0,
        "required_data": [],
        "opening_scans": [],
        "combo_scans": [],
    }
    portfolio_ctx = {
        "portfolio_source_name": "futu",
        "capacity_authority": {
            "status": "available",
            "logical_account": "lx",
            "futu_account_id": "10001",
            "trd_env": "REAL",
            "market": market,
            "source": "opend",
        },
        "stocks_by_symbol": {
            symbol: {
                "symbol": symbol,
                "shares": 1_000,
                "can_sell_qty": 1_000,
                "avg_cost": 100.0,
            }
        },
        "exchange_rates": {},
    }
    option_ctx = {
        "context_status": "available",
        "locked_shares_status": "available",
        "locked_shares_by_symbol": {},
        "locked_shares_unavailable_by_symbol": {},
        "cash_secured_by_symbol_by_ccy": {},
        "cash_secured_total_by_ccy": {},
        "cash_secured_unavailable_by_symbol": {},
        "exchange_rates": {},
    }

    def _build_context(**_kwargs: Any) -> tuple[dict, dict, float, float]:
        observed["context_builds"] = int(observed["context_builds"]) + 1
        return portfolio_ctx, option_ctx, 7.0, 0.92

    def _ensure_required_data(**kwargs: Any) -> dict[str, Any]:
        required_calls = observed["required_data"]
        assert isinstance(required_calls, list)
        required_calls.append(str(kwargs["symbol"]))
        evidence: dict[str, Any] = {
            "snapshot_id": "quote-1",
            "receipt_relpath": "quotes/quote-1/receipt.json",
            "source_outcome": "success_rows",
            "reason_code": None,
        }
        if scenario == "success_empty":
            evidence.update(
                {
                    "source_outcome": "success_empty",
                    "reason_code": "no_contract_rows",
                }
            )
        return evidence

    def _opening_runner(mode: str, strategy: str):
        def _run(**kwargs: Any) -> dict[str, Any] | list[dict[str, Any]]:
            opening_calls = observed["opening_scans"]
            assert isinstance(opening_calls, list)
            opening_calls.append(mode)
            if scenario == "failure":
                raise RuntimeError(f"{mode} scan failed for test")
            final_sink = kwargs.get("final_candidates_sink_fn")
            decision_sink = kwargs.get("candidate_decisions_sink_fn")
            if final_sink is not None:
                final_sink(mode, [])
            if decision_sink is not None:
                decision_sink(mode, [])
            summary: dict[str, Any] = {
                "symbol": symbol,
                "strategy": strategy,
                "candidate_count": 0,
                "_strategy_status": "completed",
            }
            if scenario == "empty":
                summary["_strategy_reason"] = "no_candidate"
            return [summary] if mode == "put" else summary

        return _run

    def _combo_runner(**kwargs: Any) -> dict[str, Any]:
        combo_calls = observed["combo_scans"]
        assert isinstance(combo_calls, list)
        combo_calls.append(variant)
        if scenario == "failure":
            raise RuntimeError("combo scan failed for test")

        pair_rows: list[dict[str, Any]] = []
        if scenario == "enabled":
            pair_rows = [
                {
                    "candidate_pair_id": (
                        f"cc_lp:{symbol}:C:P"
                        if variant == "cc_lp"
                        else f"combo_yield:{symbol}:P:C"
                    ),
                    "symbol": symbol,
                }
            ]
        evidence_sink = kwargs.get("combo_evidence_sink_fn")
        if evidence_sink is not None:
            evidence_sink(
                {
                    "schema_version": "combo_yield_scan_evidence.v1",
                    "variant": variant,
                    "symbol": symbol,
                    "funding_put_decisions": [],
                    "pair_evaluations": [
                        {
                            **pair,
                            "diagnostic_scope": "pair",
                            "diagnostic_stage": "pair_filter",
                            "accepted": True,
                            "reject_reasons": [],
                        }
                        for pair in pair_rows
                    ],
                    "rank_records": [
                        {
                            **pair,
                            "baseline_rank": rank,
                            "shadow_rank": rank,
                            "baseline_selected": True,
                            "shadow_selected": True,
                            "rank_changed": False,
                        }
                        for rank, pair in enumerate(pair_rows, start=1)
                    ],
                    "ranked_pairs": pair_rows,
                }
            )

        if variant == "sp_lc":
            count = len(pair_rows)
            append_candidate_filter_trace_rows(
                account_dir / "candidate_filter_trace.jsonl",
                [
                    build_candidate_filter_trace_row(
                        run_id=RUN_ID,
                        account="lx",
                        symbol=symbol,
                        function="combo_yield",
                        mode="sp_lc",
                        strategy_family="combo_yield",
                        strategy_profile="sp_lc",
                        status="accepted" if count else "post_filtered",
                        stage="post_filter",
                        rule=(
                            "combo_yield_pair_accepted"
                            if count
                            else "combo_yield_no_pair"
                        ),
                        metric_value=count,
                        threshold=1,
                        evidence_path=(
                            f"state/{COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE}"
                        ),
                    )
                ],
            )

        summary: dict[str, Any] = {
            "symbol": symbol,
            "strategy": "combo_yield",
            "candidate_count": len(pair_rows),
            "_strategy_status": "completed",
        }
        if scenario == "empty":
            summary["_strategy_reason"] = "no_candidate"
        if variant == "cc_lp":
            summary["status"] = (
                "candidates_found" if pair_rows else "no_candidate"
            )
        return summary

    monkeypatch.setattr(pipeline_context, "build_pipeline_context", _build_context)
    monkeypatch.setattr(pipeline_symbol, "build_converter", lambda **_kwargs: object())
    monkeypatch.setattr(
        pipeline_symbol,
        "ensure_required_data",
        _ensure_required_data,
    )
    monkeypatch.setattr(
        pipeline_symbol,
        "run_sell_put_scan_and_summarize",
        _opening_runner("put", "sell_put"),
    )
    monkeypatch.setattr(
        pipeline_symbol,
        "run_sell_call_scan_and_summarize",
        _opening_runner("call", "sell_call"),
    )
    monkeypatch.setattr(
        pipeline_symbol,
        "run_combo_yield_for_symbol_and_summarize",
        _combo_runner,
    )

    active = scenario != "disabled"
    pipeline_watchlist.run_watchlist_pipeline_default(
        py="python3",
        base=tmp_path,
        cfg={
            "portfolio": {"account": "lx"},
            "runtime": {"pipeline_symbol_max_workers": 1},
            "symbols": [
                _symbol_config(
                    symbol,
                    variant=variant,
                    market=market,
                    sell_put_enabled=active and opening_mode == "put",
                    sell_call_enabled=active and opening_mode == "call",
                    combo_enabled=active,
                )
            ],
        },
        report_dir=account_dir,
        state_dir=tmp_path / "state",
        shared_state_dir=tmp_path / "shared_state",
        required_data_dir=required_data_dir,
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=10,
        portfolio_timeout_sec=10,
        want_scan=True,
        no_context=False,
        symbols_arg=None,
        log=lambda _message: None,
        want_fn=lambda name: name == "scan",
        source_account_run_id=RUN_ID,
        required_data_snapshot_manifest=required_manifest,
        prepared_portfolio_context_manifest=portfolio_manifest,
        prepared_option_positions_context_manifest=ledger_manifest,
        account_config_sha256=ACCOUNT_CONFIG_SHA256,
    )
    return observed


@pytest.mark.parametrize(
    ("market", "symbol"),
    (("US", "NVDA"), ("HK", "0700.HK")),
)
@pytest.mark.parametrize(
    ("variant", "opening_mode"),
    (("sp_lc", "put"), ("cc_lp", "call")),
)
@pytest.mark.parametrize(
    "scenario",
    ("enabled", "disabled", "empty", "failure", "success_empty"),
)
def test_candidate_csv_retirement_account_run_matrix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    market: str,
    symbol: str,
    variant: str,
    opening_mode: str,
    scenario: str,
) -> None:
    from src.application.candidate_filter_trace import (
        TRACE_SCHEMA_VERSION,
        read_candidate_filter_trace,
    )
    from src.application.candidate_snapshot_manifest import (
        load_candidate_snapshot_bundle,
    )
    from src.application.strategy_scan_status import (
        STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
        load_strategy_scan_status_index_v2,
    )

    observed = _run_full_symbol_capture(
        monkeypatch,
        tmp_path,
        market=market,
        symbol=symbol,
        variant=variant,
        opening_mode=opening_mode,
        scenario=scenario,
    )
    assert observed["context_builds"] == 1
    assert observed["required_data"] == [symbol]
    assert observed["opening_scans"] == (
        [] if scenario == "disabled" else [opening_mode]
    )
    assert observed["combo_scans"] == (
        [] if scenario == "disabled" else [variant]
    )

    account_dir = tmp_path / "output_runs" / RUN_ID / "accounts" / "lx"
    assert (account_dir / "symbols_summary.csv").is_file()
    forbidden = sorted(
        path.relative_to(account_dir).as_posix()
        for path in account_dir.rglob("*.csv")
        if any(
            fragment in path.name.lower()
            for fragment in RETIRED_CANDIDATE_CSV_FRAGMENTS
        )
    )
    assert forbidden == []
    assert not (account_dir / "strategy_scan_status_index.v1.json").exists()

    bundle = load_candidate_snapshot_bundle(
        base=tmp_path,
        run_id=RUN_ID,
        account="lx",
    )
    expected_owners = [] if scenario == "disabled" else sorted(["opening", variant])
    assert sorted(bundle["owners"]) == expected_owners

    status_index = load_strategy_scan_status_index_v2(
        account_dir / STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
        expected_run_id=RUN_ID,
        expected_account="lx",
        expected_account_config_sha256=ACCOUNT_CONFIG_SHA256,
    )
    assert {item["market"] for item in status_index["items"]} == (
        set() if scenario == "disabled" else {market}
    )
    if scenario == "success_empty":
        assert {
            (item.get("source_outcome"), item.get("reason_code"))
            for item in status_index["items"]
        } == {("success_empty", "no_contract_rows")}

    trace_path = account_dir / "candidate_filter_trace.jsonl"
    if variant == "sp_lc" and scenario in {
        "enabled",
        "empty",
        "success_empty",
    }:
        trace_rows = read_candidate_filter_trace(trace_path)
        assert trace_rows
        assert {row["schema_version"] for row in trace_rows} == {
            TRACE_SCHEMA_VERSION
        }
        assert {row["evidence_path"] for row in trace_rows} == {
            "state/combo_yield_candidate_snapshot.json"
        }
    else:
        assert not trace_path.exists()


def test_default_pipeline_routes_opening_and_legacy_sp_lc_capture_separately(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.application.cc_lp_candidate_snapshot import (
        CC_LP_CANDIDATE_SNAPSHOT_FILE,
    )
    from src.application.combo_yield_candidate_snapshot import (
        load_combo_yield_candidate_snapshot,
    )
    from src.application.opening_candidate_snapshot import (
        load_opening_candidate_snapshot,
    )

    symbol = "0700.HK"
    _run_default_capture(
        monkeypatch,
        tmp_path,
        symbols=[_symbol_config(symbol)],
        statuses=[
            _status(symbol, "put", "completed"),
            _status(symbol, "combo_yield", "completed", variant="sp_lc"),
        ],
        pairs=[
            {
                "candidate_pair_id": "combo_yield:0700.HK:P:C",
                "symbol": symbol,
            }
        ],
    )

    opening = load_opening_candidate_snapshot(
        base=tmp_path,
        run_id=RUN_ID,
        account="lx",
    )
    assert opening["strategy_modes"] == ["put"]
    assert {
        (row["symbol"], row["strategy_mode"])
        for row in opening["scope_results"]
        if row["scope"] == "strategy"
    } == {(symbol, "put")}

    combo = load_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id=RUN_ID,
        account="lx",
    )
    assert combo["opening_status"] == "candidates_found"
    assert [row["candidate_pair_id"] for row in combo["ranked_pairs"]] == [
        "combo_yield:0700.HK:P:C"
    ]
    assert not (
        tmp_path
        / "output_runs"
        / RUN_ID
        / "accounts"
        / "lx"
        / "state"
        / CC_LP_CANDIDATE_SNAPSHOT_FILE
    ).exists()


def test_default_pipeline_preserves_cc_lp_not_applicable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.application.cc_lp_candidate_snapshot import (
        load_cc_lp_candidate_snapshot,
    )
    from src.application.combo_yield_candidate_snapshot import (
        COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
    )

    symbol = "0700.HK"
    _run_default_capture(
        monkeypatch,
        tmp_path,
        symbols=[_symbol_config(symbol, variant="cc_lp")],
        statuses=[
            _status(symbol, "put", "completed"),
            _status(
                symbol,
                "combo_yield",
                "not_applicable",
                variant="cc_lp",
                reason="no_covered_stock",
            ),
        ],
    )

    snapshot = load_cc_lp_candidate_snapshot(
        base=tmp_path,
        run_id=RUN_ID,
        account="lx",
    )
    assert snapshot["opening_status"] == "not_applicable"
    assert snapshot["ranked_pairs"] == []
    assert not (
        tmp_path
        / "output_runs"
        / RUN_ID
        / "accounts"
        / "lx"
        / "state"
        / COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE
    ).exists()


@pytest.mark.parametrize(
    ("combo_states", "expected_status"),
    [
        (["completed", "completed"], "no_candidate"),
        (["failed", "failed"], "data_unavailable"),
        (["completed", "failed"], "partial_data"),
        (["not_applicable", "not_applicable"], "not_applicable"),
        (["completed", None], "partial_data"),
    ],
)
def test_default_pipeline_aggregates_sp_lc_capture_statuses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    combo_states: list[str | None],
    expected_status: str,
) -> None:
    from src.application.combo_yield_candidate_snapshot import (
        load_combo_yield_candidate_snapshot,
    )

    symbols = ["0700.HK", "9992.HK"]
    statuses = [_status(symbol, "put", "completed") for symbol in symbols]
    statuses.extend(
        _status(
            symbol,
            "combo_yield",
            state,
            variant="sp_lc",
            reason=("no_covered_stock" if state == "not_applicable" else None),
        )
        for symbol, state in zip(symbols, combo_states, strict=True)
        if state is not None
    )

    _run_default_capture(
        monkeypatch,
        tmp_path,
        symbols=[_symbol_config(symbol) for symbol in symbols],
        statuses=statuses,
    )

    snapshot = load_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id=RUN_ID,
        account="lx",
    )
    assert snapshot["opening_status"] == expected_status


def test_default_pipeline_preserves_completed_partial_combo_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.application.combo_yield_candidate_snapshot import (
        load_combo_yield_candidate_snapshot,
    )

    symbols = ["0700.HK", "9992.HK"]
    _run_default_capture(
        monkeypatch,
        tmp_path,
        symbols=[_symbol_config(symbol) for symbol in symbols],
        statuses=[
            *[_status(symbol, "put", "completed") for symbol in symbols],
            _status(
                "0700.HK",
                "combo_yield",
                "completed",
                variant="sp_lc",
                reason="partial_data",
            ),
            _status(
                "9992.HK",
                "combo_yield",
                "completed",
                variant="sp_lc",
            ),
        ],
        pairs=[
            {
                "candidate_pair_id": "combo_yield:9992.HK:P:C",
                "symbol": "9992.HK",
            }
        ],
    )

    snapshot = load_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id=RUN_ID,
        account="lx",
    )
    assert snapshot["opening_status"] == "partial_data"
    assert [item["candidate_pair_id"] for item in snapshot["ranked_pairs"]] == [
        "combo_yield:9992.HK:P:C"
    ]


@pytest.mark.parametrize(
    ("statuses", "pairs", "error"),
    [
        (
            [_status("0700.HK", "combo", "completed")],
            [],
            "unexpected candidate capture mode",
        ),
        (
            [
                _status("0700.HK", "combo_yield", "completed", variant="sp_lc"),
                _status("0700.HK", "combo_yield", "completed", variant="sp_lc"),
            ],
            [],
            "duplicate combo yield scan scope",
        ),
        (
            [_status("9992.HK", "combo_yield", "completed", variant="sp_lc")],
            [],
            "unexpected combo yield scan scope",
        ),
        (
            [_status("0700.HK", "combo_yield", "completed", variant="other")],
            [],
            "invalid combo yield scan variant",
        ),
        (
            [_status("0700.HK", "combo_yield", "completed", variant="sp_lc")],
            [
                {
                    "candidate_pair_id": "combo_yield:0700.HK:P:C",
                    "symbol": "0700.HK",
                    "variant": "other",
                }
            ],
            "invalid combo yield evidence variant",
        ),
    ],
)
def test_default_pipeline_rejects_invalid_capture_contracts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    statuses: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    error: str,
) -> None:
    all_statuses = [_status("0700.HK", "put", "completed"), *statuses]
    with pytest.raises(ValueError, match=error):
        _run_default_capture(
            monkeypatch,
            tmp_path,
            symbols=[_symbol_config("0700.HK")],
            statuses=all_statuses,
            pairs=pairs,
        )

    assert not (
        tmp_path
        / "output_runs"
        / RUN_ID
        / "accounts"
        / "lx"
        / "state"
        / "opening_candidate_snapshot.json"
    ).exists()


def test_default_pipeline_rejects_cross_owner_quote_binding_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    symbol = "0700.HK"
    with pytest.raises(ValueError, match="quote bindings conflict"):
        _run_default_capture(
            monkeypatch,
            tmp_path,
            symbols=[_symbol_config(symbol)],
            statuses=[
                _status(symbol, "put", "completed", quote="quote-opening"),
                _status(
                    symbol,
                    "combo_yield",
                    "completed",
                    variant="sp_lc",
                    quote="quote-combo",
                ),
            ],
        )

    assert not (
        tmp_path
        / "output_runs"
        / RUN_ID
        / "accounts"
        / "lx"
        / "state"
        / "candidate_snapshot_manifest.v1.json"
    ).exists()


def test_default_pipeline_rejects_completed_combo_without_typed_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    symbol = "0700.HK"
    with pytest.raises(ValueError, match="completed combo yield evidence is missing"):
        _run_default_capture(
            monkeypatch,
            tmp_path,
            symbols=[_symbol_config(symbol)],
            statuses=[
                _status(symbol, "put", "completed"),
                _status(
                    symbol,
                    "combo_yield",
                    "completed",
                    variant="sp_lc",
                ),
            ],
            emit_combo_evidence=False,
        )

    assert not (
        tmp_path
        / "output_runs"
        / RUN_ID
        / "accounts"
        / "lx"
        / "state"
        / "candidate_snapshot_manifest.v1.json"
    ).exists()


def test_default_pipeline_allows_failed_combo_without_typed_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.application.combo_yield_candidate_snapshot import (
        load_combo_yield_candidate_snapshot,
    )

    symbol = "0700.HK"
    _run_default_capture(
        monkeypatch,
        tmp_path,
        symbols=[_symbol_config(symbol)],
        statuses=[
            _status(symbol, "put", "completed"),
            _status(symbol, "combo_yield", "failed", variant="sp_lc"),
        ],
        emit_combo_evidence=False,
    )

    snapshot = load_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id=RUN_ID,
        account="lx",
    )
    assert snapshot["opening_status"] == "data_unavailable"


def test_default_pipeline_rejects_completed_scope_without_status_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    symbol = "0700.HK"
    with pytest.raises(
        ValueError,
        match="completed candidate capture status is missing",
    ):
        _run_default_capture(
            monkeypatch,
            tmp_path,
            symbols=[_symbol_config(symbol)],
            statuses=[
                _status(symbol, "put", "completed"),
                _status(symbol, "combo_yield", "failed", variant="sp_lc"),
            ],
            emit_combo_evidence=False,
            emit_status_capture=False,
        )

    assert not (
        tmp_path
        / "output_runs"
        / RUN_ID
        / "accounts"
        / "lx"
        / "state"
        / "candidate_snapshot_manifest.v1.json"
    ).exists()
