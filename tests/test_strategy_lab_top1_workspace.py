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
    start_confirmed_research,
)
from src.infrastructure.performance_evidence_sqlite import EvidenceReadBundle
from src.infrastructure.strategy_lab.experiment_store import ExperimentStore
from tests.test_strategy_lab_top1_research import AVAILABLE, _fee_contract
from tests.test_strategy_lab_top1_research_runner import FakeGateway, _direct_limiter
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


def _command(preview: dict[str, object], *, preview_hash: str | None = None) -> dict[str, object]:
    return {
        "schema_version": CONFIRMED_START_COMMAND_SCHEMA_VERSION,
        "stage": "research",
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
