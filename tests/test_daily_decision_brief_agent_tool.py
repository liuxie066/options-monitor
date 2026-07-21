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
        "status": "ready",
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
        "status": "ready",
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
    assert "## lx · 港股期权监控" in data["rendered_markdown"]
    assert "## lx · 美股期权监控" in data["rendered_markdown"]
    assert "## sy · 美股期权监控" in data["rendered_markdown"]
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
