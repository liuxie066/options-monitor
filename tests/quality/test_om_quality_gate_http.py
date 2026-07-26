from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from src.application.quality.gate import QualityGateBlocked, assert_quality_allows
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
    service = _service(tmp_path, _payload(blocked=True))
    with pytest.raises(QualityGateBlocked) as exc:
        assert_quality_allows("close_advice", account="lx", market="us", service=service)
    assert exc.value.blocked_by == ("OM-POS-002",)
    assert_quality_allows("close_advice", account="sy", market="us", service=service)


def test_gate_fails_closed_on_stale_artifact(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "1")
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    with pytest.raises(QualityGateBlocked) as exc:
        assert_quality_allows("close_advice", service=_service(tmp_path, _payload(observed_at=stale)))
    assert exc.value.reason_code == "QUALITY_STATUS_STALE"


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
