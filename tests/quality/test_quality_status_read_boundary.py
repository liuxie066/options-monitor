from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from src.application.quality import model
from src.application.quality.gate import QualityGateBlocked, assert_quality_allows
from src.application.quality.paths import default_quality_artifact_path
from src.application.quality.service import OMQualityService
from src.infrastructure.quality.artifact_repository import QualityArtifactRepository
from src.infrastructure.quality.control_state_repository import QualityControlStateRepository
from src.interfaces.agent.cli import main as agent_main
from src.interfaces.cli.main import main as human_main


def _payload() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": model.SCHEMA_VERSION,
        "producer": {
            "service": "options-monitor", "producer_version": "test",
            "policy_version": model.POLICY_VERSION, "instance_id": "synthetic",
        },
        "observed_at_utc": now,
        "runtime": {"status": "healthy", "as_of_utc": now, "checks": []},
        "datasets": [model.dataset_status(
            dataset_id="om.option_positions", scope={"account": "qa", "market": "us"},
            status="trusted", as_of_utc=now, checks=[], usable_for=["close_advice", "option_position_report"],
        )],
        "incidents": [],
    }


@pytest.fixture
def quality_runtime(tmp_path, monkeypatch):
    env_file = tmp_path / "empty.env"
    env_file.write_text("")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("OM_ENV_FILE", str(env_file))
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "true")
    folder = tmp_path / "output_shared" / "state" / "quality"
    folder.mkdir(parents=True)
    return folder


def _write(folder: Path, payload, *, integrity=False) -> Path:
    path = folder / ("integrity_status.v1.json" if integrity else "status.v1.json")
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _read_public(capsys, *, integrity=False, filtered=False):
    exit_code = human_main(["quality", "integrity-status" if integrity else "status", "--json"])
    human = json.loads(capsys.readouterr().out)
    query = {"account": "qa", "market": "us"} if filtered else {}
    if integrity:
        query["dataset_id"] = "om.lifecycle_history"
    agent_exit = agent_main([
        "run", "--tool", "quality_status", "--no-local-env-file", "--input-json", json.dumps(query),
    ])
    agent = json.loads(capsys.readouterr().out)
    return exit_code, human, agent_exit, agent


@pytest.mark.parametrize("integrity", [False, True])
@pytest.mark.parametrize("invalid", [
    "version", "missing_schema_version", "missing_producer", "missing_observed_at_utc",
    "missing_runtime", "missing_datasets", "missing_incidents", "runtime_array",
    "datasets_number", "bad_nested_enum", "extra_field", "extensions_array",
    "array", "null", "json", "utf8", "missing",
])
def test_public_readers_reject_invalid_stored_status(quality_runtime, capsys, integrity, invalid):
    payload = _payload()
    if invalid == "version":
        payload["schema_version"] = "investment.quality_status.v0"
    elif invalid.startswith("missing_"):
        del payload[invalid.removeprefix("missing_")]
    elif invalid == "runtime_array":
        payload["runtime"] = []
    elif invalid == "datasets_number":
        payload["datasets"] = 7
    elif invalid == "bad_nested_enum":
        # An out-of-scope invalid row must not disappear before whole-artifact validation.
        other = deepcopy(payload["datasets"][0])
        other["scope"]["account"] = "other"
        other["status"] = "invented"
        payload["datasets"].append(other)
    elif invalid == "extra_field":
        payload["unexpected"] = True
    elif invalid == "extensions_array":
        payload["extensions"] = ["invalid"]
    elif invalid in {"array", "null", "json", "utf8"}:
        payload = {"array": [], "null": None, "json": b"{broken", "utf8": b"\xff"}[invalid]
    if invalid != "missing":
        path = _write(quality_runtime, payload, integrity=integrity)
        before = path.read_bytes()
    service = OMQualityService()
    result = service.read_integrity_published() if integrity else service.read_published()
    assert result is None
    for filtered in (False, True):
        exit_code, human, agent_exit, agent = _read_public(capsys, integrity=integrity, filtered=filtered)
        assert exit_code == agent_exit == 2
        assert human["error"]["code"] == (
            "QUALITY_INTEGRITY_STATUS_UNAVAILABLE" if integrity else "QUALITY_STATUS_UNAVAILABLE"
        )
        assert agent["ok"] is False
        assert agent["error"]["code"] == "QUALITY_STATUS_UNAVAILABLE"
    if not integrity:
        for reader in (None, service):
            for consumer in ("close_advice", "option_position_report"):
                with pytest.raises(QualityGateBlocked) as error:
                    assert_quality_allows(consumer, account="qa", market="us", service=reader)
                assert error.value.reason_code == "QUALITY_STATUS_UNAVAILABLE"
    if invalid != "missing":
        assert path.read_bytes() == before


@pytest.mark.parametrize("integrity", [False, True])
@pytest.mark.parametrize("variant", ["full", "empty", "blocked", "extensions", "pm", "hub"])
def test_public_readers_preserve_valid_status(quality_runtime, capsys, integrity, variant):
    payload = _payload()
    if variant == "empty":
        payload["datasets"] = []
    elif variant == "blocked":
        payload["datasets"][0].update(
            status="untrusted", required_evidence_complete=False, usable_for=[],
            blocked_consumers=["close_advice", "option_position_report"], blocked_by=["OM-POS-002"],
            reason_codes=["SYNTHETIC_BLOCK"],
        )
    elif variant == "extensions":
        payload["extensions"] = {"opaque": [1, {"note": "中文"}], "current_decision_migration": {}}
    elif variant in {"pm", "hub"}:
        payload["producer"]["service"] = "portfolio-management" if variant == "pm" else "investment-quality"
    path = _write(quality_runtime, payload, integrity=integrity)
    before = path.read_bytes()
    service = OMQualityService()
    result = service.read_integrity_published() if integrity else service.read_published()
    assert result == payload
    exit_code, human, agent_exit, agent = _read_public(capsys, integrity=integrity, filtered=True)
    assert exit_code == agent_exit == 0 and agent["ok"] is True
    assert human == payload
    assert agent["data"]["schema_version"] == model.SCHEMA_VERSION
    assert agent["data"]["producer"] == payload["producer"]
    expected_datasets = [] if integrity else payload["datasets"]
    assert agent["data"]["datasets"] == expected_datasets
    if not integrity:
        if variant in {"blocked", "empty"}:
            with pytest.raises(QualityGateBlocked) as error:
                assert_quality_allows("close_advice", account="qa", market="us")
            assert error.value.reason_code == ("SYNTHETIC_BLOCK" if variant == "blocked" else "QUALITY_DATASET_UNAVAILABLE")
        else:
            assert_quality_allows("close_advice", account="qa", market="us")
        if variant == "empty":
            assert_quality_allows("option_position_report", account="qa", market="us")
    assert path.read_bytes() == before


def test_status_replacement_is_revalidated_without_repair(quality_runtime):
    payload = _payload()
    service = OMQualityService()
    for index, version in enumerate((model.SCHEMA_VERSION, "wrong-version", model.SCHEMA_VERSION)):
        payload["schema_version"] = version
        path = _write(quality_runtime, payload)
        before = path.read_bytes()
        (quality_runtime / f"retained-version-{index}.json").write_bytes(before)
        assert (service.read_published() is not None) == (version == model.SCHEMA_VERSION)
        assert path.read_bytes() == before


def test_generic_control_state_keeps_its_distinct_schema(quality_runtime):
    payload = {"schema_version": "om.quality_control_state.v1", "position_mismatches": {"synthetic": {"count": 2}}}
    path = quality_runtime / "control_state.v1.json"
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    assert QualityArtifactRepository(path).read() == payload
    assert QualityControlStateRepository(path).read()["position_mismatches"] == payload["position_mismatches"]
    assert path.read_bytes() == before


def test_off_gate_reads_neither_status_nor_schema(quality_runtime, monkeypatch):
    _write(quality_runtime, {"datasets": 7})
    monkeypatch.setenv("OM_QUALITY_ONBOARDED", "false")

    def forbidden(*args, **kwargs):
        raise AssertionError("off mode must not read artifact or schema")

    monkeypatch.setattr(Path, "read_text", forbidden)
    assert_quality_allows("close_advice", account="qa", market="us")


@pytest.mark.parametrize("where,reason", [
    ("artifact", "QUALITY_STATUS_TIME_INVALID"), ("dataset", "QUALITY_DATASET_TIME_INVALID"),
    ("stale_artifact", "QUALITY_STATUS_STALE"), ("stale_dataset", "QUALITY_DATASET_STALE"),
])
def test_existing_gate_time_policy_is_preserved(quality_runtime, where, reason):
    payload = _payload()
    value = "not-time" if not where.startswith("stale") else (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    if where.endswith("artifact"):
        payload["observed_at_utc"] = value
    else:
        payload["datasets"][0]["freshness"]["observed_at_utc"] = value
    _write(quality_runtime, payload)
    with pytest.raises(QualityGateBlocked) as error:
        assert_quality_allows("close_advice", account="qa", market="us")
    # Current jsonschema install has no date-time extras. If present, schema-invalid
    # timestamps are rejected earlier; stale but valid timestamps retain gate policy.
    format_supported = "date-time" in FormatChecker().checkers
    expected = "QUALITY_STATUS_UNAVAILABLE" if format_supported and value == "not-time" else reason
    assert error.value.reason_code == expected


def test_each_canonical_read_validates_once(quality_runtime, monkeypatch, capsys):
    _write(quality_runtime, _payload())
    real_iter_errors = Draft202012Validator.iter_errors
    seen = []

    def observed(self, instance, *args, **kwargs):
        if isinstance(instance, dict) and instance.get("schema_version") == model.SCHEMA_VERSION:
            seen.append(instance)
        return real_iter_errors(self, instance, *args, **kwargs)

    monkeypatch.setattr(Draft202012Validator, "iter_errors", observed)
    service = OMQualityService()
    actions = [
        service.read_published,
        lambda: human_main(["quality", "status", "--json"]),
        lambda: agent_main(["run", "--tool", "quality_status", "--no-local-env-file"]),
        lambda: assert_quality_allows("close_advice", account="qa", market="us"),
        lambda: assert_quality_allows("close_advice", account="qa", market="us", service=service),
    ]
    for action in actions:
        before = len(seen)
        action()
        capsys.readouterr()
        assert len(seen) == before + 1


def test_schema_construction_reuses_text_but_observes_updates(tmp_path, monkeypatch):
    original_check = Draft202012Validator.check_schema
    constructions = []

    def observed(schema, *args, **kwargs):
        constructions.append(deepcopy(schema))
        return original_check(schema, *args, **kwargs)

    monkeypatch.setattr(Draft202012Validator, "check_schema", observed)
    schema = {"title": "unique-689-cache-test", "type": "object", "properties": {"value": {"const": 1}}}
    path = tmp_path / "schema.json"
    same = tmp_path / "same-schema.json"
    text = json.dumps(schema)
    path.write_text(text)
    same.write_text(text)
    for source in (path, path, same):
        model.validate_payload({"value": 1}, schema_path=source)
    assert len(constructions) == 1
    with pytest.raises(ValueError, match="quality payload schema validation failed: value: 1 was expected"):
        model.validate_payload({"value": 2}, schema_path=path)
    assert len(constructions) == 1
    schema["properties"]["value"]["const"] = 2
    path.write_text(json.dumps(schema))
    model.validate_payload({"value": 2}, schema_path=path)
    assert len(constructions) == 2
    with pytest.raises(ValueError, match="2 was expected"):
        model.validate_payload({"value": 1}, schema_path=path)
    model.validate_payload({"value": 1}, schema_path=same)
    assert len(constructions) == 3


@pytest.mark.parametrize("invalid", ["json", "definition", "missing"])
def test_schema_errors_still_propagate(tmp_path, invalid, monkeypatch):
    path = tmp_path / "broken-schema.json"
    if invalid != "missing":
        path.write_text("{" if invalid == "json" else '{"type":"unknown-type"}')
    expected = {"json": json.JSONDecodeError, "definition": SchemaError, "missing": FileNotFoundError}[invalid]
    with pytest.raises(expected):
        model.validate_payload({}, schema_path=path)
    monkeypatch.setattr(model, "QUALITY_STATUS_SCHEMA_PATH", path)
    with pytest.raises(expected):
        model.validated_quality_status(_payload())


def test_unreadable_status_is_unavailable(quality_runtime):
    # A directory produces a real OSError without changing filesystem permissions.
    default_quality_artifact_path().mkdir()
    assert OMQualityService().read_published() is None
    with pytest.raises(QualityGateBlocked) as error:
        assert_quality_allows("close_advice", account="qa", market="us")
    assert error.value.reason_code == "QUALITY_STATUS_UNAVAILABLE"
