from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.opend_fetch_config import OpenDEndpointRateLimit
from src.application.opend_market_snapshot_fetching import MarketSnapshotFetchResult
from src.application.service_deploy import next_systemd_tick_target_utc
from src.application.strategy_lab.evidence import (
    StrategyLabEvidenceError,
    build_hidden_batch_manifest,
    build_validation_fill_evidence,
    build_validation_point_evidence,
    hidden_snapshot_crossings,
    normalize_hidden_snapshot,
    publish_evidence_artifact,
)
from src.infrastructure.strategy_lab.experiment_store import (
    ExperimentStore,
    ExperimentStoreError,
)
from src.infrastructure.futu_gateway import FutuGatewayError
from src.interfaces.cli.main import parse_args
from src.interfaces.cli.strategy_lab_ops import (
    _is_bounded_systemd_invocation,
    handle_strategy_lab_command,
)


SLOT = "2026-09-01T02:00:00Z"
DEADLINE = "2026-09-01T02:00:20Z"
NOW = "2026-09-01T02:00:05Z"
SOURCE = "b" * 40


def _validation_store(
    tmp_path: Path,
    plan: dict[str, object],
    *,
    spec: dict[str, object] | None = None,
    research_observations: list[dict[str, object]] | None = None,
) -> tuple[ExperimentStore, dict]:
    store = ExperimentStore(tmp_path / "experiments.sqlite3")
    store.initialize()
    spec = spec or {"recipe_id": "sell_put_option_position_concentration"}
    manifest = [{"path": "owner.py", "sha256": "a" * 64}]
    store.create_experiment(
        experiment_id="experiment-1",
        spec=spec,
        spec_sha256=canonical_sha256(spec),
        source_commit_sha=SOURCE,
        behavior_manifest=manifest,
        evaluator_behavior_sha256=canonical_sha256(manifest),
        confirmation_sha256="c" * 64,
        idempotency_key="research-confirm",
        actor="tester",
        occurred_at_utc="2026-08-30T00:00:00Z",
    )
    for observation in research_observations or []:
        store.put_observation("experiment-1", **observation)
    completed = store.append_event_and_transition(
        "experiment-1",
        expected_state="research_running",
        expected_revision=0,
        new_state="research_complete",
        event_type="research_materialized",
        actor="tester",
        payload={},
        occurred_at_utc="2026-08-30T00:01:00Z",
    )
    awaiting = store.attach_research_receipt_and_transition(
        "experiment-1",
        expected_state="research_complete",
        expected_revision=completed["revision"],
        new_state="awaiting_validation_confirmation",
        receipt_ref="experiments/experiment-1/receipts/research.json",
        receipt_sha256="d" * 64,
        leader={
            "variant_id": "challenger_0.002",
            "near_return_threshold": 0.002,
        },
        actor="tester",
        occurred_at_utc="2026-08-30T00:02:00Z",
        payload={"status": "leader"},
        idempotency_key="research-concluded",
    )
    confirmed = store.confirm_validation(
        "experiment-1",
        expected_revision=awaiting["revision"],
        validation_plan=plan,
        validation_plan_sha256=canonical_sha256(plan),
        preview_sha256="e" * 64,
        actor="tester",
        idempotency_key="validation-confirm",
        occurred_at_utc="2026-08-30T00:03:00Z",
    )
    return store, confirmed


def _manifest(plan: dict[str, object]) -> dict[str, object]:
    return {
        "trading_day": "2026-09-01",
        "observation_slot_utc": SLOT,
        "deadline_utc": DEADLINE,
        "validation_plan_sha256": canonical_sha256(plan),
        "arms": [
            {
                "recommendation_point_id": "1" * 64,
                "arm_id": "baseline",
                "code": "HK.80000001",
                "sell_limit": 1.0,
            },
            {
                "recommendation_point_id": "1" * 64,
                "arm_id": "challenger_0.002",
                "code": "HK.80000001",
                "sell_limit": 1.2,
            },
        ],
        "option_codes": ["HK.80000001"],
        "provider_source": {"provider": "futu_opend"},
        "evaluator_behavior_sha256": "f" * 64,
        "query_controls": {
            "max_wait_sec": 0,
            "no_retry": True,
            "snapshot_fallback_max_codes": 0,
        },
    }


def _put_available_validation_point(
    store: ExperimentStore,
    plan: dict[str, object],
    *,
    active_slots_utc: list[str] | None = None,
) -> None:
    store.put_observation(
        "experiment-1",
        observation_key="validation_point:" + "1" * 64,
        recommendation_point_id="1" * 64,
        kind="validation_point",
        status="available",
        payload={
            "status": "available",
            "trading_day": "2026-09-01",
            "recommendation_point_id": "1" * 64,
            "active_slots_utc": active_slots_utc or [SLOT],
            "session_endpoint_utc": "2026-09-01T02:01:00Z",
            "formal_point_ref": "formal/point.json.gz",
            "formal_point_sha256": "4" * 64,
            "formal_point_content_sha256": "5" * 64,
            "validation_plan_sha256": canonical_sha256(plan),
            "evaluator_behavior_sha256": "f" * 64,
            "arms": [
                {
                    "arm_id": "baseline",
                    "provider_code": "HK.80000001",
                    "sell_limit": 1.0,
                }
            ],
        },
        artifact_ref="formal/point.json.gz",
        artifact_sha256="4" * 64,
        created_at_utc=NOW,
    )


def _snapshot(*, bid: object = 1.1, bid_vol: object = 3) -> MarketSnapshotFetchResult:
    return MarketSnapshotFetchResult(
        snap_map={
            "HK.80000001": {
                "code": "HK.80000001",
                "bid_price": bid,
                "bid_vol": bid_vol,
                "update_time": "2026-09-01 10:00:04",
            }
        },
        errors=[],
        requested_codes=frozenset({"HK.80000001"}),
        returned_codes=frozenset({"HK.80000001"}),
        missing_codes=frozenset(),
        unexpected_codes=frozenset(),
        complete=True,
        opend_call_count=1,
        requested_at_utc="2026-09-01T02:00:03+00:00",
        received_at_utc="2026-09-01T02:00:04+00:00",
    )


def _hidden_artifact(
    manifest: dict[str, object], payload: dict[str, object], *, digest: str = "a" * 64
) -> dict[str, object]:
    return {
        "artifact_ref": f"evidence/hidden_batch/{digest}.json",
        "artifact_sha256": digest,
        "payload": payload,
        "artifact": {
            "kind": "hidden_batch",
            "query": manifest,
            "observed_at_utc": payload["observed_at_utc"],
            "payload": payload,
        },
    }


@pytest.mark.parametrize(
    ("reason_code", "artifact_ref", "artifact_sha256"),
    [
        ("formal_expectation_missing", None, None),
        ("formal_point_evidence_missing", "formal/point.json.gz", "4" * 64),
    ],
)
def test_store_accepts_both_validation_point_not_evaluable_forms(
    tmp_path: Path,
    reason_code: str,
    artifact_ref: str | None,
    artifact_sha256: str | None,
) -> None:
    plan = {"experiment_id": "experiment-1"}
    store, _confirmed = _validation_store(tmp_path, plan)
    payload: dict[str, object] = {
        "status": "not_evaluable",
        "reason_code": reason_code,
        "recommendation_point_id": "1" * 64,
    }
    if artifact_ref is not None:
        payload.update(
            formal_point_ref=artifact_ref,
            formal_point_sha256=artifact_sha256,
            formal_point_content_sha256="5" * 64,
        )
    first = store.put_observation(
        "experiment-1",
        observation_key="validation_point:" + "1" * 64,
        recommendation_point_id="1" * 64,
        kind="validation_point",
        status="not_evaluable",
        payload=payload,
        artifact_ref=artifact_ref,
        artifact_sha256=artifact_sha256,
        created_at_utc=NOW,
    )
    assert first["artifact_ref"] == artifact_ref
    assert (
        store.put_observation(
            "experiment-1",
            observation_key="validation_point:" + "1" * 64,
            recommendation_point_id="1" * 64,
            kind="validation_point",
            status="not_evaluable",
            payload=payload,
            artifact_ref=artifact_ref,
            artifact_sha256=artifact_sha256,
            created_at_utc="2026-09-01T02:00:10Z",
        )
        == first
    )


def test_store_requires_source_pair_for_available_validation_point(tmp_path: Path) -> None:
    plan = {"experiment_id": "experiment-1"}
    store, _confirmed = _validation_store(tmp_path, plan)
    with pytest.raises(ExperimentStoreError) as exc_info:
        store.put_observation(
            "experiment-1",
            observation_key="validation_point:" + "1" * 64,
            recommendation_point_id="1" * 64,
            kind="validation_point",
            status="available",
            payload={
                "status": "available",
                "recommendation_point_id": "1" * 64,
            },
            artifact_ref=None,
            artifact_sha256=None,
            created_at_utc=NOW,
        )
    assert exc_info.value.reason_code == "experiment_input_invalid"


def test_validation_point_slots_depend_only_on_authoritative_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.application.strategy_lab.evidence as evidence

    projected = {
        "recommendation_point_id": "1" * 64,
        "scheduled_scan_target_market": "2026-09-01T02:00:00Z",
        "recommendation_available_at_utc": "2026-09-01T02:00:05Z",
        "source_commit_sha": SOURCE,
        "opening_fx_binding": {},
        "arms": [
            {
                "arm_id": "baseline",
                "candidate": {
                    "contract_symbol": "HK.POP260828P127500",
                    "sell_limit": 1.0,
                },
            }
        ],
    }
    monkeypatch.setattr(evidence, "project_validation_arms", lambda *_args: projected)
    payload = build_validation_point_evidence(
        {"content_sha256": "5" * 64},
        {
            "artifact_ref": "formal/point.json.gz",
            "artifact_file_sha256": "4" * 64,
        },
        {},
        {
            "trading_date": "2026-09-01",
            "minute_grid_utc": [
                "2026-09-01T02:00:00Z",
                "2026-09-01T02:01:00Z",
                "2026-09-01T02:02:00Z",
            ],
            "session_endpoint_utc": "2026-09-01T02:03:00Z",
        },
    )
    assert payload["active_slots_utc"] == [
        "2026-09-01T02:01:00Z",
        "2026-09-01T02:02:00Z",
    ]
    assert "bound_at_utc" not in payload


def test_store_completes_batch_and_crossing_fill_atomically_then_transitions(
    tmp_path: Path,
) -> None:
    plan = {"experiment_id": "experiment-1"}
    store, confirmed = _validation_store(tmp_path, plan)
    manifest = _manifest(plan)
    key = f"hidden_batch:2026-09-01:{SLOT}"
    started, created = store.start_observation(
        "experiment-1", observation_key=key, manifest=manifest, created_at_utc=NOW
    )
    assert (started["status"], created) == ("started", True)
    assert store.start_observation("experiment-1", observation_key=key, manifest=manifest, created_at_utc=NOW) == (
        started,
        False,
    )
    complete = store.complete_observation(
        "experiment-1",
        observation_key=key,
        manifest=manifest,
        artifact_ref="evidence/hidden_batch/a.json",
        artifact_sha256="a" * 64,
        artifact_received_at_utc="2026-09-01T02:00:06Z",
        crossing_arm_ids=[("1" * 64, "baseline")],
    )
    assert complete["status"] == "complete"
    observations = store.list_observations("experiment-1")
    fill = next(item for item in observations if item["kind"] == "validation_fill")
    assert fill["payload"]["fill_time"] == "2026-09-01T02:00:06Z"
    assert fill["payload"]["fill_price"] == 1.0
    assert (
        store.complete_observation(
            "experiment-1",
            observation_key=key,
            manifest=manifest,
            artifact_ref="evidence/hidden_batch/a.json",
            artifact_sha256="a" * 64,
            artifact_received_at_utc="2026-09-01T02:00:06Z",
            crossing_arm_ids=[("1" * 64, "baseline")],
        )
        == complete
    )
    with pytest.raises(ExperimentStoreError) as exc_info:
        store.complete_observation(
            "experiment-1",
            observation_key=key,
            manifest=manifest,
            artifact_ref="evidence/hidden_batch/a.json",
            artifact_sha256="a" * 64,
            artifact_received_at_utc="2026-09-01T02:00:06Z",
            crossing_arm_ids=[],
        )
    assert exc_info.value.reason_code == "validation_batch_manifest_conflict"
    transitioned = store.complete_validation_collection(
        "experiment-1",
        expected_revision=confirmed["revision"],
        actor="strategy-lab-advance",
        occurred_at_utc="2026-09-01T08:00:00Z",
    )
    assert transitioned["state"] == "waiting_outcome"
    assert (
        store.complete_validation_collection(
            "experiment-1",
            expected_revision=0,
            actor="retry",
            occurred_at_utc="2026-09-02T08:00:00Z",
        )
        == transitioned
    )


def test_store_terminal_fill_conflict_rolls_back_batch_and_all_crossings(tmp_path: Path) -> None:
    plan = {"experiment_id": "experiment-1"}
    store, _confirmed = _validation_store(tmp_path, plan)
    manifest = _manifest(plan)
    key = f"hidden_batch:2026-09-01:{SLOT}"
    store.start_observation("experiment-1", observation_key=key, manifest=manifest, created_at_utc=NOW)
    store.put_observation(
        "experiment-1",
        observation_key=f"validation_fill:{'1' * 64}:challenger_0.002",
        recommendation_point_id="1" * 64,
        arm_id="challenger_0.002",
        kind="validation_fill",
        status="not_evaluable",
        payload={"status": "not_evaluable"},
        artifact_ref="evidence/validation_fill/existing.json",
        artifact_sha256="b" * 64,
        created_at_utc=NOW,
    )
    with pytest.raises(ExperimentStoreError) as exc_info:
        store.complete_observation(
            "experiment-1",
            observation_key=key,
            manifest=manifest,
            artifact_ref="evidence/hidden_batch/a.json",
            artifact_sha256="a" * 64,
            artifact_received_at_utc="2026-09-01T02:00:06Z",
            crossing_arm_ids=[("1" * 64, "baseline"), ("1" * 64, "challenger_0.002")],
        )
    assert exc_info.value.reason_code == "validation_batch_manifest_conflict"
    observations = store.list_observations("experiment-1")
    assert next(item for item in observations if item["kind"] == "hidden_batch")["status"] == "started"
    assert not any(item["observation_key"] == f"validation_fill:{'1' * 64}:baseline" for item in observations)


def test_store_rejects_hidden_batch_over_absolute_code_ceiling(tmp_path: Path) -> None:
    plan = {"experiment_id": "experiment-1"}
    store, _confirmed = _validation_store(tmp_path, plan)
    manifest = _manifest(plan)
    manifest["option_codes"] = [f"HK.{index:08d}" for index in range(201)]
    manifest["arms"] = [
        {
            "recommendation_point_id": "1" * 64,
            "arm_id": f"arm-{index}",
            "code": code,
            "sell_limit": 1.0,
        }
        for index, code in enumerate(manifest["option_codes"])
    ]
    with pytest.raises(ExperimentStoreError) as exc_info:
        store.start_observation(
            "experiment-1",
            observation_key=f"hidden_batch:2026-09-01:{SLOT}",
            manifest=manifest,
            created_at_utc=NOW,
        )
    assert exc_info.value.reason_code == "validation_snapshot_batch_limit_exceeded"


def test_snapshot_uses_same_row_positive_bid_and_raw_volume() -> None:
    manifest = _manifest({"experiment_id": "experiment-1"})
    payload = normalize_hidden_snapshot(manifest, _snapshot())
    artifact = _hidden_artifact(manifest, payload)
    assert hidden_snapshot_crossings(manifest, artifact) == [("1" * 64, "baseline")]
    assert payload["observed_at_utc"] == "2026-09-01T02:00:04Z"

    invalid = normalize_hidden_snapshot(manifest, _snapshot(bid_vol=0))
    assert hidden_snapshot_crossings(manifest, _hidden_artifact(manifest, invalid)) == []

    shared_contract = {
        **manifest,
        "arms": [
            *manifest["arms"],
            {
                "recommendation_point_id": "2" * 64,
                "arm_id": "baseline",
                "code": "HK.80000001",
                "sell_limit": 1.05,
            },
        ],
    }
    shared_payload = normalize_hidden_snapshot(shared_contract, _snapshot())
    assert hidden_snapshot_crossings(shared_contract, _hidden_artifact(shared_contract, shared_payload)) == [
        ("1" * 64, "baseline"),
        ("2" * 64, "baseline"),
    ]


def test_validation_fill_projects_no_fill_or_missing_slot() -> None:
    point = {
        "recommendation_point_id": "1" * 64,
        "validation_plan_sha256": "2" * 64,
        "evaluator_behavior_sha256": "3" * 64,
        "formal_point_ref": "formal.json.gz",
        "formal_point_sha256": "4" * 64,
        "session_endpoint_utc": "2026-09-01T02:02:00Z",
        "active_slots_utc": [SLOT, "2026-09-01T02:01:00Z"],
        "arms": [{"arm_id": "baseline", "provider_code": "HK.80000001"}],
    }
    manifest = _manifest({"experiment_id": "experiment-1"})
    manifest["arms"] = [manifest["arms"][0]]
    payload = normalize_hidden_snapshot(manifest, _snapshot(bid=0.9))
    artifact = _hidden_artifact(manifest, payload)
    batch = {
        "kind": "hidden_batch",
        "status": "complete",
        "observation_key": f"hidden_batch:2026-09-01:{SLOT}",
        "observation_slot_utc": SLOT,
        "payload": manifest,
        "artifact_ref": artifact["artifact_ref"],
        "artifact_sha256": artifact["artifact_sha256"],
    }
    no_fill = build_validation_fill_evidence(
        {**point, "active_slots_utc": [SLOT]},
        "baseline",
        [batch],
        {batch["observation_key"]: artifact},
    )
    assert no_fill["payload"] == {"status": "no_fill"}
    missing = build_validation_fill_evidence(
        point,
        "baseline",
        [batch],
        {batch["observation_key"]: artifact},
    )
    assert missing["payload"]["status"] == "not_evaluable"
    assert missing["payload"]["missing_slots_utc"] == ["2026-09-01T02:01:00Z"]


@pytest.mark.parametrize(
    ("local_time", "expected_protected"),
    [
        ("09:59:39", False),
        ("09:59:40", True),
        ("10:00:00", True),
        ("10:00:00.001", True),
        ("10:00:19.999", True),
        ("10:00:20", True),
        ("10:00:20.001", False),
    ],
)
def test_tick_target_owner_supports_symmetric_inclusive_guard(local_time: str, expected_protected: bool) -> None:
    occurred = datetime.fromisoformat(f"2026-09-01T{local_time}+08:00").astimezone(timezone.utc)
    target = next_systemd_tick_target_utc("hk", occurred - timedelta(seconds=20))
    assert (abs((target - occurred).total_seconds()) <= 20) is expected_protected


def test_native_systemd_admission_requires_all_exact_markers() -> None:
    env = {"INVOCATION_ID": "a" * 32, "SYSTEMD_EXEC_PID": "123"}
    cgroup = "0::/system.slice/options-monitor-strategy-lab-advance.service\n"
    assert _is_bounded_systemd_invocation(env, 123, cgroup) is True
    assert _is_bounded_systemd_invocation({}, 123, cgroup) is False
    assert _is_bounded_systemd_invocation(env, 124, cgroup) is False
    assert _is_bounded_systemd_invocation(env, 123, cgroup.strip() + ".scope") is False


@pytest.mark.parametrize("scheduled", [False, True])
def test_advance_cli_freezes_one_clock_and_only_native_scheduled_is_provider_capable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scheduled: bool
) -> None:
    import src.interfaces.cli.strategy_lab_ops as cli

    calls = 0
    received: dict[str, object] = {}

    def now() -> str:
        nonlocal calls
        calls += 1
        return NOW

    monkeypatch.setattr(cli, "_now_utc", now)
    monkeypatch.setattr(cli, "load_service_profile", lambda _path: {})
    monkeypatch.setattr(cli, "resolve_strategy_lab_context", lambda _profile: {"context": 1})
    monkeypatch.setattr(cli, "_is_bounded_systemd_invocation", lambda: True)
    monkeypatch.setattr(
        cli,
        "advance_experiment",
        lambda context, experiment_id, **kwargs: (
            received.update(context=context, experiment_id=experiment_id, **kwargs) or {"status": "progress"}
        ),
    )
    monkeypatch.setattr(
        cli,
        "advance_scheduled",
        lambda context, **kwargs: received.update(context=context, **kwargs) or {"status": "progress"},
    )
    target = ["--scheduled"] if scheduled else ["--experiment-id", "experiment-1"]
    response = handle_strategy_lab_command(
        parse_args(
            [
                "strategy-lab",
                "advance",
                "--profile-path",
                str(tmp_path / "profile.json"),
                *target,
            ]
        )
    )
    assert response["ok"] is True
    assert calls == 1
    assert received["occurred_at_utc"] == NOW
    assert received["provider_capable"] is scheduled


def test_provider_batch_starts_before_one_zero_wait_snapshot_and_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.application.strategy_lab.service as service

    plan = {
        "experiment_id": "experiment-1",
        "provider_source": {"opend_binding": {"host": "127.0.0.1", "port": 11111}},
        "hidden_snapshot_batch_ceiling": 200,
        "validation_wake_tolerance_seconds": 20,
        "evaluator_behavior_sha256": "f" * 64,
    }
    store, experiment = _validation_store(tmp_path, plan)
    _put_available_validation_point(store, plan)
    manifest = _manifest(plan)
    context = {
        "artifact_root": tmp_path / "artifacts",
        "opend_limiter_root": tmp_path / "runtime",
        "opend_binding": {"host": "127.0.0.1", "port": 11111},
        "tick_markets": ("hk",),
        "tick_lock_paths": (tmp_path / "tick.lock",),
    }
    seen: list[str] = []

    class Gateway:
        def close(self) -> None:
            seen.append("closed")

    monkeypatch.setattr(service, "_tick_guard", lambda *_args: None)
    monkeypatch.setattr(service, "build_futu_gateway", lambda **_kwargs: Gateway())

    def fetch(**kwargs: object) -> MarketSnapshotFetchResult:
        started = store.list_observations("experiment-1", kind="hidden_batch")
        assert len(started) == 1 and started[0]["status"] == "started"
        assert kwargs["snapshot_limit"].max_wait_sec == 0
        assert kwargs["snapshot_fallback_max_codes"] == 0
        assert kwargs["no_retry"] is True
        assert kwargs["rate_limited_call"](call=lambda: "direct") == "direct"
        seen.append("snapshot")
        return _snapshot()

    monkeypatch.setattr(service, "fetch_option_snapshots", fetch)
    monkeypatch.setattr(
        service,
        "try_low_priority_opend_call",
        lambda **kwargs: (seen.append("admitted"), kwargs["call"]())[1],
    )
    result = service._advance_current_batch(
        context,
        store,
        experiment,
        plan,
        manifest,
        SOURCE,
        {SOURCE},
        OpenDEndpointRateLimit(30.0, 60, 0.0),
        provider_capable=True,
        occurred_at_utc=NOW,
    )
    assert result["provider_logical_units"] == 1
    assert seen == ["admitted", "snapshot", "closed"]
    observations = store.list_observations("experiment-1")
    assert next(item for item in observations if item["kind"] == "hidden_batch")["status"] == "complete"
    assert [item["status"] for item in observations if item["kind"] == "validation_fill"] == ["observed_fill"]


def test_direct_advance_stops_before_started_row_or_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.application.strategy_lab.service as service

    plan = {
        "experiment_id": "experiment-1",
        "provider_source": {"provider": "futu_opend"},
        "hidden_snapshot_batch_ceiling": 200,
        "validation_wake_tolerance_seconds": 20,
        "evaluator_behavior_sha256": "f" * 64,
    }
    store, experiment = _validation_store(tmp_path, plan)
    _put_available_validation_point(store, plan)
    manifest = _manifest(plan)
    monkeypatch.setattr(
        service,
        "build_futu_gateway",
        lambda **_kwargs: pytest.fail("recovery-only advance must not build a gateway"),
    )
    result = service._advance_current_batch(
        {
            "artifact_root": tmp_path / "artifacts",
            "opend_limiter_root": tmp_path / "runtime",
            "opend_binding": {"host": "127.0.0.1", "port": 11111},
            "tick_markets": ("hk",),
            "tick_lock_paths": (tmp_path / "tick.lock",),
        },
        store,
        experiment,
        plan,
        manifest,
        SOURCE,
        {SOURCE},
        OpenDEndpointRateLimit(30.0, 60, 0.0),
        provider_capable=False,
        occurred_at_utc=NOW,
    )
    assert result == {
        "status": "blocked",
        "reason_code": "advance_external_timeout_required",
    }
    assert store.list_observations("experiment-1", kind="hidden_batch") == []


def test_started_batch_binds_artifact_from_audited_prior_source_commit(
    tmp_path: Path,
) -> None:
    import src.application.strategy_lab.service as service

    plan = {"experiment_id": "experiment-1"}
    store, experiment = _validation_store(tmp_path, plan)
    manifest = _manifest(plan)
    key = f"hidden_batch:2026-09-01:{SLOT}"
    store.start_observation("experiment-1", observation_key=key, manifest=manifest, created_at_utc=NOW)
    digest = canonical_sha256(manifest)
    payload = normalize_hidden_snapshot(manifest, _snapshot())
    publish_evidence_artifact(
        tmp_path / "artifacts",
        "hidden_batch",
        digest,
        payload,
        query=manifest,
        observed_at_utc=payload["observed_at_utc"],
        producer_source_commit_sha=SOURCE,
    )
    result = service._advance_current_batch(
        {
            "artifact_root": tmp_path / "artifacts",
            "opend_limiter_root": tmp_path / "runtime",
            "opend_binding": {"host": "127.0.0.1", "port": 11111},
            "tick_markets": ("hk",),
            "tick_lock_paths": (tmp_path / "tick.lock",),
        },
        store,
        experiment,
        plan,
        manifest,
        "c" * 40,
        {SOURCE, "c" * 40},
        OpenDEndpointRateLimit(30.0, 60, 0.0),
        provider_capable=False,
        occurred_at_utc="2026-09-01T02:00:21Z",
    )
    assert result["reason_code"] == "validation_batch_recovered"
    assert store.list_observations("experiment-1", kind="hidden_batch")[0]["status"] == "complete"


@pytest.mark.parametrize(
    "invalid",
    [
        replace(_snapshot(), opend_call_count=2),
        replace(
            _snapshot(),
            errors=[{"stage": "market_snapshot", "error_code": "PROVIDER_ERROR"}],
        ),
        replace(_snapshot(), received_at_utc="2026-09-01T02:00:21+00:00"),
    ],
)
def test_snapshot_rejects_invalid_provider_envelope(
    invalid: MarketSnapshotFetchResult,
) -> None:
    with pytest.raises(StrategyLabEvidenceError) as exc_info:
        normalize_hidden_snapshot(_manifest({"experiment_id": "experiment-1"}), invalid)
    assert exc_info.value.reason_code == "validation_snapshot_invalid"


def test_snapshot_keeps_missing_contract_as_invalid_row() -> None:
    result = replace(
        _snapshot(),
        snap_map={},
        returned_codes=frozenset(),
        missing_codes=frozenset({"HK.80000001"}),
        complete=False,
    )
    payload = normalize_hidden_snapshot(_manifest({"experiment_id": "experiment-1"}), result)
    assert payload["quotes"] == [
        {
            "code": "HK.80000001",
            "status": "not_evaluable",
            "reason_code": "validation_snapshot_invalid",
        }
    ]


def test_recovery_leaves_started_batch_without_artifact(tmp_path: Path) -> None:
    import src.application.strategy_lab.service as service

    plan = {"experiment_id": "experiment-1"}
    store, experiment = _validation_store(tmp_path, plan)
    manifest = _manifest(plan)
    store.start_observation(
        "experiment-1",
        observation_key=f"hidden_batch:2026-09-01:{SLOT}",
        manifest=manifest,
        created_at_utc=NOW,
    )
    assert service._recover_started_batches({"artifact_root": tmp_path / "artifacts"}, store, experiment, {SOURCE}) == 0
    rows = store.list_observations("experiment-1")
    assert [(row["kind"], row["status"]) for row in rows] == [("hidden_batch", "started")]


def test_tick_busy_has_priority_over_calendar_protection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.application.strategy_lab.service as service

    monkeypatch.setattr(service, "tick_cron_is_busy", lambda _path: True)
    monkeypatch.setattr(
        service,
        "next_systemd_tick_target_utc",
        lambda *_args: pytest.fail("busy Tick must short-circuit calendar calculation"),
    )
    assert (
        service._tick_guard(
            {"tick_markets": ("hk",), "tick_lock_paths": (tmp_path / "tick.lock",)},
            "2026-09-01T02:00:00Z",
        )
        == "tick_busy"
    )


@pytest.mark.parametrize("minute", [10, 20, 30, 40, 50])
def test_history_provider_guard_yields_at_each_late_hk_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, minute: int
) -> None:
    import src.application.strategy_lab.service as service

    monkeypatch.setattr(service, "tick_cron_is_busy", lambda _path: False)
    assert (
        service._provider_guard(
            {"tick_markets": ("hk",), "tick_lock_paths": (tmp_path / "tick.lock",)},
            f"2026-09-01T08:{minute:02d}:00Z",
        )
        == "tick_protection_window"
    )


def test_history_provider_guard_allows_first_post_tick_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.application.strategy_lab.service as service

    monkeypatch.setattr(service, "tick_cron_is_busy", lambda _path: False)
    assert (
        service._provider_guard(
            {"tick_markets": ("hk",), "tick_lock_paths": (tmp_path / "tick.lock",)},
            "2026-09-01T09:00:00Z",
        )
        is None
    )


def test_history_provider_guard_yields_to_friday_us_tick_on_hk_saturday(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.application.strategy_lab.service as service

    monkeypatch.setattr(service, "tick_cron_is_busy", lambda _path: False)
    assert (
        service._provider_guard(
            {"tick_markets": ("hk", "us"), "tick_lock_paths": (tmp_path / "hk.lock", tmp_path / "us.lock")},
            "2026-09-04T17:00:00Z",
        )
        == "tick_protection_window"
    )


def test_real_tick_lock_blocks_batch_before_store_or_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl

    import src.application.strategy_lab.service as service

    plan = {
        "experiment_id": "experiment-1",
        "provider_source": {"provider": "futu_opend"},
        "hidden_snapshot_batch_ceiling": 200,
    }
    store, experiment = _validation_store(tmp_path, plan)
    lock_path = tmp_path / "tick.lock"
    monkeypatch.setattr(
        service,
        "build_futu_gateway",
        lambda **_kwargs: pytest.fail("busy Tick must block the provider"),
    )
    with lock_path.open("a+", encoding="utf-8") as tick_lock:
        fcntl.flock(tick_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = service._advance_current_batch(
            {
                "artifact_root": tmp_path / "artifacts",
                "opend_limiter_root": tmp_path / "runtime",
                "opend_binding": {"host": "127.0.0.1", "port": 11111},
                "tick_markets": ("hk",),
                "tick_lock_paths": (lock_path,),
            },
            store,
            experiment,
            plan,
            _manifest(plan),
            SOURCE,
            {SOURCE},
            OpenDEndpointRateLimit(30.0, 60, 0.0),
            provider_capable=True,
            occurred_at_utc=NOW,
        )

    assert result == {"status": "blocked", "reason_code": "tick_busy"}
    assert store.list_observations("experiment-1", kind="hidden_batch") == []


def test_tick_runs_while_strategy_lab_provider_is_in_flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import subprocess
    import threading

    import src.application.strategy_lab.service as service
    from src.application.tick_cron import run_tick_cron

    plan = {
        "experiment_id": "experiment-1",
        "provider_source": {"provider": "futu_opend"},
        "hidden_snapshot_batch_ceiling": 200,
    }
    store, experiment = _validation_store(tmp_path, plan)
    manifest = _manifest(plan)
    manifest["observation_slot_utc"] = "2026-09-01T02:01:00Z"
    manifest["deadline_utc"] = "2026-09-01T02:01:20Z"
    provider_started = threading.Event()
    provider_finish = threading.Event()
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []
    tick_calls: list[list[str]] = []
    lock_path = tmp_path / "tick.lock"

    class Gateway:
        def close(self) -> None:
            pass

    def fetch(**_kwargs: object) -> MarketSnapshotFetchResult:
        provider_started.set()
        if not provider_finish.wait(2):
            raise AssertionError("provider test did not release")
        return replace(
            _snapshot(),
            requested_at_utc="2026-09-01T02:01:03+00:00",
            received_at_utc="2026-09-01T02:01:04+00:00",
        )

    monkeypatch.setattr(service, "build_futu_gateway", lambda **_kwargs: Gateway())
    monkeypatch.setattr(service, "fetch_option_snapshots", fetch)
    monkeypatch.setattr(
        service,
        "try_low_priority_opend_call",
        lambda **kwargs: kwargs["call"](),
    )

    def run_lab() -> None:
        try:
            results.append(
                service._advance_current_batch(
                    {
                        "artifact_root": tmp_path / "artifacts",
                        "opend_limiter_root": tmp_path / "runtime",
                        "opend_binding": {"host": "127.0.0.1", "port": 11111},
                        "tick_markets": ("hk",),
                        "tick_lock_paths": (lock_path,),
                    },
                    store,
                    experiment,
                    plan,
                    manifest,
                    SOURCE,
                    {SOURCE},
                    OpenDEndpointRateLimit(30.0, 60, 0.0),
                    provider_capable=True,
                    occurred_at_utc="2026-09-01T02:01:05Z",
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    lab = threading.Thread(target=run_lab)
    lab.start()
    assert provider_started.wait(2), errors
    stdout = io.StringIO()
    try:
        rc = run_tick_cron(
            market="hk",
            lock_path=str(lock_path),
            run_cmd=lambda command, **_kwargs: (
                tick_calls.append(command),
                subprocess.CompletedProcess(command, 0),
            )[1],
            preflight_config_fn=None,
            seal_formal_expectations_fn=None,
            stdout=stdout,
            environ={},
        )
    finally:
        provider_finish.set()
        lab.join(2)

    assert rc == 0
    assert tick_calls
    assert "SKIP_LOCKED" not in stdout.getvalue()
    assert not lab.is_alive()
    assert errors == []
    assert results == [{"status": "progress", "provider_logical_units": 1}]


def test_public_advance_busy_does_not_enter_business_logic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.application.strategy_lab.service as service

    context = {"artifact_root": tmp_path / "artifacts"}
    monkeypatch.setattr(
        service,
        "_advance_experiment_once",
        lambda *_args, **_kwargs: pytest.fail("busy advance must not enter Store or provider logic"),
    )

    def busy_lock(*_args: object, **_kwargs: object) -> None:
        raise BlockingIOError

    monkeypatch.setattr(service, "exclusive_private_file_lock", busy_lock)
    result = service.advance_experiment(
        context,
        "experiment-1",
        occurred_at_utc=NOW,
        provider_capable=True,
    )
    assert result == {
        "status": "progress",
        "reason_code": "validation_advance_busy",
        "experiment_id": "experiment-1",
    }


@pytest.mark.parametrize(
    ("failure", "reason_code"),
    [
        (StrategyLabEvidenceError("opend_low_priority_deferred", "busy"), "opend_low_priority_deferred"),
        (StrategyLabEvidenceError("research_provider_failed", "failed"), "research_provider_failed"),
        (FutuGatewayError("failed"), "research_provider_failed"),
    ],
)
def test_validation_outcome_transient_failure_is_retryable_without_store_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    reason_code: str,
) -> None:
    import src.application.strategy_lab.service as service

    point_id = "1" * 64
    timer_binding = service.build_strategy_lab_timer_binding()
    plan = {
        "experiment_id": "experiment-1",
        "provider_source": {"provider": "futu_opend"},
        "market_calendar": {
            "sessions": [
                {
                    "trading_date": "2026-09-01",
                    "expected_recommendation_point_ids": [point_id],
                }
            ]
        },
        "hidden_snapshot_batch_ceiling": service.HIDDEN_SNAPSHOT_BATCH_CEILING,
        "validation_wake_tolerance_seconds": service.VALIDATION_WAKE_TOLERANCE_SECONDS,
        "tick_protection_seconds": service.TICK_PROTECTION_SECONDS,
        "timer_binding": timer_binding,
        "timer_binding_sha256": canonical_sha256(timer_binding),
    }
    store, confirmed = _validation_store(
        tmp_path,
        plan,
        spec={
            "recipe_id": "sell_put_option_position_concentration",
            "fee_plan": {"receipt": {}},
        },
    )
    store.put_observation(
        "experiment-1",
        observation_key=f"validation_point:{point_id}",
        recommendation_point_id=point_id,
        kind="validation_point",
        status="available",
        payload={
            "status": "available",
            "trading_day": "2026-09-01",
            "recommendation_point_id": point_id,
            "formal_point_ref": f"formal/{point_id}.json.gz",
            "formal_point_sha256": "4" * 64,
            "formal_point_content_sha256": "5" * 64,
            "opening_fx_binding": {},
            "arms": [
                {
                    "arm_id": "baseline",
                    "candidate": {"expiration": "2026-09-01", "currency": "HKD"},
                }
            ],
        },
        artifact_ref=f"formal/{point_id}.json.gz",
        artifact_sha256="4" * 64,
        created_at_utc=NOW,
    )
    store.put_observation(
        "experiment-1",
        observation_key=f"validation_fill:{point_id}:baseline",
        recommendation_point_id=point_id,
        arm_id="baseline",
        kind="validation_fill",
        status="observed_fill",
        payload={
            "status": "observed_fill",
            "fill_price": 1.0,
            "fill_evidence_ref": {
                "artifact_ref": "evidence/fill.json",
                "artifact_sha256": "6" * 64,
            },
        },
        artifact_ref="evidence/fill.json",
        artifact_sha256="6" * 64,
        created_at_utc=NOW,
    )
    waiting = store.complete_validation_collection(
        "experiment-1",
        expected_revision=confirmed["revision"],
        actor="tester",
        occurred_at_utc="2026-09-02T08:00:00Z",
    )
    before = store.list_observations("experiment-1")
    monkeypatch.setattr(service, "_current_behavior", lambda *_args: "frozen")
    monkeypatch.setattr(service, "_provider_guard", lambda *_args: None)
    monkeypatch.setattr(service, "_current_validation_config", lambda *_args: None)
    monkeypatch.setattr(service, "source_commit_sha", lambda _path: SOURCE)
    monkeypatch.setattr(service, "resolve_terminal_fx_binding", lambda *_args, **_kwargs: ({}, None))
    monkeypatch.setattr(
        service,
        "build_expiry_close_query",
        lambda *_args, **_kwargs: {"fee_plan": {}},
    )
    monkeypatch.setattr(service, "load_runtime_config", lambda **_kwargs: (Path("config"), {}))
    monkeypatch.setattr(
        service,
        "resolve_opend_fetch_limits",
        lambda _config: SimpleNamespace(history_kline=SimpleNamespace(window_sec=30, max_calls=20)),
    )
    monkeypatch.setattr(
        service,
        "build_futu_gateway",
        lambda **_kwargs: SimpleNamespace(close=lambda: None),
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(service, "resolve_expiry_outcome", fail)
    result = service._advance_waiting_outcome(
        {
            "store_path": tmp_path / "experiments.sqlite3",
            "artifact_root": tmp_path / "artifacts",
            "opend_limiter_root": tmp_path / "runtime",
            "repo_root": tmp_path,
            "config_hk": tmp_path / "config.hk.json",
            "opend_binding": {"host": "127.0.0.1", "port": 11111},
        },
        store,
        waiting,
        occurred_at_utc="2026-09-12T08:00:00Z",
        provider_capable=True,
    )

    assert result["reason_code"] == reason_code
    assert store.list_observations("experiment-1") == before


@pytest.mark.parametrize(
    ("case", "conclusion"),
    [
        ("complete", "challenger_passed"),
        ("missing", "insufficient_evidence"),
        ("challenger_not_passed", "keep_baseline"),
    ],
)
def test_frozen_ten_day_validation_reaches_each_final_conclusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    conclusion: str,
) -> None:
    import src.application.strategy_lab.service as service

    sessions = [
        {
            "trading_date": f"2026-09-{day:02d}",
            "expected_recommendation_point_ids": [f"{day:064x}"],
        }
        for day in range(1, 11)
    ]
    timer_binding = service.build_strategy_lab_timer_binding()
    plan = {
        "experiment_id": "experiment-1",
        "provider_source": {"provider": "futu_opend"},
        "market_calendar": {"sessions": sessions},
        "hidden_snapshot_batch_ceiling": service.HIDDEN_SNAPSHOT_BATCH_CEILING,
        "validation_wake_tolerance_seconds": service.VALIDATION_WAKE_TOLERANCE_SECONDS,
        "tick_protection_seconds": service.TICK_PROTECTION_SECONDS,
        "timer_binding": timer_binding,
        "timer_binding_sha256": canonical_sha256(timer_binding),
    }
    research_result_keys = []
    research_observations = []
    for arm_id in ("baseline", "challenger_0.002"):
        key = f"single_result:{'f' * 64}:{arm_id}"
        research_result_keys.append(key)
        research_observations.append(
            {
                "observation_key": key,
                "recommendation_point_id": "f" * 64,
                "arm_id": arm_id,
                "kind": "single_result",
                "status": "available",
                "payload": {
                    "status": "available",
                    "recommendation_point_id": "f" * 64,
                    "trading_day": "2026-08-01",
                    "arm": "baseline" if arm_id == "baseline" else "challenger",
                    "variant_id": None if arm_id == "baseline" else arm_id,
                },
                "created_at_utc": "2026-08-30T00:00:30Z",
            }
        )
    store, confirmed = _validation_store(
        tmp_path,
        plan,
        research_observations=research_observations,
    )
    fill_status = "not_evaluable" if case == "missing" else "observed_fill"
    terminal_results: list[tuple[str, str, dict[str, object]]] = []
    for session in sessions:
        point_id = session["expected_recommendation_point_ids"][0]
        store.put_observation(
            "experiment-1",
            observation_key=f"validation_point:{point_id}",
            recommendation_point_id=point_id,
            kind="validation_point",
            status="available",
            payload={
                "status": "available",
                "trading_day": session["trading_date"],
                "recommendation_point_id": point_id,
                "formal_point_ref": f"formal/{point_id}.json.gz",
                "formal_point_sha256": "4" * 64,
                "formal_point_content_sha256": "5" * 64,
                "opening_fx_binding": {},
                "arms": [
                    {
                        "kind": arm,
                        "arm_id": arm_id,
                        "near_return_threshold": threshold,
                        "candidate_id": f"{point_id}-{arm_id}",
                        "candidate": {"contract_symbol": "HK.80000001"},
                    }
                    for arm, arm_id, threshold in (
                        ("baseline", "baseline", None),
                        ("challenger", "challenger_0.002", 0.002),
                    )
                ],
            },
            artifact_ref=f"formal/{point_id}.json.gz",
            artifact_sha256="4" * 64,
            created_at_utc=NOW,
        )
        for arm, arm_id, threshold, annualized, pnl in (
            ("baseline", "baseline", None, 0.10, 100.0),
            (
                "challenger",
                "challenger_0.002",
                0.002,
                0.12 if case == "complete" else 0.09,
                101.0,
            ),
        ):
            store.put_observation(
                "experiment-1",
                observation_key=f"validation_fill:{point_id}:{arm_id}",
                recommendation_point_id=point_id,
                arm_id=arm_id,
                kind="validation_fill",
                status=fill_status,
                payload={
                    "status": fill_status,
                    "fill_evidence_ref": {
                        "artifact_ref": f"evidence/fill/{point_id}-{arm_id}.json",
                        "artifact_sha256": "6" * 64,
                    },
                    **(
                        {"reason_code": "validation_snapshot_invalid"}
                        if fill_status == "not_evaluable"
                        else {
                            "fill_price": 1.0,
                            "fill_time": "2026-09-01T02:00:05Z",
                            "quote_evidence_not_broker_execution": True,
                        }
                    ),
                },
                artifact_ref=f"evidence/fill/{point_id}-{arm_id}.json",
                artifact_sha256="6" * 64,
                created_at_utc=NOW,
            )
            if case != "missing":
                terminal_results.append(
                    (
                        point_id,
                        arm_id,
                        {
                            "status": "available",
                            "recommendation_point_id": point_id,
                            "trading_day": session["trading_date"],
                            "arm": arm,
                            "variant_id": None if arm == "baseline" else arm_id,
                            "near_return_threshold": threshold,
                            "candidate_ref": f"{point_id}-{arm_id}",
                            "fill_status": "observed_fill",
                            "outcome_status": "available",
                            "outcome_evidence_ref": {
                                "artifact_ref": f"evidence/outcome/{point_id}-{arm_id}.json",
                                "artifact_sha256": "7" * 64,
                            },
                            "safety_status": "pass",
                            "annualized_return": annualized,
                            "economic_pnl_cny": pnl,
                        },
                    )
                )
    waiting = store.complete_validation_collection(
        "experiment-1",
        expected_revision=confirmed["revision"],
        actor="tester",
        occurred_at_utc="2026-09-11T08:00:00Z",
    )
    assert waiting["state"] == "waiting_outcome"
    for point_id, arm_id, payload in terminal_results:
        outcome_query = {
            "provider_source": {"provider": "futu_opend"},
            "validation_plan_sha256": canonical_sha256(plan),
            "formal_point_ref": f"formal/{point_id}.json.gz",
            "formal_point_sha256": "4" * 64,
            "evaluator_behavior_sha256": canonical_sha256([{"path": "owner.py", "sha256": "a" * 64}]),
            "arm_id": arm_id,
        }
        outcome_sha = canonical_sha256(outcome_query)
        store.put_observation(
            "experiment-1",
            observation_key=f"expiry_close_query:{outcome_sha}",
            kind="expiry_close_query",
            status="available",
            payload={
                "query": outcome_query,
                "query_sha256": outcome_sha,
                "payload": {"status": "available"},
            },
            artifact_ref=payload["outcome_evidence_ref"]["artifact_ref"],
            artifact_sha256=payload["outcome_evidence_ref"]["artifact_sha256"],
            created_at_utc="2026-09-12T06:00:00Z",
        )
        store.put_observation(
            "experiment-1",
            observation_key=f"single_result:{point_id}:{arm_id}",
            recommendation_point_id=point_id,
            arm_id=arm_id,
            kind="single_result",
            status="available",
            payload=payload,
            created_at_utc="2026-09-12T07:00:00Z",
        )

    context = {
        "store_path": tmp_path / "experiments.sqlite3",
        "artifact_root": tmp_path / "artifacts",
        "repo_root": tmp_path,
    }
    monkeypatch.setattr(service, "_current_behavior", lambda *_args: "frozen")
    monkeypatch.setattr(
        service,
        "_current_validation_config",
        lambda *_args: pytest.fail("durable terminal evidence must not read live config"),
    )
    monkeypatch.setattr(
        service,
        "source_commit_sha",
        lambda _path: pytest.fail("durable terminal evidence must not require a current commit"),
    )

    result = service.advance_experiment(
        context,
        "experiment-1",
        occurred_at_utc="2026-09-12T08:00:00Z",
    )
    receipt = service.read_receipt(context, "experiment-1", kind="final")["receipt"]

    assert result["experiment"]["state"] == "completed"
    assert receipt["conclusion"] == conclusion
    assert receipt["safety_status"] == ("not_evaluable" if conclusion == "insufficient_evidence" else "pass")
    assert set(receipt["confirmations"]) == {
        "research_confirmed",
        "validation_confirmed",
    }
    assert len(receipt["validation_window"]["sessions"]) == 10
    assert len(receipt["comparison"]["daily_aggregates"]) == (0 if conclusion == "insufficient_evidence" else 10)
    assert sum(item["kind"] == "expiry_close_query" for item in receipt["terminal_observations"]) == (
        0 if conclusion == "insufficient_evidence" else 20
    )
    assert not set(research_result_keys) & {item["observation_key"] for item in receipt["terminal_observations"]}
    assert receipt["declarations"]["experimental_improvement_is_not_realized_online_profit"] is True
    assert service.advance_experiment(
        context,
        "experiment-1",
        occurred_at_utc="2026-09-13T08:00:00Z",
    ) == {"status": "complete", "experiment": result["experiment"]}
