from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import src.application.strategy_lab.top1.research_runner as runner_module
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.option_lifecycle import expiration_observation_start_ms
from domain.domain.performance.models import FXRateFact
from src.application.recommendation_point import (
    RECOMMENDATION_POINT_FILE,
    capture_scheduled_recommendation_point,
)
from src.application.research.formal_corpus import (
    capture_formal_point_attempt,
    seal_formal_day_expectation,
)
from src.application.strategy_lab.top1.contracts import (
    CONFIRMED_START_COMMAND_SCHEMA_VERSION,
)
from src.application.strategy_lab.top1.corpus import capture_recommendation_point
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
from tests.test_strategy_lab_top1_corpus import (
    CALENDAR_HASH,
    SOURCE_SHA,
    _candidate,
    _schedule,
    _scheduler,
    _seal,
    _store,
    _trading_days,
)
from tests.candidate_evidence_helpers import (
    seal_market_calendar_fixture,
    seal_opening_candidate_fixture,
    top1_hk_schedule_fixture,
)
from tests.test_strategy_lab_top1_research_runner import (
    FakeGateway,
    _direct_limiter,
    _prepared,
    _run,
)


NOW = "2026-08-15T12:00:00Z"


def _preview_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    omit_last: bool = False,
    formal: bool = False,
) -> tuple[ExperimentStore, Path, dict[str, object]]:
    from src.application import recommendation_point as recommendation_module
    from src.application.research import formal_corpus as formal_corpus_module
    from src.application.strategy_lab.top1 import corpus as corpus_module

    store = _store(tmp_path)
    artifact_root = tmp_path / "artifacts"
    source_root = tmp_path / "source"
    days = _trading_days("2026-06-08", 20)
    receipts: dict[str, dict[str, object]] = {}

    def receipt_loader(**kwargs: object) -> dict[str, object]:
        return receipts[str(kwargs["expected_run_id"])]

    required_bytes = b'{"fixture":true}\n'
    required_hash = hashlib.sha256(required_bytes).hexdigest()

    def required_loader(**kwargs: object) -> tuple[dict[str, object], Path, bytes]:
        return (
            {
                "run_id": str(kwargs["expected_run_id"]),
                "symbols": {
                    "0700.HK": {
                        "status": "ready",
                        "source_observed_at": "2026-06-01T00:00:00Z",
                        "payload_sha256": "e" * 64,
                        "scan_blob_ref": "blob/ref",
                    }
                },
            },
            source_root,
            required_bytes,
        )

    monkeypatch.setattr(
        recommendation_module,
        "find_prepared_option_positions_manifest",
        lambda **_kwargs: source_root / "prepared_option_positions_context.v2.json",
    )
    monkeypatch.setattr(
        recommendation_module,
        "load_prepared_option_positions_context_receipt",
        receipt_loader,
    )
    monkeypatch.setattr(
        corpus_module,
        "find_prepared_option_positions_manifest",
        lambda **_kwargs: source_root / "prepared_option_positions_context.v2.json",
    )
    monkeypatch.setattr(
        corpus_module,
        "load_prepared_option_positions_context_receipt",
        receipt_loader,
    )
    if formal:
        for module in (recommendation_module, formal_corpus_module):
            monkeypatch.setattr(
                module,
                "_required_data_binding",
                lambda _opening: ("required/manifest.json", required_hash),
            )
            monkeypatch.setattr(
                module,
                "load_required_data_snapshot_manifest_snapshot",
                required_loader,
            )
        monkeypatch.setattr(
            formal_corpus_module,
            "load_prepared_option_positions_context_receipt",
            receipt_loader,
        )
        monkeypatch.setattr(
            formal_corpus_module,
            "validate_strategy_lab_option_market_evidence",
            lambda value, **_kwargs: value,
        )
    for index, trading_date in enumerate(days):
        if formal:
            assert seal_formal_day_expectation(
                artifact_root,
                market="HK",
                account="lx",
                schedule=_schedule(),
                trading_date=trading_date,
                market_calendar_version="hk-calendar.fixture.v1",
                market_calendar_sha256=CALENDAR_HASH,
                sealed_at_utc=f"{trading_date}T01:00:00Z",
            )["status"] == "published"
        else:
            assert _seal(store, artifact_root, day=trading_date)["status"] == "published"
        run_id = f"workspace-{index:02d}"
        seal_opening_candidate_fixture(
            source_root,
            run_id=run_id,
            market="HK",
            accepted_rows=[
                {
                    **_candidate(),
                    "currency": "CNY",
                    "snapshot_received_at_utc": "2026-06-01T00:00:00Z",
                }
            ],
        )
        evidence: dict[str, object] = {
            "status": "ready",
            "run_id": run_id,
            "account": "lx",
            "account_config_sha256": "a" * 64,
            "evidence_at_utc": "2026-06-01T00:00:00Z",
            "open_option_positions": [],
            "valuation_mark_facts": [],
            "fx_rate_facts": [],
        }
        evidence["content_sha256"] = canonical_sha256(evidence)
        payload = {"strategy_lab_option_market_evidence": evidence}
        payload_bytes = (
            json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n"
        ).encode()
        manifest = {
            "schema_version": "prepared_option_positions_context.v2",
            "status": "ready",
            "run_id": run_id,
            "account": "lx",
            "account_config_sha256": "a" * 64,
            "application_received_at_utc": "2026-06-01T00:00:00Z",
            "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        }
        receipts[run_id] = {
            "manifest": manifest,
            "payload": payload,
            "manifest_bytes": (
                json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False)
                + "\n"
            ).encode(),
            "payload_bytes": payload_bytes,
        }
        publication, point = capture_scheduled_recommendation_point(
            source_root,
            run_id,
            "lx",
            _scheduler(trading_date),
            source_commit_sha=SOURCE_SHA,
            require_option_market_evidence=True,
            require_formal_contract=formal,
        )
        assert publication == "published"
        point_ref = (
            f"output_runs/{run_id}/accounts/lx/state/{RECOMMENDATION_POINT_FILE}"
        )
        if omit_last and index == len(days) - 1:
            continue
        if formal:
            assert capture_formal_point_attempt(
                artifact_root,
                source_root,
                market="HK",
                account="lx",
                trading_date=trading_date,
                run_id=run_id,
                scheduled_scan_target_market=str(
                    point["scheduled_scan_target_market"]
                ),
                captured_at_utc=f"{trading_date}T02:01:00Z",
                producer_behavior_version="recommendation_point.v3",
                recommendation_point=point,
            )["status"] == "published"
        else:
            assert capture_recommendation_point(
                store,
                source_root,
                artifact_root,
                point_ref=point_ref,
                trading_date=trading_date,
                captured_at_utc=f"{trading_date}T02:01:00Z",
                environ=AVAILABLE,
            )["status"] == "published"
    terminal_at_ms = expiration_observation_start_ms("2026-08-21", "HK")
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
    return store, artifact_root, {
        "market": "HK",
        "account": "lx",
        "cutoff_at_utc": "2026-10-01T08:00:00Z",
        "latest_mature_trading_date": days[-1],
        "market_calendar": {
            "market_calendar_version": "hk-calendar.fixture.v1",
            "snapshot_ref": "evidence/hk-calendar.fixture.json",
            "snapshot_content_sha256": CALENDAR_HASH,
            "snapshot_file_sha256": "c" * 64,
            "trading_dates": days,
        },
        "fee_contract": _fee_contract(),
        "capability_facts": {name: True for name in CAPABILITY_FACTS},
        "corpus_status": {
            "schema_version": "corpus_health_receipt.v2",
            "status": "healthy",
            "continuous_complete_trading_days": 20,
            "storage": {"capacity": {"status": "insufficient_history"}},
        },
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


def test_research_preview_is_deterministic_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, artifact_root, args = _preview_args(tmp_path, monkeypatch)
    before = _files(tmp_path)

    preview = preview_sell_put_top1_research(store, artifact_root, **args)

    assert preview_sell_put_top1_research(store, artifact_root, **args) == preview
    assert preview["status"] == "available", preview
    assert preview["reason_codes"] == []
    assert preview["stage_spec_sha256"]
    assert preview["preview_sha256"]
    assert preview["stage_spec_sha256"] != preview["preview_sha256"]
    assert _files(tmp_path) == before


def test_research_preview_uses_complete_formal_v3_corpus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, artifact_root, args = _preview_args(
        tmp_path, monkeypatch, formal=True
    )
    before = _files(tmp_path)

    preview = preview_sell_put_top1_research(store, artifact_root, **args)

    assert preview["status"] == "available", preview
    assert preview["experiment_spec"]["baseline"][  # type: ignore[index]
        "ranking_projection_schema_version"
    ] == "sell_put_ranking_projection.v3"
    assert _files(tmp_path) == before


def test_research_preview_blocks_when_formal_point_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, artifact_root, args = _preview_args(
        tmp_path, monkeypatch, omit_last=True, formal=True
    )
    before = _files(tmp_path)

    preview = preview_sell_put_top1_research(store, artifact_root, **args)

    assert preview["status"] == "blocked"
    assert preview["reason_codes"] == ["research_window_coverage_missing"]
    assert _files(tmp_path) == before


def test_research_preview_blocks_when_one_expected_point_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, artifact_root, args = _preview_args(
        tmp_path, monkeypatch, omit_last=True
    )
    before = _files(tmp_path)

    preview = preview_sell_put_top1_research(store, artifact_root, **args)

    assert preview["status"] == "blocked"
    assert preview["reason_codes"] == ["research_dataset_coverage_missing"]
    assert _files(tmp_path) == before


def test_research_preview_enforces_corpus_health_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, artifact_root, args = _preview_args(tmp_path, monkeypatch)
    corpus = args["corpus_status"]
    assert isinstance(corpus, dict)
    corpus["continuous_complete_trading_days"] = 19

    warming = preview_sell_put_top1_research(store, artifact_root, **args)
    assert warming["reason_codes"] == ["research_corpus_warming"]

    corpus["continuous_complete_trading_days"] = 20
    corpus["storage"] = {"capacity": {"status": "critical"}}
    at_risk = preview_sell_put_top1_research(store, artifact_root, **args)
    assert at_risk["reason_codes"] == ["research_storage_capacity_risk"]


def test_changed_research_confirmation_has_zero_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, artifact_root, args = _preview_args(tmp_path, monkeypatch)
    preview = preview_sell_put_top1_research(store, artifact_root, **args)
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
    assert store.active_experiments("HK", "lx") == []
    assert _files(tmp_path) == before


def test_confirmed_research_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, artifact_root, args = _preview_args(tmp_path, monkeypatch)
    preview = preview_sell_put_top1_research(store, artifact_root, **args)
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
