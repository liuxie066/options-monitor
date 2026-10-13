"""Public query completeness and Tool continuation over the same business pages."""
from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from src.application import query_cursor
from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools import positions, materialization_impl
from src.application.agent_tools.project_reader import redacted_text
from src.application.bot import tools as bot_tools
from src.application.performance import query, query_http
from src.application.performance.query_contracts import PerformanceQueryError
from src.application.ledger.api import encode_evidence_cursor
from test_option_performance_agent_tool import _patch_dependencies, _report
from test_performance_query_service import QUERY, TOKEN, _remote, service as service
from test_performance_service_v2 import _event


@pytest.fixture(autouse=True)
def signing(monkeypatch):
    monkeypatch.setattr(query_cursor, "_key", lambda: "isolated-performance-presentation-key")


def _local(payload, *, now_ms=None):
    return query.option_performance_report_tool(
        payload, load_runtime_config=positions.load_runtime_config,
        resolve_public_data_config_path=positions.resolve_public_data_config_path,
        normalize_broker=positions.normalize_broker,
        resolve_option_positions_repo=positions.open_performance_query_ledger_from_data_config,
        build_option_period_performance=positions.build_option_period_performance,
        repo_base=positions.repo_base, mask_path=positions.mask_path, now_ms=now_ms,
    )


def _large_report():
    report = _report(include_rows=True)
    report["rows"] = [{"fact_id": f"fact-{n}", "symbol": "TEST", "note": "收益🙂" * (2000 if n == 0 else 140)} for n in range(7)]
    report["breakdowns"] = {group: [{"key": f"group-{n}", "note": "收益🙂" * 140} for n in range(7)] for group in report["breakdowns"]}
    return report


def _bytes(data):
    return len(json.dumps(data, ensure_ascii=False).encode())


def _collect(call, payload, expected, *, initial=None):
    rows, fragments, pages = [], "", []
    request = dict(payload)
    for _ in range(100):
        data = initial if not pages and initial is not None else call(request)[0]
        pages.append(data)
        assert _bytes(data) <= 5000
        assert "page_cursor" not in data["pagination"]
        assert data["pagination"]["next_cursor"] == data["next_cursor"]
        if "text" in data:
            fragments += data["text"]
            if data["body_complete"]:
                raw = json.dumps(expected[len(rows)], ensure_ascii=False, sort_keys=True).encode()
                assert fragments == redacted_text(raw)
                rows.append(expected[len(rows)])
                fragments = ""
        else:
            assert not fragments
            rows.extend(data["rows"])
        if not data["next_cursor"]:
            assert not data["pagination"]["has_more"]
            assert not fragments
            return rows, pages
        request = {**payload, "cursor": data["next_cursor"]}
    pytest.fail("continuation did not make progress")


def test_local_query_is_complete_and_tool_reconstructs_every_row(monkeypatch):
    report = _large_report()
    _patch_dependencies(monkeypatch, report=report)
    payload = {**QUERY, "view": "rows", "limit": 3}
    full = _local(payload)[0]
    assert full["rows"] == report["rows"][:3] and _bytes(full) > 5000
    assert "text" not in full and full["pagination"]["returned_count"] == 3
    rows, pages = _collect(positions.OPTION_PERFORMANCE_REPORT_TOOL.call, payload, report["rows"])
    assert rows == report["rows"]
    assert any("text" in page for page in pages) and any(page["rows"] for page in pages)
    assert {page["pagination"]["offset"] for page in pages} == {0, 3, 6}
    summary = _local({**QUERY, "view": "summary"})[0]
    assert summary["breakdowns"] == report["breakdowns"] and _bytes(summary) > 6000
    report.pop("rows")
    tool = positions.OPTION_PERFORMANCE_REPORT_TOOL.call({**QUERY, "view": "summary"})[0]
    assert _bytes(tool) <= 6000 and tool["detail_query"]
    assert tool["quality"] == report["quality"]
    assert positions.OPTION_PERFORMANCE_REPORT_TOOL.call(QUERY)[0] == report


def test_http_full_rows_and_summary_survive_tool_and_bot(service, monkeypatch):
    original = query_http.build_option_period_performance

    def enlarged(*args, **kw):
        value = original(*args, **kw)
        if "rows" in value:
            for n, row in enumerate(value["rows"]):
                row["note"] = "收益🙂" * (1600 if n == 0 else 140)
        for group in value["breakdowns"].values():
            for row in group:
                row["note"] = "收益🙂" * 700
        return value

    monkeypatch.setattr(query_http, "build_option_period_performance", enlarged)
    monkeypatch.setenv("OM_PERFORMANCE_SERVICE_URL", service[0])
    monkeypatch.setenv("OM_PERFORMANCE_SERVICE_TOKEN", TOKEN)
    payload = {**QUERY, "view": "rows", "limit": 2}
    full = _remote(service[0], {**payload, "limit": 40})[0]
    assert _bytes(full) > 5000 and "text" not in full
    rows, pages = _collect(positions.OPTION_PERFORMANCE_REPORT_TOOL.call, payload, full["rows"])
    assert rows == full["rows"]
    full_summary = _remote(service[0], {**QUERY, "view": "summary"})[0]
    assert _bytes(full_summary) > 6000
    response = bot_tools.call_read_tool("option_performance_report", payload, allowed_tools=("option_performance_report",))
    assert response["ok"] and response["data"]["next_cursor"]
    assert response["data"]["text"] == pages[0]["text"]
    observation = bot_tools.model_observation("option_performance_report", response)
    assert observation["data"]["next_cursor"] == response["data"]["next_cursor"]
    summary = positions.OPTION_PERFORMANCE_REPORT_TOOL.call({**QUERY, "view": "summary"})[0]
    assert _bytes(summary) <= 6000 and summary["quality"] == full_summary["quality"]


@pytest.mark.parametrize("view", ["rows", "breakdowns"])
def test_composed_budget_includes_duplicate_rows_and_both_cursors(monkeypatch, view):
    report = _large_report()
    _patch_dependencies(monkeypatch, report=report)
    payload = {**QUERY, "view": view, "limit": 3}
    rows, pages = _collect(positions.OPTION_PERFORMANCE_REPORT_TOOL.call, payload, report["rows"] if view == "rows" else report["breakdowns"]["symbols"])
    assert rows == (report["rows"] if view == "rows" else report["breakdowns"]["symbols"])
    assert all(page["quality"] == report["quality"] for page in pages)
    if view == "breakdowns":
        assert all(page["breakdowns"] == {"symbols": page["rows"]} for page in pages if page["rows"])


def test_cursor_binds_query_and_revalidates_source_and_authority(monkeypatch):
    report = _large_report()
    _patch_dependencies(monkeypatch, report=report)
    payload = {**QUERY, "view": "rows", "limit": 3}
    cursor = positions.OPTION_PERFORMANCE_REPORT_TOOL.call(payload)[0]["next_cursor"]
    for change in ({"account": "sy"}, {"limit": 4}, {"config_path": "/other"}, {"symbol": "OTHER"}):
        with pytest.raises(AgentToolError, match="cursor_invalidated"):
            positions.OPTION_PERFORMANCE_REPORT_TOOL.call({**payload, **change, "cursor": cursor})
    encoded, signature = cursor.rsplit(".", 1)
    forged = encoded + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(AgentToolError, match="cursor_invalidated"):
        positions.OPTION_PERFORMANCE_REPORT_TOOL.call({**payload, "cursor": forged})
    report["rows"][-1]["note"] = "changed outside current page"
    with pytest.raises(AgentToolError, match="source_changed"):
        positions.OPTION_PERFORMANCE_REPORT_TOOL.call({**payload, "cursor": cursor})
    report["rows"][-1]["note"] = "收益🙂" * 140
    monkeypatch.setattr(positions, "load_runtime_config", lambda **kw: (Path("/tmp/config.us.json"), {"accounts": ["sy"], "portfolio": {}}))
    report["scope"]["accounts"] = ["sy"]
    with pytest.raises(AgentToolError, match="source_changed"):
        positions.OPTION_PERFORMANCE_REPORT_TOOL.call({**payload, "cursor": cursor})


@pytest.mark.parametrize("body", ["legacy", "", {}, False, 0])
def test_query_rejects_legacy_body_cursor_before_io(monkeypatch, body):
    _patch_dependencies(monkeypatch, report=_large_report())
    payload = {**QUERY, "view": "rows", "limit": 2}
    page = _local(payload)[0]
    state = query_cursor.decode_evidence_cursor(page["pagination"]["page_cursor"], query_cursor._key())
    state["body_cursor"] = body
    monkeypatch.setattr(positions, "load_runtime_config", lambda **kw: pytest.fail("invalid cursor entered config I/O"))
    with pytest.raises(PerformanceQueryError, match="cursor_invalidated"):
        _local({**payload, "cursor": query_cursor.encode_cursor(state)})


def test_old_unwrapped_tool_cursor_is_rejected_and_null_body_query_is_compatible(monkeypatch):
    _patch_dependencies(monkeypatch, report=_large_report())
    payload = {**QUERY, "view": "rows", "limit": 2}
    page = _local(payload)[0]
    cursor = page["next_cursor"]
    with pytest.raises(AgentToolError, match="cursor_invalidated"):
        positions.OPTION_PERFORMANCE_REPORT_TOOL.call({**payload, "cursor": cursor})
    state = query_cursor.decode_evidence_cursor(cursor, query_cursor._key())
    state["body_cursor"] = None
    assert _local({**payload, "cursor": query_cursor.encode_cursor(state)})[0]["pagination"]["offset"] == 2


def test_missing_signer_distinguishes_complete_finite_page_and_needed_continuation(monkeypatch):
    report = _report(include_rows=True)
    _patch_dependencies(monkeypatch, report=report)
    monkeypatch.setattr(query_cursor, "_key", lambda: (_ for _ in ()).throw(query_cursor.CursorError("continuation_unavailable")))
    payload = {**QUERY, "view": "rows"}
    finite = positions.OPTION_PERFORMANCE_REPORT_TOOL.call(payload)[0]
    assert finite["rows"] == report["rows"] and finite["coverage"]["status"] == "complete"
    report["rows"].append(deepcopy(report["rows"][0]))
    partial = positions.OPTION_PERFORMANCE_REPORT_TOOL.call({**payload, "limit": 1})[0]
    assert partial["next_cursor"] is None and partial["continuation_status"] == "continuation_unavailable"
    assert partial["coverage"]["complete"] is False
    report["rows"][0]["note"] = "收益🙂" * 2000
    fragment = positions.OPTION_PERFORMANCE_REPORT_TOOL.call(payload)[0]
    assert not fragment["body_complete"] and fragment["coverage"]["status"] == "partial"


@pytest.mark.parametrize("view", ["rows", "summary"])
def test_irreducible_metadata_has_explicit_error_without_truncating_quality(monkeypatch, view):
    report = _large_report()
    report["quality"]["diagnostics"] = ["未确认数据" * 2000]
    _patch_dependencies(monkeypatch, report=report)
    with pytest.raises(AgentToolError, match="scope_too_broad"):
        positions.OPTION_PERFORMANCE_REPORT_TOOL.call({**QUERY, "view": view})
    assert _local({**QUERY, "view": view})[0]["quality"] == report["quality"]


@pytest.mark.parametrize("layer", ["outer", "page", "body"])
def test_expired_cursor_layers_are_rejected(monkeypatch, layer):
    _patch_dependencies(monkeypatch, report=_large_report())
    payload = {**QUERY, "view": "rows", "limit": 3}
    cursor = positions.OPTION_PERFORMANCE_REPORT_TOOL.call(payload)[0]["next_cursor"]
    state = query_cursor.decode_evidence_cursor(cursor, query_cursor._key())
    old = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())
    if layer == "outer":
        cursor = encode_evidence_cursor(state, query_cursor._key(), now=old)
    else:
        name = "page_cursor" if layer == "page" else "body_cursor"
        inner = query_cursor.decode_evidence_cursor(state[name], query_cursor._key())
        state[name] = encode_evidence_cursor(inner, query_cursor._key(), now=old)
        cursor = query_cursor.encode_cursor(state)
    with pytest.raises(AgentToolError, match="cursor_invalidated"):
        positions.OPTION_PERFORMANCE_REPORT_TOOL.call({**payload, "cursor": cursor})


def test_capture_invalid_scope_is_translated_before_dependencies(monkeypatch):
    with pytest.raises(AgentToolError) as exc:
        materialization_impl.capture_option_performance_evidence(
            {"config_key": "invalid"}, apply=False, normalize_broker=positions.normalize_broker,
            **{name: lambda *a, **kw: pytest.fail("dependency called") for name in (
                "load_runtime_config", "resolve_public_data_config_path", "resolve_option_positions_repo",
                "open_performance_evidence_repository", "repo_base", "mask_path", "evidence_collector")},
        )
    assert exc.value.code == "INPUT_ERROR"


def test_canonical_query_and_transport_do_not_import_tool_contracts():
    for path in ("src/application/performance/query.py", "src/infrastructure/option_performance_client.py"):
        tree = ast.parse(Path(path).read_text())
        assert not any(isinstance(node, ast.ImportFrom) and (node.module or "").startswith(("src.application.agent_tools", "src.application.agent_tool_contracts")) for node in ast.walk(tree))


@pytest.mark.parametrize("period,instant", [("mtd", "2026-10-31T15:55:00+00:00"), ("ytd", "2026-12-31T15:55:00+00:00")])
def test_current_period_freezes_across_body_row_and_business_pages(service, monkeypatch, period, instant):
    original = query_http.build_option_period_performance
    for n in range(3):
        event = _event(f"oct-open-{n}", "open", "2026-10-31T10:00:00")
        service[1].upsert_trade_event(replace(event, lot_id=f"oct-lot-{n}", contract_key=replace(event.contract_key, underlying_symbol=f"OCT{n}", expiration_ymd="2026-11-30")))

    class Clock(datetime):
        timestamp_ms = int(datetime.fromisoformat(instant).timestamp() * 1000)

        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(cls.timestamp_ms / 1000, tz=tz)

    monkeypatch.setattr(query, "datetime", Clock)

    def enlarged(*args, **kw):
        value = original(*args, **kw)
        for n, row in enumerate(value.get("rows", [])):
            row["note"] = "收益🙂" * (800 if n == 0 else 150)
        return value

    monkeypatch.setattr(query_http, "build_option_period_performance", enlarged)
    monkeypatch.setenv("OM_PERFORMANCE_SERVICE_URL", service[0])
    monkeypatch.setenv("OM_PERFORMANCE_SERVICE_TOKEN", TOKEN)
    payload = {"config_key": "us", "period": period, "view": "rows", "limit": 2}
    first = positions.OPTION_PERFORMANCE_REPORT_TOOL.call(payload)[0]
    assert first["next_cursor"] and not first["body_complete"]
    expected = _remote(service[0], {**payload, "limit": 40})[0]
    Clock.timestamp_ms += 86400000
    fresh = _remote(service[0], {**payload, "limit": 40})[0]
    assert fresh["period"] != first["period"]
    rows, pages = _collect(positions.OPTION_PERFORMANCE_REPORT_TOOL.call, payload, expected["rows"], initial=first)
    assert rows == expected["rows"]
    assert all(page["period"] == first["period"] for page in pages)
    assert any(page["pagination"]["offset"] == 2 for page in pages)


def test_local_replay_rechecks_real_configured_account_authority(service, monkeypatch):
    _, writer, path, cfg = service
    monkeypatch.setattr(positions, "load_runtime_config", lambda **kw: (path.parent / "config.us.json", cfg))
    monkeypatch.setattr(positions, "resolve_public_data_config_path", lambda *a: path.parent / "portfolio.json")
    monkeypatch.setattr(positions, "open_performance_query_ledger_from_data_config", lambda **kw: (path, writer))
    monkeypatch.setattr(positions, "repo_base", lambda: path.parent)
    payload = {**QUERY, "account": "lx", "view": "rows", "limit": 1}
    first = positions.OPTION_PERFORMANCE_REPORT_TOOL.call(payload)[0]
    assert first["next_cursor"]
    cfg["accounts"] = ["sy"]
    with pytest.raises(AgentToolError) as exc:
        positions.OPTION_PERFORMANCE_REPORT_TOOL.call({**payload, "cursor": first["next_cursor"]})
    assert exc.value.code == "READ_ERROR"
    assert exc.value.details["reason_codes"] == ["scope_unproven"]


def test_empty_page_is_complete_without_cursor(monkeypatch):
    report = _report(include_rows=True)
    report["rows"] = []
    _patch_dependencies(monkeypatch, report=report)
    data = positions.OPTION_PERFORMANCE_REPORT_TOOL.call({**QUERY, "view": "rows"})[0]
    assert data["rows"] == [] and not data["pagination"]["has_more"]
    assert data["next_cursor"] is None and data["coverage"]["status"] == "complete"


@pytest.mark.parametrize("max_chars", [0, True, "10", -1])
def test_page_text_rejects_invalid_internal_fragment_cap(max_chars):
    from src.application.agent_tools.project_reader import page_text
    with pytest.raises(query_cursor.CursorError, match="INPUT_ERROR"):
        page_text(b"text", relative_name="row", resource="option_performance_report", scope={}, max_chars=max_chars)


@pytest.mark.parametrize("unavailable_owner", ["canonical", "tool"])
def test_independent_signer_absence_does_not_fabricate_continuation(monkeypatch, unavailable_owner):
    from src.application.agent_tools import performance_presentation
    _patch_dependencies(monkeypatch, report=_large_report())
    owner = query if unavailable_owner == "canonical" else performance_presentation
    monkeypatch.setattr(owner, "encode_cursor", lambda state: (_ for _ in ()).throw(query_cursor.CursorError("continuation_unavailable")))
    page = positions.OPTION_PERFORMANCE_REPORT_TOOL.call({**QUERY, "view": "rows", "limit": 3})[0]
    assert _bytes(page) <= 5000 and page["next_cursor"] is None
    assert page["continuation_status"] == "continuation_unavailable"
    assert page["coverage"]["status"] == "partial" and not page["body_complete"]
