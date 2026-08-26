from __future__ import annotations

import json
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.performance.models import (
    EvidenceEnvelope,
    OptionInstrumentKey,
    ValuationMarkFact,
)
from domain.domain.option_position_lots import OpenPositionCommand
from src.application.ledger.manual_trades import persist_manual_open_event
from src.application.ledger.event_codec import trade_event_application_payload
from src.application.ledger.repository import (
    SQLiteOptionPositionsRepository,
)
from src.application.performance.evidence_collection import (
    CurrentEvidenceCollection,
)
from src.infrastructure.performance_evidence_sqlite import (
    PerformanceEvidenceSQLiteRepository,
)
from src.application.prepared_option_positions_context import (
    PreparedOptionPositionsContextError,
    build_option_market_evidence_payload,
    cny_per_currency_rates_from_option_context,
    find_prepared_option_positions_manifest,
    load_prepared_option_positions_context,
    load_prepared_option_positions_context_receipt,
    prepare_option_positions_contexts,
)
from src.application.tick_run_workspace import publish_account_run_config


NOW = datetime(2026, 8, 10, 3, 0, tzinfo=timezone.utc)


def _canonical_bytes(payload: dict) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


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
    fx_observation = {
        "timestamp": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        "source": "tencent_quote",
        "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
    }
    fx_calls: list[list[str]] = []

    def _rates(cache_path=None, **_kwargs):
        fx_calls.append(str(cache_path))
        return fx_observation

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
        persist_fx_evidence=True,
    )

    assert batch.ledger_read_count == 2
    assert batch.fx_observation_count == 1
    assert batch.fx_evidence_status == "persisted"
    assert batch.fx_evidence_ledger_count == 1
    assert batch.fx_evidence_inserted_count == 2
    assert len(fx_calls) == 1
    assert fx_calls[0].endswith("rate_cache.json")
    assert batch.unavailable_by_account == {}
    assert set(batch.manifests) == {"lx", "sy"}
    second_run_id = "run-coherent-options-repeat"
    second_configs, second_authorities = _authorities(
        tmp_path,
        run_id=second_run_id,
        data_config=data_config,
    )
    repeated = prepare_option_positions_contexts(
        base=tmp_path,
        run_id=second_run_id,
        config_path=config_path,
        account_configs=second_configs,
        account_config_authorities=second_authorities,
        run_state_dir=tmp_path / "output_runs" / second_run_id / "state",
        persist_fx_evidence=True,
    )
    assert repeated.fx_evidence_status == "idempotent"
    assert repeated.fx_evidence_idempotent_count == 2
    evidence = PerformanceEvidenceSQLiteRepository(
        tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    ).read_all()
    assert {(item.base_currency, item.quote_currency) for item in evidence.fx_rates} == {
        ("USD", "CNY"),
        ("HKD", "CNY"),
    }
    assert {item.source for item in evidence.fx_rates} == {"realtime_snapshot"}
    assert {item.quality["provider_source"] for item in evidence.fx_rates} == {
        "tencent_quote"
    }
    assert all(item.observed_at_ms > item.effective_at_ms for item in evidence.fx_rates)

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
        assert "decision_state_snapshot" not in loaded[account]
        assert isinstance(loaded[account]["current_decision_read"], dict)
        assert loaded[account]["decision_snapshot_actionable"] is True
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
    assert payload_path.stat().st_size < 2 * 1024 * 1024
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


def test_prepare_default_path_does_not_persist_fx_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import prepared_option_positions_context as mod

    run_id = "run-smoke-no-fx-write"
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

    assert batch.fx_evidence_status == "disabled"
    evidence = PerformanceEvidenceSQLiteRepository(
        tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    )
    assert evidence.schema_state() == "not_initialized"


@pytest.mark.parametrize(
    "fx_observation",
    [
        {
            "timestamp": NOW.isoformat(),
            "source": "tencent_quote",
            "rates": {"USDCNY": 7.2},
        },
        {
            "timestamp": "2099-08-21T03:00:00+00:00",
            "source": "tencent_quote",
            "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
        },
    ],
    ids=("missing_required_pair", "future_timestamp"),
)
def test_prepare_rejects_invalid_fx_evidence_batch(
    monkeypatch,
    tmp_path: Path,
    fx_observation: dict,
) -> None:
    from src.application import prepared_option_positions_context as mod

    run_id = "run-invalid-fx-evidence"
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
        lambda **_kwargs: fx_observation,
    )

    batch = prepare_option_positions_contexts(
        base=tmp_path,
        run_id=run_id,
        config_path=config_path,
        account_configs=configs,
        account_config_authorities=authorities,
        run_state_dir=tmp_path / "output_runs" / run_id / "state",
        persist_fx_evidence=True,
    )

    assert batch.fx_evidence_status == "error"
    assert batch.fx_evidence_error_count == 1
    evidence = PerformanceEvidenceSQLiteRepository(
        tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    )
    assert evidence.schema_state() == "not_initialized"


def test_fx_evidence_concurrent_winner_converges_to_idempotent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import prepared_option_positions_context as mod

    db_path = tmp_path / "option_positions.sqlite3"
    evidence = PerformanceEvidenceSQLiteRepository(db_path)
    captured_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    class RacingEvidenceRepository:
        def __init__(self) -> None:
            self.import_calls = 0

        def read_all(self):
            return evidence.read_all()

        def import_envelope(self, envelope, *, apply, migrated_at_ms):
            self.import_calls += 1
            if self.import_calls == 1:
                winner = type(envelope)(
                    fx_rates=tuple(
                        replace(
                            fact,
                            fact_id=None,
                            observed_at_ms=fact.observed_at_ms - 1,
                        )
                        for fact in envelope.fx_rates
                    )
                )
                evidence.import_envelope(
                    winner,
                    apply=apply,
                    migrated_at_ms=migrated_at_ms,
                )
                raise ValueError("source identity conflict")
            return evidence.import_envelope(
                envelope,
                apply=apply,
                migrated_at_ms=migrated_at_ms,
            )

    racing = RacingEvidenceRepository()
    monkeypatch.setattr(
        mod,
        "open_performance_evidence_repository",
        lambda _repo: racing,
    )

    result = mod._persist_fx_evidence(
        repos_by_ledger_path={db_path: object()},
        observation={
            "timestamp": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "source": "tencent_quote",
            "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
        },
        observation_status="ready",
        migrated_at_ms=captured_at_ms,
        log=None,
    )

    assert result == {
        "status": "idempotent",
        "ledger_count": 1,
        "inserted_count": 0,
        "idempotent_count": 2,
        "error_count": 0,
    }
    assert racing.import_calls == 2
    assert {item.observed_at_ms for item in evidence.read_all().fx_rates} == {
        captured_at_ms - 1
    }


def test_fx_evidence_preserves_stale_cache_provenance(tmp_path: Path) -> None:
    from src.application import prepared_option_positions_context as mod

    db_path = tmp_path / "option_positions.sqlite3"
    result = mod._persist_fx_evidence(
        repos_by_ledger_path={
            db_path: SQLiteOptionPositionsRepository(db_path),
        },
        observation={
            "timestamp": NOW.isoformat(),
            "source": "tencent_quote",
            "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
        },
        observation_status="unavailable_stale",
        migrated_at_ms=int((NOW + timedelta(days=2)).timestamp() * 1000),
        log=None,
    )

    assert result["status"] == "persisted"
    rates = PerformanceEvidenceSQLiteRepository(db_path).read_all().fx_rates
    assert {item.source for item in rates} == {"cache_snapshot"}
    assert all(item.quality["stale_cache_fallback"] is True for item in rates)

    fresh_db_path = tmp_path / "fresh-option-positions.sqlite3"
    fresh_repo = SQLiteOptionPositionsRepository(fresh_db_path)
    first = mod._persist_fx_evidence(
        repos_by_ledger_path={fresh_db_path: fresh_repo},
        observation={
            "timestamp": NOW.isoformat(),
            "source": "tencent_quote",
            "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
        },
        observation_status="ready",
        migrated_at_ms=int((NOW + timedelta(hours=2)).timestamp() * 1000),
        log=None,
    )
    repeated_stale = mod._persist_fx_evidence(
        repos_by_ledger_path={fresh_db_path: fresh_repo},
        observation={
            "timestamp": NOW.isoformat(),
            "source": "tencent_quote",
            "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
        },
        observation_status="unavailable_stale",
        migrated_at_ms=int((NOW + timedelta(days=2)).timestamp() * 1000),
        log=None,
    )
    assert first["status"] == "persisted"
    assert repeated_stale["status"] == "idempotent"
    assert {
        item.source
        for item in PerformanceEvidenceSQLiteRepository(
            fresh_db_path
        ).read_all().fx_rates
    } == {"realtime_snapshot"}


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

    assert batch.ledger_read_count == 2
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


@pytest.mark.parametrize("collector_fails", [False, True])
def test_prepare_v2_captures_current_marks_and_degrades_lab_only(
    monkeypatch,
    tmp_path: Path,
    collector_fails: bool,
) -> None:
    from src.application import prepared_option_positions_context as mod

    run_id = "run-option-market-evidence"
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
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    mark = ValuationMarkFact(
        fact_id="mark-nvda",
        instrument=OptionInstrumentKey(
            symbol="NVDA",
            option_type="put",
            strike="95",
            expiration_ymd="2099-09-18",
            currency="USD",
            multiplier="100",
        ),
        price="1.25",
        mark_kind="realtime_mid",
        effective_at_ms=now_ms - 1_000,
        observed_at_ms=now_ms - 1_000,
        source="realtime_snapshot",
        source_id="NVDA-put-95",
    )
    captured_accounts: list[str] = []

    def _collect(**kwargs):
        captured_accounts.extend(position.account for position in kwargs["option_positions"])
        if collector_fails:
            raise RuntimeError("snapshot unavailable")
        return CurrentEvidenceCollection(
            status="collected",
            valuation_marks=(mark,),
        )

    monkeypatch.setattr(mod, "collect_current_performance_evidence", _collect)
    monkeypatch.setattr(
        mod,
        "get_exchange_rates_or_fetch_latest",
        lambda **_kwargs: {
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
        persist_fx_evidence=True,
        mark_evidence_accounts=("lx",),
    )
    manifest = batch.manifests["lx"]
    payload = load_prepared_option_positions_context(
        manifest_path=Path(manifest["manifest_path"]),
        expected_base=tmp_path,
        expected_run_id=run_id,
        expected_account="lx",
        expected_account_config_sha256=authorities["lx"].account_config_sha256,
        expected_manifest_sha256=manifest["manifest_sha256"],
        expected_runtime_config=configs["lx"],
    )

    evidence = payload["strategy_lab_option_market_evidence"]
    assert captured_accounts == ["lx"]
    assert batch.unavailable_by_account == {}
    assert manifest["schema_version"] == "prepared_option_positions_context.v2"
    assert Path(manifest["manifest_path"]).name == (
        "prepared_option_positions_context.v2.json"
    )
    monkeypatch.setattr(
        mod,
        "collect_current_performance_evidence",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("same-run retry must not query option marks")
        ),
    )
    monkeypatch.setattr(
        mod,
        "get_exchange_rates_or_fetch_latest",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("same-run retry must not query FX")
        ),
    )
    replayed = prepare_option_positions_contexts(
        base=tmp_path,
        run_id=run_id,
        config_path=config_path,
        account_configs=configs,
        account_config_authorities=authorities,
        run_state_dir=tmp_path / "output_runs" / run_id / "state",
        persist_fx_evidence=True,
        mark_evidence_accounts=("lx",),
    )
    assert replayed.ledger_read_count == 0
    assert replayed.fx_observation_count == 0
    assert replayed.unavailable_by_account == {}
    assert replayed.manifests["lx"]["manifest_sha256"] == manifest["manifest_sha256"]
    if collector_fails:
        assert evidence["status"] == "unavailable"
        assert evidence["reason_code"] == "option_market_evidence_mark_missing"
        assert PerformanceEvidenceSQLiteRepository(
            ledger_path
        ).read_all().valuation_marks == ()
        return

    assert evidence["status"] == "ready"
    assert evidence["ledger_generation_sha256_a"] == (
        evidence["ledger_generation_sha256_b"]
    )
    assert evidence["decision_state_fingerprint_a"] == (
        evidence["decision_state_fingerprint_b"]
    )
    assert [row["lot_id"] for row in evidence["open_option_positions"]]
    assert evidence["valuation_mark_facts"][0]["fact_id"] == "mark-nvda"
    assert [
        fact.fact_id
        for fact in PerformanceEvidenceSQLiteRepository(
            ledger_path
        ).read_all().valuation_marks
    ] == ["mark-nvda"]
    assert evidence["fx_rate_facts"][0]["base_currency"] == "USD"
    assert "sell_limit" not in json.dumps(evidence)


@pytest.mark.parametrize(
    ("second_read", "expected_reason"),
    [
        ("drift", "option_market_evidence_position_drift"),
        (
            "unavailable",
            "option_market_evidence_position_snapshot_unavailable",
        ),
    ],
)
def test_position_fence_failure_only_disables_strategy_lab_evidence(
    monkeypatch,
    tmp_path: Path,
    second_read: str,
    expected_reason: str,
) -> None:
    from src.application import prepared_option_positions_context as mod

    run_id = "run-option-market-drift"
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
    writer = SQLiteOptionPositionsRepository(ledger_path)
    original_read = mod.read_decision_state_rows_many
    reads = 0

    def _read_with_drift(repo, *, accounts):
        nonlocal reads
        reads += 1
        if reads == 2:
            if second_read == "unavailable":
                raise RuntimeError("snapshot unavailable")
            _open_position(
                writer,
                account="lx",
                symbol="NVDA",
                option_type="put",
                side="short",
                contracts=1,
                strike=95,
                expiry="2099-09-18",
                opened_at_ms=1_000,
            )
        return original_read(repo, accounts=accounts)

    monkeypatch.setattr(mod, "read_decision_state_rows_many", _read_with_drift)
    monkeypatch.setattr(
        mod,
        "get_exchange_rates_or_fetch_latest",
        lambda **_kwargs: {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "tencent_quote",
            "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
        },
    )

    messages: list[str] = []
    batch = prepare_option_positions_contexts(
        base=tmp_path,
        run_id=run_id,
        config_path=config_path,
        account_configs=configs,
        account_config_authorities=authorities,
        run_state_dir=tmp_path / "output_runs" / run_id / "state",
        persist_fx_evidence=True,
        log=messages.append,
    )
    manifest = batch.manifests["lx"]
    common = {
        "manifest_path": Path(manifest["manifest_path"]),
        "expected_base": tmp_path,
        "expected_run_id": run_id,
        "expected_account": "lx",
        "expected_account_config_sha256": authorities[
            "lx"
        ].account_config_sha256,
        "expected_manifest_sha256": manifest["manifest_sha256"],
        "expected_runtime_config": configs["lx"],
    }
    payload = load_prepared_option_positions_context(**common)

    assert sum(
        row["contracts_open"] for row in payload["open_positions_min"]
    ) == (1 if second_read == "drift" else 0)
    evidence = payload["strategy_lab_option_market_evidence"]
    assert evidence["status"] == "unavailable"
    assert evidence["reason_code"] == expected_reason
    if second_read == "unavailable":
        assert messages == [
            "[WARN] prepared option position snapshot B unavailable: RuntimeError"
        ]
    with pytest.raises(
        PreparedOptionPositionsContextError,
        match=expected_reason,
    ):
        load_prepared_option_positions_context(
            **common,
            require_option_market_evidence=True,
        )


def test_option_market_evidence_diagnostics_are_account_isolated() -> None:
    observed_at_ms = int(NOW.timestamp() * 1000)
    lx_key = ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=95,
        expiration_ymd="2099-09-18",
    )
    sy_key = ContractKey.from_values(
        broker="futu",
        account="sy",
        underlying_symbol="AAPL",
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd="2099-09-18",
    )
    rows = [
        trade_event_application_payload(
            TradeEvent(
                event_id="lx-open",
                event_type="open",
                event_time_ms=1_000,
                contract_key=lx_key,
                contracts=1,
                price=1.5,
                currency="CNY",
                source="test",
                multiplier=100,
                fees=0,
                lot_id="lx-lot",
            )
        ),
        trade_event_application_payload(
            TradeEvent(
                event_id="sy-orphan-close",
                event_type="close",
                event_time_ms=2_000,
                contract_key=sy_key,
                contracts=1,
                price=1,
                currency="CNY",
                source="test",
                multiplier=100,
                fees=0,
                target_lot_id="missing-sy-lot",
            )
        ),
    ]
    mark = ValuationMarkFact(
        fact_id="lx-mark",
        instrument=OptionInstrumentKey(
            symbol="NVDA",
            option_type="put",
            strike="95",
            expiration_ymd="2099-09-18",
            currency="CNY",
            multiplier="100",
        ),
        price="1.25",
        mark_kind="realtime_mid",
        effective_at_ms=observed_at_ms - 1,
        observed_at_ms=observed_at_ms - 1,
        source="test",
        source_id="lx-mark",
    )
    bundle = type(
        "EvidenceBundle",
        (),
        {
            "schema_state": "initialized_v1",
            "valuation_marks": (mark,),
            "fx_rates": (),
        },
    )()
    common = {
        "run_id": "account-isolation",
        "account_config_sha256": "a" * 64,
        "broker": "futu",
        "scan_currency": "CNY",
        "evidence_bundle": bundle,
        "evidence_at_utc": NOW.isoformat().replace("+00:00", "Z"),
        "ledger_generation_sha256_a": "b" * 64,
        "ledger_generation_sha256_b": "b" * 64,
        "decision_state_fingerprint_a": "c" * 64,
        "decision_state_fingerprint_b": "c" * 64,
    }

    lx = build_option_market_evidence_payload(
        **common,
        account="lx",
        rows_a={"trade_events": rows},
    )
    sy = build_option_market_evidence_payload(
        **common,
        account="sy",
        rows_a={"trade_events": rows},
    )
    unscoped = build_option_market_evidence_payload(
        **common,
        account="lx",
        rows_a={
            "trade_events": [
                *rows,
                {
                    "event_id": "unscoped-invalid",
                    "event_type": "open",
                    "event_time_ms": 3_000,
                    "contract_key": "invalid",
                },
            ]
        },
    )

    assert lx["status"] == "ready"
    assert [row["lot_id"] for row in lx["open_option_positions"]] == [
        "lx-lot"
    ]
    assert sy["reason_code"] == "option_market_evidence_position_invalid"
    assert unscoped["reason_code"] == "option_market_evidence_position_invalid"


def test_generic_recovery_accepts_v1_and_discovery_prefers_v2(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import prepared_option_positions_context as mod

    run_id = "run-v1-recovery"
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
    batch = prepare_option_positions_contexts(
        base=tmp_path,
        run_id=run_id,
        config_path=config_path,
        account_configs=configs,
        account_config_authorities=authorities,
        run_state_dir=tmp_path / "output_runs" / run_id / "state",
    )
    v2_path = Path(batch.manifests["lx"]["manifest_path"])
    payload_path = v2_path.parent / "option_positions_context.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    _ledger_path, repo = mod.open_position_ledger_from_data_config(
        base=tmp_path,
        data_config=data_config,
    )
    rows = mod.read_decision_state_rows_many(
        repo,
        accounts=("lx",),
    )["lx"]
    payload["decision_state_snapshot"] = (
        mod.decision_state_snapshot_from_rows(
            rows,
            account="lx",
            portfolio_scope_id=mod.portfolio_scope_id("lx"),
            source_observed_at=payload["prepared_authority"][
                "source_observed_at"
            ],
            current_projection=None,
            current_decision_now_ms=int(
                datetime.now(timezone.utc).timestamp() * 1000
            ),
        )
    )
    for v2_only_key in (
        "current_decision_read",
        "decision_snapshot_actionable",
        "current_decision_shadow",
        "strategy_lab_option_market_evidence",
    ):
        payload.pop(v2_only_key, None)
    payload["prepared_authority"]["schema_version"] = (
        "prepared_option_positions_context.v1"
    )
    payload_bytes = _canonical_bytes(payload)
    payload_path.write_bytes(payload_bytes)
    manifest = json.loads(v2_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "prepared_option_positions_context.v1"
    manifest["payload_sha256"] = hashlib.sha256(payload_bytes).hexdigest()
    v1_path = v2_path.with_name("prepared_option_positions_context.v1.json")
    v1_bytes = _canonical_bytes(manifest)
    v1_path.write_bytes(v1_bytes)
    v2_bytes = v2_path.read_bytes()
    v2_path.unlink()

    assert find_prepared_option_positions_manifest(
        base=tmp_path,
        run_id=run_id,
        account="lx",
    ) == v1_path
    recovered = load_prepared_option_positions_context(
        manifest_path=v1_path,
        expected_base=tmp_path,
        expected_run_id=run_id,
        expected_account="lx",
        expected_account_config_sha256=authorities["lx"].account_config_sha256,
        expected_manifest_sha256=hashlib.sha256(v1_bytes).hexdigest(),
        expected_runtime_config=configs["lx"],
    )
    assert recovered["prepared_authority"]["schema_version"].endswith(".v1")
    with pytest.raises(
        PreparedOptionPositionsContextError,
        match="option_market_evidence_contract_missing",
    ):
        load_prepared_option_positions_context(
            manifest_path=v1_path,
            expected_base=tmp_path,
            expected_run_id=run_id,
            expected_account="lx",
            expected_account_config_sha256=authorities[
                "lx"
            ].account_config_sha256,
            expected_manifest_sha256=hashlib.sha256(v1_bytes).hexdigest(),
            expected_runtime_config=configs["lx"],
            require_option_market_evidence=True,
        )

    v2_path.write_bytes(v2_bytes)
    assert find_prepared_option_positions_manifest(
        base=tmp_path,
        run_id=run_id,
        account="lx",
    ) == v2_path
