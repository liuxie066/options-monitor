from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = ROOT / "contracts" / "quality-monitoring"
SCHEMA_PATH = CONTRACT_DIR / "quality_status.v1.schema.json"


def test_quality_status_schema_is_valid() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["$id"] == "urn:investment:quality-status:v1"
    assert schema["properties"]["schema_version"]["const"] == "investment.quality_status.v1"


def test_minimal_om_quality_status_fixture_validates() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    payload = {
        "schema_version": "investment.quality_status.v1",
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
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    payload = {
        "schema_version": "investment.quality_status.v1",
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
