from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _minimal_cfg() -> dict:
    return {
        "accounts": ["lx"],
        "portfolio": {
            "market": "富途",
        },
        "templates": {
            "put_base": {
                "sell_put": {
                    "min_annualized_net_return": 0.1,
                    "min_net_income": 50,
                    "min_open_interest": 10,
                    "min_volume": 1,
                    "max_spread_ratio": 0.3,
                }
            }
        },
        "symbols": [
            {
                "symbol": "NVDA",
                "market": "US",
                "fetch": {"source": "yahoo", "limit_expirations": 8},
                "use": ["put_base"],
                "sell_put": {
                    "enabled": True,
                    "min_dte": 20,
                    "max_dte": 45,
                    "min_strike": 100,
                    "max_strike": 120,
                },
                "sell_call": {"enabled": False},
            }
        ],
    }


def test_healthcheck_works_with_om_config_dir(monkeypatch, tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.us.json").write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setenv("OM_CONFIG_DIR", str(cfg_dir))

    out = run_tool("healthcheck", {"config_key": "us"})

    assert out["ok"] is True
    assert out["data"]["config"]["accounts"] == ["lx"]
    assert out["meta"]["config_path"] == ".../config.us.json"
    assert any("portfolio.pm_config is not configured" in x for x in out["warnings"])


def test_manage_symbols_list_and_dry_run_add(monkeypatch, tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")

    out_list = run_tool("manage_symbols", {"config_path": str(cfg_path), "action": "list"})
    assert out_list["ok"] is True
    assert out_list["data"]["symbol_count"] == 1
    assert out_list["data"]["symbols"][0]["symbol"] == "NVDA"

    out_dry = run_tool(
        "manage_symbols",
        {
            "config_path": str(cfg_path),
            "action": "add",
            "symbol": "TSLA",
            "sell_put_enabled": True,
            "sell_put_min_dte": 20,
            "sell_put_max_dte": 45,
            "sell_put_min_strike": 100,
            "sell_put_max_strike": 120,
            "dry_run": True,
        },
    )
    assert out_dry["ok"] is True
    assert out_dry["data"]["dry_run"] is True
    assert out_dry["data"]["symbol_count"] == 2

    current = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert [x["symbol"] for x in current["symbols"]] == ["NVDA"]


def test_manage_symbols_write_requires_gate_and_confirm(tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")

    blocked = run_tool(
        "manage_symbols",
        {
            "config_path": str(cfg_path),
            "action": "add",
            "symbol": "TSLA",
        },
    )
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == "PERMISSION_DENIED"


def test_manage_symbols_write_applies_when_enabled(monkeypatch, tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setenv("OM_AGENT_ENABLE_WRITE_TOOLS", "true")

    out = run_tool(
        "manage_symbols",
        {
            "config_path": str(cfg_path),
            "action": "add",
            "symbol": "TSLA",
            "sell_put_enabled": True,
            "sell_put_min_dte": 20,
            "sell_put_max_dte": 45,
            "sell_put_min_strike": 100,
            "sell_put_max_strike": 120,
            "confirm": True,
        },
    )
    assert out["ok"] is True
    assert out["meta"]["write_applied"] is True

    current = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert [x["symbol"] for x in current["symbols"]] == ["NVDA", "TSLA"]


def test_preview_notification_is_read_only() -> None:
    from scripts.agent_plugin.main import run_tool

    alerts = """# Symbols Alerts

## 高优先级
- NVDA | sell_put | 2026-06-18 156P | 年化 10.00% | 净收入 100.0 | DTE 30 | Strike 156 | 中性 | ccy USD | mid 1.000 | cash_req $15,600 | 通过准入后，收益/风险组合较强，值得优先看。
"""
    out = run_tool("preview_notification", {"alerts_text": alerts, "account_label": "lx"})

    assert out["ok"] is True
    assert "### [lx] NVDA · 卖Put" in out["data"]["notification_text"]
