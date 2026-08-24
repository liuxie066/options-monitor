from __future__ import annotations

from pathlib import Path

import pytest

import src.application.strategy_lab.top1.research_runner as runner_module
from domain.domain.option_lifecycle import expiration_observation_start_ms
from domain.domain.performance.models import FXRateFact
from src.application.strategy_lab.top1.contracts import (
    CONFIRMED_START_COMMAND_SCHEMA_VERSION,
)
from src.application.strategy_lab.top1.readiness import CAPABILITY_FACTS
from src.application.strategy_lab.top1.workspace import (
    Top1WorkspaceError,
    preview_sell_put_top1_research,
    preview_sell_put_top1_validation,
    start_confirmed_research,
    start_confirmed_validation,
)
from src.infrastructure.performance_evidence_sqlite import EvidenceReadBundle
from src.infrastructure.strategy_lab.experiment_store import ExperimentStore
from tests.test_strategy_lab_top1_research import AVAILABLE, _fee_contract
from tests.candidate_evidence_helpers import (
    seal_market_calendar_fixture,
    top1_hk_schedule_fixture,
)
from tests.test_strategy_lab_top1_research_runner import (
    FakeGateway,
    _direct_limiter,
    _prepared,
    _run,
)
from tests.test_strategy_lab_top1_research_window import _window_fixture


NOW = "2026-08-15T12:00:00Z"


def _preview_args(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    artifact_root, _research_root, _days, window_args = _window_fixture(tmp_path)
    terminal_at_ms = expiration_observation_start_ms("2026-01-30", "HK")
    assert terminal_at_ms is not None
    bundle = EvidenceReadBundle(
        schema_state="initialized_v1",
        fx_rates=(
            FXRateFact(
                fact_id="hkd-terminal",
                base_currency="HKD",
                quote_currency="CNY",
                rate="0.92",
                rate_kind="mid",
                effective_at_ms=terminal_at_ms,
                observed_at_ms=terminal_at_ms,
                source="fixture",
                source_id="hkd-terminal",
            ),
        ),
    )
    return artifact_root, {
        **window_args,
        "fee_contract": _fee_contract(),
        "capability_facts": {name: True for name in CAPABILITY_FACTS},
        "evidence_bundle": bundle,
        "environ": AVAILABLE,
    }


def _command(
    preview: dict[str, object],
    *,
    preview_hash: str | None = None,
    stage: str = "research",
) -> dict[str, object]:
    return {
        "schema_version": CONFIRMED_START_COMMAND_SCHEMA_VERSION,
        "stage": stage,
        "market": "HK",
        "account": "lx",
        "experiment_id": preview["experiment_id"],
        "confirmed_preview_sha256": preview_hash or preview["preview_sha256"],
        "idempotency_key": "workspace-research-start",
        "actor": "human",
        "confirmed_at_utc": NOW,
    }


def _files(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )


def test_research_preview_is_deterministic_and_read_only(tmp_path: Path) -> None:
    artifact_root, args = _preview_args(tmp_path)
    before = _files(tmp_path)

    preview = preview_sell_put_top1_research(artifact_root, **args)

    assert preview_sell_put_top1_research(artifact_root, **args) == preview
    assert preview["status"] == "available"
    assert preview["reason_codes"] == []
    assert preview["stage_spec_sha256"]
    assert preview["preview_sha256"]
    assert preview["stage_spec_sha256"] != preview["preview_sha256"]
    assert _files(tmp_path) == before


def test_changed_research_confirmation_has_zero_writes(tmp_path: Path) -> None:
    artifact_root, args = _preview_args(tmp_path)
    preview = preview_sell_put_top1_research(artifact_root, **args)
    store = ExperimentStore(tmp_path / "strategy-lab.sqlite3")
    before = _files(tmp_path)

    with pytest.raises(Top1WorkspaceError) as exc_info:
        start_confirmed_research(
            store,
            artifact_root,
            confirmed_start=_command(preview, preview_hash="f" * 64),
            gateway=FakeGateway(),
            config=None,
            **args,
        )

    assert exc_info.value.reason_code == "preview_hash_changed"
    assert store.schema_state()["status"] == "not_initialized"
    assert _files(tmp_path) == before


def test_confirmed_research_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root, args = _preview_args(tmp_path)
    preview = preview_sell_put_top1_research(artifact_root, **args)
    store = ExperimentStore(tmp_path / "strategy-lab.sqlite3")
    store.migrate(migrated_at_utc=NOW)
    monkeypatch.setattr(runner_module, "rate_limited_opend_call", _direct_limiter)
    gateway = FakeGateway()
    command = _command(preview)

    first = start_confirmed_research(
        store,
        artifact_root,
        confirmed_start=command,
        gateway=gateway,
        config=None,
        **args,
    )
    calls = (gateway.quota_calls, len(gateway.close_calls))
    second = start_confirmed_research(
        store,
        artifact_root,
        confirmed_start=command,
        gateway=gateway,
        config=None,
        **args,
    )

    assert first == second
    assert first["selection"] == "no_research_winner"
    assert (gateway.quota_calls, len(gateway.close_calls)) == calls
    assert len(store.active_experiments("HK", "lx")) == 1


def test_validation_preview_and_confirmed_start_complete_the_public_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, artifact_root, case, research_hash = _prepared(tmp_path)
    monkeypatch.setattr(runner_module, "rate_limited_opend_call", _direct_limiter)
    result = _run(store, artifact_root, case, research_hash, FakeGateway())
    assert result["selection"] == "research_leader"
    days = [
        "2026-08-17",
        "2026-08-18",
        "2026-08-19",
        "2026-08-20",
        "2026-08-21",
        "2026-08-24",
        "2026-08-25",
        "2026-08-26",
        "2026-08-27",
        "2026-08-28",
    ]
    seal_market_calendar_fixture(
        artifact_root,
        days,
        version=case["experiment_spec"]["economics_contracts"][
            "market_calendar_version"
        ],
    )
    preview_args = {
        "experiment_id": case["experiment_spec"]["experiment_id"],
        "validation_start_trading_date": days[0],
        "schedule": top1_hk_schedule_fixture(),
        "timer_binding": {
            "revision": "top1-advance.v1",
            "producer_catchup_grace_seconds": 30,
            "producer_run_timeout_upper_bound_seconds": 120,
            "advance_cadence_seconds": 60,
            "fill_observation_duration_upper_bound_seconds": 120,
            "terms_capture_duration_upper_bound_seconds": 120,
        },
        "as_of_utc": NOW,
        "environ": AVAILABLE,
    }
    before = _files(tmp_path)

    preview = preview_sell_put_top1_validation(
        store,
        artifact_root,
        **preview_args,
    )

    assert preview["status"] == "available"
    assert preview["stage_spec_sha256"] == preview["preview_sha256"]
    assert _files(tmp_path) == before
    with pytest.raises(Top1WorkspaceError) as exc_info:
        start_confirmed_validation(
            store,
            artifact_root,
            confirmed_start=_command(
                preview,
                preview_hash="f" * 64,
                stage="validation",
            ),
            **preview_args,
        )
    assert exc_info.value.reason_code == "preview_hash_changed"
    assert store.experiment(str(preview["experiment_id"]))[
        "research_progress"
    ] == "ready_to_compare"
    assert _files(tmp_path) == before
    command = _command(preview, stage="validation")

    from src.application.strategy_lab.top1 import corpus as corpus_module

    read_calendar = corpus_module.read_market_calendar_binding

    def drifted_calendar(*args: object, **kwargs: object) -> dict[str, object]:
        binding = read_calendar(*args, **kwargs)
        return {**binding, "snapshot_content_sha256": "f" * 64}

    monkeypatch.setattr(
        corpus_module,
        "read_market_calendar_binding",
        drifted_calendar,
    )
    with pytest.raises(Top1WorkspaceError) as drift_error:
        start_confirmed_validation(
            store,
            artifact_root,
            confirmed_start=command,
            **preview_args,
        )
    assert drift_error.value.reason_code == "preview_hash_changed"
    unchanged = store.experiment(str(preview["experiment_id"]))
    assert unchanged["research_progress"] == "ready_to_compare"
    assert unchanged["validation_spec_sha256"] is None
    assert unchanged["proposed_commitment_sha256"] is None
    assert _files(tmp_path) == before
    monkeypatch.setattr(
        corpus_module,
        "read_market_calendar_binding",
        read_calendar,
    )

    first = start_confirmed_validation(
        store,
        artifact_root,
        confirmed_start=command,
        **preview_args,
    )
    second = start_confirmed_validation(
        store,
        artifact_root,
        confirmed_start=command,
        **{**preview_args, "as_of_utc": "2026-08-17T03:00:00Z"},
    )

    assert first == second
    assert first["status"] == "validation_started"
    assert first["validation_progress"] == "collecting_decisions"
    experiment = store.experiment(str(preview["experiment_id"]))
    assert experiment["phase"] == "validation"
    assert experiment["validation_spec_sha256"] == preview["preview_sha256"]
