from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.application.inbound.manual_trade_parser import build_manual_trade_draft
from src.application.multiplier_cache import save_cache


def _runtime_config() -> dict[str, Any]:
    return {"accounts": ["lx", "sy"]}


def _patch_multiplier(
    monkeypatch: pytest.MonkeyPatch,
    value: int | None,
    source: str | None = "cache",
    *,
    expected_allow_opend_refresh: bool = False,
) -> None:
    def _fake_resolve(**kwargs: Any) -> tuple[int | None, str | None, dict[str, Any]]:
        assert kwargs["allow_opend_refresh"] is expected_allow_opend_refresh
        attempted = [
            {"source": "payload", "status": "missing" if kwargs.get("multiplier") in (None, "") else "resolved", "value": kwargs.get("multiplier")},
            {"source": source or "cache", "status": "resolved" if value else "miss", "value": value},
        ]
        diagnostics = {
            "attempted_sources": attempted,
            "cache_path": str(Path(kwargs["repo_base"]) / "output_shared" / "state" / "multiplier_cache.json"),
        }
        if not value:
            diagnostics["message"] = f"recognized {kwargs.get('symbol')} but multiplier could not be resolved"
        return value, source if value else None, diagnostics

    monkeypatch.setattr("src.application.inbound.manual_trade_parser.resolve_multiplier_with_source_and_diagnostics", _fake_resolve)


def test_manual_trade_draft_parses_futu_open_without_manual_multiplier(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_multiplier(monkeypatch, 1000, expected_allow_opend_refresh=True)
    message = (
        "成交提醒: 【成交提醒】成功卖出1张$中海油 260629 30.00 购$，成交价格：0.41，"
        "此笔订单委托已全部成交，2026/05/20 11:50:42 (香港)。【富途证券(香港)】"
    )
    expected_ms = int(datetime(2026, 5, 20, 3, 50, 42, tzinfo=timezone.utc).timestamp() * 1000)

    draft = build_manual_trade_draft(
        "manual_open",
        raw_text=f"记录开仓 sy {message}",
        accounts=("lx", "sy"),
        config_key="hk",
        config_path="/var/lib/options-monitor/config.hk.json",
        runtime_config=_runtime_config(),
        repo_base=tmp_path,
        allow_opend_refresh=True,
    )

    assert draft["arguments"] == {
        "account": "sy",
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
    assert diagnostics["multiplier_source_policy"]["mode"] == "cache_opend"
    assert diagnostics["multiplier_source_policy"]["allow_opend_refresh"] is True
    assert diagnostics["fill_time_ms"] == expected_ms
    assert diagnostics["missing_fields"] == []


def test_manual_trade_draft_reads_runtime_multiplier_cache_from_config_path(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    runtime.mkdir()
    repo.mkdir()
    cache_path = runtime / "output_shared" / "state" / "multiplier_cache.json"
    save_cache(cache_path, {"0883.HK": {"multiplier": 1000, "source": "runtime_seed"}})

    message = (
        "记录开仓 sy 成交提醒: 【成交提醒】成功卖出1张$中海油 260629 30.00 购$，"
        "成交价格：0.41，此笔订单委托已全部成交，2026/05/20 11:50:42 (香港)。"
    )

    draft = build_manual_trade_draft(
        "manual_open",
        raw_text=message,
        accounts=("lx", "sy"),
        config_key="hk",
        config_path=runtime / "config.hk.json",
        runtime_config=_runtime_config(),
        repo_base=repo,
        allow_opend_refresh=False,
    )

    assert draft["arguments"]["symbol"] == "0883.HK"
    assert draft["arguments"]["multiplier"] == 1000.0
    assert draft["arguments"]["currency"] == "HKD"
    assert draft["diagnostics"]["multiplier_source"] == "runtime_seed"
    assert draft["diagnostics"]["multiplier_cache_path"] == str(cache_path.resolve())
    assert draft["diagnostics"]["missing_fields"] == []


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
    assert draft["diagnostics"]["multiplier_resolution_message"] == "recognized 0700.HK but multiplier could not be resolved"
