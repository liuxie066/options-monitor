from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application.account_config import accounts_from_config, normalize_accounts
from src.application.agent_tool_contracts import build_response, mask_path
from src.application.agent_tools import runtime_status_impl as runtime
from src.application.quality import intake_checks
from src.application.runtime_config_paths import read_json_object_or_empty
from src.application.trades.state import (
    STATE_BUCKETS,
    load_trade_intake_state,
    normalize_trade_intake_state_payload,
)
from src.interfaces.cli.observability_ops import (
    add_runtime_observability_commands,
    handle_observability_command,
)


COUNT_KEYS = (
    "processed_count", "failed_count", "unresolved_count", "pending_count",
    "receipt_count", "receipt_confirmed_count", "receipt_failed_count",
)
KNOWN = {
    "processed_deal_ids": {"private-deal-canary": {"receipt": {"delivery_confirmed": True}}},
    "failed_deal_ids": {"invented-f": {"receipt": {"status": "failed"}}},
    "unresolved_deal_ids": {"invented-u": {"receipt": {"status": "unresolved"}}},
}
KNOWN_COUNTS = [1, 1, 1, 2, 3, 1, 2]
BLOBS = {
    "empty": b"{}",
    "known": json.dumps(KNOWN).encode(),
    "json": b'{"private-error-canary":',
    "utf8": b"\xff",
    "root": b"[]",
    "bucket": b'{"processed_deal_ids":[],"failed_deal_ids":{"invented-f":{}}}',
    "opaque": b'{"unresolved_deal_ids":{"invented-u":17},"extra":"preserved"}',
    "io": b"{}",
}


def _state(path: Path, kind: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "directory":
        path.mkdir()
    elif kind != "missing":
        path.write_bytes(BLOBS[kind])


def _runtime_fixture(tmp_path, monkeypatch, kinds, *, enabled=True, explicit=False, listener="listening"):
    """Real collector; unrelated service/secret/ledger boundaries are synthetic.

    No configured provider is called. Paths, config and ordinary status artifacts
    are real fresh fixtures. Both reconciliation owners run their no-ledger branch.
    """
    names = [f"demo{index}" for index in range(len(kinds))]
    cfg_path = tmp_path / "config.us.json"
    cfg = {
        "accounts": names,
        "account_settings": {
            name: {"type": "futu", "futu": {
                "account_id": f"synthetic-{index}", "host": "127.0.0.1", "port": 19000 + index,
            }} for index, name in enumerate(names)
        },
        "trade_intake": {"enabled": enabled, "mode": "dry-run"},
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    shared = tmp_path / "output_shared/state"
    shared.mkdir(parents=True)
    (shared / "last_run.json").write_text(json.dumps({"status": "skipped"}), encoding="utf-8")
    state_paths = []
    for index, kind in enumerate(kinds):
        state_path = (
            shared / "auto_trade_intake_state.json" if len(kinds) == 1
            else shared / "trade_intake" / names[index] / "state.json"
        )
        _state(state_path, kind)
        state_paths.append(state_path)
        status_path = (
            shared / "auto_trade_intake_status.json" if len(kinds) == 1
            else state_path.with_name("status.json")
        )
        if listener is not None:
            status_path.write_text(json.dumps({
                "status": listener, "last_fee_sync": {"pending_count": 9},
                "last_backfill_deal_count": 4,
            }), encoding="utf-8")
    original_read = Path.read_text

    def read_text(path, *args, **kwargs):
        if path in state_paths and kinds[state_paths.index(path)] == "io":
            raise PermissionError("private-error-canary")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(runtime, "ledger_store_payload", lambda _path: {"runtime_root": str(tmp_path)})
    monkeypatch.setattr(runtime, "build_wheel_activation_readiness", lambda **_kw: {
        "monitoring_gate": "not_required", "storage_status": "not_required", "enabled_account_count": 0,
    })
    monkeypatch.setattr(runtime, "service_drift_status", lambda **_kw: {"summary": {"ok": True}})
    monkeypatch.setattr(runtime, "build_effective_env_with_status", lambda **_kw: (
        SimpleNamespace(warnings=[], values={}), {"warnings": []},
    ))
    monkeypatch.setattr(runtime, "_bot_runtime_summary", lambda **_kw: {"enabled": False})
    monkeypatch.setattr(runtime, "build_channel_status", lambda **_kw: {"channels": {}})
    monkeypatch.setattr(runtime, "_config_authority_payload", lambda *_args, **_kw: {"ok": True, "market": "us"})
    payload = {"config_path": str(cfg_path), "config_key": "us", "include_service_status": False}
    if explicit:
        payload["trade_intake_state_path"] = str(state_paths[0])
        payload["trade_intake_status_path"] = str(shared / "auto_trade_intake_status.json")
        payload["trade_intake_audit_path"] = str(shared / "auto_trade_intake_audit.jsonl")
    dependencies = {
        "load_runtime_config": lambda **_kw: (cfg_path, json.loads(cfg_path.read_text())),
        "normalize_accounts": normalize_accounts, "accounts_from_config": accounts_from_config,
        "read_json_object_or_empty": read_json_object_or_empty, "repo_base": lambda: tmp_path,
        "mask_path": mask_path,
    }
    return payload, dependencies, state_paths


@pytest.mark.parametrize("raw", [{}, {"failed_deal_ids": {}}, {"unresolved_deal_ids": {"u": 7}, "extra": [1]}, KNOWN])
def test_canonical_normalizer_preserves_loader_contract(tmp_path, raw):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(raw))
    expected = {name: dict(raw.get(name, {})) for name in STATE_BUCKETS}
    assert normalize_trade_intake_state_payload(raw, path=path) == expected
    assert load_trade_intake_state(path) == expected
    assert json.loads(path.read_text()) == raw


@pytest.mark.parametrize("raw", [[], None, {"processed_deal_ids": []}, {"failed_deal_ids": None}, {"unresolved_deal_ids": 1}])
def test_canonical_normalizer_preserves_validation_errors(tmp_path, raw):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError) as normalized:
        normalize_trade_intake_state_payload(raw, path=path)
    with pytest.raises(ValueError) as loaded:
        load_trade_intake_state(path)
    assert str(normalized.value) == str(loaded.value)
    assert str(path) in str(loaded.value)
    assert json.loads(path.read_text()) == raw


def test_canonical_missing_state_still_initializes_without_writing(tmp_path):
    path = tmp_path / "missing-parent/state.json"
    assert load_trade_intake_state(path) == {name: {} for name in STATE_BUCKETS}
    assert not path.parent.exists()


@pytest.mark.parametrize("kind,expected", [
    ("empty", "available"), ("known", "available"), ("opaque", "available"),
    ("missing", "missing"), ("directory", "unreadable"), ("io", "unreadable"),
    ("json", "invalid"), ("utf8", "invalid"), ("root", "invalid"), ("bucket", "invalid"),
])
def test_state_observation_reads_once_and_preserves_bytes(tmp_path, monkeypatch, kind, expected):
    path = tmp_path / "state.json"
    _state(path, kind)
    before = path.read_bytes() if path.is_file() else None
    reads = []
    decodes = []
    original = Path.read_text
    original_decode = json.loads

    def read_text(target, *args, **kwargs):
        if target == path:
            reads.append(target)
            if kind == "io":
                raise PermissionError("synthetic read denial")
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    def decode(text, *args, **kwargs):
        decodes.append(text)
        return original_decode(text, *args, **kwargs)

    monkeypatch.setattr(json, "loads", decode)
    info = runtime._trade_intake_state_info(path, base=tmp_path)
    assert info["read_status"] == expected
    assert len(reads) == (0 if kind in {"missing", "directory"} else 1)
    assert len(decodes) == (0 if kind in {"missing", "directory", "io", "utf8"} else 1)
    assert (path.read_bytes() if path.is_file() else None) == before
    if kind in {"known", "opaque", "bucket"}:
        assert info["json"] == json.loads(before)
    if kind in {"json", "utf8", "root", "bucket", "io", "directory"}:
        with pytest.raises(ValueError):
            load_trade_intake_state(path)


def test_missing_path_with_existing_directory_entry_is_not_startup_absence(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    original = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda target: object() if target == path else original(target))
    assert runtime._trade_intake_state_info(path, base=tmp_path)["read_status"] == "unreadable"
    with pytest.raises(ValueError, match="cannot read trade intake state"):
        load_trade_intake_state(path)
    assert not path.exists()


@pytest.mark.parametrize("kind,enabled,listener,availability,warning", [
    ("empty", True, "listening", "available", False),
    ("known", True, None, "available", False),
    ("opaque", True, "listening", "available", False),
    ("missing", True, "starting", "unavailable", False),
    ("missing", True, "listening", "unavailable", False),
    ("json", True, "listening", "unavailable", True),
    ("utf8", True, "listening", "unavailable", True),
    ("root", True, "listening", "unavailable", True),
    ("bucket", True, "listening", "unavailable", True),
    ("io", True, "listening", "unavailable", True),
    ("directory", True, "listening", "unavailable", True),
    ("missing", False, "starting", "not_required", False),
    ("known", False, "listening", "available", False),
    ("json", False, "listening", "unavailable", False),
])
@pytest.mark.parametrize("explicit", [False, True])
def test_real_public_facade_state_availability(tmp_path, monkeypatch, kind, enabled, listener, availability, warning, explicit):
    payload, dependencies, paths = _runtime_fixture(tmp_path, monkeypatch, [kind], enabled=enabled, explicit=explicit, listener=listener)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    private, _, _ = runtime.private_runtime_status_tool(payload, **dependencies)
    public, warnings, _ = runtime.runtime_status_tool(payload, **dependencies)
    summary = public["trade_intake"]["summary"]
    complete = availability in {"available", "not_required"}
    assert summary["state_availability"] == availability
    assert summary["state_counts_complete"] is complete
    assert summary["listener_status"] == listener
    assert public["trade_intake"]["enabled"] is enabled
    assert ("TRADE_INTAKE_STATE_UNAVAILABLE" in public["summary"]["warning_codes"]) is warning
    assert public["summary"]["ok"] is (not warning)
    assert bool(warnings) is warning
    old_summary = private["trade_intake"]["summary"]
    assert [summary[key] for key in COUNT_KEYS] == ([old_summary[key] for key in COUNT_KEYS] if complete else [None] * 7)
    if kind == "known":
        assert [summary[key] for key in COUNT_KEYS] == KNOWN_COUNTS
    if kind == "bucket":
        assert old_summary["failed_count"] == 1
    assert summary["fee_pending_count"] == (9 if listener is not None else None)
    assert summary["last_backfill_deal_count"] == (4 if listener is not None else None)
    serialized = json.dumps(public)
    for private_value in (str(paths[0]), "private-deal-canary", "private-error-canary"):
        assert private_value not in serialized
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("other,availability", [
    ("missing", "partial"), ("json", "partial"), ("io", "partial"),
    ("bucket", "partial"), ("empty", "available"), ("known", "available"),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_actual_multisource_facade_never_exposes_partial_totals(tmp_path, monkeypatch, other, availability, reverse):
    kinds = ["known", other]
    if reverse:
        kinds.reverse()
    payload, dependencies, _ = _runtime_fixture(tmp_path, monkeypatch, kinds)
    private, _, _ = runtime.private_runtime_status_tool(payload, **dependencies)
    public, _, _ = runtime.runtime_status_tool(payload, **dependencies)
    assert public["trade_intake"]["source_count"] == 2
    assert public["trade_intake"]["enabled_source_count"] == 2
    summary = public["trade_intake"]["summary"]
    assert summary["state_availability"] == availability
    assert summary["state_counts_complete"] is (availability == "available")
    assert private["trade_intake"]["summary"]["processed_count"] == (2 if other == "known" else 1)
    assert [summary[key] for key in COUNT_KEYS] == (
        [private["trade_intake"]["summary"][key] for key in COUNT_KEYS]
        if availability == "available" else [None] * 7
    )
    assert summary["fee_pending_count"] == 18


@pytest.mark.parametrize("enabled,kind,availability", [
    (True, "missing", "unavailable"), (False, "missing", "not_required"), (False, "known", "available"),
])
def test_all_sources_missing_or_disabled_history(tmp_path, monkeypatch, enabled, kind, availability):
    payload, dependencies, _ = _runtime_fixture(tmp_path, monkeypatch, [kind, kind], enabled=enabled)
    public, warnings, _ = runtime.runtime_status_tool(payload, **dependencies)
    summary = public["trade_intake"]["summary"]
    assert summary["state_availability"] == availability
    assert not warnings
    assert summary["processed_count"] == (None if enabled else 2 if kind == "known" else 0)


@pytest.mark.parametrize("kind", ["empty", "json"])
def test_legacy_config_fallback_uses_same_state_contract(tmp_path, monkeypatch, kind):
    payload, dependencies, _ = _runtime_fixture(tmp_path, monkeypatch, [kind])
    cfg_path = Path(payload["config_path"])
    cfg_path.write_text(json.dumps({"accounts": ["demo"], "trade_intake": {"enabled": True}}))
    private, _, _ = runtime.private_runtime_status_tool(payload, **dependencies)
    public, _, _ = runtime.runtime_status_tool(payload, **dependencies)
    assert private["trade_intake"]["sources"][0]["id"] == "legacy"
    assert public["trade_intake"]["summary"]["state_counts_complete"] is (kind == "empty")
    assert public["trade_intake"]["summary"]["processed_count"] == (0 if kind == "empty" else None)


@pytest.mark.parametrize("disabled_kind,availability", [("missing", "available"), ("known", "available"), ("json", "partial")])
@pytest.mark.parametrize("reverse", [False, True])
def test_disabled_history_keeps_contributing_without_requiring_absent_state(tmp_path, disabled_kind, availability, reverse):
    rows = []
    for index, (kind, enabled) in enumerate([("known", True), (disabled_kind, False)]):
        path = tmp_path / str(index) / "state.json"
        _state(path, kind)
        info = runtime._trade_intake_state_info(path, base=tmp_path)
        rows.append({"enabled": enabled, "state": info, "summary": runtime._trade_intake_summary(info.get("json") or {}, {})})
    if reverse:
        rows.reverse()
    summary = runtime._aggregate_trade_intake_summaries([row["summary"] for row in rows])
    summary.update(runtime._trade_intake_state_availability(rows))
    public = runtime._status_safe_trade_intake({"enabled": True, "sources": rows, "summary": summary})
    assert summary["processed_count"] == (2 if disabled_kind == "known" else 1)
    assert public["summary"]["state_availability"] == availability
    assert public["summary"]["processed_count"] == (summary["processed_count"] if availability == "available" else None)


@pytest.mark.parametrize("kind,availability", [("missing", "unavailable"), ("json", "unavailable"), ("empty", "available")])
@pytest.mark.parametrize("output", ["normal", "journal", "json"])
def test_real_cli_dispatch_preserves_public_unknown(tmp_path, monkeypatch, kind, availability, output):
    payload, dependencies, _ = _runtime_fixture(tmp_path, monkeypatch, [kind])
    parser = argparse.ArgumentParser()
    add_runtime_observability_commands(parser.add_subparsers(dest="command"))
    argv = ["status", "--config-path", payload["config_path"], "--config-key", "us"]
    argv += {"normal": [], "journal": ["--journal-summary"], "json": ["--json"]}[output]
    args = parser.parse_args(argv)
    calls = []

    def execute(name, actual_payload):
        calls.append((name, actual_payload))
        data, warnings, meta = runtime.runtime_status_tool(actual_payload, **dependencies)
        return build_response(tool_name=name, ok=True, data=data, warnings=warnings, meta=meta)

    stdout = StringIO()
    with redirect_stdout(stdout):
        assert handle_observability_command(args, execute_tool_fn=execute) == 0
    text = stdout.getvalue()
    assert calls == [("runtime_status", {"config_path": payload["config_path"], "config_key": "us"})]
    if output == "json":
        summary = json.loads(text)["data"]["trade_intake"]["summary"]
        assert summary["state_availability"] == availability
        assert summary["processed_count"] == (0 if kind == "empty" else None)
    else:
        assert f"state={availability}" in text
        assert ("processed=0" if kind == "empty" else "processed=-") in text
    assert "private-error-canary" not in text


@pytest.mark.parametrize("kind", ["missing", "json", "bucket", "io", "empty", "known"])
@pytest.mark.parametrize("audit_available", [True, False])
def test_actual_private_output_quality_does_not_rehabilitate_incomplete_state(tmp_path, monkeypatch, kind, audit_available):
    payload, dependencies, _ = _runtime_fixture(tmp_path, monkeypatch, [kind])
    private, _, _ = runtime.private_runtime_status_tool(payload, **dependencies)
    source = private["trade_intake"]["sources"][0]
    # Explicit synthetic audit control, not execution of a ledger/audit preview.
    source["summary"]["audit_reconciliation"] = {"available": audit_available, "pending_after_reconcile_count": 0}
    evidence_values = []
    original_evidence_ref = intake_checks.evidence_ref

    def observe_evidence(**kwargs):
        evidence_values.append(kwargs["value"])
        return original_evidence_ref(**kwargs)

    monkeypatch.setattr(intake_checks, "evidence_ref", observe_evidence)
    if kind not in {"empty", "known"}:
        def forbidden_fallback(*_args, **_kwargs):
            pytest.fail("incomplete state must not be reread for pending age")
        monkeypatch.setattr(intake_checks, "_state_oldest_pending_age_seconds", forbidden_fallback)
    datasets = intake_checks.build_trade_intake_datasets(
        runtime_statuses=[private], accounts=["demo0"], market="us", repo_root=tmp_path,
        observed_at_utc="2026-10-21T10:00:00Z", now=datetime(2026, 10, 21, 10, tzinfo=timezone.utc),
    )
    dataset = datasets[0]
    from jsonschema import Draft202012Validator, FormatChecker
    schema = json.loads((Path(__file__).resolve().parents[1] / "contracts/quality-monitoring/quality_status.v1.schema.json").read_text())
    Draft202012Validator(
        {"$ref": "#/$defs/datasetStatus", "$defs": schema["$defs"]}, format_checker=FormatChecker(),
    ).validate(dataset)
    first, second, audit = dataset["checks"]
    assert audit["status"] == ("pass" if audit_available else "unknown")
    if kind not in {"empty", "known"}:
        assert dataset["status"] == "unavailable"
        for check in (first, second):
            assert check["status"] == "unknown"
            assert check["reason_code"] == "INTAKE_STATE_UNAVAILABLE"
            assert all(value is None for value in check["observed"].values())
        evidence = evidence_values[0]
        assert evidence["state_counts_complete"] is False
        assert evidence["pending_count"] is None
    elif kind == "empty":
        assert first["status"] == second["status"] == "pass"
        assert dataset["status"] == ("trusted" if audit_available else "unavailable")
    else:
        assert second["status"] == "fail"


@pytest.mark.parametrize("marker", [False, True, None])
def test_quality_audit_delegation_is_independent_of_state_output_mask(tmp_path, marker):
    summary = {
        "pending_count": 1, "unresolved_count": 1, "failed_count": 0,
        "reconciliation_preview_available": True, "delegated_lifecycle_pending_deal_ids": ["u"],
        "audit_reconciliation": {"available": True, "pending_after_reconcile_count": 1,
                                 "delegated_lifecycle_pending_deal_ids": ["u"]},
    }
    if marker is not None:
        summary["state_counts_complete"] = marker
    state = {"json": {"unresolved_deal_ids": {"u": {}}}}
    runtime_payload = {"trade_intake": {"enabled": True, "summary": summary, "state": state}}
    before = deepcopy(runtime_payload)
    result = intake_checks.build_trade_intake_datasets(
        runtime_statuses=[runtime_payload], accounts=["demo"], market="us", repo_root=tmp_path,
        observed_at_utc="2026-10-21T10:00:00Z", now=datetime(2026, 10, 21, 10, tzinfo=timezone.utc),
    )[0]
    assert result["checks"][2]["status"] == "pass"
    assert result["checks"][2]["observed"] == {
        "pending_after_reconcile_count": 1, "audit_reconciliation_available": True,
        "delegated_lifecycle_pending_count": 1, "missing_local_terminal_count": 0,
    }
    assert result["checks"][0]["status"] == ("unknown" if marker is False else "pass")
    assert runtime_payload == before


@pytest.mark.parametrize("age_seconds,expected", [(60, "warn"), (600, "fail")])
def test_complete_quality_preserves_pending_grace(tmp_path, age_seconds, expected):
    now = datetime(2026, 10, 21, 10, tzinfo=timezone.utc)
    state = {"json": {"failed_deal_ids": {"f": {"created_at_ms": int(now.timestamp() * 1000) - age_seconds * 1000}}}}
    summary = {"state_counts_complete": True, "pending_count": 1, "failed_count": 1,
               "audit_reconciliation": {"available": True, "pending_after_reconcile_count": 0}}
    dataset = intake_checks.build_trade_intake_datasets(
        runtime_statuses=[{"trade_intake": {"enabled": True, "state": state, "summary": summary}}],
        accounts=["demo"], market="us", repo_root=tmp_path, observed_at_utc=now.isoformat(), now=now,
    )[0]
    assert dataset["checks"][0]["status"] == expected
    assert dataset["checks"][0]["observed"]["oldest_pending_age_seconds"] == age_seconds
    assert dataset["checks"][1]["status"] == "fail"


def test_disabled_quality_still_not_applicable(tmp_path):
    dataset = intake_checks.build_trade_intake_datasets(
        runtime_statuses=[{"trade_intake": {"enabled": False, "summary": {"state_counts_complete": False}}}],
        accounts=["demo"], market="us", repo_root=tmp_path,
        observed_at_utc="2026-10-21T10:00:00Z", now=datetime(2026, 10, 21, 10, tzinfo=timezone.utc),
    )[0]
    assert dataset["status"] == "trusted"
    assert all(check["reason_code"] == "TRADE_INTAKE_NOT_APPLICABLE" for check in dataset["checks"])
