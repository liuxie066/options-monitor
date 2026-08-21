from __future__ import annotations

import json
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import src.application.agent_tools.quality as quality_tool_module
import src.application.quality.gate as quality_gate_module
from src.application.quality.gate import (
    QualityGateBlocked,
    assert_quality_allows,
    quality_consumer_telemetry_snapshot,
)
from src.application.quality.service import OMQualityService
from src.infrastructure.quality.artifact_repository import QualityArtifactRepository
from src.infrastructure.quality.control_state_repository import QualityControlStateRepository
from src.interfaces.quality.http import build_quality_handler


def _payload(*, blocked: bool = False, observed_at: str | None = None) -> dict:
    observed_at = observed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": "investment.quality_status.v1",
        "producer": {
            "service": "options-monitor",
            "producer_version": "test",
            "policy_version": "quality-policy-v1",
            "instance_id": "test",
        },
        "observed_at_utc": observed_at,
        "runtime": {"status": "healthy", "as_of_utc": observed_at, "checks": []},
        "datasets": [
            {
                "dataset_id": "om.option_positions",
                "scope": {"account": "lx", "market": "us"},
                "status": "untrusted" if blocked else "trusted",
                "as_of_utc": observed_at,
                "required_evidence_complete": not blocked,
                "freshness": {"status": "fresh", "observed_at_utc": observed_at},
                "checks": [],
                "evidence_refs": [],
                "usable_for": [] if blocked else ["close_advice"],
                "blocked_consumers": ["close_advice"] if blocked else [],
                "blocked_by": ["OM-POS-002"] if blocked else [],
                "reason_codes": ["POSITION_DIVERGENCE_PERSISTENT"] if blocked else [],
            }
        ],
        "incidents": [],
    }


def _service(tmp_path: Path, payload: dict) -> OMQualityService:
    artifact = QualityArtifactRepository(tmp_path / "status.json")
    artifact.write_atomic(payload)
    return OMQualityService(
        artifact_repository=artifact,
        control_repository=QualityControlStateRepository(tmp_path / "control.json"),
    )


def test_gate_is_inactive_before_onboarding(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OM_QUALITY_ONBOARDED", raising=False)
    assert_quality_allows("close_advice", service=_service(tmp_path, _payload(blocked=True)))


def test_gate_blocks_only_matching_account_after_onboarding(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "true")
    payload = _payload(blocked=True)
    payload["datasets"].append(
        {
            **payload["datasets"][0],
            "scope": {"account": "sy", "market": "us"},
            "status": "trusted",
            "required_evidence_complete": True,
            "usable_for": ["close_advice"],
            "blocked_consumers": [],
            "blocked_by": [],
            "reason_codes": [],
        }
    )
    service = _service(tmp_path, payload)
    with pytest.raises(QualityGateBlocked) as exc:
        assert_quality_allows("close_advice", account="lx", market="us", service=service)
    assert exc.value.blocked_by == ("OM-POS-002",)
    assert_quality_allows("close_advice", account="sy", market="us", service=service)


def test_gate_fails_closed_when_target_position_dataset_is_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "true")

    with pytest.raises(QualityGateBlocked) as exc:
        assert_quality_allows(
            "close_advice",
            account="sy",
            market="us",
            service=_service(tmp_path, _payload()),
        )

    assert exc.value.reason_code == "QUALITY_DATASET_UNAVAILABLE"
    assert exc.value.blocked_by == ("OM-POS-001",)


def test_gate_fails_closed_when_target_position_dataset_is_ambiguous(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "true")
    payload = _payload()
    payload["datasets"].append(dict(payload["datasets"][0]))

    with pytest.raises(QualityGateBlocked) as exc:
        assert_quality_allows(
            "close_advice",
            account="lx",
            market="us",
            service=_service(tmp_path, payload),
        )

    assert exc.value.reason_code == "QUALITY_DATASET_AMBIGUOUS"
    assert exc.value.blocked_by == ("OM-POS-001",)


def test_gate_fails_closed_on_stale_artifact(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "1")
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    with pytest.raises(QualityGateBlocked) as exc:
        assert_quality_allows("close_advice", service=_service(tmp_path, _payload(observed_at=stale)))
    assert exc.value.reason_code == "QUALITY_STATUS_STALE"


def test_gate_fails_closed_on_stale_position_source_inside_fresh_artifact(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "1")
    payload = _payload()
    payload["datasets"][0]["freshness"]["observed_at_utc"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat().replace("+00:00", "Z")

    with pytest.raises(QualityGateBlocked) as exc:
        assert_quality_allows(
            "close_advice",
            account="lx",
            market="us",
            service=_service(tmp_path, payload),
        )

    assert exc.value.reason_code == "QUALITY_DATASET_STALE"
    assert exc.value.blocked_by == ("OM-POS-001",)


def test_shadow_lifecycle_summary_never_changes_legacy_gate_authority(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "true")
    payload = _payload()
    payload["datasets"].append(
        {
            **payload["datasets"][0],
            "dataset_id": "om.lifecycle_evidence_summary",
            "status": "unavailable",
            "blocked_consumers": ["close_advice"],
            "blocked_by": ["OM-LCY-SHADOW-001"],
            "reason_codes": ["CURRENT_DECISION_QUALITY_MISMATCH"],
        }
    )

    assert_quality_allows(
        "close_advice",
        account="lx",
        market="us",
        service=_service(tmp_path, payload),
    )


def test_current_lifecycle_summary_becomes_gate_authority_after_cutover(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "true")
    payload = _payload()
    payload["extensions"] = {
        "quality_hot_path_cutover": {"status": "active"}
    }
    payload["datasets"].append(
        {
            **payload["datasets"][0],
            "dataset_id": "om.lifecycle_evidence_summary",
            "status": "unavailable",
            "blocked_consumers": ["close_advice"],
            "blocked_by": ["OM-LCY-CURRENT-001"],
            "reason_codes": ["CURRENT_LIFECYCLE_QUALITY_UNAVAILABLE"],
        }
    )

    with pytest.raises(QualityGateBlocked) as exc:
        assert_quality_allows(
            "close_advice",
            account="lx",
            market="us",
            service=_service(tmp_path, payload),
        )
    assert exc.value.reason_code == "CURRENT_LIFECYCLE_QUALITY_UNAVAILABLE"


def test_quality_reads_count_declared_and_unexplained_without_payloads(
    tmp_path: Path,
) -> None:
    payload = _payload()
    payload["extensions"] = {
        "current_decision_migration": {
            "quality_consumer_telemetry": {},
        }
    }
    payload["datasets"].append(
        {
            **payload["datasets"][0],
            "dataset_id": "om.lifecycle_evidence",
            "scope": {
                "account": "lx",
                "market": "us",
                "lifecycle_case_id": "case-1",
            },
        }
    )
    service = _service(tmp_path, payload)

    before = quality_consumer_telemetry_snapshot()
    assert service.read_published() == payload
    declared = service.read_published(
        consumer="close_advice",
        account="lx",
        market="us",
        lifecycle_rows_requested=True,
    )
    after = quality_consumer_telemetry_snapshot()

    assert after["total_count"] == before["total_count"] + 2
    assert after["unexplained_count"] == before["unexplained_count"] + 1
    assert declared["extensions"]["current_decision_migration"][
        "quality_consumer_telemetry"
    ] == after
    assert service.artifact_repository.read() == payload
    assert any(
        item["consumer"] == "close_advice"
        and item["account"] == "lx"
        and item["market"] == "us"
        and item["legacy_rows_requested"] is True
        and item["legacy_rows_returned"] is True
        for item in after["entries"]
    )


def test_quality_read_telemetry_is_bounded_and_overflow_is_unexplained(
    monkeypatch,
) -> None:
    monkeypatch.setattr(quality_gate_module, "_TELEMETRY_COUNTS", Counter())
    monkeypatch.setattr(quality_gate_module, "_TELEMETRY_OVERFLOW_COUNT", 0)

    for index in range(quality_gate_module._TELEMETRY_LIMIT + 3):
        quality_gate_module.record_quality_consumer_read(
            consumer=f"consumer-{index}",
            account="lx",
            market="us",
            lifecycle_rows_requested=True,
            lifecycle_rows_returned=True,
        )

    telemetry = quality_consumer_telemetry_snapshot()
    assert len(telemetry["entries"]) == quality_gate_module._TELEMETRY_LIMIT
    assert telemetry["overflow_count"] == 3
    assert telemetry["unexplained_count"] == 3
    assert telemetry["coverage_status"] == "unexplained"


def test_quality_tool_declares_its_consumer_scope(monkeypatch) -> None:
    calls: list[dict] = []

    class _Reader:
        def read_published(self, **kwargs):
            calls.append(kwargs)
            return _payload()

        def read_integrity_published(self):
            calls.append({"integrity": True})
            return _payload()

    monkeypatch.setattr(
        quality_tool_module,
        "OMQualityService",
        _Reader,
    )
    quality_tool_module._quality_status_tool(  # noqa: SLF001 - facade proof
        {
            "account": "lx",
            "market": "us",
            "dataset_id": "om.lifecycle_evidence",
        }
    )

    assert calls == [{"integrity": True}]


def test_http_is_read_only_authenticated_and_etag_aware(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OM_QUALITY_READ_TOKEN", "secret-read-token")
    service = _service(tmp_path, _payload())
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_quality_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET", "/quality/status")
        unauthorized = conn.getresponse()
        assert unauthorized.status == 401
        assert json.loads(unauthorized.read())["error"]["code"] == "QUALITY_AUTH_FAILED"

        conn.request(
            "GET",
            "/quality/status",
            headers={"Authorization": "Bearer secret-read-token"},
        )
        response = conn.getresponse()
        assert response.status == 200
        etag = response.getheader("ETag")
        assert response.getheader("Cache-Control") == "no-store"
        assert json.loads(response.read())["producer"]["service"] == "options-monitor"

        conn.request(
            "GET",
            "/quality/status",
            headers={
                "Authorization": "Bearer secret-read-token",
                "If-None-Match": etag,
            },
        )
        unchanged = conn.getresponse()
        assert unchanged.status == 304
        unchanged.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
