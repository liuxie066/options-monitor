from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.application.ai_decision_advice.collector import ModelCallResult
from src.application.ai_decision_advice.orchestration import (
    run_or_reuse_ai_decision_advice,
)


NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _state_dir(tmp_path: Path, run_id: str = "run-1", account: str = "lx") -> Path:
    state_dir = tmp_path / "output_runs" / run_id / "accounts" / account / "state"
    _write(
        state_dir / "opening_candidate_snapshot.json",
        {
            "market": "US",
            "content_sha256": "snap",
            "ranked_candidates": [
                {
                    "candidate_id": "put-1",
                    "rank": 1,
                    "strategy_mode": "put",
                    "facts": {"symbol": "NVDA", "strike": 100, "expiry": "2026-09-18"},
                }
            ],
        },
    )
    _write(
        state_dir / "portfolio_context.json",
        {
            "stocks_by_symbol": {"NVDA": {"shares": 100, "currency": "USD"}},
            "cash_by_currency": {"USD": 1000},
        },
    )
    _write(
        state_dir / "option_positions_context.json",
        {"position_lots": []},
    )
    return state_dir


def _runner(output: dict):
    def run(instructions, payload, schema, timeout):
        return ModelCallResult(
            raw_response={"ok": True},
            output_text=json.dumps(output, ensure_ascii=False),
            usage={},
        )

    return run


def _model_output(payload_bindings: dict, *, run_id: str, account_ref: str) -> dict:
    return {
        "schema": "ai_decision_advice.v1",
        "run_id": run_id,
        "account_ref": account_ref,
        "market": "US",
        "input_bindings": payload_bindings,
        "strategies": [
            {
                "strategy_family": "sell_put",
                "status": "completed",
                "decisions": [
                    {
                        "scope_symbol": None,
                        "baseline_candidate_id": "put-1",
                        "action": "defer",
                        "selected_candidate_id": None,
                        "rationale": {
                            "risk_mechanism": "m",
                            "candidate_effect": "e",
                            "decision_reason": "r",
                        },
                        "internal_fact_refs": [],
                        "external_evidence_refs": [],
                    }
                ],
            }
        ],
    }


def test_disabled_config_is_not_applicable_without_model(tmp_path):
    calls = []

    def runner(*args):  # pragma: no cover
        calls.append(args)

    view = run_or_reuse_ai_decision_advice(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="US",
        config={"ai_decision_advice": {"enabled": False}},
        state_dir=_state_dir(tmp_path),
        model_runner=runner,
        now=NOW,
    )
    assert view["status"] == "not_applicable"
    assert calls == []


def test_missing_candidate_snapshot_is_unavailable(tmp_path):
    state_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "state"
    state_dir.mkdir(parents=True)
    view = run_or_reuse_ai_decision_advice(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="US",
        config={"ai_decision_advice": {"enabled": True}},
        state_dir=state_dir,
        model_runner=_runner({}),
        now=NOW,
    )
    assert view["status"] == "unavailable"
    assert view["unavailable_reason"] == "candidate_snapshot_missing"


def test_completed_run_flows_through_to_brief_view(tmp_path):
    # Evidence for NVDA is not present, so keep would demote; the model
    # outputs defer, which validation accepts (defer does not require
    # completed evidence).
    captured: dict = {}

    def runner(instructions, payload, schema, timeout):
        captured["payload"] = payload
        output = _model_output(
            payload["input_bindings"],
            run_id=payload["run_id"],
            account_ref=payload["account_ref"],
        )
        return ModelCallResult(
            raw_response={"ok": True},
            output_text=json.dumps(output, ensure_ascii=False),
            usage={},
        )

    view = run_or_reuse_ai_decision_advice(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="US",
        config={"ai_decision_advice": {"enabled": True}},
        state_dir=_state_dir(tmp_path),
        model_runner=runner,
        now=NOW,
    )
    assert view["status"] == "completed"
    assert view["sell_put"]["action"] == "defer"
    assert view["zero_candidate"] == {"sell_put": False, "covered_call": True}
    assert view["covered_call"] is None
    # Privacy: no account label in model input; no NAV/totals keys.
    text = json.dumps(captured["payload"], ensure_ascii=False)
    assert "lx" not in text.replace('"account_ref"', "")
    assert "nav" not in text.lower()


def test_second_run_reuses_without_model_call(tmp_path):
    def runner(instructions, payload, schema, timeout):
        output = _model_output(
            payload["input_bindings"],
            run_id=payload["run_id"],
            account_ref=payload["account_ref"],
        )
        return ModelCallResult(
            raw_response={"ok": True},
            output_text=json.dumps(output, ensure_ascii=False),
            usage={},
        )

    kwargs = dict(
        base=tmp_path,
        account="lx",
        market="US",
        config={"ai_decision_advice": {"enabled": True}},
        model_runner=runner,
        now=NOW,
    )
    first = run_or_reuse_ai_decision_advice(
        run_id="run-1", state_dir=_state_dir(tmp_path, "run-1"), **kwargs
    )
    assert first["status"] == "completed"

    def forbidden(*args):  # pragma: no cover
        raise AssertionError("reuse must not call the model")

    second = run_or_reuse_ai_decision_advice(
        **{**kwargs, "model_runner": forbidden},
        run_id="run-2",
        state_dir=_state_dir(tmp_path, "run-2"),
    )
    assert second["status"] == "completed"
    assert second["reused"] is True
    assert second["sell_put"]["action"] == "defer"


def test_model_failure_degrades_to_unavailable_without_raising(tmp_path):
    def runner(instructions, payload, schema, timeout):
        raise TimeoutError("slow")

    view = run_or_reuse_ai_decision_advice(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="US",
        config={"ai_decision_advice": {"enabled": True}},
        state_dir=_state_dir(tmp_path),
        model_runner=runner,
        now=NOW,
    )
    assert view["status"] == "unavailable"
    assert "provider_error" in (view["unavailable_reason"] or "")
