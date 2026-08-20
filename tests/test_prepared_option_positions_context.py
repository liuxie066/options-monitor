from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from domain.domain.option_position_lots import OpenPositionCommand
from src.application.ledger.manual_trades import persist_manual_open_event
from src.application.ledger.repository import (
    SQLiteOptionPositionsRepository,
)
from src.application.prepared_option_positions_context import (
    PreparedOptionPositionsContextError,
    cny_per_currency_rates_from_option_context,
    load_prepared_option_positions_context,
    load_prepared_option_positions_context_receipt,
    prepare_option_positions_contexts,
)
from src.application.tick_run_workspace import publish_account_run_config


NOW = datetime(2026, 8, 10, 3, 0, tzinfo=timezone.utc)


def test_cny_per_currency_rates_requires_ready_prepared_fx_authority() -> None:
    ready = {
        "prepared_authority": {"fx_status": "ready"},
        "exchange_rates": {
            "rates": {"USDCNY": "7.2", "HKDCNY": 0.92}
        },
    }
    unavailable = {
        **ready,
        "prepared_authority": {"fx_status": "unavailable"},
    }

    assert cny_per_currency_rates_from_option_context(ready) == {
        "CNY": 1.0,
        "USD": 7.2,
        "HKD": 0.92,
    }
    assert cny_per_currency_rates_from_option_context(unavailable) == {
        "CNY": 1.0
    }


def _authorities(
    tmp_path: Path,
    *,
    run_id: str,
    data_config: Path,
):
    configs = {
        account: {
            "portfolio": {
                "account": account,
                "broker": "富途",
                "data_config": str(data_config),
            },
            "runtime": {},
            "symbols": [],
        }
        for account in ("lx", "sy")
    }
    authorities = {
        account: publish_account_run_config(
            base=tmp_path,
            run_id=run_id,
            account=account,
            config=config,
        )
        for account, config in configs.items()
    }
    retained = {
        account: json.loads(authority.canonical_bytes.decode("utf-8"))
        for account, authority in authorities.items()
    }
    return retained, authorities


def _open_position(
    repo: SQLiteOptionPositionsRepository,
    *,
    account: str,
    symbol: str,
    option_type: str,
    side: str,
    contracts: int,
    strike: float,
    expiry: str,
    opened_at_ms: int,
) -> None:
    persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account=account,
            symbol=symbol,
            option_type=option_type,
            side=side,
            contracts=contracts,
            currency="USD",
            strike=strike,
            multiplier=100,
            expiration_ymd=expiry,
            premium_per_share=2.0,
            opened_at_ms=opened_at_ms,
        ),
    )


def test_repository_reads_multi_account_generation_once(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(
        tmp_path / "option_positions.sqlite3"
    )
    counts = {"events": 0, "lots": 0}
    original_events = repo.list_trade_events
    original_lots = repo.list_position_lots

    def _events(*args, **kwargs):
        counts["events"] += 1
        return original_events(*args, **kwargs)

    def _lots(*args, **kwargs):
        counts["lots"] += 1
        return original_lots(*args, **kwargs)

    monkeypatch.setattr(repo, "list_trade_events", _events)
    monkeypatch.setattr(repo, "list_position_lots", _lots)

    rows = repo.read_decision_state_rows_many(accounts=("sy", "lx"))

    assert list(rows) == ["lx", "sy"]
    assert counts == {"events": 1, "lots": 1}
    assert rows["lx"]["trade_events"] == rows["sy"]["trade_events"]
    assert rows["lx"]["stored_position_lots"] == rows["sy"][
        "stored_position_lots"
    ]


def test_prepare_publishes_zero_position_slices_from_one_ledger_and_fx_read(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import prepared_option_positions_context as mod

    run_id = "run-coherent-options"
    data_config = tmp_path / "portfolio.runtime.json"
    data_config.write_text("{}\n", encoding="utf-8")
    config_path = tmp_path / "config.us.json"
    config_path.write_text("{}\n", encoding="utf-8")
    configs, authorities = _authorities(
        tmp_path,
        run_id=run_id,
        data_config=data_config,
    )
    fx_calls: list[list[str]] = []

    def _rates(cache_path=None, **_kwargs):
        fx_calls.append(str(cache_path))
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "tencent_quote",
            "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
        }

    monkeypatch.setattr(
        mod,
        "get_exchange_rates_or_fetch_latest",
        _rates,
    )

    def _current_read_fails(*_args, **_kwargs):
        raise RuntimeError("shadow unavailable")

    monkeypatch.setattr(
        mod,
        "read_current_decision_projection",
        _current_read_fails,
    )

    batch = prepare_option_positions_contexts(
        base=tmp_path,
        run_id=run_id,
        config_path=config_path,
        account_configs=configs,
        account_config_authorities=authorities,
        run_state_dir=tmp_path / "output_runs" / run_id / "state",
    )

    assert batch.ledger_read_count == 1
    assert batch.fx_observation_count == 1
    assert len(fx_calls) == 1
    assert fx_calls[0].endswith("rate_cache.json")
    assert batch.unavailable_by_account == {}
    assert set(batch.manifests) == {"lx", "sy"}

    loaded = {}
    for account in ("lx", "sy"):
        manifest = batch.manifests[account]
        loaded[account] = load_prepared_option_positions_context(
            manifest_path=Path(manifest["manifest_path"]),
            expected_base=tmp_path,
            expected_run_id=run_id,
            expected_account=account,
            expected_account_config_sha256=authorities[
                account
            ].account_config_sha256,
            expected_manifest_sha256=manifest["manifest_sha256"],
            expected_runtime_config=configs[account],
        )
        assert loaded[account]["filters"] == {
            "broker": "富途",
            "account": account,
        }
        assert loaded[account]["context_status"] == "available"
        assert loaded[account]["raw_selected_count"] == 0
        assert loaded[account]["open_positions_min"] == []
        assert loaded[account]["decision_snapshot_status"] == "trusted"
        assert loaded[account]["current_decision_shadow"] == loaded[account][
            "decision_state_snapshot"
        ]["current_decision_shadow"]
        assert loaded[account]["current_decision_shadow"]["status"] == (
            "not_available"
        )
        assert loaded[account]["current_decision_shadow"]["reason"] == (
            "current_projection_read_failed:RuntimeError"
        )
        receipt = load_prepared_option_positions_context_receipt(
            manifest_path=Path(manifest["manifest_path"]),
            expected_base=tmp_path,
            expected_run_id=run_id,
            expected_account=account,
            expected_account_config_sha256=authorities[account].account_config_sha256,
            expected_manifest_sha256=manifest["manifest_sha256"],
            expected_runtime_config=configs[account],
        )
        assert receipt["payload"] == loaded[account]
        assert (
            receipt["manifest"]["application_received_at_utc"]
            == (loaded[account]["prepared_authority"]["application_received_at_utc"])
        )

    assert loaded["lx"]["as_of_utc"] == loaded["sy"]["as_of_utc"]
    assert (
        loaded["lx"]["prepared_authority"][
            "ledger_generation_sha256"
        ]
        == loaded["sy"]["prepared_authority"][
            "ledger_generation_sha256"
        ]
    )
    assert (
        loaded["lx"]["prepared_authority"]["fx_observation_sha256"]
        == loaded["sy"]["prepared_authority"][
            "fx_observation_sha256"
        ]
    )

    sy_manifest_path = Path(batch.manifests["sy"]["manifest_path"])
    sy_manifest = json.loads(sy_manifest_path.read_text(encoding="utf-8"))
    sy_manifest["application_received_at_utc"] = "2026-08-10T03:00:01+00:00"
    sy_manifest_bytes = (
        json.dumps(
            sy_manifest,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode()
    sy_manifest_path.write_bytes(sy_manifest_bytes)
    sy_manifest_sha256 = hashlib.sha256(sy_manifest_bytes).hexdigest()
    assert (
        load_prepared_option_positions_context(
            manifest_path=sy_manifest_path,
            expected_base=tmp_path,
            expected_run_id=run_id,
            expected_account="sy",
            expected_account_config_sha256=authorities["sy"].account_config_sha256,
            expected_manifest_sha256=sy_manifest_sha256,
            expected_runtime_config=configs["sy"],
        )
        == loaded["sy"]
    )
    with pytest.raises(
        PreparedOptionPositionsContextError,
        match="application_received_at_utc",
    ):
        load_prepared_option_positions_context_receipt(
            manifest_path=sy_manifest_path,
            expected_base=tmp_path,
            expected_run_id=run_id,
            expected_account="sy",
            expected_account_config_sha256=authorities["sy"].account_config_sha256,
            expected_manifest_sha256=sy_manifest_sha256,
            expected_runtime_config=configs["sy"],
        )

    lx_manifest = batch.manifests["lx"]
    payload_path = (
        Path(lx_manifest["manifest_path"]).parent
        / lx_manifest["payload_relpath"]
    )
    payload_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(
        PreparedOptionPositionsContextError,
        match="payload hash mismatch",
    ):
        load_prepared_option_positions_context(
            manifest_path=Path(lx_manifest["manifest_path"]),
            expected_base=tmp_path,
            expected_run_id=run_id,
            expected_account="lx",
            expected_account_config_sha256=authorities[
                "lx"
            ].account_config_sha256,
            expected_manifest_sha256=lx_manifest["manifest_sha256"],
            expected_runtime_config=configs["lx"],
        )


def test_prepare_fails_account_closed_when_wheel_projection_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import prepared_option_positions_context as mod

    run_id = "run-wheel-projection-failure"
    data_config = tmp_path / "portfolio.runtime.json"
    data_config.write_text("{}\n", encoding="utf-8")
    config_path = tmp_path / "config.us.json"
    config_path.write_text("{}\n", encoding="utf-8")
    configs, authorities = _authorities(
        tmp_path,
        run_id=run_id,
        data_config=data_config,
    )
    monkeypatch.setattr(
        mod,
        "get_exchange_rates_or_fetch_latest",
        lambda **_kwargs: {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "test",
            "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
        },
    )
    monkeypatch.setattr(
        mod,
        "build_wheel_read_model_from_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("broken")),
    )

    batch = prepare_option_positions_contexts(
        base=tmp_path,
        run_id=run_id,
        config_path=config_path,
        account_configs=configs,
        account_config_authorities=authorities,
        run_state_dir=tmp_path / "output_runs" / run_id / "state",
    )

    assert set(batch.manifests) == {"lx", "sy"}
    assert batch.wheel_read_models_by_account == {}
    assert batch.unavailable_by_account == {
        "lx": "wheel_projection_failed:RuntimeError",
        "sy": "wheel_projection_failed:RuntimeError",
    }

def test_one_ledger_freezes_account_isolated_option_contexts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import prepared_option_positions_context as mod

    run_id = "run-account-isolated-options"
    data_config = tmp_path / "portfolio.runtime.json"
    data_config.write_text("{}\n", encoding="utf-8")
    config_path = tmp_path / "config.us.json"
    config_path.write_text("{}\n", encoding="utf-8")
    configs, authorities = _authorities(
        tmp_path,
        run_id=run_id,
        data_config=data_config,
    )
    ledger_path = (
        tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    )
    ledger_path.parent.mkdir(parents=True)
    repo = SQLiteOptionPositionsRepository(ledger_path)
    _open_position(
        repo,
        account="lx",
        symbol="NVDA",
        option_type="put",
        side="short",
        contracts=2,
        strike=95,
        expiry="2099-09-18",
        opened_at_ms=1_000,
    )
    _open_position(
        repo,
        account="sy",
        symbol="AAPL",
        option_type="call",
        side="short",
        contracts=3,
        strike=210,
        expiry="2099-09-23",
        opened_at_ms=2_000,
    )

    monkeypatch.setattr(
        mod,
        "get_exchange_rates_or_fetch_latest",
        lambda cache_path=None, **_kwargs: {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "tencent_quote",
            "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
        },
    )

    batch = prepare_option_positions_contexts(
        base=tmp_path,
        run_id=run_id,
        config_path=config_path,
        account_configs=configs,
        account_config_authorities=authorities,
        run_state_dir=tmp_path / "output_runs" / run_id / "state",
    )

    assert batch.ledger_read_count == 1
    assert batch.fx_observation_count == 1
    assert batch.unavailable_by_account == {}
    loaded = {
        account: load_prepared_option_positions_context(
            manifest_path=Path(batch.manifests[account]["manifest_path"]),
            expected_base=tmp_path,
            expected_run_id=run_id,
            expected_account=account,
            expected_account_config_sha256=authorities[
                account
            ].account_config_sha256,
            expected_manifest_sha256=batch.manifests[account][
                "manifest_sha256"
            ],
            expected_runtime_config=configs[account],
        )
        for account in ("lx", "sy")
    }

    assert {
        row["account"] for row in loaded["lx"]["open_positions_min"]
    } == {"lx"}
    assert {
        row["account"] for row in loaded["sy"]["open_positions_min"]
    } == {"sy"}
    assert sum(
        row["contracts_open"]
        for row in loaded["lx"]["open_positions_min"]
    ) == 2
    assert sum(
        row["contracts_open"]
        for row in loaded["sy"]["open_positions_min"]
    ) == 3
    assert loaded["lx"]["prepared_authority"][
        "ledger_generation_sha256"
    ] == loaded["sy"]["prepared_authority"][
        "ledger_generation_sha256"
    ]


    lx_manifest = batch.manifests["lx"]
    with pytest.raises(
        PreparedOptionPositionsContextError,
        match="account config hash mismatch",
    ):
        load_prepared_option_positions_context(
            manifest_path=Path(lx_manifest["manifest_path"]),
            expected_base=tmp_path,
            expected_run_id=run_id,
            expected_account="lx",
            expected_account_config_sha256="f" * 64,
            expected_manifest_sha256=lx_manifest["manifest_sha256"],
            expected_runtime_config=configs["lx"],
        )

    sy_after_lx_rejection = load_prepared_option_positions_context(
        manifest_path=Path(batch.manifests["sy"]["manifest_path"]),
        expected_base=tmp_path,
        expected_run_id=run_id,
        expected_account="sy",
        expected_account_config_sha256=authorities[
            "sy"
        ].account_config_sha256,
        expected_manifest_sha256=batch.manifests["sy"]["manifest_sha256"],
        expected_runtime_config=configs["sy"],
    )
    assert sum(
        row["contracts_open"]
        for row in sy_after_lx_rejection["open_positions_min"]
    ) == 3
