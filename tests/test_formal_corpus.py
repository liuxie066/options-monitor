from __future__ import annotations

import gzip
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.candidate_snapshot_manifest import load_candidate_snapshot_bundle
from src.application.recommendation_point import (
    build_option_position_evidence_binding,
    build_recommendation_point,
    build_recommendation_point_id,
)
from src.application.research.formal_corpus import (
    FormalCorpusError,
    build_corpus_health_receipt,
    capture_formal_point_attempt,
    formal_corpus_present,
    load_formal_expectation,
    read_expectation_bound_market_calendar_snapshot,
    read_market_calendar_binding,
    refresh_market_calendar_binding,
    seal_formal_day_expectation,
    seal_profile_formal_expectations,
)
from src.infrastructure.private_storage import exclusive_private_file_lock
from tests.candidate_evidence_helpers import seal_opening_candidate_fixture


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _prepared_receipt(
    opening: Mapping[str, Any],
    *,
    hkd_cny: float = 0.92,
    open_positions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    observed_at = str(opening["sealed_at_utc"])
    payload = {
        "prepared_authority": {
            "schema_version": "prepared_option_positions_context",
            "fx_status": "ready",
            "fx_observation_sha256": "c" * 64,
            "source_observed_at": observed_at,
            "application_received_at_utc": observed_at,
        },
        "exchange_rates": {
            "timestamp": observed_at,
            "rates": {"HKDCNY": hkd_cny, "USDCNY": 7.2},
        },
        "open_positions_min": open_positions or [],
        "decision_snapshot_actionable": True,
    }
    payload_bytes = _canonical_bytes(payload)
    manifest = {
        "schema_version": "prepared_option_positions_context",
        "status": "ready",
        "run_id": opening["run_id"],
        "account": opening["account"],
        "account_config_sha256": opening["account_config_sha256"],
        "application_received_at_utc": observed_at,
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "ledger_generation_sha256": "a" * 64,
        "decision_state_fingerprint": "b" * 64,
    }
    return {
        "manifest": manifest,
        "payload": payload,
        "manifest_bytes": _canonical_bytes(manifest),
        "payload_bytes": payload_bytes,
    }


def _open_position(
    record_id: str,
    *,
    symbol: str,
    currency: str,
    market_code: str,
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "status": "open",
        "broker": "futu",
        "symbol": symbol,
        "option_type": "put",
        "strike": "100",
        "expiration_ymd": "2026-08-21",
        "currency": currency,
        "multiplier": "100",
        "side": "short",
        "contracts_open": 1,
        "premium": "2",
        "opened_at": 1_700_000_000_000,
        "market_code": market_code,
    }


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
    lock = tmp_path / "output_shared/research/formal_corpus/v1/hk/lx/.locks/expectations/2026-08-26.lock"
    with exclusive_private_file_lock(lock):
        pass

    assert not formal_corpus_present(tmp_path, market="HK", account="lx")
    _seal(tmp_path)
    assert formal_corpus_present(tmp_path, market="HK", account="lx")


def test_market_calendar_uses_neutral_strategy_lab_artifact_path(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "output_shared/research/strategy_lab"

    class Gateway:
        def get_trading_days_with_receipt(self, **_kwargs: object) -> dict[str, object]:
            return {
                "retcode": 0,
                "rows": [{"time": "2026-08-26", "trade_date_type": "WHOLE"}],
                "coverage_complete": True,
                "pagination_complete": True,
                "page_count": 1,
            }

    result = refresh_market_calendar_binding(
        artifact_root,
        gateway=Gateway(),
        market="HK",
        market_calendar_version="fixture.v1",
        coverage_start="2026-08-26",
        coverage_end="2026-08-26",
        observed_at_utc="2026-08-25T00:00:00Z",
    )

    binding = result["binding"]
    assert binding == read_market_calendar_binding(artifact_root, market="HK")
    assert str(binding["snapshot_ref"]).startswith("capabilities/market-calendar/hk/snapshots/")
    assert (artifact_root / "capabilities/market-calendar/hk/current.json").is_file()
    assert not (artifact_root / "strategy_lab").exists()
    assert not (artifact_root / "top1").exists()

    class ExtendedGateway:
        def get_trading_days_with_receipt(self, **_kwargs: object) -> dict[str, object]:
            return {
                "retcode": 0,
                "rows": [
                    {"time": "2026-08-26", "trade_date_type": "WHOLE"},
                    {"time": "2026-08-27", "trade_date_type": "WHOLE"},
                ],
                "coverage_complete": True,
                "pagination_complete": True,
                "page_count": 1,
            }

    refresh_market_calendar_binding(
        artifact_root,
        gateway=ExtendedGateway(),
        market="HK",
        market_calendar_version="fixture.v1",
        coverage_start="2026-08-26",
        coverage_end="2026-08-27",
        observed_at_utc="2026-08-26T00:00:00Z",
    )
    current = read_market_calendar_binding(artifact_root, market="HK")
    old = read_expectation_bound_market_calendar_snapshot(
        artifact_root,
        market="HK",
        market_calendar_version=binding["market_calendar_version"],
        market_calendar_sha256=binding["snapshot_content_sha256"],
    )
    assert current["snapshot_content_sha256"] != binding["snapshot_content_sha256"]
    assert old["snapshot_ref"] == binding["snapshot_ref"]
    assert old["snapshot_file_sha256"] == binding["snapshot_file_sha256"]
    assert old["trading_sessions"] == [{"trading_date": "2026-08-26", "trade_date_type": "WHOLE"}]


def test_expectation_lock_is_idempotent_and_conflicts_on_denominator_change(
    tmp_path: Path,
) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _seal(tmp_path), range(2)))

    assert {result["status"] for result in results} == {"published", "idempotent"}
    files = list(tmp_path.glob("output_shared/research/formal_corpus/v1/hk/lx/expectations/2026-08-26/*.json"))
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
            scheduled_scan_target_market=first["scheduled_scan_targets_market"][0],
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
        schedule["timezone"] = "Asia/Hong_Kong" if expected_market == "hk" else "America/New_York"
        return Path(config_path), {
            "accounts": ["user1"] if expected_market == "hk" else ["user2"],
            "schedule": schedule,
        }

    monkeypatch.setattr(mod, "load_runtime_config", load_config)
    profile = {
        "markets": ["hk", "us"],
        "accounts": ["user1", "user2"],
        "config_paths": {
            "hk": str(tmp_path / "config.hk.json"),
            "us": str(tmp_path / "config.us.json"),
        },
    }
    seal_formal_day_expectation(
        tmp_path,
        market="HK",
        account="user1",
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

    assert [(item["market"], item["account"], item["status"]) for item in first["results"]] == [
        ("HK", "user1", "idempotent"),
        ("US", "user2", "published"),
    ]
    assert [item["status"] for item in second["results"]] == [
        "idempotent",
        "idempotent",
    ]


def test_point_retry_preserves_first_capture_and_gzip_is_transparent(
    tmp_path: Path,
) -> None:
    expectation = _seal(tmp_path)
    payload = json.loads((tmp_path / expectation["artifact_ref"]).read_text(encoding="utf-8"))
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

    unexpected = tmp_path / "output_shared/research/formal_corpus/v1/hk/lx/points/2026-08-26" / ("f" * 64)
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
        "recommendation_point_id": health["days"][-1]["points"][0]["recommendation_point_id"],
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
                    "formal_point_time_coherence": {"maximum_observed_at_utc": "2026-08-26T02:00:00Z"}
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
        lambda _path: type("Usage", (), {"total": 100 * 1024**3, "free": 9 * 1024**3})(),
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
    unexpected = tmp_path / "output_shared/research/formal_corpus/v1/hk/lx/points" / mature_dates[0] / ("f" * 64)
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
                    "formal_point_time_coherence": {"maximum_observed_at_utc": f"{trading_date}T02:00:00Z"}
                },
            },
        }

    monkeypatch.setattr(mod, "load_formal_point", load_point)
    monkeypatch.setattr(
        mod.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"total": 100 * 1024**3, "free": 20 * 1024**3})(),
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
    from src.application import recommendation_point as point_mod
    from src.application.research import formal_corpus as mod

    expectation = _seal(tmp_path)
    expected = json.loads((tmp_path / expectation["artifact_ref"]).read_text())
    target = expected["scheduled_scan_targets_market"][0]
    run_id = "formal-ready"
    seal_opening_candidate_fixture(
        tmp_path,
        run_id=run_id,
        market="HK",
        sealed_at="2026-08-26T02:00:01Z",
        manifest_sealed_at="2026-08-26T02:00:02Z",
    )
    bundle = load_candidate_snapshot_bundle(
        base=tmp_path,
        run_id=run_id,
        account="lx",
    )
    opening = bundle["owners"]["opening"]
    required_bytes = b'{"fixture":true}\n'
    required_hash = hashlib.sha256(required_bytes).hexdigest()
    open_positions = [
        _open_position(
            "hk-lot",
            symbol="0700.HK",
            currency="HKD",
            market_code="HK.0700260821P100000",
        ),
        _open_position(
            "us-lot",
            symbol="FUTU",
            currency="USD",
            market_code="US.FUTU260821P100000",
        ),
    ]
    receipt = _prepared_receipt(opening, open_positions=open_positions)
    target_ms = int(datetime.fromisoformat(target.replace("Z", "+00:00")).timestamp() * 1000)
    sealed_ms = int(datetime.fromisoformat(str(opening["sealed_at_utc"]).replace("Z", "+00:00")).timestamp() * 1000)
    required_entries = {
        "0700.HK": (
            {
                "scan_blob_ref": {
                    "blob_relpath": "required/0700.HK.csv",
                    "blob_sha256": "e" * 64,
                }
            },
            (
                "code,bid_price,ask_price,snapshot_requested_at_utc,"
                "snapshot_received_at_utc\n"
                f"HK.0700260821P100000,2.0,2.4,{opening['sealed_at_utc']},"
                f"{opening['sealed_at_utc']}\n"
            ).encode(),
        )
    }
    evidence = build_option_position_evidence_binding(
        run_id=run_id,
        account="lx",
        market="HK",
        recommendation_point_id=build_recommendation_point_id("HK", "lx", target),
        account_config_sha256=opening["account_config_sha256"],
        evidence_at_utc=opening["sealed_at_utc"],
        prepared_receipt=receipt,
        required_data_entries=required_entries,
        formal_time_bounds=(target_ms - 300000, sealed_ms),
    )
    assert [row["lot_id"] for row in evidence["open_option_positions"]] == ["hk-lot"]
    required_manifest = {
        "run_id": run_id,
        "symbols": {
            "0700.HK": {
                "status": "ready",
                "source_observed_at": opening["sealed_at_utc"],
                "payload_sha256": "e" * 64,
                "scan_blob_ref": "blob/ref",
            }
        },
    }
    monkeypatch.setattr(
        point_mod,
        "_required_data_binding",
        lambda _opening: ("required/manifest.json", required_hash),
    )
    point = build_recommendation_point(
        {
            "should_run_scan": True,
            "scheduled_scan_target_market": target,
            "now_utc": "2026-08-26T02:00:01Z",
        },
        bundle["manifest"],
        opening,
        terminal_manifest_sha256=hashlib.sha256(_canonical_bytes(bundle["manifest"])).hexdigest(),
        source_commit_sha="c" * 40,
        prepared_option_receipt=receipt,
        required_data_manifest=required_manifest,
        required_data_entries=required_entries,
        required_data_manifest_ref="required/manifest.json",
        required_data_manifest_sha256=required_hash,
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
            required_manifest,
            tmp_path,
            required_bytes,
        ),
    )
    monkeypatch.setattr(
        mod,
        "load_prepared_option_positions_context_receipt",
        lambda **_kwargs: receipt,
    )
    monkeypatch.setattr(
        mod,
        "resolve_frozen_required_data_csv_bytes_batch",
        lambda **_kwargs: SimpleNamespace(entries=required_entries, unavailable={}),
    )

    alternate_receipt = _prepared_receipt(
        opening,
        hkd_cny=0.93,
        open_positions=open_positions,
    )
    alternate_evidence = build_option_position_evidence_binding(
        run_id=run_id,
        account="lx",
        market="HK",
        recommendation_point_id=point["recommendation_point_id"],
        account_config_sha256=opening["account_config_sha256"],
        evidence_at_utc=opening["sealed_at_utc"],
        prepared_receipt=alternate_receipt,
        required_data_entries=required_entries,
        formal_time_bounds=(target_ms - 300000, sealed_ms),
    )
    alternate_evidence["position_source"] = dict(evidence["position_source"])
    alternate_evidence["content_sha256"] = canonical_sha256(
        {key: value for key, value in alternate_evidence.items() if key != "content_sha256"}
    )
    alternate_point = dict(point)
    alternate_point["option_position_evidence_binding"] = alternate_evidence
    alternate_point["content_sha256"] = canonical_sha256(
        {key: value for key, value in alternate_point.items() if key != "content_sha256"}
    )
    tampered_root = tmp_path / "tampered-corpus"
    _seal(tampered_root)
    rejected = capture_formal_point_attempt(
        tampered_root,
        tmp_path,
        market="HK",
        account="lx",
        trading_date="2026-08-26",
        run_id=run_id,
        scheduled_scan_target_market=target,
        captured_at_utc="2026-08-26T02:00:02Z",
        producer_behavior_version="recommendation_point.v3",
        recommendation_point=alternate_point,
    )
    assert rejected["reason_code"] == "formal_point_evidence_missing"
    assert (
        mod.load_formal_point(
            tampered_root,
            market="HK",
            account="lx",
            trading_date="2026-08-26",
            recommendation_point_id=point["recommendation_point_id"],
        )["status"]
        == "not_evaluable"
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
    artifact = tmp_path / published["artifact_ref"]
    tampered = json.loads(gzip.decompress(artifact.read_bytes()))
    tampered["required_data_symbols"][0]["source_observed_at"] = "2026-06-01T00:00:10Z"
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
    artifact.with_name(f"{tampered['content_sha256']}.json.gz").write_bytes(gzip.compress(tampered_bytes, mtime=0))
    with pytest.raises(FormalCorpusError) as raised:
        mod.load_formal_point(
            tmp_path,
            market="HK",
            account="lx",
            trading_date="2026-08-26",
            recommendation_point_id=point["recommendation_point_id"],
        )
    assert raised.value.reason_code == "formal_corpus_artifact_invalid"
