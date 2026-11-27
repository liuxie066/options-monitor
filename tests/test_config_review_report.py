from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_configs(root: Path) -> None:
    hk = {
        "symbols": [
            {
                "symbol": "0700.HK",
                "use": ["put_base", "call_base"],
                "sell_put": {"max_strike": 450},
                "sell_call": {"enabled": True, "min_strike": 550},
                "combo_yield": {"enabled": True, "call": {"min_strike": 560, "max_strike": 700}, "min_dte": 30},
            },
            {
                "symbol": "9992.HK",
                "use": "put_base",
                "sell_put": {"max_strike": 145},
            },
        ]
    }
    us = {
        "symbols": [
            {
                "symbol": "NVDA",
                "use": ["put_base", "call_base"],
                "sell_put": {"max_strike": 150, "min_strike": 100, "min_dte": 20, "max_dte": 45},
                "sell_call": {"enabled": True, "min_strike": 190},
                "combo_yield": {"enabled": True},
            },
            {
                "symbol": "PDD",
                "use": ["put_base"],
                "sell_put": {"max_strike": 85},
            },
        ]
    }
    (root / "user.hk.json").write_text(json.dumps(hk), encoding="utf-8")
    (root / "user.us.json").write_text(json.dumps(us), encoding="utf-8")


def _write_prices(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "generated_at_utc": "2026-11-12T00:00:00Z",
                "prices": {"0700.HK": 403.2, "NVDA": 173.73, "PDD": 79.01},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_config_review_renders_boundaries_prices_and_distances(tmp_path: Path) -> None:
    from src.application.reports.config_review import build_config_review

    configs = tmp_path / "configs"
    configs.mkdir()
    _write_configs(configs)
    prices = _write_prices(tmp_path / "prices.json")

    result = build_config_review(configs_root=configs, runtime_root=tmp_path, prices_file=prices)

    payload = result.payload
    assert payload["schema_version"] == "config_review.v2"
    assert payload["summary"]["symbol_count"] == 4
    assert payload["summary"]["market_counts"] == {"HK": 2, "US": 2}
    assert payload["summary"]["price_available"] == 3
    assert payload["summary"]["price_missing"] == 1
    assert payload["summary"]["put_cap_configured"] == 4
    assert payload["summary"]["call_floor_configured"] == 2
    assert payload["summary"]["combo_enabled"] == 2
    assert payload["conclusion"]["state"] == "观察"
    assert payload["strike_rejections"]["count"] is None

    markdown = result.markdown
    assert markdown.index("**结论：观察**") < markdown.index("## 行权价边界筛选")
    assert "该期无数据" in markdown
    assert "上一完整周的边界拒绝观测与数据覆盖" in markdown
    assert "距离定义：(现价 − 边界) ÷ 边界 × 100%" in markdown
    assert "候选数暂不统计" not in markdown
    assert "÷ 现价" not in markdown
    assert "| 0700.HK | 403.20 | 450 | 低于 10.4% | 550 | 低于 26.7% | 启用 |" in markdown
    assert "上限 450（低于 10.4%；现价成为实际边界）" in markdown
    assert "下限 550（低于 26.7%；配置边界生效）" in markdown
    assert "看涨下限 560" in markdown
    assert "DTE 20–45" in markdown
    assert "| 9992.HK | — | 145 | — |" in markdown
    assert "现价：不可用" in markdown
    assert "| NVDA | 173.73 | 150 | 高于 15.8% | 190 |" in markdown
    assert "配置上限生效" in markdown

    nvda = next(
        symbol
        for market in payload["markets"]
        for symbol in market["symbols"]
        if symbol["symbol"] == "NVDA"
    )
    distances = {item["path"]: item for item in nvda["distances"]}
    assert distances["sell_put.max_strike"]["signed_pct"] == 15.82
    assert distances["sell_put.max_strike"]["binding"] == "config_bound"
    assert distances["sell_put.min_strike"]["binding"] is None
    assert distances["sell_call.min_strike"]["relation"] == "below"
    hk_0700 = next(
        symbol
        for market in payload["markets"]
        for symbol in market["symbols"]
        if symbol["symbol"] == "0700.HK"
    )
    combo_distances = {item["path"]: item for item in hk_0700["distances"]}
    assert combo_distances["combo_yield.call.min_strike"]["value"] == 560


def test_config_review_inherits_market_defaults_for_undeclared_fields(tmp_path: Path) -> None:
    from src.application.reports.config_review import build_config_review

    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "system.json").write_text(
        json.dumps(
            {
                "markets": {
                    "hk": {
                        "symbol_defaults": {
                            "sell_put": {"enabled": True, "min_dte": 7, "max_dte": 60},
                            "sell_call": {"enabled": False, "min_dte": 7, "max_dte": 60},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (configs / "user.hk.json").write_text(
        json.dumps(
            {
                "symbols": [
                    {
                        "symbol": "0700.HK",
                        "sell_put": {"max_strike": 450},
                        "sell_call": {"min_strike": 550},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = build_config_review(configs_root=configs, runtime_root=tmp_path)

    assert result.payload["system_config"]["exists"] is True
    row = result.payload["markets"][0]["symbols"][0]
    assert row["strategies"]["sell_put"]["enabled"] is True
    assert row["strategies"]["sell_put"]["enabled_source"] == "market_default"
    assert row["strategies"]["sell_put"]["min_dte"] == 7
    assert row["strategies"]["sell_put"]["dte_source"] == "market_default"
    assert row["strategies"]["sell_call"]["enabled"] is False
    assert row["strategies"]["sell_call"]["enabled_source"] == "market_default"

    assert "卖出看跌（启用，继承市场默认）" in result.markdown
    assert "DTE 7–60（继承）" in result.markdown
    assert "卖出看涨（停用，继承市场默认）" in result.markdown
    assert "仅用于用户配置未声明字段的回退" not in result.markdown
    assert "继承市场默认" in result.markdown


def test_config_review_marks_missing_market_file_as_data_gap(tmp_path: Path) -> None:
    from src.application.reports.config_review import build_config_review

    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "user.hk.json").write_text(
        json.dumps({"symbols": [{"symbol": "0700.HK", "sell_put": {"max_strike": 450}}]}),
        encoding="utf-8",
    )
    result = build_config_review(configs_root=configs, runtime_root=tmp_path)
    assert any("US" in gap for gap in result.payload["data_gaps"])
    assert result.payload["summary"]["market_counts"] == {"HK": 1}
    assert result.payload["price_source"] == "runtime_scan"


def test_config_review_requires_at_least_one_market_config(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.reports.config_review import build_config_review

    configs = tmp_path / "configs"
    configs.mkdir()
    with pytest.raises(AgentToolError) as exc:
        build_config_review(configs_root=configs, runtime_root=tmp_path)
    assert exc.value.code == "REPORT_CONFIG_MISSING"


def test_config_review_rejects_invalid_boundary_values(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.reports.config_review import build_config_review

    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "user.hk.json").write_text(
        json.dumps({"symbols": [{"symbol": "0700.HK", "sell_put": {"max_strike": "not-a-number"}}]}),
        encoding="utf-8",
    )
    with pytest.raises(AgentToolError) as exc:
        build_config_review(configs_root=configs, runtime_root=tmp_path)
    assert exc.value.code == "REPORT_CONFIG_INVALID"
    assert "0700.HK.sell_put.max_strike" in " ".join(exc.value.details["errors"])


def test_runtime_brief_prices_use_newest_observation(tmp_path: Path) -> None:
    from src.application.reports.config_review import build_config_review
    from src.application.reports.prices import load_runtime_brief_prices

    configs = tmp_path / "configs"
    configs.mkdir()
    _write_configs(configs)
    lx_state = tmp_path / "output_accounts" / "lx" / "state"
    sy_state = tmp_path / "output_accounts" / "sy" / "state"
    lx_state.mkdir(parents=True)
    sy_state.mkdir(parents=True)
    (lx_state / "daily_decision_brief.HK.current.json").write_text(
        json.dumps(
            {
                "generated_at_utc": "2026-11-11T07:55:03Z",
                "candidate_index": [
                    {"representative": {"metrics": {"symbol": "0700.HK", "spot": 403.2}}},
                    {"representative": {"metrics": {"symbol": "9992.HK", "spot": None}}},
                ],
            }
        ),
        encoding="utf-8",
    )
    (sy_state / "daily_decision_brief.HK.current.json").write_text(
        json.dumps(
            {
                "generated_at_utc": "2026-11-10T07:55:03Z",
                "candidate_index": [
                    {"representative": {"metrics": {"symbol": "0700.HK", "spot": 999.0}}},
                ],
            }
        ),
        encoding="utf-8",
    )
    snapshot = load_runtime_brief_prices(tmp_path, markets=["HK"])
    assert snapshot.get("0700.HK").price == 403.2
    assert snapshot.get("9992.HK") is None

    result = build_config_review(configs_root=configs, runtime_root=tmp_path)
    assert snapshot.source == "runtime_daily_brief"
    assert snapshot.observed_at_utc == "2026-11-11T07:55:03Z"
    assert result.payload["price_source"] == "runtime_scan"
    assert result.payload["price_observed_at_utc"] is None
    assert result.payload["summary"]["price_available"] == 0
    assert "| 0700.HK | — |" in result.markdown
    assert any("成功扫描" in gap for gap in result.payload["data_gaps"])


def test_prices_file_rejects_missing_and_invalid_entries(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.reports.prices import load_prices_file

    missing = tmp_path / "missing.json"
    with pytest.raises(AgentToolError) as missing_exc:
        load_prices_file(missing)
    assert missing_exc.value.code == "REPORT_PRICES_INVALID"

    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"prices": {"NVDA": 0}}), encoding="utf-8")
    with pytest.raises(AgentToolError) as invalid_exc:
        load_prices_file(invalid)
    assert invalid_exc.value.code == "REPORT_PRICES_INVALID"
    assert "NVDA" in invalid_exc.value.details["errors"][0]


@pytest.mark.parametrize("side,field", [
    ("sell_put", "min_strike"), ("sell_put", "max_strike"),
    ("sell_call", "min_strike"), ("sell_call", "max_strike"),
])
@pytest.mark.parametrize("mode", ["omitted", "number", "null", "zero"])
def test_strike_override_matches_builder_and_report_cli(tmp_path, side, field, mode):
    import contextlib
    import io
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.config_defaults import default_config
    from src.application.layered_config import build_layered_runtime_config_from_user_config
    from src.application.reports.config_review import build_config_review
    from src.interfaces.cli.main import main

    system = default_config()
    for name in ("sell_put", "sell_call"):
        system["markets"]["us"]["symbol_defaults"][name] = {
            "enabled": True, "strategy": "insurance_underwriting", "min_dte": 7,
            "max_dte": 30, "min_strike": 100, "max_strike": 200,
        }
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "system.json").write_text(json.dumps(system))
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"prices": {"NVDA": 150}}))
    values = {"number": 175, "null": None, "zero": 0}
    section = {} if mode == "omitted" else {field: values[mode]}
    user = {"accounts": ["demo"],
            "account_settings": {"demo": {"type": "futu", "futu": {"account_id": "REAL_00000000"}}},
            "symbols": [{"symbol": "NVDA", side: section}]}
    (configs / "user.us.json").write_text(json.dumps(user))
    builder_args = dict(repo_root=tmp_path, market="us", user_config=user, system_config=system)
    if mode == "zero":
        # Existing difference: canonical config rejects zero; this report preserves it.
        with pytest.raises(AgentToolError) as caught:
            build_layered_runtime_config_from_user_config(**builder_args)
        assert caught.value.code == "CONFIG_ERROR"
        expected = 0
    else:
        canonical, _ = build_layered_runtime_config_from_user_config(**builder_args)
        expected = canonical["symbols"][0][side][field]
    report = build_config_review(configs_root=configs, runtime_root=tmp_path, prices_file=prices)
    row = report.payload["markets"][0]["symbols"][0]
    assert row["strategies"][side][field] == expected
    if mode in {"null", "zero"}:
        path = f"{side}.{field}"
        assert all(entry["path"] != path for entry in row["distances"])
        assert all(entry["path"] != path for entry in report.payload["boundary_assessments"])
    if mode == "null":
        assert expected is None
        label = "上限" if field == "max_strike" else "下限"
        side_label = "卖出看跌" if side == "sell_put" else "卖出看涨"
        detail = next(line for line in report.markdown.splitlines() if line.startswith(f"- {side_label}（"))
        assert f"{label}未设置" in detail
        if (side, field) == ("sell_put", "max_strike"):
            assert report.summary["put_cap_configured"] == 0
        elif (side, field) == ("sell_call", "min_strike"):
            assert report.summary["call_floor_configured"] == 0
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        rc = main(["report", "run", "config_review", "--dry-run", "--json",
                   "--runtime-root", str(tmp_path), "--configs-root", str(configs),
                   "--prices-file", str(prices)])
    assert rc == 0
    envelope = json.loads(captured.getvalue())
    payload = json.loads(next(a["content"] for a in envelope["artifacts"] if a["name"] == "report.json"))
    assert payload["markets"][0]["symbols"][0]["strategies"][side][field] == expected
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize("field", ["min_strike", "max_strike"])
@pytest.mark.parametrize("mode", ["omitted", "null"])
def test_combo_strike_null_does_not_inherit_nonempty_default(tmp_path, field, mode):
    from src.application.reports.config_review import build_config_review

    configs = tmp_path / "configs"
    configs.mkdir()
    defaults = {"combo_yield": {"enabled": True, "min_dte": 7, "max_dte": 30,
                                "call": {"min_strike": 100, "max_strike": 200}}}
    (configs / "system.json").write_text(json.dumps({"markets": {"us": {"symbol_defaults": defaults}}}))
    call = {} if mode == "omitted" else {field: None}
    (configs / "user.us.json").write_text(json.dumps({"symbols": [
        {"symbol": "NVDA", "combo_yield": {"enabled": False, "call": call}}]}))
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"prices": {"NVDA": 150}}))
    report = build_config_review(configs_root=configs, runtime_root=tmp_path, prices_file=prices)
    row = report.payload["markets"][0]["symbols"][0]
    assert row["strategies"]["combo_yield"]["enabled"] is False
    expected = None if mode == "null" else defaults["combo_yield"]["call"][field]
    assert row["strategies"]["combo_yield"][f"call_{field}"] == expected
    if mode == "null":
        path = f"combo_yield.call.{field}"
        assert all(entry["path"] != path for entry in row["distances"])
        assert all(entry["path"] != path for entry in report.payload["boundary_assessments"])


@pytest.mark.parametrize("invalid", [False, "invalid", float("inf")])
def test_null_support_does_not_turn_invalid_strike_into_default(tmp_path, invalid):
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.reports.config_review import build_config_review

    (tmp_path / "system.json").write_text(json.dumps({"markets": {"us": {"symbol_defaults": {
        "sell_put": {"max_strike": 200}}}}}))
    (tmp_path / "user.us.json").write_text(json.dumps({"symbols": [
        {"symbol": "NVDA", "sell_put": {"max_strike": invalid}}]}))
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"prices": {"NVDA": 150}}))
    with pytest.raises(AgentToolError) as caught:
        build_config_review(configs_root=tmp_path, runtime_root=tmp_path, prices_file=prices)
    assert caught.value.code == "REPORT_CONFIG_INVALID"
