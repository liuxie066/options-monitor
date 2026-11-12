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
    assert markdown.index("**结论：观察**") < markdown.index("## 指标 A")
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
    assert "仅用于用户配置未声明字段的回退" in result.markdown


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
    assert result.payload["price_source"] == "runtime_daily_brief"


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
    assert result.payload["price_source"] == "runtime_daily_brief"
    assert result.payload["price_observed_at_utc"] == "2026-11-11T07:55:03Z"
    assert result.payload["summary"]["price_available"] == 1
    assert "| 0700.HK | 403.20 |" in result.markdown


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
