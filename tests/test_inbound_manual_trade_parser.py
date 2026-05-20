from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.application.inbound.manual_trade_parser import build_manual_trade_draft


def _runtime_config() -> dict[str, Any]:
    return {"accounts": ["lx", "sy"]}


def _patch_multiplier(monkeypatch: pytest.MonkeyPatch, value: int | None, source: str | None = "cache") -> None:
    def _fake_resolve(**kwargs: Any) -> tuple[int | None, str | None, dict[str, Any]]:
        assert kwargs["allow_opend_refresh"] is False
        attempted = [
            {"source": "payload", "status": "missing" if kwargs.get("multiplier") in (None, "") else "resolved", "value": kwargs.get("multiplier")},
            {"source": source or "cache", "status": "resolved" if value else "miss", "value": value},
        ]
        return value, source if value else None, {"attempted_sources": attempted}

    monkeypatch.setattr("src.application.inbound.manual_trade_parser.resolve_multiplier_with_source_and_diagnostics", _fake_resolve)


def test_manual_trade_draft_parses_futu_open_without_manual_multiplier(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_multiplier(monkeypatch, 1000)
    message = (
        "成交提醒: 【成交提醒】成功卖出1张$中海油 260629 30.00 购$，成交价格：0.41，"
        "此笔订单委托已全部成交，2026/05/20 11:50:42 (香港)。【富途证券(香港)】"
    )
    expected_ms = int(datetime(2026, 5, 20, 3, 50, 42, tzinfo=timezone.utc).timestamp() * 1000)

    draft = build_manual_trade_draft(
        "manual_open",
        raw_text=f"记录开仓 lx {message}",
        accounts=("lx", "sy"),
        config_key="hk",
        config_path="/var/lib/options-monitor/config.hk.json",
        runtime_config=_runtime_config(),
        repo_base=tmp_path,
        allow_opend_refresh=False,
    )

    assert draft["arguments"] == {
        "account": "lx",
        "symbol": "0883.HK",
        "option_type": "call",
        "side": "short",
        "contracts": 1,
        "strike": 30.0,
        "expiration_ymd": "2026-06-29",
        "multiplier": 1000.0,
        "premium_per_share": 0.41,
        "currency": "HKD",
        "broker": "富途",
        "opened_at_ms": expected_ms,
    }
    diagnostics = draft["diagnostics"]
    assert diagnostics["fill_parser_source"] == "futu_fill_alert"
    assert diagnostics["raw_symbol"] == "中海油"
    assert diagnostics["canonical_symbol"] == "0883.HK"
    assert diagnostics["multiplier_source"] == "cache"
    assert diagnostics["fill_time_ms"] == expected_ms
    assert diagnostics["missing_fields"] == []


def test_manual_trade_draft_canonicalizes_handwritten_open_and_resolves_multiplier(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_multiplier(monkeypatch, 500)

    draft = build_manual_trade_draft(
        "manual_open",
        raw_text="记录开仓 sy 腾讯 short put strike 450 exp 2026-05-28 6张 premium 2.35",
        accounts=("lx", "sy"),
        config_key="hk",
        config_path=None,
        runtime_config=_runtime_config(),
        repo_base=tmp_path,
        allow_opend_refresh=False,
    )

    assert draft["arguments"]["symbol"] == "0700.HK"
    assert draft["arguments"]["multiplier"] == 500.0
    assert draft["diagnostics"]["raw_symbol"] == "腾讯"
    assert draft["diagnostics"]["canonical_symbol"] == "0700.HK"
    assert draft["diagnostics"]["symbol_source"] == "text"
    assert draft["diagnostics"]["multiplier_source_policy"]["allow_opend_refresh"] is False


def test_manual_trade_draft_canonicalizes_handwritten_close_symbol(tmp_path: Path) -> None:
    draft = build_manual_trade_draft(
        "manual_close",
        raw_text="记录平仓 sy HK.00700 short put strike 450 exp 2026-05-28 2张 close 1.2",
        accounts=("lx", "sy"),
        config_key="hk",
        config_path=None,
        runtime_config=_runtime_config(),
        repo_base=tmp_path,
        allow_opend_refresh=False,
    )

    assert draft["arguments"]["symbol"] == "0700.HK"
    assert draft["arguments"]["side"] == "short"
    assert draft["arguments"]["contracts_to_close"] == 2
    assert draft["arguments"]["close_price"] == 1.2
    assert draft["diagnostics"]["canonical_symbol"] == "0700.HK"
    assert draft["diagnostics"]["position_side"] == "short"


def test_manual_trade_draft_converts_futu_close_fill_side_to_position_side(tmp_path: Path) -> None:
    message = "【成交提醒】成功买入1张$腾讯 260629 450.00 沽$，成交价格：1.20，此笔订单委托已全部成交，2026/05/20 11:50:42 (香港)。【富途证券(香港)】"

    draft = build_manual_trade_draft(
        "manual_close",
        raw_text=f"记录平仓 sy {message}",
        accounts=("lx", "sy"),
        config_key="hk",
        config_path=None,
        runtime_config=_runtime_config(),
        repo_base=tmp_path,
        allow_opend_refresh=False,
    )

    assert draft["arguments"]["symbol"] == "0700.HK"
    assert draft["arguments"]["side"] == "short"
    assert draft["diagnostics"]["trade_side_raw"] == "long"
    assert draft["diagnostics"]["position_side"] == "short"


def test_manual_trade_draft_reports_missing_multiplier(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_multiplier(monkeypatch, None, None)

    draft = build_manual_trade_draft(
        "manual_open",
        raw_text="记录开仓 sy 腾讯 short put strike 450 exp 2026-05-28 6张 premium 2.35",
        accounts=("lx", "sy"),
        config_key="hk",
        config_path=None,
        runtime_config=_runtime_config(),
        repo_base=tmp_path,
        allow_opend_refresh=False,
    )

    assert "multiplier" in draft["diagnostics"]["missing_fields"]
    assert draft["diagnostics"]["multiplier_resolution_attempts"]
