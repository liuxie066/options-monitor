from __future__ import annotations

import gzip
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.candidate_snapshot_manifest import load_candidate_snapshot_bundle
from src.application.recommendation_point import (
    capture_scheduled_recommendation_point,
)
from src.application.research.formal_corpus import (
    FormalCorpusError,
    build_corpus_health_receipt,
    capture_formal_point_attempt,
    formal_corpus_present,
    load_formal_expectation,
    seal_formal_day_expectation,
    seal_profile_formal_expectations,
)
from src.infrastructure.private_storage import exclusive_private_file_lock
from src.application.strategy_lab.top1.ranking import (
    build_top1_recipe_projection,
    materialize_top1_recipe_input,
)
from tests.candidate_evidence_helpers import seal_opening_candidate_fixture


def _schedule(*, start_plus_min: int = 10) -> dict[str, object]:
    return {
        "enabled": True,
        "timezone": "Asia/Hong_Kong",
        "run_window": {"start": "09:50", "end": "10:10"},
        "run_points": {"start_plus_min": start_plus_min},
    }


def _seal(
    root: Path,
    *,
    schedule: dict[str, object] | None = None,
    sealed_at_utc: str = "2026-08-26T01:00:00Z",
) -> dict:
    return seal_formal_day_expectation(
        root,
        market="HK",
        account="lx",
        schedule=schedule or _schedule(),
        trading_date="2026-08-26",
        market_calendar_version="fixture.v1",
        market_calendar_sha256="a" * 64,
        sealed_at_utc=sealed_at_utc,
    )


def test_persistent_lock_is_not_a_formal_corpus_artifact(tmp_path: Path) -> None:
    lock = (
        tmp_path
        / "output_shared/research/formal_corpus/v1/hk/lx/.locks/expectations/2026-08-26.lock"
    )
    with exclusive_private_file_lock(lock):
        pass

    assert not formal_corpus_present(tmp_path, market="HK", account="lx")
    _seal(tmp_path)
    assert formal_corpus_present(tmp_path, market="HK", account="lx")


def test_expectation_lock_is_idempotent_and_conflicts_on_denominator_change(
    tmp_path: Path,
) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _seal(tmp_path), range(2)))

    assert {result["status"] for result in results} == {"published", "idempotent"}
    files = list(
        tmp_path.glob(
            "output_shared/research/formal_corpus/v1/hk/lx/expectations/2026-08-26/*.json"
        )
    )
    assert len(files) == 1

    later_retry = _seal(tmp_path, sealed_at_utc="2026-08-26T01:00:30Z")
    assert later_retry["status"] == "idempotent"
    assert later_retry["sealed_at_utc"] == "2026-08-26T01:00:00Z"

    changed = _seal(tmp_path, schedule=_schedule(start_plus_min=11))
    assert changed["status"] == "conflict"
    assert len(list(files[0].parent.glob("*.json"))) == 2
    first = json.loads(files[0].read_text(encoding="utf-8"))
    with pytest.raises(FormalCorpusError) as raised:
        capture_formal_point_attempt(
            tmp_path,
            tmp_path,
            market="HK",
            account="lx",
            trading_date="2026-08-26",
            run_id="conflicted-expectation",
            scheduled_scan_target_market=first[
                "scheduled_scan_targets_market"
            ][0],
            captured_at_utc="2026-08-26T02:00:02Z",
            producer_behavior_version="recommendation_point.v3",
            reason_code="formal_point_evidence_missing",
        )
    assert raised.value.reason_code == "formal_corpus_conflict"


def test_profile_seals_hk_and_us_before_recipe_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.application.research import formal_corpus as mod

    sessions = {
        "HK": "2026-08-25",
        "US": "2026-08-25",
    }
    monkeypatch.setattr(
        mod,
        "read_market_calendar_binding",
        lambda _root, *, market: {
            "market_calendar_version": f"{market.lower()}.fixture.v1",
            "snapshot_content_sha256": ("a" if market == "HK" else "b") * 64,
            "coverage_start": "2026-08-01",
            "coverage_end": "2026-08-31",
            "trading_sessions": [
                {
                    "trading_date": sessions[market],
                    "trade_date_type": "WHOLE",
                }
            ],
        },
    )

    def load_config(*, config_path, expected_market):
        schedule = _schedule()
        schedule["timezone"] = (
            "Asia/Hong_Kong"
            if expected_market == "hk"
            else "America/New_York"
        )
        return Path(config_path), {"schedule": schedule}

    monkeypatch.setattr(mod, "load_runtime_config", load_config)
    profile = {
        "markets": ["hk", "us"],
        "accounts": ["lx"],
        "config_paths": {
            "hk": str(tmp_path / "config.hk.json"),
            "us": str(tmp_path / "config.us.json"),
        },
    }
    seal_formal_day_expectation(
        tmp_path,
        market="HK",
        account="lx",
        schedule=_schedule(),
        trading_date="2026-08-25",
        market_calendar_version="hk.fixture.v1",
        market_calendar_sha256="a" * 64,
        sealed_at_utc="2026-08-25T00:00:00Z",
    )
    first = seal_profile_formal_expectations(
        tmp_path,
        profile=profile,
        artifact_root=tmp_path,
        occurred_at_utc="2026-08-25T12:00:00Z",
    )
    second = seal_profile_formal_expectations(
        tmp_path,
        profile=profile,
        artifact_root=tmp_path,
        occurred_at_utc="2026-08-25T12:01:00Z",
    )

    assert [(item["market"], item["status"]) for item in first["results"]] == [
        ("HK", "idempotent"),
        ("US", "published"),
    ]
    assert [item["status"] for item in second["results"]] == [
        "idempotent",
        "idempotent",
    ]


def test_point_retry_preserves_first_capture_and_gzip_is_transparent(
    tmp_path: Path,
) -> None:
    expectation = _seal(tmp_path)
    payload = json.loads(
        (
            tmp_path
            / expectation["artifact_ref"]
        ).read_text(encoding="utf-8")
    )
    target = payload["scheduled_scan_targets_market"][0]
    common = {
        "market": "HK",
        "account": "lx",
        "trading_date": "2026-08-26",
        "run_id": "run-1",
        "scheduled_scan_target_market": target,
        "producer_behavior_version": "recommendation_point.v3",
        "reason_code": "formal_point_evidence_missing",
    }
    first = capture_formal_point_attempt(
        tmp_path,
        tmp_path,
        captured_at_utc="2026-08-26T02:00:01Z",
        **common,
    )
    retry = capture_formal_point_attempt(
        tmp_path,
        tmp_path,
        captured_at_utc="2026-08-26T02:00:02Z",
        **common,
    )

    assert first["status"] == "published"
    assert retry["status"] == "idempotent"
    assert retry["captured_at_utc"] == first["captured_at_utc"]
    archive = tmp_path / first["artifact_ref"]
    archived = json.loads(gzip.decompress(archive.read_bytes()))
    assert archived["captured_at_utc"] == "2026-08-26T02:00:01Z"
    assert archive.read_bytes()[:2] == b"\x1f\x8b"

    conflict = capture_formal_point_attempt(
        tmp_path,
        tmp_path,
        captured_at_utc="2026-08-26T02:00:03Z",
        **{**common, "producer_behavior_version": "recommendation_point.v4"},
    )
    assert conflict["status"] == "conflict"


def test_health_is_unhealthy_for_zero_or_incomplete_facts(tmp_path: Path) -> None:
    empty = build_corpus_health_receipt(
        tmp_path,
        market="US",
        account="lx",
    )
    assert empty["status"] == "unhealthy"
    assert empty["continuous_complete_trading_days"] == 0

    _seal(tmp_path)
    incomplete = build_corpus_health_receipt(
        tmp_path,
        market="HK",
        account="lx",
    )
    assert incomplete["days"][0]["status"] == "incomplete"
    assert incomplete["points_missing"] == 1

    unexpected = (
        tmp_path
        / "output_shared/research/formal_corpus/v1/hk/lx/points/2026-08-26"
        / ("f" * 64)
    )
    unexpected.mkdir(parents=True)
    conflicted = build_corpus_health_receipt(tmp_path, market="HK", account="lx")
    assert conflicted["days"][0]["status"] == "conflict"
    assert (
        load_formal_expectation(
            tmp_path,
            market="HK",
            account="lx",
            trading_date="2026-08-26",
        )["status"]
        == "conflict"
    )


def test_health_uses_calendar_denominator_and_exposes_point_diagnostics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.application.research import formal_corpus as mod

    for trading_date in ("2026-08-24", "2026-08-26"):
        seal_formal_day_expectation(
            tmp_path,
            market="HK",
            account="lx",
            schedule=_schedule(),
            trading_date=trading_date,
            market_calendar_version="fixture.v1",
            market_calendar_sha256="a" * 64,
            sealed_at_utc=f"{trading_date}T01:00:00Z",
        )
    monkeypatch.setattr(
        mod,
        "read_market_calendar_binding",
        lambda *_args, **_kwargs: {
            "market_calendar_version": "fixture.v1",
            "snapshot_content_sha256": "a" * 64,
            "trading_dates": ["2026-08-24", "2026-08-25", "2026-08-26"],
        },
    )

    def load_point(*_args, trading_date, recommendation_point_id, **_kwargs):
        observed = f"{trading_date}T02:00:00Z"
        return {
            "status": "available",
            "reason_code": None,
            "point": {
                "captured_at_utc": f"{trading_date}T02:00:02Z",
                "recommendation_point": {
                    "formal_point_time_coherence": {
                        "maximum_observed_at_utc": observed,
                        "minimum_observed_at_utc": observed,
                        "skew_ms": 0,
                    }
                },
            },
        }

    monkeypatch.setattr(mod, "load_formal_point", load_point)
    health = build_corpus_health_receipt(
        tmp_path,
        market="HK",
        account="lx",
        observed_at_utc="2026-08-26T03:00:00Z",
    )

    assert [item["status"] for item in health["days"]] == [
        "complete",
        "missing",
        "complete",
    ]
    assert health["continuous_complete_trading_days"] == 0
    assert health["status"] == "unhealthy"
    assert health["days"][-1]["points"][0] == {
        "recommendation_point_id": health["days"][-1]["points"][0][
            "recommendation_point_id"
        ],
        "status": "available",
        "reason_code": None,
        "captured_at_utc": "2026-08-26T02:00:02Z",
        "source_observed_at_utc": "2026-08-26T02:00:00Z",
        "time_coherence": {
            "maximum_observed_at_utc": "2026-08-26T02:00:00Z",
            "minimum_observed_at_utc": "2026-08-26T02:00:00Z",
            "skew_ms": 0,
        },
    }
    assert health["latest_source_observed_at_utc"] == "2026-08-26T02:00:00Z"
    assert health["freshness_seconds"] == 3600


def test_health_normalizes_storage_root_and_fails_closed_without_capacity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.application.research import formal_corpus as mod
    _seal(tmp_path)
    monkeypatch.setattr(
        mod,
        "read_market_calendar_binding",
        lambda *_args, **_kwargs: {
            "market_calendar_version": "fixture.v1",
            "snapshot_content_sha256": "a" * 64,
            "trading_dates": ["2026-08-26"],
        },
    )
    monkeypatch.setattr(
        mod,
        "load_formal_point",
        lambda *_args, recommendation_point_id, **_kwargs: {
            "status": "available",
            "reason_code": None,
            "point": {
                "captured_at_utc": "2026-08-26T02:00:02Z",
                "recommendation_point": {
                    "formal_point_time_coherence": {
                        "maximum_observed_at_utc": "2026-08-26T02:00:00Z"
                    }
                },
            },
        },
    )
    calls: list[Path] = []

    def disk_usage(path: Path):
        calls.append(path)
        return type("Usage", (), {"total": 100 * 1024**3, "free": 20 * 1024**3})()

    monkeypatch.setattr(mod.shutil, "disk_usage", disk_usage)
    repo_root = tmp_path / "repo"
    artifact_root = tmp_path / "output_shared/research/strategy_lab"
    healthy = build_corpus_health_receipt(
        artifact_root,
        market="HK",
        account="lx",
        repo_root=repo_root,
        observed_at_utc="2026-08-26T03:00:00Z",
    )
    assert healthy["status"] == "healthy"
    assert healthy["storage"]["capacity"]["status"] == "insufficient_history"
    assert calls == [tmp_path]

    monkeypatch.setattr(
        mod.shutil,
        "disk_usage",
        lambda _path: type(
            "Usage", (), {"total": 100 * 1024**3, "free": 9 * 1024**3}
        )(),
    )
    critical = build_corpus_health_receipt(
        artifact_root,
        market="HK",
        account="lx",
        observed_at_utc="2026-08-26T03:00:00Z",
    )
    assert critical["status"] == "unhealthy"
    assert critical["storage"]["capacity"]["status"] == "critical"
    assert critical["storage"]["capacity"]["critical_floor_bytes"] == 10 * 1024**3

    monkeypatch.setattr(
        mod.shutil,
        "disk_usage",
        lambda _path: (_ for _ in ()).throw(OSError("unavailable")),
    )
    unavailable = build_corpus_health_receipt(
        artifact_root,
        market="HK",
        account="lx",
        repo_root=repo_root,
        observed_at_utc="2026-08-26T03:00:00Z",
    )
    assert unavailable["status"] == "unhealthy"
    assert unavailable["storage"]["capacity"]["status"] == "unavailable"


def test_health_window_reads_only_twenty_mature_days_and_current(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.application.research import formal_corpus as mod

    cursor = date(2026, 7, 20)
    mature_dates: list[str] = []
    while len(mature_dates) < 22:
        if cursor.weekday() < 5:
            mature_dates.append(cursor.isoformat())
        cursor += timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor += timedelta(days=1)
    current_date = cursor.isoformat()
    for trading_date in mature_dates:
        seal_formal_day_expectation(
            tmp_path,
            market="HK",
            account="lx",
            schedule=_schedule(),
            trading_date=trading_date,
            market_calendar_version="fixture.v1",
            market_calendar_sha256="a" * 64,
            sealed_at_utc=f"{trading_date}T01:00:00Z",
        )
    unexpected = (
        tmp_path
        / "output_shared/research/formal_corpus/v1/hk/lx/points"
        / mature_dates[0]
        / ("f" * 64)
    )
    unexpected.mkdir(parents=True)
    monkeypatch.setattr(
        mod,
        "read_market_calendar_binding",
        lambda *_args, **_kwargs: {
            "market_calendar_version": "fixture.v1",
            "snapshot_content_sha256": "a" * 64,
            "coverage_start": mature_dates[0],
            "coverage_end": current_date,
            "trading_dates": [*mature_dates, current_date],
        },
    )
    loaded_dates: list[str] = []

    def load_point(*_args, trading_date, **_kwargs):
        loaded_dates.append(trading_date)
        return {
            "status": "available",
            "reason_code": None,
            "point": {
                "captured_at_utc": f"{trading_date}T02:00:02Z",
                "recommendation_point": {
                    "formal_point_time_coherence": {
                        "maximum_observed_at_utc": f"{trading_date}T02:00:00Z"
                    }
                },
            },
        }

    monkeypatch.setattr(mod, "load_formal_point", load_point)
    monkeypatch.setattr(
        mod.shutil,
        "disk_usage",
        lambda _path: type(
            "Usage", (), {"total": 100 * 1024**3, "free": 20 * 1024**3}
        )(),
    )
    window = build_corpus_health_receipt(
        tmp_path,
        market="HK",
        account="lx",
        observed_at_utc=f"{(cursor - timedelta(days=1)).isoformat()}T16:00:00Z",
        scope="latest_mature_window",
        mature_day_limit=20,
    )

    assert loaded_dates == mature_dates[-20:]
    assert window["schema_version"] == "corpus_health_receipt.v2"
    assert window["days_total"] == 21
    assert window["days"][-1]["trading_date"] == current_date
    assert window["days"][-1]["status"] == "missing"
    assert window["continuous_complete_trading_days"] == 20
    assert window["days_conflicting"] == 0
    assert window["earliest_trading_date"] == mature_dates[-20]

    loaded_dates.clear()
    full = build_corpus_health_receipt(
        tmp_path,
        market="HK",
        account="lx",
        observed_at_utc=f"{(cursor - timedelta(days=1)).isoformat()}T16:00:00Z",
    )
    assert loaded_dates == mature_dates
    assert full["days_conflicting"] == 1
    assert full["earliest_trading_date"] == mature_dates[0]


@pytest.mark.parametrize(
    "observed_at_utc",
    ["2026-07-31T15:59:59Z", "2026-08-23T16:00:00Z"],
)
def test_health_window_rejects_out_of_coverage_before_artifact_reads(
    tmp_path: Path,
    monkeypatch,
    observed_at_utc: str,
) -> None:
    from src.application.research import formal_corpus as mod

    monkeypatch.setattr(
        mod,
        "read_market_calendar_binding",
        lambda *_args, **_kwargs: {
            "market_calendar_version": "fixture.v1",
            "snapshot_content_sha256": "a" * 64,
            "coverage_start": "2026-08-01",
            "coverage_end": "2026-08-23",
            "trading_dates": ["2026-08-01", "2026-08-23"],
        },
    )
    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("artifacts must not be read outside calendar coverage")

    monkeypatch.setattr(mod, "_expectation_artifacts", unexpected_read)
    monkeypatch.setattr(mod, "load_formal_point", unexpected_read)

    with pytest.raises(FormalCorpusError) as raised:
        build_corpus_health_receipt(
            tmp_path,
            market="HK",
            account="lx",
            observed_at_utc=observed_at_utc,
            scope="latest_mature_window",
            mature_day_limit=20,
        )
    assert raised.value.reason_code == "market_calendar_binding_unavailable"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scope": "unknown"},
        {"scope": "full", "mature_day_limit": 20},
        {"scope": "latest_mature_window"},
        {"scope": "latest_mature_window", "mature_day_limit": 0},
        {"scope": "latest_mature_window", "mature_day_limit": True},
    ],
)
def test_health_scope_contract_rejects_invalid_combinations(
    tmp_path: Path,
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(FormalCorpusError) as raised:
        build_corpus_health_receipt(
            tmp_path,
            market="HK",
            account="lx",
            **kwargs,
        )
    assert raised.value.reason_code == "formal_corpus_input_invalid"


def test_ready_point_reloads_and_materializes_recipe_projection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.application.research import formal_corpus as mod

    expectation = _seal(tmp_path)
    expected = json.loads((tmp_path / expectation["artifact_ref"]).read_text())
    target = expected["scheduled_scan_targets_market"][0]
    run_id = "formal-ready"
    seal_opening_candidate_fixture(
        tmp_path,
        run_id=run_id,
        market="HK",
    )
    _publication, point = capture_scheduled_recommendation_point(
        tmp_path,
        run_id,
        "lx",
        {
            "should_run_scan": True,
            "scheduled_scan_target_market": target,
            "now_utc": "2026-08-26T02:00:01Z",
        },
        source_commit_sha="c" * 40,
    )
    opening = load_candidate_snapshot_bundle(
        base=tmp_path,
        run_id=run_id,
        account="lx",
    )["owners"]["opening"]
    required_bytes = b'{"fixture":true}\n'
    required_hash = hashlib.sha256(required_bytes).hexdigest()
    payload_bytes = b'{"prepared":true}\n'
    evidence = {
        "status": "ready",
        "run_id": run_id,
        "account": "lx",
        "account_config_sha256": opening["account_config_sha256"],
        "evidence_at_utc": "2026-06-01T00:00:00Z",
        "open_option_positions": [],
        "valuation_mark_facts": [],
        "fx_rate_facts": [],
    }
    evidence["content_sha256"] = canonical_sha256(evidence)
    point.update(
        {
            "schema_version": "recommendation_point.v3",
            "required_data_manifest_ref": "required/manifest.json",
            "required_data_manifest_sha256": required_hash,
            "prepared_context_manifest_ref": "prepared/manifest.json",
            "prepared_context_manifest_sha256": "d" * 64,
            "prepared_context_payload_sha256": hashlib.sha256(
                payload_bytes
            ).hexdigest(),
            "formal_point_time_coherence": {
                "schema_version": "formal_point_time_coherence.v1",
                "status": "ready",
                "reason_code": None,
                "minimum_observed_at_utc": "2026-06-01T00:00:00Z",
                "maximum_observed_at_utc": "2026-06-01T00:00:00Z",
                "observation_count": 1,
                "skew_ms": 0,
                "max_skew_ms": 300000,
            },
        }
    )
    point["content_sha256"] = canonical_sha256(
        {key: value for key, value in point.items() if key != "content_sha256"}
    )
    monkeypatch.setattr(
        mod,
        "_required_data_binding",
        lambda _opening: ("required/manifest.json", required_hash),
    )
    monkeypatch.setattr(
        mod,
        "load_required_data_snapshot_manifest_snapshot",
        lambda **_kwargs: (
            {
                "run_id": run_id,
                "symbols": {
                    "0700.HK": {
                        "status": "ready",
                        "source_observed_at": "2026-06-01T00:00:00Z",
                        "payload_sha256": "e" * 64,
                        "scan_blob_ref": "blob/ref",
                    }
                },
            },
            tmp_path,
            required_bytes,
        ),
    )
    monkeypatch.setattr(
        mod,
        "load_prepared_option_positions_context_receipt",
        lambda **_kwargs: {
            "manifest": {},
            "payload": {"strategy_lab_option_market_evidence": evidence},
            "payload_bytes": payload_bytes,
        },
    )
    monkeypatch.setattr(
        mod,
        "validate_strategy_lab_option_market_evidence",
        lambda value, **_kwargs: value,
    )

    published = capture_formal_point_attempt(
        tmp_path,
        tmp_path,
        market="HK",
        account="lx",
        trading_date="2026-08-26",
        run_id=run_id,
        scheduled_scan_target_market=target,
        captured_at_utc="2026-08-26T02:00:02Z",
        producer_behavior_version="recommendation_point.v3",
        recommendation_point=point,
    )
    assert published["status"] == "published"
    loaded = mod.load_formal_point(
        tmp_path,
        market="HK",
        account="lx",
        trading_date="2026-08-26",
        recommendation_point_id=point["recommendation_point_id"],
    )
    assert loaded["status"] == "available"
    recipe_projection = build_top1_recipe_projection(
        loaded["point"], formal_point_ref=loaded["artifact_ref"]
    )
    assert recipe_projection["schema_version"] == "sell_put_ranking_projection.v3"
    assert "account_config_sha256" not in recipe_projection
    projection = materialize_top1_recipe_input(loaded["point"], recipe_projection)
    assert recipe_projection["materialized_input_content_sha256"] == projection[
        "artifact_provenance"
    ]["content_sha256"]
    assert projection["schema_version"] == "sell_put_ranking_projection.v2"
    assert projection["candidates"] == []

    artifact = tmp_path / published["artifact_ref"]
    tampered = json.loads(gzip.decompress(artifact.read_bytes()))
    tampered["required_data_symbols"][0][
        "source_observed_at"
    ] = "2026-06-01T00:00:10Z"
    tampered["content_sha256"] = canonical_sha256(
        {key: value for key, value in tampered.items() if key != "content_sha256"}
    )
    tampered_bytes = (
        json.dumps(
            tampered,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode()
    artifact.unlink()
    artifact.with_name(f"{tampered['content_sha256']}.json.gz").write_bytes(
        gzip.compress(tampered_bytes, mtime=0)
    )
    with pytest.raises(FormalCorpusError) as raised:
        mod.load_formal_point(
            tmp_path,
            market="HK",
            account="lx",
            trading_date="2026-08-26",
            recommendation_point_id=point["recommendation_point_id"],
        )
    assert raised.value.reason_code == "formal_corpus_artifact_invalid"
