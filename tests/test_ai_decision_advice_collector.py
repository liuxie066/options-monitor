from __future__ import annotations

import json
from pathlib import Path

from src.application.ai_decision_advice.collector import (
    EVIDENCE_OUTPUT_SCHEMA,
    ModelCallResult,
    build_evidence_input,
    compute_cutoffs,
    run_evidence_collector,
    validate_evidence_payload,
)
from src.application.ai_decision_advice.evidence_store import (
    freeze_evidence_index,
    read_evidence_records,
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
    return ModelCallResult(
        output_text=json.dumps(output),
        usage={"input_tokens": 1, "output_tokens": 2},
        response_sha256="b" * 64,
        web_search_audit={
            "count": 1,
            "status_counts": {"completed": 1},
            "provider_call_id": "must-not-persist",
            "query": "must-not-persist",
        },
    )


def _ok_output(symbols: list[str], evidence_by_symbol: dict[str, list] | None = None) -> dict:
    evidence_by_symbol = evidence_by_symbol or {}
    return {
        "results": [
            {"symbol": symbol, "evidence": evidence_by_symbol.get(symbol, [])}
            for symbol in symbols
        ]
    }


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
    assert validated["missing_symbols"] == []


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
                "source": {"title": "t", "publisher": "p", "url": " ", "published_at": None},
            }]}]},
            batch_symbols=["NVDA"],
        )


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
    assert audit["identity_snapshot_hash"] == snapshot["content_sha256"]
    assert "web_search_calls" not in audit
    assert audit["web_search_audit"] == {
        "count": 1,
        "status_counts": {"completed": 1},
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
    assert sorted(summary.completed_symbols) == ["AAPL", "NVDA"]
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


def test_collector_budget_exhaustion_marks_unfinished(tmp_path: Path) -> None:
    snapshot = _identity_snapshot(["NVDA", "AAPL", "MSFT"])
    prompt = compile_prompt_pack(PROMPT_PACK_EVIDENCE)
    ticks = iter([0.0, 100.0, 400.0, 400.0])

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
