from __future__ import annotations

import json
import threading
from pathlib import Path

from src.application.ai_decision_advice.collector import (
    EVIDENCE_OUTPUT_SCHEMA,
    ModelCallResult,
    build_evidence_input,
    compute_cutoffs,
    normalize_https_url,
    run_evidence_collector,
    sanitize_source_text,
    validate_evidence_payload,
)
from src.application.ai_decision_advice.evidence_store import (
    freeze_evidence_index,
    read_evidence_records,
    resolve_latest_success_snapshot,
)
from src.application.ai_decision_advice.identity import (
    build_observation_set,
    build_symbol_identity_snapshot,
)
from src.application.ai_decision_advice.prompts import (
    PROMPT_PACK_EVIDENCE,
    compile_prompt_pack,
)
from datetime import datetime, timezone

import pytest


def _identity_snapshot(symbols: list[str], unavailable: set[str] | None = None) -> dict:
    unavailable = unavailable or set()
    observed = build_observation_set(scan_symbols=symbols)

    def _snap(market, codes):
        return {
            code: {"name": f"{code} Inc", "exchange_type": "NASDAQ"}
            for code in codes
            if code not in unavailable
        }

    return build_symbol_identity_snapshot(
        observed,
        market_snapshot_provider=_snap,
        basic_info_provider=lambda codes: [],
        observed_at="2026-08-09T00:00:00+00:00",
    )


def _runner(output: dict) -> ModelCallResult:
    symbols = [str(row["symbol"]) for row in output.get("results") or []]
    citations = []
    for row in output.get("results") or []:
        for item in row.get("evidence") or []:
            citations.append(
                {
                    "url": item["source"]["url"],
                    "title": item["source"].get("title"),
                    "publisher": item["source"].get("publisher"),
                }
            )
    return ModelCallResult(
        output_text=json.dumps(output),
        usage={"input_tokens": 1, "output_tokens": 2},
        response_sha256="b" * 64,
        web_search_audit={
            "count": len(symbols),
            "unattributed_count": 0,
            "auxiliary_count": 0,
            "status_counts": {
                "completed": len(symbols),
                "failed": 0,
                "in_progress": 0,
                "unknown": 0,
            },
            "symbols": {
                symbol: {
                    "completed": 1,
                    "failed": 0,
                    "in_progress": 0,
                    "unknown": 0,
                }
                for symbol in symbols
            },
            "provider_call_id": "must-not-persist",
            "query": "must-not-persist",
        },
        native_citations=tuple(citations),
        native_search_sources=tuple(
            {"symbol": row["symbol"], "url": item["source"]["url"]}
            for row in output.get("results") or []
            for item in row.get("evidence") or []
        ),
    )


def _ok_output(symbols: list[str], evidence_by_symbol: dict[str, list] | None = None) -> dict:
    evidence_by_symbol = evidence_by_symbol or {}
    return {
        "results": [
            {"symbol": symbol, "evidence": evidence_by_symbol.get(symbol, [])}
            for symbol in symbols
        ]
    }


def _strict_schema_gaps(schema: dict, *, path: str = "$") -> list[str]:
    gaps: list[str] = []
    if schema.get("type") == "object":
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        property_names = set(properties)
        if required != property_names:
            gaps.append(
                f"{path}: required={sorted(required)!r} properties={sorted(property_names)!r}"
            )
        if schema.get("additionalProperties") is not False:
            gaps.append(f"{path}: additionalProperties must be false")
        for name, child in properties.items():
            gaps.extend(_strict_schema_gaps(child, path=f"{path}.properties.{name}"))
    items = schema.get("items")
    if isinstance(items, dict):
        gaps.extend(_strict_schema_gaps(items, path=f"{path}.items"))
    return gaps


def test_validate_payload_happy_path() -> None:
    payload = _ok_output(["NVDA"], {
        "NVDA": [
            {
                "topic": "regulatory",
                "claim": "SEC filed a comment letter",
                "event_status": "developing",
                "event_time": "2026-08-08",
                "source": {
                    "title": "t",
                    "publisher": "p",
                    "url": "https://example.com",
                    "published_at": "2026-08-08",
                },
            }
        ]
    })
    validated = validate_evidence_payload(payload, batch_symbols=["NVDA"])
    assert validated["results"]["NVDA"][0]["topic"] == "regulatory"


def test_validate_payload_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError):
        validate_evidence_payload("not a dict", batch_symbols=["NVDA"])
    with pytest.raises(ValueError):
        validate_evidence_payload({"other": []}, batch_symbols=["NVDA"])
    with pytest.raises(ValueError, match="unexpected symbol"):
        validate_evidence_payload({"results": [{"symbol": "AAPL", "evidence": []}]}, batch_symbols=["NVDA"])
    with pytest.raises(ValueError, match="url"):
        validate_evidence_payload(
            {"results": [{"symbol": "NVDA", "evidence": [{
                "topic": "t", "claim": "c", "event_status": "developing",
                "event_time": None,
                "source": {"title": "t", "publisher": "p", "url": " ", "published_at": None},
            }]}]},
            batch_symbols=["NVDA"],
        )
    with pytest.raises(ValueError, match="event_time"):
        validate_evidence_payload(
            {"results": [{"symbol": "NVDA", "evidence": [{
                "topic": "t", "claim": "c", "event_status": "developing",
                "source": {
                    "title": "t",
                    "publisher": "p",
                    "url": "https://example.com",
                    "published_at": None,
                },
            }]}]},
            batch_symbols=["NVDA"],
        )
    with pytest.raises(ValueError, match="missing symbols"):
        validate_evidence_payload({"results": []}, batch_symbols=["NVDA"])


def test_build_evidence_input_carries_identity_and_cutoff() -> None:
    snapshot = _identity_snapshot(["NVDA"])
    from src.application.ai_decision_advice.identity import identity_by_symbol

    rows = identity_by_symbol(snapshot)
    payload = build_evidence_input(
        ["NVDA"],
        identity_rows=rows,
        cutoff_by_symbol={"NVDA": "2026-08-01T00:00:00+00:00"},
    )
    item = payload["symbols"][0]
    assert item["symbol"] == "NVDA"
    assert item["company_name"] == "NVDA Inc"
    assert item["query_cutoff"] == "2026-08-01T00:00:00+00:00"
    assert item["first_search_lookback_days"] == 30


def test_compute_cutoffs_first_search_vs_incremental() -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    cutoffs = compute_cutoffs(
        {"NVDA": "2026-08-05T00:00:00+00:00", "AAPL": None},
        now=now,
    )
    assert cutoffs["NVDA"] == "2026-08-05T00:00:00+00:00"
    assert cutoffs["AAPL"].startswith("2026-07-10")


def test_collector_success_appends_records(tmp_path: Path) -> None:
    snapshot = _identity_snapshot(["NVDA", "AAPL"])
    prompt = compile_prompt_pack(PROMPT_PACK_EVIDENCE)
    output = _ok_output(["NVDA", "AAPL"], {
        "NVDA": [
            {
                "topic": "regulatory",
                "claim": "claim",
                "event_status": "developing",
                "event_time": None,
                "source": {"title": "t", "publisher": "p", "url": "https://x", "published_at": None},
            }
        ]
    })
    summary = run_evidence_collector(
        base=tmp_path,
        queue_symbols=["NVDA", "AAPL"],
        identity_snapshot=snapshot,
        cutoff_by_symbol={},
        compiled_prompt=prompt,
        model_runner=lambda instructions, payload, schema, timeout: _runner(output),
        evidence_run_id="run-1",
    )
    assert summary.budget_exhausted is False
    assert sorted(summary.completed_symbols) == ["AAPL", "NVDA"]
    assert summary.failed_symbols == []
    records = read_evidence_records(tmp_path)
    kinds = sorted(row["kind"] for row in records)
    assert "batch_audit" in kinds
    assert kinds.count("symbol_status") == 2
    assert kinds.count("symbol_evidence") == 1
    audit = [row for row in records if row["kind"] == "batch_audit"][0]
    assert audit["identity_artifact_sha256"] == snapshot["content_sha256"]
    assert audit["identity_snapshot_semantic_sha256"] == snapshot["semantic_sha256"]
    assert "web_search_calls" not in audit
    assert audit["web_search_audit"] == {
        "count": 2,
        "unattributed_count": 0,
        "auxiliary_count": 0,
        "status_counts": {
            "completed": 2,
            "failed": 0,
            "in_progress": 0,
            "unknown": 0,
        },
        "symbols": {
            "AAPL": {"completed": 1, "failed": 0, "in_progress": 0, "unknown": 0},
            "NVDA": {"completed": 1, "failed": 0, "in_progress": 0, "unknown": 0},
        },
    }
    assert audit["model_response_audit"]["response_sha256"] == "b" * 64
    assert "must-not-persist" not in json.dumps(audit)
    assert audit["prompt"]["compiled_sha256"] == prompt.compiled_sha256


def test_collector_identity_unavailable_skips_model(tmp_path: Path) -> None:
    snapshot = _identity_snapshot(["NVDA", "AAPL"], unavailable={"AAPL"})
    prompt = compile_prompt_pack(PROMPT_PACK_EVIDENCE)
    called: list[str] = []

    def runner(instructions, payload, schema, timeout):
        called.extend(item["symbol"] for item in payload["symbols"])
        return _runner(_ok_output(["NVDA"]))

    summary = run_evidence_collector(
        base=tmp_path,
        queue_symbols=["NVDA", "AAPL"],
        identity_snapshot=snapshot,
        cutoff_by_symbol={},
        compiled_prompt=prompt,
        model_runner=runner,
        evidence_run_id="run-2",
    )
    assert called == ["NVDA"]
    assert summary.completed_symbols == ["NVDA"]
    assert summary.identity_unavailable_symbols == ["AAPL"]
    index = freeze_evidence_index(
        tmp_path,
        symbols=["AAPL", "NVDA"],
        now=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )
    assert index.view_for("AAPL").coverage == "identity_unavailable"
    assert index.view_for("NVDA").coverage == "completed"


def test_collector_repair_once_then_success(tmp_path: Path) -> None:
    snapshot = _identity_snapshot(["NVDA"])
    prompt = compile_prompt_pack(PROMPT_PACK_EVIDENCE)
    attempts: list[int] = []

    def runner(instructions, payload, schema, timeout):
        attempts.append(1)
        if len(attempts) == 1:
            return ModelCallResult(output_text="not json", usage={})
        return _runner(_ok_output(["NVDA"]))

    summary = run_evidence_collector(
        base=tmp_path,
        queue_symbols=["NVDA"],
        identity_snapshot=snapshot,
        cutoff_by_symbol={},
        compiled_prompt=prompt,
        model_runner=runner,
        evidence_run_id="run-3",
    )
    assert len(attempts) == 2
    assert summary.repair_attempts == 1
    assert summary.completed_symbols == ["NVDA"]
    audit = [row for row in read_evidence_records(tmp_path) if row["kind"] == "batch_audit"][0]
    assert audit["repair_attempted"] is True


def test_collector_repair_still_invalid_marks_failed(tmp_path: Path) -> None:
    snapshot = _identity_snapshot(["NVDA"])
    prompt = compile_prompt_pack(PROMPT_PACK_EVIDENCE)

    def runner(instructions, payload, schema, timeout):
        return ModelCallResult(output_text="still bad", usage={})

    summary = run_evidence_collector(
        base=tmp_path,
        queue_symbols=["NVDA"],
        identity_snapshot=snapshot,
        cutoff_by_symbol={},
        compiled_prompt=prompt,
        model_runner=runner,
        evidence_run_id="run-4",
    )
    assert summary.failed_symbols == ["NVDA"]
    assert summary.completed_symbols == []
    statuses = [row for row in read_evidence_records(tmp_path) if row["kind"] == "symbol_status"]
    assert statuses[0]["search_status"] == "failed"
    assert "last_success_at" not in statuses[0]


def test_provider_exception_fails_only_current_batch_and_continues(
    tmp_path: Path,
) -> None:
    snapshot = _identity_snapshot(["NVDA", "AAPL"])
    calls: list[str] = []

    def runner(instructions, payload, schema, timeout):
        symbol = payload["symbols"][0]["symbol"]
        calls.append(symbol)
        if symbol == "NVDA":
            raise TimeoutError("provider timeout")
        return _runner(_ok_output([symbol]))

    summary = run_evidence_collector(
        base=tmp_path,
        queue_symbols=["NVDA", "AAPL"],
        identity_snapshot=snapshot,
        cutoff_by_symbol={},
        compiled_prompt=compile_prompt_pack(PROMPT_PACK_EVIDENCE),
        model_runner=runner,
        evidence_run_id="run-provider-exception",
        batch_size=1,
    )
    assert sorted(calls) == ["AAPL", "NVDA"]
    assert summary.failed_symbols == ["NVDA"]
    assert summary.completed_symbols == ["AAPL"]
    statuses = {
        row["symbol"]: row
        for row in read_evidence_records(tmp_path)
        if row.get("kind") == "symbol_status"
    }
    assert statuses["NVDA"]["search_status"] == "failed"
    assert statuses["AAPL"]["search_status"] == "completed"


def test_collector_budget_exhaustion_marks_unfinished(tmp_path: Path) -> None:
    snapshot = _identity_snapshot(["NVDA", "AAPL", "MSFT"])
    prompt = compile_prompt_pack(PROMPT_PACK_EVIDENCE)
    ticks = iter([0.0, 0.0, 0.0, 400.0])

    summary = run_evidence_collector(
        base=tmp_path,
        queue_symbols=["NVDA", "AAPL", "MSFT"],
        identity_snapshot=snapshot,
        cutoff_by_symbol={},
        compiled_prompt=prompt,
        model_runner=lambda instructions, payload, schema, timeout: _runner(
            _ok_output(list(item["symbol"] for item in payload["symbols"]))
        ),
        evidence_run_id="run-5",
        budget_seconds=300,
        batch_size=2,
        max_concurrent_batches=1,
        monotonic=lambda: next(ticks, 500.0),
    )
    assert summary.budget_exhausted is True
    assert summary.unfinished_symbols == ["MSFT"]
    assert sorted(summary.completed_symbols) == ["AAPL", "NVDA"]


def test_evidence_prompt_pack_compiles() -> None:
    prompt = compile_prompt_pack(PROMPT_PACK_EVIDENCE)
    assert prompt.pack == "external_evidence"
    assert len(prompt.fragments) == 4
    assert len(prompt.compiled_sha256) == 64
    assert "web_search" in prompt.prompt
    assert "JSON" in prompt.prompt


def test_output_schema_shape() -> None:
    assert EVIDENCE_OUTPUT_SCHEMA["required"] == ["results"]
    item_props = EVIDENCE_OUTPUT_SCHEMA["properties"]["results"]["items"]["properties"]
    assert set(item_props) == {"symbol", "evidence"}
    assert _strict_schema_gaps(EVIDENCE_OUTPUT_SCHEMA) == []


def test_collector_requires_attributable_completed_search(tmp_path: Path) -> None:
    snapshot = _identity_snapshot(["NVDA"])
    result = _runner(_ok_output(["NVDA"]))
    bad = ModelCallResult(
        output_text=result.output_text,
        usage=result.usage,
        web_search_audit={
            "count": 1,
            "unattributed_count": 1,
            "symbols": {"NVDA": {"completed": 0}},
        },
    )
    summary = run_evidence_collector(
        base=tmp_path,
        queue_symbols=["NVDA"],
        identity_snapshot=snapshot,
        cutoff_by_symbol={},
        compiled_prompt=compile_prompt_pack(PROMPT_PACK_EVIDENCE),
        model_runner=lambda *args: bad,
        evidence_run_id="run-search-missing",
    )
    assert summary.completed_symbols == []
    assert summary.failed_symbols == ["NVDA"]
    statuses = [
        row
        for row in read_evidence_records(tmp_path)
        if row.get("kind") == "symbol_status"
    ]
    assert statuses[-1]["search_status"] == "failed"


def test_citation_binding_drops_unbound_and_normalizes_source(tmp_path: Path) -> None:
    snapshot = _identity_snapshot(["NVDA"])
    output = _ok_output(
        ["NVDA"],
        {
            "NVDA": [
                {
                    "topic": "regulatory",
                    "claim": "bound claim",
                    "event_status": "developing",
                    "event_time": None,
                    "source": {
                        "title": "model title",
                        "publisher": "model publisher",
                        "url": "https://Example.COM/report#fragment",
                        "published_at": None,
                    },
                },
                {
                    "topic": "regulatory",
                    "claim": "unbound claim",
                    "event_status": "developing",
                    "event_time": None,
                    "source": {
                        "title": "x",
                        "publisher": "x",
                        "url": "https://unbound.example/report",
                        "published_at": None,
                    },
                },
            ]
        },
    )
    base_result = _runner(output)
    result = ModelCallResult(
        output_text=base_result.output_text,
        usage={},
        web_search_audit=base_result.web_search_audit,
        native_citations=(
            {
                "url": "https://example.com/report",
                "title": "# [Native]\nTitle",
                "publisher": "**Publisher**",
            },
        ),
        native_search_sources=(
            {"symbol": "NVDA", "url": "https://example.com/report"},
        ),
    )
    summary = run_evidence_collector(
        base=tmp_path,
        queue_symbols=["NVDA"],
        identity_snapshot=snapshot,
        cutoff_by_symbol={},
        compiled_prompt=compile_prompt_pack(PROMPT_PACK_EVIDENCE),
        model_runner=lambda *args: result,
        evidence_run_id="run-citation",
    )
    assert summary.completed_symbols == ["NVDA"]
    evidence = [
        row
        for row in read_evidence_records(tmp_path)
        if row.get("kind") == "symbol_evidence"
    ]
    assert len(evidence) == 1
    assert evidence[0]["claim"] == "bound claim"
    assert evidence[0]["url"] == "https://example.com/report"
    assert evidence[0]["source"] == {
        "title": "Native Title",
        "publisher": "Publisher",
        "visible_domain": "example.com",
        "url": "https://example.com/report",
        "published_at": None,
    }
    assert "model title" not in json.dumps(evidence)


def test_citation_binding_cannot_cross_symbol_search_sources(tmp_path: Path) -> None:
    snapshot = _identity_snapshot(["NVDA", "AAPL"])
    output = _ok_output(
        ["NVDA", "AAPL"],
        {
            "NVDA": [
                {
                    "topic": "regulatory",
                    "claim": "NVDA claim",
                    "event_status": "developing",
                    "event_time": None,
                    "source": {
                        "title": "n",
                        "publisher": "p",
                        "url": "https://example.com/nvda",
                        "published_at": None,
                    },
                }
            ],
            "AAPL": [
                {
                    "topic": "regulatory",
                    "claim": "AAPL claim",
                    "event_status": "developing",
                    "event_time": None,
                    "source": {
                        "title": "a",
                        "publisher": "p",
                        "url": "https://example.com/aapl",
                        "published_at": None,
                    },
                }
            ],
        },
    )
    base_result = _runner(output)
    swapped = ModelCallResult(
        output_text=base_result.output_text,
        usage={},
        web_search_audit=base_result.web_search_audit,
        native_citations=base_result.native_citations,
        native_search_sources=(
            {"symbol": "NVDA", "url": "https://example.com/aapl"},
            {"symbol": "AAPL", "url": "https://example.com/nvda"},
        ),
    )
    summary = run_evidence_collector(
        base=tmp_path,
        queue_symbols=["NVDA", "AAPL"],
        identity_snapshot=snapshot,
        cutoff_by_symbol={},
        compiled_prompt=compile_prompt_pack(PROMPT_PACK_EVIDENCE),
        model_runner=lambda *args: swapped,
        evidence_run_id="run-cross-symbol-citation",
    )
    assert sorted(summary.completed_symbols) == ["AAPL", "NVDA"]
    records = read_evidence_records(tmp_path)
    assert not [row for row in records if row.get("kind") == "symbol_evidence"]
    statuses = [row for row in records if row.get("kind") == "symbol_status"]
    assert all(row["evidence_count"] == 0 for row in statuses)


def test_failed_auxiliary_web_action_fails_batch(tmp_path: Path) -> None:
    snapshot = _identity_snapshot(["NVDA"])
    base_result = _runner(_ok_output(["NVDA"]))
    failed = ModelCallResult(
        output_text=base_result.output_text,
        usage={},
        web_search_audit={
            **base_result.web_search_audit,
            "count": 2,
            "auxiliary_count": 1,
            "status_counts": {
                "completed": 1,
                "failed": 1,
                "in_progress": 0,
                "unknown": 0,
            },
        },
    )
    summary = run_evidence_collector(
        base=tmp_path,
        queue_symbols=["NVDA"],
        identity_snapshot=snapshot,
        cutoff_by_symbol={},
        compiled_prompt=compile_prompt_pack(PROMPT_PACK_EVIDENCE),
        model_runner=lambda *args: failed,
        evidence_run_id="run-failed-auxiliary",
    )
    assert summary.completed_symbols == []
    assert summary.failed_symbols == ["NVDA"]


def test_url_and_source_sanitization_fail_closed() -> None:
    assert normalize_https_url("http://example.com") is None
    assert normalize_https_url("https://EXAMPLE.com:443/path#x") == "https://example.com/path"
    userinfo_url = "https://" + ":".join(("userinfo", "placeholder")) + "@example.com"
    assert normalize_https_url(userinfo_url) is None
    assert normalize_https_url("https://" + "a" * 64 + ".com/path") is None
    assert sanitize_source_text("# **Hello**\nWorld", fallback="x") == "Hello World"


def test_collector_runs_two_batches_concurrently(tmp_path: Path) -> None:
    snapshot = _identity_snapshot(["NVDA", "AAPL"])
    rendezvous = threading.Barrier(2)

    def runner(instructions, payload, schema, timeout):
        rendezvous.wait(timeout=2)
        symbols = [item["symbol"] for item in payload["symbols"]]
        return _runner(_ok_output(symbols))

    summary = run_evidence_collector(
        base=tmp_path,
        queue_symbols=["NVDA", "AAPL"],
        identity_snapshot=snapshot,
        cutoff_by_symbol={},
        compiled_prompt=compile_prompt_pack(PROMPT_PACK_EVIDENCE),
        model_runner=runner,
        evidence_run_id="run-concurrent",
        batch_size=1,
    )

    assert sorted(summary.completed_symbols) == ["AAPL", "NVDA"]
    assert summary.failed_symbols == []


def test_incremental_and_full_search_modes_declare_exact_snapshot_members(
    tmp_path: Path,
) -> None:
    snapshot = _identity_snapshot(["NVDA"])
    identity_hash = snapshot["symbols"][0]["identity_semantic_sha256"]
    prompt = compile_prompt_pack(PROMPT_PACK_EVIDENCE)

    def item(url: str, claim: str) -> dict:
        return {
            "topic": "regulatory",
            "claim": claim,
            "event_status": "developing",
            "event_time": None,
            "source": {
                "title": claim,
                "publisher": "Publisher",
                "url": url,
                "published_at": None,
            },
        }

    def collect(run_id: str, mode: str, rows: list[dict]) -> None:
        output = _ok_output(["NVDA"], {"NVDA": rows})
        summary = run_evidence_collector(
            base=tmp_path,
            queue_symbols=["NVDA"],
            identity_snapshot=snapshot,
            cutoff_by_symbol={"NVDA": "2026-08-01T00:00:00+00:00"},
            search_mode_by_symbol={"NVDA": mode},
            compiled_prompt=prompt,
            model_runner=lambda *args: _runner(output),
            evidence_run_id=run_id,
        )
        assert summary.completed_symbols == ["NVDA"]

    collect("run-a", "full", [item("https://a.example/fact", "A")])
    status_a, rows_a, error = resolve_latest_success_snapshot(
        read_evidence_records(tmp_path),
        symbol="NVDA",
        identity_semantic_sha256=identity_hash,
    )
    assert error is None
    assert status_a is not None
    assert [row["claim"] for row in rows_a] == ["A"]
    hash_a = status_a["semantic_snapshot_hash"]

    collect("run-empty", "incremental", [])
    status_empty, rows_empty, error = resolve_latest_success_snapshot(
        read_evidence_records(tmp_path),
        symbol="NVDA",
        identity_semantic_sha256=identity_hash,
    )
    assert error is None
    assert [row["claim"] for row in rows_empty] == ["A"]
    assert status_empty["semantic_snapshot_hash"] == hash_a

    collect("run-b", "incremental", [item("https://b.example/fact", "B")])
    _status_b, rows_b, error = resolve_latest_success_snapshot(
        read_evidence_records(tmp_path),
        symbol="NVDA",
        identity_semantic_sha256=identity_hash,
    )
    assert error is None
    assert sorted(row["claim"] for row in rows_b) == ["A", "B"]

    collect("run-full-b", "full", [item("https://b.example/fact", "B")])
    _status_full, rows_full, error = resolve_latest_success_snapshot(
        read_evidence_records(tmp_path),
        symbol="NVDA",
        identity_semantic_sha256=identity_hash,
    )
    assert error is None
    assert [row["claim"] for row in rows_full] == ["B"]

    collect("run-full-zero", "full", [])
    status_zero, rows_zero, error = resolve_latest_success_snapshot(
        read_evidence_records(tmp_path),
        symbol="NVDA",
        identity_semantic_sha256=identity_hash,
    )
    assert error is None
    assert rows_zero == ()
    assert status_zero["active_evidence_refs"] == []
