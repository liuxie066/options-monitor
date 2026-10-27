from __future__ import annotations

import json
from functools import partial
from hashlib import sha256
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from domain.domain.candidate_defaults import (
    DEFAULT_SELL_CALL_WINDOW,
    DEFAULT_SELL_PUT_WINDOW,
    resolve_candidate_window,
)
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_defaults import default_config
from src.application.config_validator import validate_config
from src.application.config_yaml import build_yaml_runtime_config_file, resolve_yaml_runtime_config
from src.application.layered_config import build_layered_runtime_config_from_user_config
from src.application.opening_candidate_snapshot import strategy_policy_hash
from src.application.wheel.config import WHEEL_DEFAULTS, build_wheel_policy_hash, resolve_wheel_config


REPO_ROOT = Path(__file__).resolve().parents[1]


def _runtime(**extra):
    return {"symbols": [{"symbol": "NVDA"}], **extra}


def _yaml_config(tmp_path, *, override=None, close_advice=None, features=None, market_close_advice=None, market_features=None):
    doc = {
        "accounts": {"lx": {"type": "futu", "futu_account_id": "REAL_12345678"}},
        "markets": {"us": {"accounts": ["lx"], "symbols": ["NVDA"], "overrides": {"NVDA": override or {}}}},
    }
    if close_advice is not None:
        doc["close_advice"] = close_advice
    if features is not None:
        doc["features"] = features
    if market_close_advice is not None:
        doc["markets"]["us"]["close_advice"] = market_close_advice
    if market_features is not None:
        doc["markets"]["us"]["features"] = market_features
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=path)[0]


@pytest.mark.parametrize("side", ["sell_put", "sell_call"])
def test_null_enabled_dte_is_config_error(side):
    config = _runtime()
    config["symbols"][0][side] = {"enabled": True, "strategy": "insurance_underwriting", "min_dte": None, "max_dte": 30}
    with pytest.raises(SystemExit, match=rf"\[CONFIG_ERROR\] NVDA\.{side} enabled but missing min_dte"):
        validate_config(config)


@pytest.mark.parametrize("side", ["sell_put", "sell_call"])
def test_reversed_enabled_dte_still_config_error(side):
    config = _runtime()
    config["symbols"][0][side] = {"enabled": True, "strategy": "insurance_underwriting", "min_dte": 31, "max_dte": 30}
    with pytest.raises(SystemExit, match=rf"\[CONFIG_ERROR\] NVDA\.{side}\.min_dte > NVDA\.{side}\.max_dte"):
        validate_config(config)


@pytest.mark.parametrize("side", ["sell_put", "sell_call"])
def test_null_enabled_max_dte_is_config_error(side):
    config = _runtime()
    config["symbols"][0][side] = {"enabled": True, "strategy": "insurance_underwriting", "min_dte": 7, "max_dte": None}
    with pytest.raises(SystemExit, match=rf"\[CONFIG_ERROR\] NVDA\.{side} enabled but missing max_dte"):
        validate_config(config)


@pytest.mark.parametrize(
    ("side", "min_dte", "max_dte"),
    [
        pytest.param("sell_put", "2", "10", id="put-strings"),
        pytest.param("sell_call", "2", "10", id="call-strings"),
        pytest.param("sell_put", 2, "10", id="put-int-string"),
        pytest.param("sell_call", 2, "10", id="call-int-string"),
        pytest.param("sell_put", "2", 10, id="put-string-int"),
        pytest.param("sell_call", "2", 10, id="call-string-int"),
        pytest.param("sell_put", 2, 10, id="put-integers"),
        pytest.param("sell_call", 2, 10, id="call-integers"),
        pytest.param("sell_put", 2.0, 10.0, id="put-integral-floats"),
        pytest.param("sell_call", 2.0, 10.0, id="call-integral-floats"),
    ],
)
def test_enabled_dte_window_uses_numeric_validation(side, min_dte, max_dte):
    strategy = {"enabled": True, "strategy": "insurance_underwriting", "min_dte": min_dte, "max_dte": max_dte}
    config = _runtime(symbols=[{"symbol": "NVDA", side: strategy}])
    before = deepcopy(config)

    validate_config(config)

    assert config == before
    assert type(config["symbols"][0][side]["min_dte"]) is type(min_dte)
    assert type(config["symbols"][0][side]["max_dte"]) is type(max_dte)
    defaults = DEFAULT_SELL_PUT_WINDOW if side == "sell_put" else DEFAULT_SELL_CALL_WINDOW
    window = resolve_candidate_window(config["symbols"][0][side], defaults=defaults)
    assert (window.min_dte, window.max_dte) == (2, 10)
    assert type(window.min_dte) is int
    assert type(window.max_dte) is int


@pytest.fixture
def dte_build_fixture(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    home = tmp_path / "home"
    for directory in (repo, runtime, home):
        directory.mkdir()
    env_file = home / "options-monitor.env"
    env_file.write_text("", encoding="utf-8")
    data_path = runtime / "portfolio.runtime.json"
    data_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("OM_ENV_FILE", str(env_file))
    monkeypatch.setenv("OM_DATA_CONFIG", str(data_path))

    from src.application import agent_tool_config, config_yaml, runtime_config_paths, runtime_paths, secret_store
    from src.application.secret_store import runtime as secret_runtime

    forbidden_calls = []

    def forbid(name, *_args, **_kwargs):
        forbidden_calls.append(name)
        raise AssertionError(f"unexpected fallback: {name}")

    for module, name in (
        (agent_tool_config, "resolve_runtime_root"),
        (agent_tool_config, "build_effective_env"),
        (runtime_config_paths, "build_effective_env"),
        (runtime_paths, "build_effective_env"),
        (secret_store, "default_secret_provider"),
        (secret_runtime, "default_secret_provider"),
    ):
        monkeypatch.setattr(module, name, partial(forbid, f"{module.__name__}.{name}"))

    source = repo / "config.yaml"
    system = repo / "system.json"
    system.write_text(json.dumps({"defaults": {"trade_intake": {"enabled": False}}, "markets": {"us": {}}}), encoding="utf-8")
    output = runtime / "config.us.json"
    writes = []
    real_writer = config_yaml.write_json_atomic

    def write_output(path, payload):
        writes.append(path)
        assert path == output
        real_writer(path, payload)

    monkeypatch.setattr(config_yaml, "write_json_atomic", write_output)

    def snapshot():
        return {
            str(path.relative_to(tmp_path)): None if path.is_dir() else path.read_bytes()
            for path in sorted(tmp_path.rglob("*"))
        }

    yield {
        "root": tmp_path,
        "source": source,
        "output": output,
        "document": {
            "accounts": {"fixture": {"type": "futu", "futu_account_id": "REAL_00000001"}},
            "portfolio": {"data_config": str(data_path)},
            "markets": {"us": {"accounts": ["fixture"], "symbols": ["NVDA"]}},
        },
        "build_args": {
            "repo_root": repo,
            "market": "us",
            "config_path": source,
            "system_config_path": system,
            "output_config_path": output,
            "runtime_root": runtime,
            "dry_run": False,
        },
        "writes": writes,
        "forbidden_calls": forbidden_calls,
        "snapshot": snapshot,
    }
    assert forbidden_calls == []


@pytest.mark.parametrize(
    ("side", "min_dte", "max_dte"),
    [
        pytest.param("sell_put", "2", "10", id="put-strings"),
        pytest.param("sell_call", 2, "10", id="call-int-string"),
        pytest.param("sell_put", "2", 10, id="put-string-int"),
        pytest.param("sell_call", 2.0, 10.0, id="call-integral-floats"),
    ],
)
def test_dte_build_preserves_supported_representations(dte_build_fixture, side, min_dte, max_dte):
    fixture = dte_build_fixture
    document = deepcopy(fixture["document"])
    strategy = {"enabled": True, "strategy": "insurance_underwriting", "min_dte": min_dte, "max_dte": max_dte}
    document["markets"]["us"]["overrides"] = {"NVDA": {side: strategy}}
    fixture["source"].write_text(yaml.safe_dump(document), encoding="utf-8")
    before = fixture["snapshot"]()

    result = build_yaml_runtime_config_file(**fixture["build_args"])

    assert result["ok"] is True
    assert result["write_applied"] is True
    assert result["dry_run"] is False
    assert result["output_config_path"] == str(fixture["output"])
    assert result["market"] == "us"
    assert result["accounts"] == ["fixture"]
    assert result["symbols"] == ["NVDA"]
    assert fixture["writes"] == [fixture["output"]]
    after_build = fixture["snapshot"]()
    assert after_build == {**before, str(fixture["output"].relative_to(fixture["root"])): fixture["output"].read_bytes()}

    generated = json.loads(fixture["output"].read_text(encoding="utf-8"))
    assert generated["accounts"] == ["fixture"]
    assert generated["account_settings"] == {"fixture": {"type": "futu", "futu": {"account_id": "REAL_00000001"}}}
    assert generated["portfolio"]["account"] == "fixture"
    assert generated["portfolio"]["data_config"] == str(fixture["root"] / "runtime" / "portfolio.runtime.json")
    assert generated["trade_intake"]["enabled"] is False
    assert generated["symbols"] == [{"symbol": "NVDA", side: strategy}]
    generated_strategy = generated["symbols"][0][side]
    assert generated_strategy["enabled"] is True
    assert type(generated_strategy["min_dte"]) is type(min_dte)
    assert type(generated_strategy["max_dte"]) is type(max_dte)
    assert generated["_generated"]["market"] == "us"
    assert generated["_generated"]["source_format"] == "yaml"
    assert generated["_resolved"]["market"] == "us"
    for role, filename, metadata_path, metadata_hash in (
        ("system", "system.json", "system_config_path", "system_config_sha256"),
        ("market_user", "config.yaml", "config_yaml_path", "config_yaml_sha256"),
    ):
        source = fixture["build_args"]["repo_root"] / filename
        digest = sha256(source.read_bytes()).hexdigest()
        assert result[metadata_path] == str(source)
        assert result[metadata_hash] == digest
        source_metadata = next(item for item in generated["_generated"]["sources"] if item["role"] == role)
        assert source_metadata["loaded"] is True
        assert source_metadata["path"] == filename
        assert source_metadata["sha256"] == digest

    from src.application.tool_execution import execute_tool

    response = execute_tool("config_validate", {"config_path": str(fixture["output"])})
    assert response["ok"] is True, response.get("error")
    assert response["data"]["ok"] is True
    assert response["data"]["account_count"] == 1
    assert response["data"]["accounts"] == ["fixture"]
    assert response["data"]["symbol_count"] == 1
    defaults = DEFAULT_SELL_PUT_WINDOW if side == "sell_put" else DEFAULT_SELL_CALL_WINDOW
    window = resolve_candidate_window(generated_strategy, defaults=defaults)
    assert (window.min_dte, window.max_dte) == (2, 10)
    assert type(window.min_dte) is int
    assert type(window.max_dte) is int
    assert fixture["snapshot"]() == after_build
    assert fixture["writes"] == [fixture["output"]]
    assert fixture["forbidden_calls"] == []


@pytest.mark.parametrize(
    ("side", "bounds", "message"),
    [
        pytest.param("sell_put", {"min_dte": "10", "max_dte": "2"}, "NVDA.sell_put.min_dte > NVDA.sell_put.max_dte", id="put-reversed-strings"),
        pytest.param("sell_call", {"min_dte": 10, "max_dte": "2"}, "NVDA.sell_call.min_dte > NVDA.sell_call.max_dte", id="call-reversed-mixed"),
        pytest.param("sell_put", {"min_dte": 2.5, "max_dte": 10}, "NVDA.sell_put.min_dte must be an integer", id="put-fractional"),
        pytest.param("sell_call", {"min_dte": True, "max_dte": 10}, "NVDA.sell_call.min_dte must be an integer", id="call-boolean"),
        pytest.param("sell_put", {"min_dte": None, "max_dte": 10}, "NVDA.sell_put enabled but missing min_dte", id="put-null-min"),
        pytest.param("sell_call", {"min_dte": 2}, "NVDA.sell_call enabled but missing max_dte", id="call-missing-max"),
    ],
)
def test_invalid_dte_build_preserves_outputs(dte_build_fixture, side, bounds, message):
    fixture = dte_build_fixture
    strategy = {"enabled": True, "strategy": "insurance_underwriting", **bounds}
    config = _runtime(symbols=[{"symbol": "NVDA", side: strategy}])
    before_config = deepcopy(config)
    with pytest.raises(SystemExit) as direct_error:
        validate_config(config)
    assert str(direct_error.value) == f"[CONFIG_ERROR] {message}"
    assert config == before_config
    for key, value in bounds.items():
        assert type(config["symbols"][0][side][key]) is type(value)

    document = deepcopy(fixture["document"])
    document["markets"]["us"]["overrides"] = {"NVDA": {side: strategy}}
    fixture["source"].write_text(yaml.safe_dump(document), encoding="utf-8")
    sentinel = b"retained existing output\n"
    fixture["output"].write_bytes(sentinel)
    absent_output = fixture["root"] / "uncreated" / "config.us.json"
    before_files = fixture["snapshot"]()
    for output in (fixture["output"], absent_output):
        with pytest.raises(AgentToolError) as build_error:
            build_yaml_runtime_config_file(**{**fixture["build_args"], "output_config_path": output})
        assert build_error.value.code == "CONFIG_ERROR"
        assert build_error.value.message == f"[CONFIG_ERROR] {message}"
        assert fixture["writes"] == []
        assert fixture["snapshot"]() == before_files
        assert fixture["output"].read_bytes() == sentinel
        assert not absent_output.parent.exists()
        assert fixture["forbidden_calls"] == []


@pytest.fixture
def schedule_offset_build_fixture(dte_build_fixture, request: pytest.FixtureRequest):
    monkeypatch = pytest.MonkeyPatch()
    request.addfinalizer(monkeypatch.undo)

    import os
    import socket
    import sqlite3
    import subprocess

    from src.application import config_yaml

    fixture = dte_build_fixture
    forbidden_calls = []
    prior_reads = []
    real_reader = config_yaml.read_json_object_or_empty

    def forbid(name, *_args, **_kwargs):
        forbidden_calls.append(name)
        raise AssertionError(f"unexpected schedule-offset effect: {name}")

    for owner, name in (
        (socket.socket, "connect"), (socket.socket, "connect_ex"),
        (socket, "create_connection"), (socket, "getaddrinfo"),
        (subprocess, "Popen"), (sqlite3, "connect"),
        (os, "unlink"), (os, "remove"), (os, "rmdir"),
        (Path, "unlink"), (Path, "rmdir"),
    ):
        monkeypatch.setattr(owner, name, partial(forbid, f"{owner.__name__}.{name}"))

    def read_output(path):
        assert path.resolve().is_relative_to(fixture["root"].resolve())
        prior_reads.append(path)
        return real_reader(path)

    monkeypatch.setattr(config_yaml, "read_json_object_or_empty", read_output)
    try:
        yield {**fixture, "prior_reads": prior_reads}
    finally:
        assert forbidden_calls == []
        assert fixture["forbidden_calls"] == []


def _write_schedule_offset_source(fixture, schedule_key, run_points):
    schedule = {
        "enabled": True, "timezone": "America/New_York", "beijing_timezone": "Asia/Shanghai",
        "cron_interval_min": 10, "run_window": {"start": "09:30", "end": "16:00", "breaks": []},
        "run_points": deepcopy(run_points), "gates": [],
    }
    document = deepcopy(fixture["document"])
    system_path = fixture["build_args"]["system_config_path"]
    system = json.loads(system_path.read_text(encoding="utf-8"))
    if schedule_key == "schedule":
        document["markets"]["us"]["schedule"] = schedule
    else:
        # Legacy runtime keys enter through the supported system template, not YAML authoring.
        assert schedule_key == "schedule_hk"
        system["markets"]["us"][schedule_key] = schedule
    fixture["source"].write_text(yaml.safe_dump(document), encoding="utf-8")
    system_path.write_text(json.dumps(system), encoding="utf-8")
    return schedule


def _assert_schedule_offset_values(actual, expected):
    assert actual == expected
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
    for field in ("start_plus_min", "end_minus_min"):
        if field in expected["run_points"]:
            assert type(actual["run_points"][field]) is type(expected["run_points"][field])


def _build_schedule_offset_output(fixture, schedule_key, schedule):
    before = fixture["snapshot"]()
    result = build_yaml_runtime_config_file(**fixture["build_args"])
    assert result["ok"] is True
    assert result["write_applied"] is True
    assert result["dry_run"] is False
    assert result["output_config_path"] == str(fixture["output"])
    assert result["market"] == "us"
    assert result["accounts"] == ["fixture"]
    assert result["symbols"] == ["NVDA"]
    assert fixture["prior_reads"] == [fixture["output"]]
    assert fixture["writes"] == [fixture["output"]]
    assert fixture["snapshot"]() == {
        **before, str(fixture["output"].relative_to(fixture["root"])): fixture["output"].read_bytes(),
    }

    generated = json.loads(fixture["output"].read_text(encoding="utf-8"))
    _assert_schedule_offset_values(generated[schedule_key], schedule)
    assert generated["accounts"] == ["fixture"]
    assert generated["account_settings"] == {"fixture": {"type": "futu", "futu": {"account_id": "REAL_00000001"}}}
    assert generated["portfolio"]["account"] == "fixture"
    assert generated["portfolio"]["data_config"] == str(fixture["root"] / "runtime" / "portfolio.runtime.json")
    assert generated["trade_intake"]["enabled"] is False
    assert generated["symbols"] == [{"symbol": "NVDA"}]
    for metadata_key in ("_generated", "_resolved"):
        assert generated[metadata_key]["market"] == "us"
        assert generated[metadata_key]["source_format"] == "yaml"
    for role, filename, path_key, hash_key in (
        ("system", "system.json", "system_config_path", "system_config_sha256"),
        ("market_user", "config.yaml", "config_yaml_path", "config_yaml_sha256"),
    ):
        source = fixture["build_args"]["repo_root"] / filename
        digest = sha256(source.read_bytes()).hexdigest()
        assert result[path_key] == str(source)
        assert result[hash_key] == digest
        metadata = next(item for item in generated["_generated"]["sources"] if item["role"] == role)
        assert metadata["loaded"] is True
        assert metadata["path"] == filename
        assert metadata["sha256"] == digest
        resolved_path = "default_source" if role == "system" else "config_yaml_path"
        resolved_hash = "default_sha256" if role == "system" else "config_yaml_sha256"
        assert generated["_resolved"][resolved_path] == filename
        assert generated["_resolved"][resolved_hash] == digest


def _read_schedule_offset_decision(fixture, schedule_key, now_utc):
    from datetime import datetime

    from src.application.scan_scheduler import build_scheduler_decision_payload

    state = fixture["root"] / "scheduler-state.json"
    before = fixture["snapshot"]()
    kwargs = {
        "config": fixture["output"], "state": state, "schedule_key": schedule_key,
        "account": "fixture", "force": False, "base_dir": fixture["build_args"]["repo_root"],
        "now_utc": datetime.fromisoformat(now_utc),
    }
    payload = build_scheduler_decision_payload(**kwargs)
    assert build_scheduler_decision_payload(**kwargs) == payload
    assert payload["schedule_key"] == schedule_key
    assert payload["now_utc"] == now_utc
    assert not state.exists()
    assert fixture["snapshot"]() == before
    assert fixture["prior_reads"] == [fixture["output"]]
    assert fixture["writes"] == [fixture["output"]]
    return payload


def _assert_schedule_offset_due(payload, target):
    assert payload["in_run_window"] is True
    assert payload["should_run_scan"] is True
    assert payload["is_notify_window_open"] is True
    assert payload["should_notify"] is True
    assert payload["scheduled_scan_target_market"] == target
    assert payload["scheduled_target_market"] == target
    assert payload["now_market"] == target
    assert payload["next_run_market"] == target
    assert payload["next_run_utc"] == payload["now_utc"]
    assert payload["reason"] == f"到达运行点 {target[11:16]}：执行扫描并允许通知。"


@pytest.mark.parametrize(
    ("schedule_key", "value"),
    [
        pytest.param(schedule_key, value, id=f"{schedule_key}-{value_id}")
        for schedule_key in ("schedule", "schedule_hk")
        for value, value_id in (
            (10, "integer"), (10.0, "integral-float"), ("10", "integer-string"),
            ("+10", "signed-string"), (" 10 ", "spaced-string"),
            ("1_0", "underscore-string"), ("１０", "unicode-string"),
        )
    ],
)
def test_schedule_offsets_build_preserves_consumable_representations(schedule_offset_build_fixture, schedule_key, value):
    from src.application.config_validator import validate_schedule_cfg

    fixture = schedule_offset_build_fixture
    schedule = _write_schedule_offset_source(
        fixture, schedule_key, {"hourly_minute": 0, "start_plus_min": value, "end_minus_min": value},
    )
    before = deepcopy(schedule)
    validate_schedule_cfg(schedule, schedule_key)
    _assert_schedule_offset_values(schedule, before)
    _build_schedule_offset_output(fixture, schedule_key, schedule)
    early = _read_schedule_offset_decision(fixture, schedule_key, "2026-10-19T13:39:00+00:00")
    assert early["in_run_window"] is True
    assert early["should_run_scan"] is False
    assert early["is_notify_window_open"] is False
    assert early["should_notify"] is False
    assert early["scheduled_scan_target_market"] is None
    assert early["scheduled_target_market"] is None
    assert early["next_run_utc"] == "2026-10-19T13:40:00+00:00"
    for now_utc, target in (
        ("2026-10-19T13:40:00+00:00", "2026-10-19T09:40:00-04:00"),
        ("2026-10-19T19:50:00+00:00", "2026-10-19T15:50:00-04:00"),
    ):
        _assert_schedule_offset_due(_read_schedule_offset_decision(fixture, schedule_key, now_utc), target)


@pytest.mark.parametrize(
    ("schedule_key", "field", "value", "suffix"),
    [
        pytest.param(schedule_key, field, value, suffix, id=f"{schedule_key}-{field}-{value_id}")
        for schedule_key in ("schedule", "schedule_hk")
        for field in ("start_plus_min", "end_minus_min")
        for value, value_id, suffix in (
            ("10.0", "decimal-string", "must be an integer"),
            ("1e1", "exponent-string", "must be an integer"),
            ("10.0000000000000000001", "rounded-fraction-string", "must be an integer"),
            ("1e-999", "underflow-string", "must be an integer"),
            ("", "empty-string", "must be an integer"),
            (-1, "negative", "must be >= 0"), (1.5, "fraction", "must be an integer"),
            (True, "boolean", "must be an integer"),
            ("nan", "nan", "must be a finite number"), ("inf", "infinity", "must be a finite number"),
        )
    ],
)
def test_schedule_offset_build_rejects_invalid_representation(schedule_offset_build_fixture, schedule_key, field, value, suffix):
    from src.application.config_validator import validate_schedule_cfg

    fixture = schedule_offset_build_fixture
    run_points = {"hourly_minute": 0, "start_plus_min": 10, "end_minus_min": 10, field: value}
    schedule = _write_schedule_offset_source(fixture, schedule_key, run_points)
    message = f"[CONFIG_ERROR] {schedule_key}.run_points.{field} {suffix}"
    sentinel = b'{"sentinel":"retained existing output"}\n'
    fixture["output"].write_bytes(sentinel)
    absent_output = fixture["root"] / "uncreated" / "config.us.json"
    before = fixture["snapshot"]()
    for output in (fixture["output"], absent_output):
        with pytest.raises(AgentToolError) as build_error:
            build_yaml_runtime_config_file(**{**fixture["build_args"], "output_config_path": output})
        assert build_error.value.code == "CONFIG_ERROR"
        assert build_error.value.message == message
        assert fixture["prior_reads"] == []
        assert fixture["writes"] == []
        assert fixture["snapshot"]() == before
        assert fixture["output"].read_bytes() == sentinel
        assert not absent_output.parent.exists()
    original_schedule = deepcopy(schedule)
    with pytest.raises(SystemExit) as direct_error:
        validate_schedule_cfg(schedule, schedule_key)
    assert str(direct_error.value) == message
    _assert_schedule_offset_values(schedule, original_schedule)

    run_points[field] = 10
    corrected = _write_schedule_offset_source(fixture, schedule_key, run_points)
    _build_schedule_offset_output(fixture, schedule_key, corrected)
    now_utc, target = (
        ("2026-10-19T13:40:00+00:00", "2026-10-19T09:40:00-04:00")
        if field == "start_plus_min"
        else ("2026-10-19T19:50:00+00:00", "2026-10-19T15:50:00-04:00")
    )
    _assert_schedule_offset_due(_read_schedule_offset_decision(fixture, schedule_key, now_utc), target)


@pytest.mark.parametrize(
    ("schedule_key", "variant"),
    [
        pytest.param(schedule_key, variant, id=f"{schedule_key}-{variant}")
        for schedule_key in ("schedule", "schedule_hk")
        for variant in ("missing", "null", "zero", "negative-zero")
    ],
)
def test_schedule_offsets_keep_optional_zero_semantics(schedule_offset_build_fixture, schedule_key, variant):
    from src.application.config_validator import validate_schedule_cfg

    fixture = schedule_offset_build_fixture
    run_points = {"hourly_minute": 0}
    if variant != "missing":
        value = {"null": None, "zero": 0, "negative-zero": -0.0}[variant]
        run_points.update({"start_plus_min": value, "end_minus_min": value})
    schedule = _write_schedule_offset_source(fixture, schedule_key, run_points)
    before = deepcopy(schedule)
    validate_schedule_cfg(schedule, schedule_key)
    _assert_schedule_offset_values(schedule, before)
    _build_schedule_offset_output(fixture, schedule_key, schedule)
    opening = _read_schedule_offset_decision(fixture, schedule_key, "2026-10-19T13:30:00+00:00")
    if variant == "missing":
        assert opening["in_run_window"] is True
        assert opening["should_run_scan"] is False
        assert opening["is_notify_window_open"] is False
        assert opening["should_notify"] is False
        assert opening["scheduled_scan_target_market"] is None
        assert opening["scheduled_target_market"] is None
        assert opening["next_run_utc"] == "2026-10-19T14:00:00+00:00"
    else:
        _assert_schedule_offset_due(opening, "2026-10-19T09:30:00-04:00")
        closing = _read_schedule_offset_decision(fixture, schedule_key, "2026-10-19T20:00:00+00:00")
        _assert_schedule_offset_due(closing, "2026-10-19T16:00:00-04:00")


def test_close_advice_runtime_typo_and_enabled_type():
    with pytest.raises(SystemExit, match="quote_sourse->quote_source"):
        validate_config(_runtime(close_advice={"quote_sourse": "auto"}))
    with pytest.raises(SystemExit, match=r"close_advice.enabled must be a boolean"):
        validate_config(_runtime(close_advice={"enabled": "false"}))


@pytest.mark.parametrize("position", ["root", "feature", "market", "market_feature"])
def test_close_advice_authoring_rejects_typo_before_build(tmp_path, position):
    payload = {"quote_sourse": "auto"}
    kwargs = {
        "root": {"close_advice": payload},
        "feature": {"features": {"close_advice": payload}},
        "market": {"market_close_advice": payload},
        "market_feature": {"market_features": {"close_advice": payload}},
    }[position]
    with pytest.raises(AgentToolError, match="quote_sourse->quote_source"):
        _yaml_config(tmp_path, **kwargs)


def test_close_advice_authoring_rejects_string_enabled(tmp_path):
    with pytest.raises(AgentToolError, match="close_advice.enabled must be a boolean"):
        _yaml_config(tmp_path, close_advice={"enabled": "false"})


def test_close_advice_retired_keys_still_warn(capsys):
    validate_config(_runtime(close_advice={"notify_levels": ["high"]}))
    assert "CLOSE_ADVICE_STRICT_POLICY_KEYS_IGNORED" in capsys.readouterr().err


def test_close_advice_authoring_retired_key_still_warns_after_build(tmp_path, capsys):
    cfg = _yaml_config(tmp_path, market_close_advice={"notify_levels": ["high"]})
    assert "notify_levels" in cfg["close_advice"]
    assert "CLOSE_ADVICE_STRICT_POLICY_KEYS_IGNORED" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("key", "value", "runtime_key"),
    [
        ("broker", "US", "broker"),
        ("accounts", ["lx"], "accounts"),
        ("fetch", {"source": "futu"}, "fetch"),
        ("use", ["put_base"], "use"),
        ("sell_put", {"enabled": False}, "sell_put"),
        ("covered_call", {"enabled": False}, "sell_call"),
        ("sell_call", {"enabled": False}, "sell_call"),
        ("combo_yield", {"enabled": False}, "combo_yield"),
    ],
)
def test_each_legal_symbol_override_survives_build_and_validation(tmp_path, key, value, runtime_key):
    cfg = _yaml_config(tmp_path, override={key: value})
    item = cfg["symbols"][0]
    assert runtime_key in item
    validate_config(cfg)


def test_symbol_override_typo_rejected_in_authoring_and_runtime(tmp_path):
    with pytest.raises(AgentToolError, match="covred_call->covered_call"):
        _yaml_config(tmp_path, override={"covred_call": {"enabled": False}})
    with pytest.raises(SystemExit, match="covred_call"):
        validate_config(_runtime(symbols=[{"symbol": "NVDA", "covred_call": {"enabled": False}}]))


def test_runtime_symbol_accounts_remain_accepted():
    validate_config(_runtime(accounts=["lx"], symbols=[{"symbol": "NVDA", "accounts": ["lx"]}]))


@pytest.mark.parametrize("key", ["yield_enhancement", "rebound_combo"])
def test_retired_symbol_keys_keep_targeted_runtime_error(key):
    with pytest.raises(SystemExit, match=rf"NVDA\.{key} has been removed; use NVDA\.combo_yield"):
        validate_config(_runtime(symbols=[{"symbol": "NVDA", key: {"enabled": True}}]))


def test_retired_symbol_authoring_error_keeps_combo_yield_hint(tmp_path):
    with pytest.raises(AgentToolError, match="yield_enhancement has been removed; use combo_yield"):
        _yaml_config(tmp_path, override={"yield_enhancement": {"enabled": True}})


def test_wheel_flat_min_delta_retired_defaults_warning_and_fingerprint(capsys):
    system = json.loads((REPO_ROOT / "configs/system.json").read_text(encoding="utf-8"))["defaults"]["wheel"]
    assert all("min_delta" not in source for source in (default_config()["defaults"]["wheel"], system, WHEEL_DEFAULTS))
    assert WHEEL_DEFAULTS["call"]["min_abs_delta"] == 0.25
    with_flat = _runtime(wheel={"min_delta": 0.3})
    without_flat = deepcopy(with_flat)
    del without_flat["wheel"]["min_delta"]
    validate_config(with_flat)
    assert "wheel.call.min_abs_delta" in capsys.readouterr().err
    assert build_wheel_policy_hash(with_flat, market="us", account="lx") == build_wheel_policy_hash(without_flat, market="us", account="lx")


@pytest.mark.parametrize(("market", "symbol"), [("us", "NVDA"), ("hk", "0700.HK")])
def test_layered_wheel_policy_stays_same_after_flat_default_removal(market, symbol):
    new_system = default_config()
    old_system = deepcopy(new_system)
    old_system["defaults"]["wheel"]["min_delta"] = 0.3
    user = {"accounts": ["lx"], "account_settings": {"lx": {"type": "futu", "futu": {"account_id": "REAL_12345678"}}}, "symbols": [{"symbol": symbol}]}
    configs = [
        build_layered_runtime_config_from_user_config(
            repo_root=REPO_ROOT,
            market=market,
            user_config=user,
            system_config=system,
        )[0]
        for system in (old_system, new_system)
    ]
    old_cfg, new_cfg = configs
    assert old_cfg["wheel"]["min_delta"] == 0.3
    assert "min_delta" not in new_cfg["wheel"]
    assert all(resolve_wheel_config(cfg, "lx", market=market)["call"]["min_abs_delta"] == 0.25 for cfg in configs)
    assert build_wheel_policy_hash(old_cfg, market=market, account="lx") == build_wheel_policy_hash(new_cfg, market=market, account="lx")
    assert strategy_policy_hash(old_cfg) != strategy_policy_hash(new_cfg)


def test_isolated_config_build_changes_bytes_but_not_wheel_policy(tmp_path):
    source = tmp_path / "config.yaml"
    source.write_text(yaml.safe_dump({
        "accounts": {"lx": {"type": "futu", "futu_account_id": "REAL_12345678"}},
        "markets": {"us": {"accounts": ["lx"], "symbols": ["NVDA"]}},
    }), encoding="utf-8")
    system_path = tmp_path / "system.json"
    output_path = tmp_path / "config.us.json"
    system = default_config()
    system["defaults"]["wheel"]["min_delta"] = 0.3
    system_path.write_text(json.dumps(system), encoding="utf-8")
    kwargs = {"repo_root": REPO_ROOT, "market": "us", "config_path": source, "system_config_path": system_path, "output_config_path": output_path}
    build_yaml_runtime_config_file(**kwargs)
    old_bytes = output_path.read_bytes()
    old_cfg = json.loads(old_bytes)

    del system["defaults"]["wheel"]["min_delta"]
    system_path.write_text(json.dumps(system), encoding="utf-8")
    result = build_yaml_runtime_config_file(**kwargs)
    new_bytes = output_path.read_bytes()
    new_cfg = json.loads(new_bytes)

    assert sha256(old_bytes).hexdigest() != sha256(new_bytes).hexdigest()
    assert old_cfg["wheel"]["min_delta"] == 0.3
    assert "min_delta" not in new_cfg["wheel"]
    assert resolve_wheel_config(old_cfg, "lx", market="us")["call"]["min_abs_delta"] == 0.25
    assert resolve_wheel_config(new_cfg, "lx", market="us")["call"]["min_abs_delta"] == 0.25
    assert build_wheel_policy_hash(old_cfg, market="us", account="lx") == build_wheel_policy_hash(new_cfg, market="us", account="lx")
    assert "wheel_policy_drift" not in result
