from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.option_position_lots import OpenPositionCommand
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


def test_wheel_disabled_without_scope_preserves_candidate_config() -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "wheel_compatibility_v1.json").read_text(
            encoding="utf-8"
        )
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
        "portfolio": {
            "account": "acct_a",
            "broker": "富途",
            "data_config": str(data_config),
        },
        "wheel": {
            "accounts": ["acct_a"],
            "activation_by_account": {
                "acct_a": {
                    "generation": 1,
                    "activated_at_ms": 500,
                    "deactivated_at_ms": None,
                }
            },
        },
        "symbols": [
            {
                "symbol": "NVDA",
                "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
                "sell_put": {"enabled": False},
                "sell_call": {"enabled": False},
            }
        ],
    }
    repo = SQLiteOptionPositionsRepository(
        tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    )
    persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="acct_a",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100,
            multiplier=100,
            expiration_ymd="2099-08-21",
            premium_per_share=2,
            opened_at_ms=1_000,
            request_id="wheel-tick-manual-open",
        ),
    )
    put_lot_id = str(repo.list_position_lots()[0]["record_id"])
    with patch(
        "src.application.ledger.repository_assigned_stock.now_ms",
        return_value=500,
    ), repo._writer_connection(begin_immediate=True) as conn:
        repo.open_wheel_activation_window(
            market="us",
            account="acct_a",
            expected_current_generation=0,
            policy_hash=build_wheel_policy_hash(
                config,
                market="us",
                account="acct_a",
            ),
            request_id="activate-wheel",
            request_hash="a" * 64,
            conn=conn,
        )
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id="assignment-1",
                event_type="assignment",
                event_time_ms=2_000,
                contract_key=ContractKey.from_values(
                    broker="富途",
                    account="acct_a",
                    underlying_symbol="NVDA",
                    option_type="put",
                    position_side="short",
                    strike=100,
                    expiration_ymd="2099-08-21",
                ),
                contracts=1,
                price=0,
                currency="USD",
                source="test",
                multiplier=100,
                target_lot_id=put_lot_id,
                raw_payload={
                    "target_lot_id": put_lot_id,
                    "stock_settlement": {
                        "side": "buy",
                        "shares": 100,
                        "price": 100,
                        "fees": 0,
                        "currency": "USD",
                        "fee_provenance": {"basis": "actual", "source": "test"},
                    },
                },
            )
        ],
    )
    run_id = "wheel-tick-integration"
    authority = publish_account_run_config(
        base=tmp_path,
        run_id=run_id,
        account="acct_a",
        config=config,
    )
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
        base=tmp_path,
        run_id=run_id,
        config_path=config_path,
        account_configs={"acct_a": config},
        account_config_authorities={"acct_a": authority},
        run_state_dir=tmp_path / "output_runs" / run_id / "state",
    )

    assert batch.ledger_read_count == 2
    wheel_model = batch.wheel_read_models_by_account["acct_a"]
    assert wheel_model["market"] == "US"
    assert wheel_model["batches"][0]["lifecycle_status"] == "active"
    assert wheel_model["monitoring_gate"] == "enabled"
    context = load_prepared_option_positions_context(
        manifest_path=Path(batch.manifests["acct_a"]["manifest_path"]),
        expected_base=tmp_path,
        expected_run_id=run_id,
        expected_account="acct_a",
        expected_account_config_sha256=authority.account_config_sha256,
        expected_manifest_sha256=batch.manifests["acct_a"]["manifest_sha256"],
        expected_runtime_config=config,
    )
    assert context["wheel_read_model"]["batches"][0]["projection_hash"] == (
        wheel_model["batches"][0]["projection_hash"]
    )

    merged = merge_wheel_requirements_into_prefetch_config(
        base_config=config,
        candidate_config={**config, "symbols": []},
        account_configs={"acct_a": config},
        wheel_read_models=batch.wheel_read_models_by_account,
    )
    assert merged["symbols"][0]["_wheel_call"] == {
        "enabled": True,
        "min_dte": 30,
        "max_dte": 45,
        "requires_realized_volatility": True,
    }


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
        "symbols": [
            {
                "symbol": "NVDA",
                "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
                "sell_put": {"enabled": sell_put_enabled},
                "sell_call": {"enabled": sell_call_enabled},
                "combo_yield": {
                    "enabled": combo_enabled,
                    "variant": "sp_lc",
                },
            }
        ],
    }
    original = deepcopy(config)
    merged = merge_wheel_requirements_into_prefetch_config(
        base_config=config,
        candidate_config=config,
        account_configs={"acct_a": config},
        wheel_read_models={
            "acct_a": {
                "batches": [
                    {
                        "symbol": "NVDA",
                        "lifecycle_status": "active",
                        "integrity_status": "trusted",
                        "phase": "ready",
                        "monitoring_gate": "enabled",
                    }
                ]
            }
        },
    )

    assert config == original
    for key in ("sell_put", "sell_call", "combo_yield"):
        assert merged["symbols"][0][key] == original["symbols"][0][key]
    assert merged["symbols"][0]["_wheel_call"]["enabled"] is True
