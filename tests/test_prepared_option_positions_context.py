from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.application.ledger.repository import (
    SQLiteOptionPositionsRepository,
)
from src.application.prepared_option_positions_context import (
    PreparedOptionPositionsContextError,
    load_prepared_option_positions_context,
    prepare_option_positions_contexts,
)
from src.application.tick_run_workspace import publish_account_run_config


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
    fx_calls: list[Path] = []

    def _rates(*, cache_path, **_kwargs):
        fx_calls.append(Path(cache_path))
        return {
            "timestamp": "2026-08-05T01:00:00+00:00",
            "source": "test",
            "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
        }

    monkeypatch.setattr(
        mod,
        "get_exchange_rates_or_fetch_latest",
        _rates,
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
