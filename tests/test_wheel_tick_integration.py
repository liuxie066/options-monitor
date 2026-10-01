from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import pandas as pd

from conftest import phase2_opening_row
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.manual_trades import persist_manual_open_event
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_objects_atomically
from src.application.prepared_option_positions_context import (
    load_prepared_option_positions_context,
    prepare_option_positions_contexts,
)
from src.application.required_data_prefetch_planning import (
    merge_wheel_requirements_into_prefetch_config,
)
from src.application.tick_run_workspace import publish_account_run_config
from src.application.wheel.config import build_wheel_policy_hash
from src.application.wheel.candidate_snapshot import load_wheel_candidate_snapshot


def _nvda_symbol() -> dict:
    """The NVDA symbol block shared by the tick-integration configs; strategy keys are added per case."""
    return {"symbol": "NVDA", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111}}


def test_wheel_disabled_without_scope_preserves_candidate_config() -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "wheel_compatibility_v1.json").read_text(encoding="utf-8")
    )
    source = fixture["synthetic_input"]
    assert canonical_sha256(source) == fixture["input_sha256"]

    merged = merge_wheel_requirements_into_prefetch_config(
        base_config=source["base_config"],
        candidate_config=source["base_config"],
        account_configs=source["account_configs"],
        wheel_read_models=source["wheel_read_models"],
    )

    assert merged == source["base_config"]
    assert all("_wheel_call" not in item for item in merged["symbols"])


def test_prepared_context_uses_generation_fence_for_active_wheel(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import prepared_option_positions_context as prepared

    data_config = tmp_path / "portfolio.runtime.json"
    data_config.write_text("{}\n", encoding="utf-8")
    config_path = tmp_path / "config.us.json"
    config_path.write_text("{}\n", encoding="utf-8")
    config = {
        "portfolio": {"account": "acct_a", "broker": "富途", "data_config": str(data_config)},
        "wheel": {
            "accounts": ["acct_a"],
            "activation_by_account": {"acct_a": {"generation": 1, "activated_at_ms": 500, "deactivated_at_ms": None}},
        },
        "symbols": [{**_nvda_symbol(), "sell_put": {"enabled": False}, "sell_call": {"enabled": False}}],
    }
    repo = SQLiteOptionPositionsRepository(tmp_path / "output_shared" / "state" / "option_positions.sqlite3")
    persist_manual_open_event(
        repo, broker="富途", account="acct_a", symbol="NVDA", option_type="put", side="short", contracts=1,
        currency="USD", strike=100, multiplier=100, expiration_ymd="2099-08-21", premium_per_share=2,
        opened_at_ms=1_000, request_id="wheel-tick-manual-open",
    )
    put_lot_id = str(repo.list_position_lots()[0]["record_id"])
    with patch("src.application.ledger.repository_assigned_stock.now_ms", return_value=500), repo._writer_connection(
        begin_immediate=True
    ) as conn:
        repo.open_wheel_activation_window(
            market="us", account="acct_a", expected_current_generation=0,
            policy_hash=build_wheel_policy_hash(config, market="us", account="acct_a"),
            request_id="activate-wheel", request_hash="a" * 64, conn=conn,
        )
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id="assignment-1", event_type="assignment", event_time_ms=2_000,
                contract_key=ContractKey.from_values(
                    broker="富途", account="acct_a", underlying_symbol="NVDA", option_type="put",
                    strike=100, expiration_ymd="2099-08-21",
                ),
                contracts=1, price=0, currency="USD", source="test", multiplier=100,
                target_lot_id=put_lot_id,
                raw_payload={
                    # §9.2 step 3: closing the assigned short put is a buy.
                    "side": "buy",
                    "target_lot_id": put_lot_id,
                    "stock_settlement": {
                        "side": "buy", "shares": 100, "price": 100, "fees": 0, "currency": "USD",
                        "fee_provenance": {"basis": "actual", "source": "test"},
                    },
                },
            )
        ],
    )
    run_id = "wheel-tick-integration"
    authority = publish_account_run_config(base=tmp_path, run_id=run_id, account="acct_a", config=config)
    monkeypatch.setattr(
        prepared,
        "get_exchange_rates_or_fetch_latest",
        lambda **_kwargs: {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "test",
            "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
        },
    )

    batch = prepare_option_positions_contexts(
        base=tmp_path, run_id=run_id, config_path=config_path, account_configs={"acct_a": config},
        account_config_authorities={"acct_a": authority}, run_state_dir=tmp_path / "output_runs" / run_id / "state",
    )

    assert batch.ledger_read_count == 2
    wheel_model = batch.wheel_read_models_by_account["acct_a"]
    now = datetime.now(timezone.utc) - timedelta(seconds=2)
    row = phase2_opening_row({
        "symbol": "NVDA", "option_type": "call",
        "expiration": (now + timedelta(days=35)).date().isoformat(), "dte": 35,
        "contract_symbol": "NVDA-CALL-110", "multiplier": 100, "currency": "USD",
        "strike": 110, "spot": 100, "bid": 2.0, "ask": 2.2, "last_price": 2.1,
        "mid": 2.1, "open_interest": 500, "volume": 50,
        "implied_volatility": 0.30, "term_matched_rv": 0.20, "delta": 0.35,
        "quote_observed_at_utc": now.isoformat(), "spot_observed_at_utc": now.isoformat(),
        "snapshot_received_at_utc": now.isoformat(),
    })
    assert wheel_model["market"] == "US"
    assert wheel_model["batches"][0]["lifecycle_status"] == "active"
    assert wheel_model["monitoring_gate"] == "enabled"
    context = load_prepared_option_positions_context(
        manifest_path=Path(batch.manifests["acct_a"]["manifest_path"]), expected_base=tmp_path,
        expected_run_id=run_id, expected_account="acct_a",
        expected_account_config_sha256=authority.account_config_sha256,
        expected_manifest_sha256=batch.manifests["acct_a"]["manifest_sha256"], expected_runtime_config=config,
    )
    assert context["wheel_read_model"]["batches"][0]["projection_hash"] == (
        wheel_model["batches"][0]["projection_hash"]
    )

    merged = merge_wheel_requirements_into_prefetch_config(
        base_config=config, candidate_config={**config, "symbols": []}, account_configs={"acct_a": config},
        wheel_read_models=batch.wheel_read_models_by_account,
    )
    assert merged["symbols"][0]["_wheel_call"] == {
        "enabled": True, "min_dte": 30, "max_dte": 45, "requires_realized_volatility": True,
    }

    removed = deepcopy(config)
    removed["wheel"]["accounts"] = []
    removed_run_id = "wheel-tick-account-removed"
    removed_authority = publish_account_run_config(
        base=tmp_path, run_id=removed_run_id, account="acct_a", config=removed,
    )
    removed_batch = prepare_option_positions_contexts(
        base=tmp_path, run_id=removed_run_id, config_path=config_path,
        account_configs={"acct_a": removed},
        account_config_authorities={"acct_a": removed_authority},
        run_state_dir=tmp_path / "output_runs" / removed_run_id / "state",
    )
    removed_model = removed_batch.wheel_read_models_by_account["acct_a"]
    assert removed_model["batches"][0]["lifecycle_status"] == "active"
    assert removed_model["monitoring_gate"] == "disabled"
    assert removed_model["monitoring_gate_reason"] == "account_not_configured"

    from src.application import pipeline_context, pipeline_symbol, pipeline_watchlist as pipeline

    def capture(prepared_batch, runtime_config, run, run_authority):
        report_dir = tmp_path / "output_runs" / run / "accounts" / "acct_a"
        report_dir.mkdir(parents=True, exist_ok=True)
        required_manifest = report_dir / "required_data_manifest.json"
        portfolio_manifest = report_dir / "prepared_portfolio_context.json"
        required_manifest.write_text("{}\n", encoding="utf-8")
        portfolio_manifest.write_text("{}\n", encoding="utf-8")
        prepared_context = load_prepared_option_positions_context(
            manifest_path=Path(prepared_batch.manifests["acct_a"]["manifest_path"]),
            expected_base=tmp_path, expected_run_id=run, expected_account="acct_a",
            expected_account_config_sha256=run_authority.account_config_sha256,
            expected_manifest_sha256=prepared_batch.manifests["acct_a"]["manifest_sha256"],
            expected_runtime_config=runtime_config,
        )

        def build_context(**kwargs):
            assert kwargs["prepared_option_positions_context_manifest"] == Path(
                prepared_batch.manifests["acct_a"]["manifest_path"]
            )
            return (
                {
                    "capacity_authority": {"status": "available", "market": "US"},
                    "stocks_by_symbol": {"NVDA": {"shares": 100, "can_sell_qty": 100}},
                },
                {
                    **prepared_context,
                    "exchange_rates": {"rates": {"USDCNY": 7.2, "HKDCNY": 0.92}},
                    "locked_shares_status": "available",
                    "locked_shares_by_symbol": {},
                    "locked_shares_unavailable_by_symbol": {},
                },
                1 / 7.2,
                0.92,
            )

        monkeypatch.setattr(pipeline_context, "build_pipeline_context", build_context)
        monkeypatch.setattr(pipeline_symbol, "process_symbol", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(
            pipeline, "resolve_frozen_required_data_csv_bytes_batch",
            lambda **_kwargs: {"frames": {"NVDA": pd.DataFrame([row])}},
        )
        pipeline.run_watchlist_pipeline_default(
            py="python3", base=tmp_path, cfg=runtime_config,
            report_dir=report_dir, state_dir=report_dir / "state",
            shared_state_dir=tmp_path / "output_shared" / "state",
            required_data_dir=report_dir, is_scheduled=True, top_n=3,
            symbol_timeout_sec=10, portfolio_timeout_sec=10,
            want_scan=True, no_context=False, symbols_arg=None,
            log=lambda _message: None, want_fn=lambda name: name == "scan",
            source_account_run_id=run, required_data_snapshot_manifest=required_manifest,
            prepared_portfolio_context_manifest=portfolio_manifest,
            prepared_option_positions_context_manifest=Path(
                prepared_batch.manifests["acct_a"]["manifest_path"]
            ),
            account_config_sha256=run_authority.account_config_sha256,
        )
        return load_wheel_candidate_snapshot(base=tmp_path, run_id=run, account="acct_a")

    open_snapshot = capture(batch, config, run_id, authority)
    assert open_snapshot["opening_status"] == "candidates_found"
    assert open_snapshot["scope_results"][0]["candidate_count"] == 1
    assert open_snapshot["batches"][0]["granted_contracts"] == 1
    assert open_snapshot["batches"][0]["final_candidate"] is not None

    removed_snapshot = capture(removed_batch, removed, removed_run_id, removed_authority)
    assert removed_snapshot["opening_status"] == "not_applicable"
    assert removed_snapshot["batches"][0]["raw_candidates"] == []
    assert removed_snapshot["batches"][0]["final_candidate"] is None

    with patch("src.application.ledger.repository_assigned_stock.now_ms", return_value=3_000), repo._writer_connection(
        begin_immediate=True
    ) as conn:
        repo.close_wheel_activation_window(
            market="us", account="acct_a", expected_current_generation=1,
            policy_hash=build_wheel_policy_hash(config, market="us", account="acct_a"),
            request_id="disable-wheel", request_hash="c" * 64, conn=conn,
        )
    closed = deepcopy(config)
    closed["wheel"]["activation_by_account"]["acct_a"]["deactivated_at_ms"] = 3_000
    closed_run_id = "wheel-tick-closed"
    closed_authority = publish_account_run_config(
        base=tmp_path, run_id=closed_run_id, account="acct_a", config=closed,
    )
    closed_batch = prepare_option_positions_contexts(
        base=tmp_path, run_id=closed_run_id, config_path=config_path,
        account_configs={"acct_a": closed},
        account_config_authorities={"acct_a": closed_authority},
        run_state_dir=tmp_path / "output_runs" / closed_run_id / "state",
    )
    closed_snapshot = capture(closed_batch, closed, closed_run_id, closed_authority)
    assert closed_snapshot["opening_status"] == "not_applicable"
    assert closed_snapshot["batches"][0]["raw_candidates"] == []
    assert closed_snapshot["batches"][0]["final_candidate"] is None
    assert closed_snapshot["batches"][0]["granted_contracts"] == 0


@pytest.mark.parametrize(
    ("sell_put_enabled", "sell_call_enabled", "combo_enabled"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
        (True, True, False),
        (True, False, True),
        (False, True, True),
        (True, True, True),
    ],
)
def test_wheel_required_data_preserves_existing_strategy_config_matrix(
    sell_put_enabled: bool,
    sell_call_enabled: bool,
    combo_enabled: bool,
) -> None:
    config = {
        "wheel": {"accounts": ["acct_a"]},
        "symbols": [{
            **_nvda_symbol(), "sell_put": {"enabled": sell_put_enabled}, "sell_call": {"enabled": sell_call_enabled},
            "combo_yield": {"enabled": combo_enabled, "variant": "sp_lc"},
        }],
    }
    original = deepcopy(config)
    merged = merge_wheel_requirements_into_prefetch_config(
        base_config=config, candidate_config=config, account_configs={"acct_a": config},
        wheel_read_models={
            "acct_a": {"batches": [{"symbol": "NVDA", "lifecycle_status": "active", "integrity_status": "trusted",
                                    "phase": "ready", "monitoring_gate": "enabled",
                                    "shares_remaining": 100, "multiplier": 100}]}
        },
    )

    assert config == original
    for key in ("sell_put", "sell_call", "combo_yield"):
        assert merged["symbols"][0][key] == original["symbols"][0][key]
    assert merged["symbols"][0]["_wheel_call"]["enabled"] is True
