from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.opend_fetch_config import OpenDEndpointRateLimit
from src.application.opend_market_snapshot_fetching import MarketSnapshotFetchResult
from src.application.service_deploy import next_systemd_tick_target_utc
from src.application.strategy_lab.evidence import (
    build_hidden_batch_manifest,
    build_validation_fill_evidence,
    build_validation_point_evidence,
    hidden_quote_rows,
    normalize_hidden_snapshot,
    publish_evidence_artifact,
)
from src.infrastructure.private_storage import exclusive_private_file_lock
from src.infrastructure.strategy_lab.experiment_store import (
    ExperimentStore,
    ExperimentStoreError,
)
from src.interfaces.cli.main import parse_args
from src.interfaces.cli.strategy_lab_ops import (
    _is_bounded_systemd_invocation,
    handle_strategy_lab_command,
)


SLOT = "2026-09-01T02:00:00Z"
DEADLINE = "2026-09-01T02:00:20Z"
NOW = "2026-09-01T02:00:05Z"
SOURCE = "b" * 40


def _validation_store(tmp_path: Path, plan: dict[str, object]) -> tuple[ExperimentStore, dict]:
    store = ExperimentStore(tmp_path / "experiments.sqlite3")
    store.initialize()
    spec = {"recipe_id": "sell_put_option_position_concentration"}
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
        leader={"variant_id": "challenger_0.002"},
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


def _service_plan() -> dict[str, object]:
    sessions = []
    for offset in range(10):
        day = (datetime(2026, 9, 1) + timedelta(days=offset)).date().isoformat()
        sessions.append(
            {
                "trading_date": day,
                "minute_grid_utc": [SLOT] if offset == 0 else [],
                "session_endpoint_utc": (
                    "2026-09-01T02:01:00Z" if offset == 0 else f"{day}T02:01:00Z"
                ),
            }
        )
    return {
        "experiment_id": "experiment-1",
        "market_calendar": {"sessions": sessions},
        "validation_wake_tolerance_seconds": 20,
        "hidden_snapshot_batch_ceiling": 200,
        "evaluator_behavior_sha256": "f" * 64,
        "provider_source": {"provider": "futu_opend"},
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
    assert store.put_observation(
        "experiment-1",
        observation_key="validation_point:" + "1" * 64,
        recommendation_point_id="1" * 64,
        kind="validation_point",
        status="not_evaluable",
        payload=payload,
        artifact_ref=artifact_ref,
        artifact_sha256=artifact_sha256,
        created_at_utc="2026-09-01T02:00:10Z",
    ) == first


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


def test_store_completes_batch_and_quotes_atomically_then_transitions(
    tmp_path: Path,
) -> None:
    plan = {"experiment_id": "experiment-1"}
    store, confirmed = _validation_store(tmp_path, plan)
    manifest = _manifest(plan)
    key = f"hidden_batch:2026-09-01:{SLOT}"
    started = store.start_observation(
        "experiment-1", observation_key=key, manifest=manifest, created_at_utc=NOW
    )
    assert started["status"] == "started"
    quotes = [
        {
            "recommendation_point_id": arm["recommendation_point_id"],
            "arm_id": arm["arm_id"],
            "status": "complete",
            "payload": {"bid_price": 0.9},
        }
        for arm in manifest["arms"]
    ]
    complete = store.complete_observation(
        "experiment-1",
        observation_key=key,
        manifest=manifest,
        quotes=quotes,
        artifact_ref="evidence/hidden_batch/a.json",
        artifact_sha256="a" * 64,
        updated_at_utc="2026-09-01T02:00:06Z",
    )
    assert complete["status"] == "complete"
    observations = store.list_observations("experiment-1")
    assert [item["kind"] for item in observations].count("hidden_quote") == 2
    transitioned = store.complete_validation_collection(
        "experiment-1",
        expected_revision=confirmed["revision"],
        actor="strategy-lab-advance",
        occurred_at_utc="2026-09-01T08:00:00Z",
    )
    assert transitioned["state"] == "waiting_outcome"
    assert store.complete_validation_collection(
        "experiment-1",
        expected_revision=0,
        actor="retry",
        occurred_at_utc="2026-09-02T08:00:00Z",
    ) == transitioned


@pytest.mark.parametrize("started", [False, True])
def test_elapsed_batch_gaps_original_manifest_arms_atomically(
    tmp_path: Path, started: bool
) -> None:
    plan = {"experiment_id": "experiment-1"}
    store, _confirmed = _validation_store(tmp_path, plan)
    manifest = _manifest(plan)
    key = f"hidden_batch:2026-09-01:{SLOT}"
    if started:
        store.start_observation(
            "experiment-1", observation_key=key, manifest=manifest, created_at_utc=NOW
        )
        row = store.expire_started_observation(
            "experiment-1",
            observation_key=key,
            manifest=manifest,
            updated_at_utc="2026-09-01T02:00:21Z",
        )
    else:
        row = store.materialize_elapsed_observation_gap(
            "experiment-1",
            observation_key=key,
            manifest=manifest,
            updated_at_utc="2026-09-01T02:00:21Z",
        )
    assert row["status"] == "gap"
    assert {
        item["status"]
        for item in store.list_observations("experiment-1")
        if item["kind"] == "hidden_quote"
    } == {"gap"}


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
    plan = {"experiment_id": "experiment-1"}
    manifest = _manifest(plan)
    payload = normalize_hidden_snapshot(manifest, _snapshot())
    artifact = {
        "artifact_ref": "evidence/hidden_batch/a.json",
        "artifact_sha256": "a" * 64,
        "payload": payload,
    }
    rows = hidden_quote_rows(manifest, artifact)
    assert [row["status"] for row in rows] == ["observed_fill", "complete"]
    assert rows[0]["payload"]["fill_price"] == 1.0
    assert rows[0]["payload"]["bid_vol"] == 3.0
    assert rows[0]["payload"]["quote_evidence_not_broker_execution"] is True

    invalid = normalize_hidden_snapshot(manifest, _snapshot(bid_vol=0))
    invalid_rows = hidden_quote_rows(manifest, {**artifact, "payload": invalid})
    assert {row["status"] for row in invalid_rows} == {"gap"}

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
    shared_rows = hidden_quote_rows(shared_contract, artifact)
    assert len(shared_rows) == 3
    assert {row["recommendation_point_id"] for row in shared_rows} == {
        "1" * 64,
        "2" * 64,
    }


def test_validation_fill_distinguishes_no_fill_and_gap() -> None:
    point = {
        "recommendation_point_id": "1" * 64,
        "validation_plan_sha256": "2" * 64,
        "evaluator_behavior_sha256": "3" * 64,
        "formal_point_ref": "formal.json.gz",
        "formal_point_sha256": "4" * 64,
        "session_endpoint_utc": "2026-09-01T02:02:00Z",
        "active_slots_utc": [SLOT, "2026-09-01T02:01:00Z"],
        "arms": [{"arm_id": "baseline"}],
    }

    def quote(slot: str, status: str) -> dict[str, object]:
        return {
            "observation_key": f"quote:{slot}",
            "recommendation_point_id": "1" * 64,
            "arm_id": "baseline",
            "observation_slot_utc": slot,
            "status": status,
            "payload": {},
            "artifact_ref": None if status == "gap" else f"batch:{slot}",
            "artifact_sha256": None if status == "gap" else "5" * 64,
            "updated_at_utc": slot,
        }

    complete = [quote(slot, "complete") for slot in point["active_slots_utc"]]
    assert build_validation_fill_evidence(point, "baseline", complete)["payload"] == {
        "status": "no_fill"
    }
    complete[-1] = quote(point["active_slots_utc"][-1], "gap")
    assert build_validation_fill_evidence(point, "baseline", complete)["payload"] == {
        "status": "not_evaluable",
        "reason_code": "validation_snapshot_invalid",
    }


def test_publisher_reuses_exact_lock_without_reacquiring(tmp_path: Path) -> None:
    query = {"batch": 1}
    digest = canonical_sha256(query)
    lock = tmp_path / "evidence" / "hidden_batch" / f"{digest}.json.lock"
    with exclusive_private_file_lock(lock, blocking=False):
        published = publish_evidence_artifact(
            tmp_path,
            "hidden_batch",
            digest,
            {"status": "available"},
            query=query,
            observed_at_utc=NOW,
            producer_source_commit_sha=SOURCE,
            lock_held=True,
        )
    assert published["artifact_ref"] == f"evidence/hidden_batch/{digest}.json"


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
def test_tick_target_owner_supports_symmetric_inclusive_guard(
    local_time: str, expected_protected: bool
) -> None:
    occurred = datetime.fromisoformat(f"2026-09-01T{local_time}+08:00").astimezone(
        timezone.utc
    )
    target = next_systemd_tick_target_utc(
        "hk", occurred - timedelta(seconds=20)
    )
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
        lambda context, experiment_id, **kwargs: received.update(
            context=context, experiment_id=experiment_id, **kwargs
        )
        or {"status": "progress"},
    )
    monkeypatch.setattr(
        cli,
        "advance_scheduled",
        lambda context, **kwargs: received.update(context=context, **kwargs)
        or {"status": "progress"},
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
        "tick_lock_path": tmp_path / "tick.lock",
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
    assert next(item for item in observations if item["kind"] == "hidden_batch")[
        "status"
    ] == "complete"
    assert [item["status"] for item in observations if item["kind"] == "hidden_quote"] == [
        "observed_fill"
    ]


def test_direct_advance_stops_before_started_row_or_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            "tick_lock_path": tmp_path / "tick.lock",
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
    store.start_observation(
        "experiment-1", observation_key=key, manifest=manifest, created_at_utc=NOW
    )
    digest = canonical_sha256(manifest)
    publish_evidence_artifact(
        tmp_path / "artifacts",
        "hidden_batch",
        digest,
        normalize_hidden_snapshot(manifest, _snapshot()),
        query=manifest,
        observed_at_utc=NOW,
        producer_source_commit_sha=SOURCE,
    )
    result = service._advance_current_batch(
        {
            "artifact_root": tmp_path / "artifacts",
            "opend_limiter_root": tmp_path / "runtime",
            "opend_binding": {"host": "127.0.0.1", "port": 11111},
            "tick_lock_path": tmp_path / "tick.lock",
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
    assert store.list_observations("experiment-1", kind="hidden_batch")[0][
        "status"
    ] == "complete"


def test_current_day_elapsed_slot_is_materialized_without_provider(tmp_path: Path) -> None:
    import src.application.strategy_lab.service as service

    plan = _service_plan()
    store, experiment = _validation_store(tmp_path, plan)
    _put_available_validation_point(store, plan)
    recovered = service._recover_one_elapsed_day(
        {"artifact_root": tmp_path / "artifacts"},
        store,
        experiment,
        plan,
        {SOURCE},
        "2026-09-01T02:00:21Z",
    )
    assert recovered == "2026-09-01"
    observations = store.list_observations("experiment-1")
    assert next(item for item in observations if item["kind"] == "hidden_batch")[
        "status"
    ] == "gap"
    assert next(item for item in observations if item["kind"] == "hidden_quote")[
        "status"
    ] == "gap"


@pytest.mark.parametrize(
    ("bid", "expected_quote_status", "expected_second_batch", "exact_reads"),
    [(1.1, "observed_fill", None, 1), (0.9, "complete", "gap", 2)],
)
def test_artifact_recovery_updates_crossing_index_for_the_next_elapsed_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bid: float,
    expected_quote_status: str,
    expected_second_batch: str | None,
    exact_reads: int,
) -> None:
    import src.application.strategy_lab.service as service

    second_slot = "2026-09-01T02:01:00Z"
    plan = _service_plan()
    plan["market_calendar"]["sessions"][0]["minute_grid_utc"] = [SLOT, second_slot]
    plan["market_calendar"]["sessions"][0][
        "session_endpoint_utc"
    ] = "2026-09-01T02:02:00Z"
    store, experiment = _validation_store(tmp_path, plan)
    _put_available_validation_point(
        store, plan, active_slots_utc=[SLOT, second_slot]
    )
    first_manifest = build_hidden_batch_manifest(
        plan,
        [
            store.get_observation(
                "experiment-1", "validation_point:" + "1" * 64
            )["payload"]
        ],
        trading_day="2026-09-01",
        observation_slot_utc=SLOT,
    )
    first_key = f"hidden_batch:2026-09-01:{SLOT}"
    store.start_observation(
        "experiment-1",
        observation_key=first_key,
        manifest=first_manifest,
        created_at_utc=NOW,
    )
    digest = canonical_sha256(first_manifest)
    publish_evidence_artifact(
        tmp_path / "artifacts",
        "hidden_batch",
        digest,
        normalize_hidden_snapshot(first_manifest, _snapshot(bid=bid)),
        query=first_manifest,
        observed_at_utc=NOW,
        producer_source_commit_sha=SOURCE,
    )
    original_list = store.list_observations
    original_get = store.get_observation
    counts = {"full": 0, "exact": 0}

    def counted_list(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        counts["full"] += 1
        return original_list(*args, **kwargs)

    def counted_get(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        counts["exact"] += 1
        return original_get(*args, **kwargs)

    monkeypatch.setattr(store, "list_observations", counted_list)
    monkeypatch.setattr(store, "get_observation", counted_get)
    assert service._recover_one_elapsed_day(
        {"artifact_root": tmp_path / "artifacts"},
        store,
        experiment,
        plan,
        {SOURCE},
        "2026-09-01T02:01:21Z",
    ) == "2026-09-01"
    assert counts == {"full": 1, "exact": exact_reads}
    rows = original_list("experiment-1")
    assert next(item for item in rows if item["observation_key"] == first_key)[
        "status"
    ] == "complete"
    assert next(
        item
        for item in rows
        if item["observation_key"].startswith("hidden_quote:")
        and item["observation_slot_utc"] == SLOT
    )["status"] == expected_quote_status
    second = next(
        (
            item
            for item in rows
            if item["observation_key"] == f"hidden_batch:2026-09-01:{second_slot}"
        ),
        None,
    )
    assert (second or {}).get("status") == expected_second_batch


def test_elapsed_recovery_reads_full_observation_set_once_for_ten_by_330_slots(
    tmp_path: Path,
) -> None:
    import src.application.strategy_lab.service as service

    sessions: list[dict[str, object]] = []
    points: list[dict[str, object]] = []
    rows: list[dict[str, object]] = []
    plan: dict[str, object] = {
        "experiment_id": "experiment-1",
        "validation_wake_tolerance_seconds": 20,
        "hidden_snapshot_batch_ceiling": 200,
        "evaluator_behavior_sha256": "f" * 64,
        "provider_source": {"provider": "futu_opend"},
    }
    for day_offset in range(10):
        start = datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(days=day_offset)
        slots = [
            (start + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")
            for minute in range(330)
        ]
        day = start.date().isoformat()
        session = {
            "trading_date": day,
            "minute_grid_utc": slots,
            "session_endpoint_utc": (start + timedelta(minutes=330))
            .isoformat()
            .replace("+00:00", "Z"),
        }
        sessions.append(session)
        point_id = f"{day_offset + 1:064x}"
        point = {
            "status": "available",
            "trading_day": day,
            "recommendation_point_id": point_id,
            "active_slots_utc": slots,
            "arms": [
                {
                    "arm_id": "baseline",
                    "provider_code": "HK.80000001",
                    "sell_limit": 1.0,
                }
            ],
        }
        points.append(point)
        rows.append(
            {
                "observation_key": f"validation_point:{point_id}",
                "kind": "validation_point",
                "status": "available",
                "payload": point,
            }
        )
    plan["market_calendar"] = {"sessions": sessions}
    for session, point in zip(sessions, points, strict=True):
        for slot in session["minute_grid_utc"]:
            manifest = build_hidden_batch_manifest(
                plan,
                [point],
                trading_day=str(session["trading_date"]),
                observation_slot_utc=str(slot),
            )
            rows.append(
                {
                    "observation_key": (
                        f"hidden_batch:{session['trading_date']}:{slot}"
                    ),
                    "kind": "hidden_batch",
                    "status": "complete",
                    "payload": manifest,
                }
            )

    class ReadCountingStore:
        full_reads = 0
        exact_reads = 0
        row_iterations = 0

        def list_observations(self, _experiment_id: str) -> list[dict[str, object]]:
            self.full_reads += 1
            owner = self

            class CountedRows(list[dict[str, object]]):
                def __iter__(self):  # type: ignore[no-untyped-def]
                    for row in super().__iter__():
                        owner.row_iterations += 1
                        yield row

            return CountedRows(rows)

        def get_observation(self, *_args: object) -> None:
            self.exact_reads += 1
            return None

    store = ReadCountingStore()
    assert service._recover_one_elapsed_day(
        {"artifact_root": tmp_path / "artifacts"},
        store,
        {
            "experiment_id": "experiment-1",
            "validation_plan_sha256": canonical_sha256(plan),
        },
        plan,
        {SOURCE},
        "2026-09-11T00:00:00Z",
    ) is None
    assert (store.full_reads, store.exact_reads) == (1, 0)
    assert store.row_iterations <= len(rows) * 4


def test_elapsed_recovery_materializes_hundreds_of_gaps_from_one_fresh_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.application.strategy_lab.service as service

    start = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)
    slots = [
        (start + timedelta(minutes=offset)).isoformat().replace("+00:00", "Z")
        for offset in range(240)
    ]
    sessions = [
        {
            "trading_date": "2026-09-01",
            "minute_grid_utc": slots,
            "session_endpoint_utc": "2026-09-01T06:01:00Z",
        }
    ]
    sessions.extend(
        {
            "trading_date": f"2026-09-{day:02d}",
            "minute_grid_utc": [],
            "session_endpoint_utc": f"2026-09-{day:02d}T06:01:00Z",
        }
        for day in range(2, 11)
    )
    plan = {
        "experiment_id": "experiment-1",
        "market_calendar": {"sessions": sessions},
        "validation_wake_tolerance_seconds": 20,
        "hidden_snapshot_batch_ceiling": 200,
        "evaluator_behavior_sha256": "f" * 64,
        "provider_source": {"provider": "futu_opend"},
    }
    store, experiment = _validation_store(tmp_path, plan)
    point_id = "1" * 64
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
            "active_slots_utc": slots,
            "formal_point_ref": "formal/point.json.gz",
            "formal_point_sha256": "4" * 64,
            "formal_point_content_sha256": "5" * 64,
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
    original_list = store.list_observations
    original_get = store.get_observation
    counts = {"full": 0, "exact": 0}

    def counted_list(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        counts["full"] += 1
        return original_list(*args, **kwargs)

    def counted_get(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        counts["exact"] += 1
        return original_get(*args, **kwargs)

    monkeypatch.setattr(store, "list_observations", counted_list)
    monkeypatch.setattr(store, "get_observation", counted_get)
    assert service._recover_one_elapsed_day(
        {"artifact_root": tmp_path / "artifacts"},
        store,
        experiment,
        plan,
        {SOURCE},
        "2026-09-01T06:01:00Z",
    ) == "2026-09-01"
    assert counts == {"full": 1, "exact": 240}
    batches = original_list("experiment-1", kind="hidden_batch")
    assert len(batches) == 240
    assert {batch["status"] for batch in batches} == {"gap"}


def test_provider_publish_and_late_recovery_cannot_create_gap_with_artifact(
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
        "tick_lock_path": tmp_path / "tick.lock",
    }
    provider_entered = threading.Event()
    release_provider = threading.Event()

    class Gateway:
        def close(self) -> None:
            pass

    monkeypatch.setattr(service, "_tick_guard", lambda *_args: None)
    monkeypatch.setattr(service, "build_futu_gateway", lambda **_kwargs: Gateway())

    def fetch(**_kwargs: object) -> MarketSnapshotFetchResult:
        provider_entered.set()
        assert release_provider.wait(2)
        return _snapshot()

    monkeypatch.setattr(service, "fetch_option_snapshots", fetch)
    monkeypatch.setattr(
        service,
        "try_low_priority_opend_call",
        lambda **kwargs: kwargs["call"](),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        provider = pool.submit(
            service._advance_current_batch,
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
        assert provider_entered.wait(2)
        recovery = pool.submit(
            service._advance_current_batch,
            context,
            store,
            experiment,
            plan,
            manifest,
            SOURCE,
            {SOURCE},
            OpenDEndpointRateLimit(30.0, 60, 0.0),
            provider_capable=False,
            occurred_at_utc="2026-09-01T02:00:21Z",
        )
        assert recovery.result(timeout=2)["reason_code"] == "validation_evidence_busy"
        release_provider.set()
        assert provider.result(timeout=2)["provider_logical_units"] == 1

    observations = store.list_observations("experiment-1")
    assert next(item for item in observations if item["kind"] == "hidden_batch")[
        "status"
    ] == "complete"
    assert not any(item["status"] == "gap" for item in observations)


def test_batch_freeze_reloads_points_after_stale_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    stale_manifest = _manifest(plan)
    context = {
        "artifact_root": tmp_path / "artifacts",
        "opend_limiter_root": tmp_path / "runtime",
        "opend_binding": {"host": "127.0.0.1", "port": 11111},
        "tick_lock_path": tmp_path / "tick.lock",
    }
    point_id = "2" * 64
    with exclusive_private_file_lock(
        service._validation_experiment_lock_path(context, "experiment-1")
    ):
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
                "active_slots_utc": [SLOT],
                "formal_point_ref": "formal/point-2.json.gz",
                "formal_point_sha256": "6" * 64,
                "formal_point_content_sha256": "7" * 64,
                "arms": [
                    {
                        "arm_id": "baseline",
                        "provider_code": "HK.80000002",
                        "sell_limit": 1.1,
                    }
                ],
            },
            artifact_ref="formal/point-2.json.gz",
            artifact_sha256="6" * 64,
            created_at_utc=NOW,
        )

    class Gateway:
        def close(self) -> None:
            pass

    monkeypatch.setattr(service, "_tick_guard", lambda *_args: None)
    monkeypatch.setattr(service, "build_futu_gateway", lambda **_kwargs: Gateway())
    monkeypatch.setattr(
        service,
        "fetch_option_snapshots",
        lambda **_kwargs: MarketSnapshotFetchResult(
            snap_map={
                "HK.80000001": _snapshot().snap_map["HK.80000001"],
                "HK.80000002": {
                    **_snapshot().snap_map["HK.80000001"],
                    "code": "HK.80000002",
                },
            },
            errors=[],
            requested_codes=frozenset({"HK.80000001", "HK.80000002"}),
            returned_codes=frozenset({"HK.80000001", "HK.80000002"}),
            missing_codes=frozenset(),
            unexpected_codes=frozenset(),
            complete=True,
            opend_call_count=1,
            requested_at_utc=NOW,
            received_at_utc=NOW,
        ),
    )
    monkeypatch.setattr(
        service, "try_low_priority_opend_call", lambda **kwargs: kwargs["call"]()
    )
    result = service._advance_current_batch(
        context,
        store,
        experiment,
        plan,
        stale_manifest,
        SOURCE,
        {SOURCE},
        OpenDEndpointRateLimit(30.0, 60, 0.0),
        provider_capable=True,
        occurred_at_utc=NOW,
    )
    assert result["provider_logical_units"] == 1
    batch = store.list_observations("experiment-1", kind="hidden_batch")[0]
    assert {arm["recommendation_point_id"] for arm in batch["payload"]["arms"]} == {
        "1" * 64,
        "2" * 64,
    }


def test_tick_busy_has_priority_over_calendar_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.application.strategy_lab.service as service

    monkeypatch.setattr(service, "tick_cron_is_busy", lambda _path: True)
    monkeypatch.setattr(
        service,
        "next_systemd_tick_target_utc",
        lambda *_args: pytest.fail("busy Tick must short-circuit calendar calculation"),
    )
    assert service._tick_guard(
        {"tick_lock_path": tmp_path / "tick.lock"}, "2026-09-01T02:00:00Z"
    ) == "tick_busy"


def test_public_advance_returns_current_slot_before_historical_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.application.strategy_lab.service as service

    plan = {"experiment_id": "experiment-1"}
    _store, _experiment = _validation_store(tmp_path, plan)
    context = {
        "store_path": tmp_path / "experiments.sqlite3",
        "repo_root": tmp_path,
        "artifact_root": tmp_path / "artifacts",
    }
    monkeypatch.setattr(service, "_validation_plan", lambda _item: plan)
    monkeypatch.setattr(service, "_current_behavior", lambda *_args: "f" * 64)
    monkeypatch.setattr(service, "source_commit_sha", lambda _root: SOURCE)
    monkeypatch.setattr(
        service,
        "_current_validation_config",
        lambda *_args: OpenDEndpointRateLimit(30.0, 60, 0.0),
    )
    monkeypatch.setattr(service, "_bind_validation_points", lambda *_args: None)
    monkeypatch.setattr(
        service, "_current_validation_slot", lambda *_args: ("2026-09-01", SLOT)
    )
    monkeypatch.setattr(service, "_batch_manifest_for", lambda *_args: _manifest(plan))
    monkeypatch.setattr(
        service,
        "_advance_current_batch",
        lambda *_args, **_kwargs: {
            "status": "blocked",
            "reason_code": "opend_low_priority_deferred",
        },
    )
    monkeypatch.setattr(
        service,
        "_recover_one_elapsed_day",
        lambda *_args: pytest.fail("current eligible slot must win over recovery"),
    )
    result = service.advance_experiment(
        context,
        "experiment-1",
        occurred_at_utc=NOW,
        provider_capable=True,
    )
    assert result["reason_code"] == "opend_low_priority_deferred"


def test_public_advance_retries_source_bound_not_evaluable_at_different_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.application.strategy_lab.service as service

    sessions = []
    for offset in range(10):
        day = (datetime(2026, 9, 1) + timedelta(days=offset)).date().isoformat()
        sessions.append(
            {
                "trading_date": day,
                "scheduled_scan_targets_utc": [f"{day}T02:00:00Z"],
                "expected_recommendation_point_ids": [f"{offset + 1:064x}"],
                "minute_grid_utc": [],
                "session_endpoint_utc": f"{day}T08:00:00Z",
            }
        )
    plan = {
        "experiment_id": "experiment-1",
        "market_calendar": {
            "market_calendar_version": "calendar-v1",
            "snapshot_content_sha256": "6" * 64,
            "sessions": sessions,
        },
        "schedule": {"schedule_config_sha256": "7" * 64},
        "validation_wake_tolerance_seconds": 20,
        "hidden_snapshot_batch_ceiling": 200,
        "evaluator_behavior_sha256": "f" * 64,
        "provider_source": {"provider": "futu_opend"},
    }
    store, _experiment = _validation_store(tmp_path, plan)
    context = {
        "store_path": tmp_path / "experiments.sqlite3",
        "repo_root": tmp_path,
        "runtime_root": tmp_path / "runtime",
        "artifact_root": tmp_path / "artifacts",
    }
    monkeypatch.setattr(service, "_validation_plan", lambda _item: plan)
    monkeypatch.setattr(service, "_current_behavior", lambda *_args: "f" * 64)
    monkeypatch.setattr(service, "source_commit_sha", lambda _root: SOURCE)
    monkeypatch.setattr(
        service,
        "_current_validation_config",
        lambda *_args: OpenDEndpointRateLimit(30.0, 60, 0.0),
    )
    monkeypatch.setattr(service, "_current_validation_slot", lambda *_args: None)
    monkeypatch.setattr(service, "_recover_one_elapsed_day", lambda *_args: None)
    monkeypatch.setattr(service, "_derive_validation_fills", lambda *_args: 0)

    def expectation(
        _root: object, *, trading_date: str, **_kwargs: object
    ) -> dict[str, object]:
        if trading_date != "2026-09-01":
            return {"status": "missing"}
        session = sessions[0]
        return {
            "status": "available",
            "expectation": {
                "market": "HK",
                "account": "lx",
                "trading_date": trading_date,
                "market_calendar_version": "calendar-v1",
                "market_calendar_sha256": "6" * 64,
                "schedule_config_sha256": "7" * 64,
                "scheduled_scan_targets_market": session[
                    "scheduled_scan_targets_utc"
                ],
                "expected_recommendation_point_ids": session[
                    "expected_recommendation_point_ids"
                ],
            },
        }

    point_id = str(sessions[0]["expected_recommendation_point_ids"][0])
    monkeypatch.setattr(service, "load_formal_expectation", expectation)
    monkeypatch.setattr(
        service,
        "load_formal_point",
        lambda *_args, **_kwargs: {
            "status": "not_evaluable",
            "reason_code": "formal_point_evidence_missing",
            "artifact_ref": "formal/point.json.gz",
            "artifact_file_sha256": "4" * 64,
            "artifact_content_sha256": "5" * 64,
            "point": {
                "market": "HK",
                "account": "lx",
                "trading_date": "2026-09-01",
                "recommendation_point_id": point_id,
                "content_sha256": "5" * 64,
                "source_binding": {
                    "market": "HK",
                    "account": "lx",
                    "scheduled_scan_target_market": "2026-09-01T02:00:00Z",
                },
            },
        },
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(
                service.advance_experiment,
                context,
                "experiment-1",
                occurred_at_utc=occurred,
            )
            for occurred in (
                "2026-08-31T01:00:00Z",
                "2026-08-31T01:00:01Z",
            )
        ]
        first, concurrent = [call.result(timeout=3) for call in calls]
    before = store.get_observation("experiment-1", f"validation_point:{point_id}")
    second = service.advance_experiment(
        context, "experiment-1", occurred_at_utc="2026-08-31T02:00:00Z"
    )
    assert (first["status"], concurrent["status"], second["status"]) == (
        "progress",
        "progress",
        "progress",
    )
    assert store.get_observation("experiment-1", f"validation_point:{point_id}") == before
    assert before is not None
    assert (before["artifact_ref"], before["artifact_sha256"]) == (
        "formal/point.json.gz",
        "4" * 64,
    )
