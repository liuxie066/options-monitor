from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = ROOT / "contracts" / "quality-monitoring"
MANIFEST_PATH = CONTRACT_DIR / "vendor-manifest.json"


def test_vendored_quality_status_schema_matches_manifest() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    schema_path = ROOT / manifest["schema_path"]
    schema_bytes = schema_path.read_bytes()

    assert hashlib.sha256(schema_bytes).hexdigest() == manifest["sha256"]
    assert str(manifest["upstream_contract_release"]).startswith("contract-v")
    assert len(manifest["upstream_commit"]) == 40

    schema = json.loads(schema_bytes)
    Draft202012Validator.check_schema(schema)
    assert schema["$id"] == manifest["schema_id"]
    assert schema["properties"]["schema_version"]["const"] == manifest["schema_version"]


def test_minimal_om_quality_status_fixture_validates() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    schema = json.loads((ROOT / manifest["schema_path"]).read_text(encoding="utf-8"))
    payload = {
        "schema_version": manifest["schema_version"],
        "producer": {
            "service": "options-monitor",
            "producer_version": "test",
            "policy_version": "quality-policy-v1",
            "instance_id": "test-redacted",
        },
        "observed_at_utc": "2026-07-26T00:00:00Z",
        "runtime": {
            "status": "healthy",
            "as_of_utc": "2026-07-26T00:00:00Z",
            "checks": [],
        },
        "datasets": [],
        "incidents": [],
    }

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


def test_v1_rejects_unknown_top_level_fields() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    schema = json.loads((ROOT / manifest["schema_path"]).read_text(encoding="utf-8"))
    payload = {
        "schema_version": manifest["schema_version"],
        "producer": {
            "service": "options-monitor",
            "producer_version": "test",
            "policy_version": "quality-policy-v1",
            "instance_id": "test-redacted",
        },
        "observed_at_utc": "2026-07-26T00:00:00Z",
        "runtime": {
            "status": "healthy",
            "as_of_utc": "2026-07-26T00:00:00Z",
            "checks": [],
        },
        "datasets": [],
        "incidents": [],
        "unexpected": True,
    }

    errors = list(Draft202012Validator(schema).iter_errors(payload))
    assert errors
