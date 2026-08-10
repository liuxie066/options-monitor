from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def _brief(*, valid_until: str = "2026-07-19T20:00:00+00:00", run_id: str = "run-tool") -> dict:
    return {
        "market": "US",
        "market_trading_date": "2026-07-19",
        "account": "lx",
        "revision": 999,
        "run_id": run_id,
        "generated_at_utc": "2026-07-19T13:40:00+00:00",
        "data_as_of_utc": "2026-07-19T13:39:00+00:00",
        "valid_until_utc": valid_until,
        "status": "completed",
        "actionability": "live_actionable",
        "strategy_summary": "test",
        "actions": [],
        "positions": [],
        "capacity": {},
        "candidates": {"sell_put": [], "covered_call": [], "combo_yield": []},
        "rejections": {},
        "events": [],
        "data_gaps": [],
        "source_artifacts": [],
    }


def _formal_advice_fixture(*, advice_id: str = "adv-fixture") -> tuple[dict, dict]:
    bindings = {
        "candidate_snapshot_hash": "a" * 64,
        "portfolio_distribution_hash": "b" * 64,
        "option_positions_hash": "c" * 64,
        "fact_registry_hash": "d" * 64,
        "external_evidence_hash": "e" * 64,
        "external_evidence_run_id": "2026-07-19T12:00:00+00:00",
    }
    rationale = {
        "risk_mechanism": "没有新增风险信号",
        "candidate_effect": "当前候选排序不变",
        "decision_reason": "维持原始首选",
    }
    refs = {
        "internal_fact_refs": ["candidate:put-1", "projection:put-1"],
        "external_evidence_refs": [],
    }
    decision = {
        "scope": "sell_put",
        "strategy_family": "sell_put",
        "symbol": None,
        "action": "keep",
        "baseline_candidate_id": "put-1",
        "selected_candidate_id": "put-1",
        "rationale": rationale,
        "source_refs": refs,
    }
    record = {
        "kind": "advice_record",
        "schema": "ai_decision_advice.v1",
        "advice_id": advice_id,
        "run_id": "run-tool",
        "account_ref": "private-account-ref",
        "market": "US",
        "recorded_at": "2026-07-19T13:40:00+00:00",
        "evidence_as_of": "2026-07-19T12:00:00+00:00",
        "input_bindings": bindings,
        "versions": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "schema_name": "ai_decision_advice.v1",
            "prompt_fingerprint": "f" * 64,
        },
        "status": "completed",
        "unavailable_reason": None,
        "zero_candidate": {"sell_put": False, "covered_call": True},
        "reused": False,
        "decisions": {"sell_put": decision},
        "demotions": [],
        "repair_attempted": False,
    }
    brief = {
        "account": "lx",
        "market": "US",
        "run_id": "run-tool",
        "ai_decision_advice": {
            "status": "completed",
            "unavailable_reason": None,
            "evidence_as_of": "2026-07-19T12:00:00+00:00",
            "sell_put": {
                "action": "keep",
                "baseline_candidate_id": "put-1",
                "selected_candidate_id": "put-1",
                "rationale": rationale,
                "source_refs": refs,
            },
            "covered_call": [],
            "zero_candidate": {"sell_put": False, "covered_call": True},
            "reused": False,
            "advice_record_id": advice_id,
        },
    }
    return record, brief


def test_read_view_supports_latest_day_revision_and_effective_planning_only(tmp_path: Path) -> None:
    from src.application.agent_tools.daily_brief import read_daily_brief_view
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief

    lifecycle = prepare_daily_decision_brief(base=tmp_path, brief=_brief())
    latest = read_daily_brief_view(
        base=tmp_path,
        account="LX",
        market="us",
        now_utc=datetime(2026, 7, 19, 21, 0, tzinfo=timezone.utc),
    )
    by_day = read_daily_brief_view(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date="2026-07-19",
        now_utc=datetime(2026, 7, 19, 21, 0, tzinfo=timezone.utc),
    )
    exact = read_daily_brief_view(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date="2026-07-19",
        revision=lifecycle["brief"]["revision"],
        now_utc=datetime(2026, 7, 19, 21, 0, tzinfo=timezone.utc),
    )

    assert latest["available"] is True
    assert latest["effective_actionability"] == "planning_only"
    assert "当前已不在可执行时段，仅供规划参考。" in latest["rendered_markdown"]
    assert by_day["brief"]["revision"] == exact["brief"]["revision"] == 0
    assert latest["brief"]["actionability"] == "live_actionable"


def test_read_view_passes_ai_decision_advice_section_through(tmp_path: Path) -> None:
    from src.application.agent_tools.daily_brief import read_daily_brief_view
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief

    advice = {
        "status": "completed",
        "unavailable_reason": None,
        "evidence_as_of": "2026-07-19T12:00:00+00:00",
        "sell_put": {
            "scope_id": "sell_put",
            "action": "keep",
            "summary": "保持首选候选。",
            "candidate_ids": ["run-tool:put:NVDA:100:2026-08-21"],
        },
        "covered_call": None,
        "zero_candidate": False,
        "reused": False,
        "advice_record_id": "adv-20260719T134000Z",
    }
    brief = _brief()
    brief["ai_decision_advice"] = advice
    prepare_daily_decision_brief(base=tmp_path, brief=brief)

    view = read_daily_brief_view(
        base=tmp_path,
        account="lx",
        market="us",
        now_utc=datetime(2026, 7, 19, 21, 0, tzinfo=timezone.utc),
    )
    assert view["available"] is True
    assert view["brief"]["ai_decision_advice"]["sell_put"]["action"] == "keep"
    assert view["brief"]["ai_decision_advice"]["advice_record_id"] == "adv-20260719T134000Z"
    assert view["brief"]["ai_decision_advice"]["formal_record"] == {
        "available": False,
        "reason": "formal_record_unavailable",
    }


def test_read_view_returns_formal_advice_bindings_actions_and_refs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.ai_decision_advice.advice as advice_mod
    import src.application.ai_decision_advice.orchestration as orchestration_mod
    import src.infrastructure.deepseek_responses as deepseek_mod
    from src.application.agent_tools.daily_brief import read_daily_brief_view
    from src.application.ai_decision_advice.advice_store import (
        advice_records_path,
        append_advice_record,
    )
    from src.application.daily_decision_brief_repository import (
        prepare_daily_decision_brief,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("read-only Agent query must not run model or search")

    monkeypatch.setattr(advice_mod, "run_decision_advice", forbidden)
    monkeypatch.setattr(orchestration_mod, "run_decision_advice", forbidden)
    monkeypatch.setattr(deepseek_mod, "create_deepseek_response", forbidden)

    advice_id = "adv-formal-1"
    bindings = {
        "candidate_snapshot_hash": "a" * 64,
        "portfolio_distribution_hash": "b" * 64,
        "option_positions_hash": "c" * 64,
        "fact_registry_hash": "d" * 64,
        "external_evidence_hash": "e" * 64,
        "external_evidence_run_id": "2026-07-19T12:00:00+00:00",
    }
    decision = {
        "scope": "sell_put",
        "strategy_family": "sell_put",
        "symbol": None,
        "action": "switch",
        "baseline_candidate_id": "put-1",
        "selected_candidate_id": "put-2",
        "rationale": {
            "risk_mechanism": "监管风险上升",
            "candidate_effect": "当前首选受影响",
            "decision_reason": "改选到期更晚的候选",
        },
        "source_refs": {
            "internal_fact_refs": ["candidate:put-2", "portfolio:distribution"],
            "external_evidence_refs": ["evidence:rule-1"],
        },
    }
    record = {
        "kind": "advice_record",
        "schema": "ai_decision_advice.v1",
        "advice_id": advice_id,
        "run_id": "run-tool",
        "account_ref": "acct-secret-ref",
        "market": "US",
        "recorded_at": "2026-07-19T13:40:00+00:00",
        "evidence_as_of": "2026-07-19T12:00:00+00:00",
        "input_bindings": bindings,
        "versions": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "schema_name": "ai_decision_advice.v1",
            "prompt_fingerprint": "f" * 64,
            "prompt": {"internal": "must not leak"},
        },
        "status": "completed",
        "zero_candidate": {"sell_put": False, "covered_call": True},
        "reused": True,
        "reuse_of_advice_id": "adv-prior",
        "decisions": {"sell_put": decision},
        "demotions": [],
        "repair_attempted": True,
        "usage": {"input_tokens": 999},
        "model_response_audit": {"response_sha256": "secret"},
    }
    append_advice_record(
        advice_records_path(tmp_path / "output_runs" / "run-tool", "lx"),
        record,
    )
    brief = _brief()
    brief["ai_decision_advice"] = {
        "status": "completed",
        "unavailable_reason": None,
        "evidence_as_of": "2026-07-19T12:00:00+00:00",
        "sell_put": {
            "action": "switch",
            "baseline_candidate_id": "put-1",
            "selected_candidate_id": "put-2",
            "rationale": decision["rationale"],
            "source_refs": decision["source_refs"],
        },
        "covered_call": [],
        "zero_candidate": {"sell_put": False, "covered_call": True},
        "reused": True,
        "advice_record_id": advice_id,
    }
    brief["ai_decision_advice_evidence_index"] = {
        "symbols": [
            {
                "symbol": "NVDA",
                "evidence": [
                    {
                        "ref": "evidence:rule-1",
                        "source": {
                            "title": "监管规则变化",
                            "publisher": "监管机构",
                            "url": "https://example.gov/rule",
                            "published_at": "2026-07-19",
                        },
                    }
                ],
            }
        ]
    }
    prepare_daily_decision_brief(base=tmp_path, brief=brief)

    view = read_daily_brief_view(
        base=tmp_path,
        account="lx",
        market="US",
        now_utc=datetime(2026, 7, 19, 14, 0, tzinfo=timezone.utc),
    )

    formal = view["brief"]["ai_decision_advice"]
    assert formal["formal_record"] == {
        "available": True,
        "reason": "ok",
        "advice_id": advice_id,
        "recorded_at": "2026-07-19T13:40:00+00:00",
        "evidence_as_of": "2026-07-19T12:00:00+00:00",
    }
    assert formal["input_bindings"] == bindings
    assert formal["actions"][0]["action"] == "switch"
    assert formal["actions"][0]["selected_candidate_id"] == "put-2"
    assert formal["actions"][0]["internal_fact_refs"] == [
        "candidate:put-2",
        "portfolio:distribution",
    ]
    assert formal["actions"][0]["external_evidence_refs"] == [
        "evidence:rule-1"
    ]
    assert formal["validation"]["repair_attempted"] is True
    assert formal["reuse_of_advice_id"] == "adv-prior"
    assert formal["versions"] == {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "schema_name": "ai_decision_advice.v1",
        "prompt_fingerprint": "f" * 64,
    }
    rendered = str(view)
    assert "acct-secret-ref" not in rendered
    assert "input_tokens" not in rendered
    assert "response_sha256" not in rendered
    assert "must not leak" not in rendered


def test_formal_advice_fails_closed_when_decision_projection_differs(
    tmp_path: Path,
) -> None:
    from src.application.agent_tools.daily_brief import (
        _with_formal_ai_decision_advice,
    )
    from src.application.ai_decision_advice.advice_store import (
        advice_records_path,
        append_advice_record,
    )

    record, brief = _formal_advice_fixture(advice_id="adv-mismatch")
    decision = record["decisions"]["sell_put"]
    decision["action"] = "switch"
    decision["selected_candidate_id"] = "put-2"
    append_advice_record(
        advice_records_path(tmp_path / "output_runs" / "run-tool", "lx"),
        record,
    )

    result = _with_formal_ai_decision_advice(base=tmp_path, brief=brief)

    advice = result["ai_decision_advice"]
    assert advice["formal_record"] == {
        "available": False,
        "reason": "formal_record_identity_mismatch",
    }
    assert advice["actions"] == []


def test_formal_advice_rejects_duplicate_advice_id_before_identity_filter(
    tmp_path: Path,
) -> None:
    from src.application.agent_tools.daily_brief import (
        _with_formal_ai_decision_advice,
    )
    from src.application.ai_decision_advice.advice_store import (
        advice_records_path,
        append_advice_record,
    )

    record, brief = _formal_advice_fixture(advice_id="adv-duplicate")
    path = advice_records_path(tmp_path / "output_runs" / "run-tool", "lx")
    append_advice_record(path, record)
    append_advice_record(path, {**record, "market": "HK"})

    result = _with_formal_ai_decision_advice(base=tmp_path, brief=brief)

    advice = result["ai_decision_advice"]
    assert advice["formal_record"] == {
        "available": False,
        "reason": "formal_record_ambiguous",
    }
    assert advice["actions"] == []


def test_formal_advice_rejects_malformed_demotion_instead_of_hiding_it(
    tmp_path: Path,
) -> None:
    from src.application.agent_tools.daily_brief import (
        _with_formal_ai_decision_advice,
    )
    from src.application.ai_decision_advice.advice_store import (
        advice_records_path,
        append_advice_record,
    )

    record, brief = _formal_advice_fixture(advice_id="adv-invalid-demotion")
    record["demotions"] = [
        {
            "scope": "sell_put",
            "from_action": "keep",
            "to_action": "switch",
            "reason": "invalid-transition",
        }
    ]
    append_advice_record(
        advice_records_path(tmp_path / "output_runs" / "run-tool", "lx"),
        record,
    )

    result = _with_formal_ai_decision_advice(base=tmp_path, brief=brief)

    assert result["ai_decision_advice"]["formal_record"] == {
        "available": False,
        "reason": "formal_record_invalid",
    }


def test_formal_advice_matches_covered_call_scope_and_selected_candidate(
    tmp_path: Path,
) -> None:
    from src.application.agent_tools.daily_brief import (
        _with_formal_ai_decision_advice,
    )
    from src.application.ai_decision_advice.advice_store import (
        advice_records_path,
        append_advice_record,
    )

    record, brief = _formal_advice_fixture(advice_id="adv-covered-call")
    rationale = record["decisions"]["sell_put"]["rationale"]
    refs = record["decisions"]["sell_put"]["source_refs"]
    covered_call = {
        "scope": "covered_call:AAPL",
        "strategy_family": "covered_call",
        "symbol": "AAPL",
        "action": "switch",
        "baseline_candidate_id": "call-1",
        "selected_candidate_id": "call-2",
        "rationale": rationale,
        "source_refs": refs,
    }
    record["zero_candidate"] = {"sell_put": True, "covered_call": False}
    record["decisions"] = {"covered_call:AAPL": covered_call}
    section = brief["ai_decision_advice"]
    section["zero_candidate"] = {"sell_put": True, "covered_call": False}
    section["sell_put"] = None
    section["covered_call"] = [
        {
            "symbol": "AAPL",
            "action": "switch",
            "baseline_candidate_id": "call-1",
            "selected_candidate_id": "call-2",
            "rationale": rationale,
            "source_refs": refs,
        }
    ]
    append_advice_record(
        advice_records_path(tmp_path / "output_runs" / "run-tool", "lx"),
        record,
    )

    result = _with_formal_ai_decision_advice(base=tmp_path, brief=brief)

    advice = result["ai_decision_advice"]
    assert advice["formal_record"]["available"] is True
    assert advice["actions"] == [
        {
            "scope": "covered_call:AAPL",
            "strategy_family": "covered_call",
            "symbol": "AAPL",
            "action": "switch",
            "baseline_candidate_id": "call-1",
            "selected_candidate_id": "call-2",
            "rationale": rationale,
            "internal_fact_refs": refs["internal_fact_refs"],
            "external_evidence_refs": refs["external_evidence_refs"],
        }
    ]


def test_read_view_reports_unavailable_and_revision_requires_date(tmp_path: Path) -> None:
    from src.application.agent_tools.daily_brief import read_daily_brief_view

    unavailable = read_daily_brief_view(base=tmp_path, account="lx", market="US")
    assert unavailable["available"] is False
    assert unavailable["reason"] == "not_found"
    assert unavailable["coverage"]["status"] == "unavailable"
    assert unavailable["freshness"]["effective_actionability"] == "unavailable"
    assert unavailable["source"]["state_path"] == ".../daily_decision_brief.US.current.json"
    assert str(tmp_path) not in str(unavailable)
    assert "不可用" in unavailable["rendered_markdown"]

    try:
        read_daily_brief_view(base=tmp_path, account="lx", market="US", revision=0)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "market_trading_date is required" in str(exc)


def test_agent_tool_is_pure_read_and_returns_structured_contract(monkeypatch, tmp_path: Path) -> None:
    import src.application.agent_tools.daily_brief as mod
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief

    prepare_daily_decision_brief(base=tmp_path, brief=_brief(valid_until="2026-07-20T20:00:00+00:00"))
    monkeypatch.setattr(mod, "repo_base", lambda: tmp_path)
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("OM_ENV_FILE", raising=False)

    data, warnings, meta = mod.DAILY_DECISION_BRIEF_READ_TOOL.call({"account": "lx", "market": "US"})

    assert mod.DAILY_DECISION_BRIEF_READ_TOOL.is_pure_read() is True
    assert data["schema_version"] == "daily_decision_brief_read.output.v1"
    assert data["available"] is True
    assert data["brief"]["revision"] == 0
    assert data["coverage"] == {
        "status": "completed",
        "reason": "ok",
        "action_count": 0,
        "position_count": 0,
        "data_gap_count": 0,
        "source_artifact_count": 0,
    }
    assert data["source"]["state_path"] == ".../daily_decision_brief.US.current.json"
    assert data["freshness"]["effective_actionability"] == "planning_only"
    assert str(tmp_path) not in str(data)
    assert warnings == []
    assert meta == {
        "read_only": True,
        "state_path": ".../daily_decision_brief.US.current.json",
    }


def test_agent_tool_rejects_invalid_revision_contract() -> None:
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.agent_tools.daily_brief import DAILY_DECISION_BRIEF_READ_TOOL

    invalid_payloads = (
        {"account": "lx", "market": "US", "revision": 0},
        {"account": "lx", "market": "US", "date": "2026-07-19", "revision": -1},
        {"account": "lx", "market": "US", "date": "2026-07-19", "revision": 1.5},
        {"account": "lx", "market": "US", "date": "2026-07-19", "revision": True},
        {"account": "lx", "market": "US", "date": "2026-07-19", "revision": "1"},
    )
    for payload in invalid_payloads:
        try:
            DAILY_DECISION_BRIEF_READ_TOOL.call(payload)
            raise AssertionError(f"expected INPUT_ERROR for {payload!r}")
        except AgentToolError as exc:
            assert exc.code == "INPUT_ERROR"


def test_agent_tool_manifest_declares_side_effect_free_read() -> None:
    from src.application.agent_tools.daily_brief import DAILY_DECISION_BRIEF_READ_TOOL

    manifest = DAILY_DECISION_BRIEF_READ_TOOL.to_manifest()

    assert manifest["read_only"] is True
    assert manifest["side_effects"] == []
    assert manifest["risk_level"] == "read_only"
    assert manifest["requires_confirm"] is False
    assert manifest["annotations"]["idempotent"] is True


def test_agent_tool_masks_state_invalid_source_path(monkeypatch, tmp_path: Path) -> None:
    import src.application.agent_tools.daily_brief as mod

    raw_path = tmp_path / "private" / "daily_decision_brief.US.current.json"
    monkeypatch.setattr(
        mod,
        "read_latest_daily_decision_brief",
        lambda **_kwargs: {
            "available": False,
            "reason": "state_invalid",
            "error": f"invalid state at {raw_path}",
            "brief": None,
            "path": raw_path,
        },
    )

    data = mod.read_daily_brief_view(base=tmp_path, account="lx", market="US")

    assert data["available"] is False
    assert data["reason"] == "state_invalid"
    assert data["source"]["state_path"] == ".../daily_decision_brief.US.current.json"
    assert str(tmp_path) not in str(data)
    assert "error" not in data


def test_agent_tool_reads_env_runtime_root_then_repo_fallback(monkeypatch, tmp_path: Path) -> None:
    import src.application.agent_tools.daily_brief as mod
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief

    repo_root = tmp_path / "repo"
    runtime_root = tmp_path / "runtime"
    prepare_daily_decision_brief(base=repo_root, brief=_brief(run_id="repo-r0"))
    prepare_daily_decision_brief(base=runtime_root, brief=_brief(run_id="runtime-r0"))
    runtime_r1 = prepare_daily_decision_brief(base=runtime_root, brief=_brief(run_id="runtime-r1"))
    monkeypatch.setattr(mod, "repo_base", lambda: repo_root)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.delenv("OM_ENV_FILE", raising=False)

    payload = {
        "account": "lx",
        "market": "US",
        "date": "2026-07-19",
        "revision": runtime_r1["brief"]["revision"],
    }
    runtime_data, runtime_warnings, _runtime_meta = mod.DAILY_DECISION_BRIEF_READ_TOOL.call(payload)
    assert runtime_data["brief"]["revision"] == 1
    assert runtime_data["brief"]["run_id"] == "runtime-r1"
    assert runtime_warnings == []

    monkeypatch.delenv("OM_RUNTIME_ROOT")
    payload["revision"] = 0
    repo_data, repo_warnings, _repo_meta = mod.DAILY_DECISION_BRIEF_READ_TOOL.call(payload)
    assert repo_data["brief"]["revision"] == 0
    assert repo_data["brief"]["run_id"] == "repo-r0"
    assert repo_warnings == []


def test_latest_query_aggregates_enabled_scopes_and_never_writes_delivery_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from copy import deepcopy

    import src.application.agent_tools.daily_brief as mod
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief

    for account, market in (("lx", "HK"), ("lx", "US")):
        brief = deepcopy(_brief(run_id=f"run-{account}-{market.lower()}"))
        brief["account"] = account
        brief["market"] = market
        brief["funds"] = {
            "cash_total_by_currency": {"HKD" if market == "HK" else "USD": 100_000.0},
            "option_opening_available_by_currency": {"HKD" if market == "HK" else "USD": 60_000.0},
            "available": True,
            "reason": "ok",
        }
        prepare_daily_decision_brief(base=tmp_path, brief=brief)

    delivery_path = tmp_path / "output_accounts" / "lx" / "state" / "daily_decision_brief.US.delivery.json"
    delivery_path.write_bytes(b'{"sentinel":true}\n')
    before = delivery_path.read_bytes()
    monkeypatch.setattr(
        mod,
        "_enabled_daily_brief_scopes",
        lambda **_kwargs: [("lx", "HK"), ("lx", "US"), ("sy", "US")],
    )

    data = mod.read_daily_brief_view(
        base=tmp_path,
        now_utc=datetime(2026, 7, 19, 14, 0, tzinfo=timezone.utc),
    )

    assert data["available"] is True
    assert data["reason"] == "partial"
    assert [(item["query"]["account"], item["query"]["market"]) for item in data["sections"]] == [
        ("lx", "HK"),
        ("lx", "US"),
        ("sy", "US"),
    ]
    assert data["coverage"] == {
        "status": "partial",
        "reason": "partial",
        "section_count": 3,
        "available_section_count": 2,
        "unavailable_section_count": 1,
    }
    assert data["rendered_markdown"].count("## OM · 决策简报 · lx") == 2
    assert "## OM · 决策简报 · sy" in data["rendered_markdown"]
    assert "部分账户或市场的成功扫描快照暂不可用" in data["rendered_markdown"]
    assert "revision" not in data["rendered_markdown"]
    assert delivery_path.read_bytes() == before


def test_agent_tool_default_query_has_no_required_scope() -> None:
    from src.application.agent_tools.daily_brief import DAILY_DECISION_BRIEF_READ_TOOL

    manifest = DAILY_DECISION_BRIEF_READ_TOOL.to_manifest()

    assert manifest["input_json_schema"].get("required") in (None, [])
    assert manifest["safe_default_input"] == {}
    assert manifest["examples"][0] == {"input": {}}
    assert "期权监控" in manifest["description"]


def test_agent_tool_day_query_keeps_existing_us_market_default() -> None:
    import src.application.agent_tools.daily_brief as mod

    mod._validate_daily_brief_input({"account": "lx", "date": "2026-07-19"})
