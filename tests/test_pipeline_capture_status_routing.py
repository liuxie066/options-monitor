from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


RUN_ID = "20260810T020017Z-test"
ACCOUNT_CONFIG_SHA256 = "a" * 64


def _symbol_config(
    symbol: str,
    *,
    variant: str = "sp_lc",
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "broker": "HK",
        "sell_put": {"enabled": True},
        "sell_call": {"enabled": False},
        "combo_yield": {"enabled": True, "variant": variant},
    }


def _status(
    symbol: str,
    mode: str,
    status: str,
    *,
    variant: str | None = None,
    quote: str = "quote-1",
    reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbol": symbol,
        "strategy_mode": mode,
        "status": status,
        "reason": reason,
        "quote_snapshot_id": quote,
        "quote_receipt_relpath": f"quotes/{quote}/receipt.json",
    }
    if variant is not None:
        payload["variant"] = variant
    return payload


def _run_default_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    symbols: list[dict[str, Any]],
    statuses: list[dict[str, Any]],
    pairs: list[dict[str, Any]] | None = None,
) -> None:
    from src.application import pipeline_watchlist as mod

    required_manifest = tmp_path / "required_data_manifest.json"
    portfolio_manifest = tmp_path / "prepared_portfolio_context.json"
    ledger_manifest = tmp_path / "prepared_option_positions_context.json"
    (
        tmp_path / "output_runs" / RUN_ID / "accounts" / "lx"
    ).mkdir(parents=True)
    for path in (required_manifest, portfolio_manifest, ledger_manifest):
        path.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(
        mod,
        "resolve_watchlist_item_runtime_config",
        lambda *, item, **_kwargs: dict(item),
    )

    def _fake_pipeline(**kwargs: Any) -> list[dict[str, Any]]:
        for item in statuses:
            kwargs["position_advice_candidate_capture_status_sink_fn"](item)
        if pairs:
            kwargs["combo_pairs_sink_fn"](pairs)
        kwargs["opening_runtime_context_sink_fn"](
            {
                "capacity_authority": {
                    "status": "available",
                    "logical_account": "lx",
                    "futu_account_id": "10001",
                    "trd_env": "REAL",
                    "market": "HK",
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
        report_dir=tmp_path / "reports",
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
        position_advice_account_run_id=RUN_ID,
        required_data_snapshot_manifest=required_manifest,
        prepared_portfolio_context_manifest=portfolio_manifest,
        prepared_option_positions_context_manifest=ledger_manifest,
        account_config_sha256=ACCOUNT_CONFIG_SHA256,
    )


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
            "unexpected combo yield scan scopes",
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
            "invalid combo yield pair variant",
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

    opening = load_opening_candidate_snapshot(
        base=tmp_path,
        run_id=RUN_ID,
        account="lx",
    )
    assert opening["opening_status"] == "data_unavailable"
    strategy_scope = next(
        row for row in opening["scope_results"] if row["scope"] == "strategy"
    )
    assert strategy_scope["status"] == "incomplete"
    assert strategy_scope["reason_code"] == "opening_quote_binding_conflict"

    combo = load_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id=RUN_ID,
        account="lx",
    )
    assert combo["opening_status"] == "data_unavailable"
