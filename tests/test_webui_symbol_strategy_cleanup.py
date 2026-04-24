from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from tempfile import TemporaryDirectory


class _FakeFastAPI:
    def __init__(self, *args, **kwargs):
        pass

    def mount(self, *args, **kwargs):
        return None

    def get(self, *args, **kwargs):
        return lambda fn: fn

    def post(self, *args, **kwargs):
        return lambda fn: fn


def _install_fastapi_stubs() -> None:
    fastapi_mod = types.ModuleType("fastapi")
    fastapi_mod.FastAPI = _FakeFastAPI
    fastapi_mod.Request = object

    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi_mod.HTTPException = _HTTPException

    responses_mod = types.ModuleType("fastapi.responses")
    responses_mod.FileResponse = object
    responses_mod.HTMLResponse = object

    staticfiles_mod = types.ModuleType("fastapi.staticfiles")

    class _StaticFiles:
        def __init__(self, *args, **kwargs):
            pass

    staticfiles_mod.StaticFiles = _StaticFiles

    sys.modules.setdefault("fastapi", fastapi_mod)
    sys.modules.setdefault("fastapi.responses", responses_mod)
    sys.modules.setdefault("fastapi.staticfiles", staticfiles_mod)


_install_fastapi_stubs()

from scripts.webui.server import (
    SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS,
    _account_rows,
    _clean_symbol_level_strategy_fields,
    _global_summary,
    _patch_notifications,
    _repair_hint_from_error,
    _patch_entry,
    _to_row,
)
import scripts.webui.server as webui_server


def test_patch_entry_removes_forbidden_symbol_level_strategy_fields() -> None:
    entry = {
        "symbol": "PDD",
        "sell_put": {
            "enabled": True,
            "min_dte": 7,
            "max_dte": 45,
            "min_strike": 0,
            "max_strike": 120,
            "require_bid_ask": True,
            "min_iv": 0.2,
            "max_iv": 1.2,
            "min_abs_delta": 0.1,
            "max_abs_delta": 0.3,
            "min_open_interest": 50,
            "min_volume": 10,
            "max_spread_ratio": 0.3,
            "event_risk": {"enabled": True},
        },
        "sell_call": {
            "enabled": False,
            "require_bid_ask": True,
            "min_iv": 0.2,
        },
    }

    _patch_entry(
        entry,
        {
            "sell_put_min_dte": 10,
            "sell_put_max_dte": 50,
            "sell_put_min_strike": 1,
            "sell_put_max_strike": 110,
        },
    )

    for field in SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS:
        assert field not in entry["sell_put"]
        assert field not in entry["sell_call"]
    assert entry["sell_put"]["min_dte"] == 10
    assert entry["sell_put"]["max_dte"] == 50
    assert entry["sell_put"]["min_strike"] == 1.0
    assert entry["sell_put"]["max_strike"] == 110.0


def test_clean_symbol_level_strategy_fields_removes_stale_keys_from_all_symbols() -> None:
    cfg = {
        "symbols": [
            {
                "symbol": "PDD",
                "sell_put": {
                    "enabled": True,
                    "require_bid_ask": True,
                    "min_iv": 0.2,
                },
            },
            {
                "symbol": "NVDA",
                "sell_call": {
                    "enabled": True,
                    "max_spread_ratio": 0.3,
                    "event_risk": {"enabled": True},
                },
            },
        ]
    }

    _clean_symbol_level_strategy_fields(cfg)

    for item in cfg["symbols"]:
        for side in ("sell_put", "sell_call"):
            side_cfg = item.get(side) or {}
            for field in SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS:
                assert field not in side_cfg


def test_to_row_exposes_symbol_name_from_supported_config_fields() -> None:
    row = _to_row("hk", {"symbol": "0700.HK", "name": "腾讯控股"})
    assert row.name == "腾讯控股"

    fallback_row = _to_row("us", {"symbol": "NVDA", "display_name": "NVIDIA"})
    assert fallback_row.name == "NVIDIA"


def test_to_row_derives_symbol_name_from_intake_aliases() -> None:
    row = _to_row(
        "hk",
        {"symbol": "0700.HK"},
        {"intake": {"symbol_aliases": {"腾讯": "0700.HK", "腾讯控股": "0700.HK"}}},
    )
    assert row.name == "腾讯"


def test_to_row_prefers_explicit_symbol_name_over_aliases() -> None:
    row = _to_row(
        "hk",
        {"symbol": "0700.HK", "name": "腾讯控股"},
        {"intake": {"symbol_aliases": {"腾讯": "0700.HK"}}},
    )
    assert row.name == "腾讯控股"


def test_global_summary_exposes_resolved_and_recommended_runtime_config_paths() -> None:
    old_base = webui_server.BASE_DIR
    old_config_files = dict(webui_server.CONFIG_FILES)
    try:
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "options-monitor-prod"
            repo.mkdir()
            canonical_dir = root / "options-monitor-config"
            canonical_dir.mkdir()

            local_cfg = repo / "config.hk.json"
            local_cfg.write_text('{"symbols": []}', encoding="utf-8")
            canonical_cfg = canonical_dir / "config.hk.json"
            canonical_cfg.write_text('{"symbols": []}', encoding="utf-8")

            webui_server.BASE_DIR = repo
            webui_server.CONFIG_FILES = {"hk": Path("config.hk.json"), "us": Path("config.us.json")}

            summary = _global_summary("hk")
            assert summary["resolvedPath"] == str(local_cfg.resolve())
            assert summary["recommendedPath"] == str(canonical_cfg.resolve())
            assert summary["recommendedPathExists"] is True
            assert summary["canonicalPathWarning"] is True
    finally:
        webui_server.BASE_DIR = old_base
        webui_server.CONFIG_FILES = old_config_files


def test_global_summary_suppresses_canonical_warning_when_webui_config_override_enabled(monkeypatch) -> None:
    old_base = webui_server.BASE_DIR
    old_config_files = dict(webui_server.CONFIG_FILES)
    try:
        with TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "options-monitor-prod"
            repo.mkdir()
            canonical_dir = root / "options-monitor-config"
            canonical_dir.mkdir()
            custom_dir = root / "runtime-configs"
            custom_dir.mkdir()

            canonical_cfg = canonical_dir / "config.hk.json"
            canonical_cfg.write_text('{"symbols": []}', encoding="utf-8")
            custom_cfg = custom_dir / "config.hk.json"
            custom_cfg.write_text('{"symbols": []}', encoding="utf-8")

            monkeypatch.setenv("OM_WEBUI_CONFIG_DIR", str(custom_dir))
            webui_server.BASE_DIR = repo
            webui_server.CONFIG_FILES = {"hk": custom_cfg, "us": Path("config.us.json")}

            summary = _global_summary("hk")
            assert Path(summary["resolvedPath"]).resolve() == custom_cfg.resolve()
            assert summary["recommendedPath"] == str(canonical_cfg.resolve())
            assert summary["recommendedPathExists"] is True
            assert summary["canonicalPathWarning"] is False
    finally:
        webui_server.BASE_DIR = old_base
        webui_server.CONFIG_FILES = old_config_files


def test_patch_notifications_updates_runtime_notification_fields() -> None:
    cfg = {
        "notifications": {
            "channel": "feishu",
            "target": "user:old",
            "quiet_hours_beijing": {"start": "01:00", "end": "07:00"},
        }
    }

    _patch_notifications(
        cfg,
        {
            "notifications": {
                "enabled": True,
                "channel": "feishu",
                "target": "user:new_target",
                "include_cash_footer": False,
                "cash_footer_accounts": ["user1", "sy"],
                "cash_footer_timeout_sec": 30,
                "cash_snapshot_max_age_sec": 180,
                "quiet_hours_beijing": {"start": "02:00", "end": "08:00"},
                "opend_alert_cooldown_sec": 600,
                "opend_alert_burst_window_sec": 900,
                "opend_alert_burst_max": 3,
            }
        },
    )

    notif = cfg["notifications"]
    assert notif["enabled"] is True
    assert notif["channel"] == "feishu"
    assert notif["target"] == "user:new_target"
    assert notif["include_cash_footer"] is False
    assert notif["cash_footer_accounts"] == ["user1", "sy"]
    assert notif["cash_footer_timeout_sec"] == 30
    assert notif["cash_snapshot_max_age_sec"] == 180
    assert notif["quiet_hours_beijing"] == {"start": "02:00", "end": "08:00"}
    assert notif["opend_alert_cooldown_sec"] == 600
    assert notif["opend_alert_burst_window_sec"] == 900
    assert notif["opend_alert_burst_max"] == 3


def test_global_summary_exposes_notification_config_fields() -> None:
    old_base = webui_server.BASE_DIR
    old_config_files = dict(webui_server.CONFIG_FILES)
    try:
        with TemporaryDirectory() as td:
            repo = Path(td)
            cfg_path = repo / "config.us.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "accounts": ["user1"],
                        "symbols": [{"symbol": "NVDA", "sell_put": {"enabled": True}, "sell_call": {"enabled": False}}],
                        "notifications": {
                            "enabled": True,
                            "channel": "feishu",
                            "target": "user:abc",
                            "include_cash_footer": False,
                            "cash_footer_accounts": ["user1"],
                            "quiet_hours_beijing": {"start": "02:00", "end": "08:00"},
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            webui_server.BASE_DIR = repo
            webui_server.CONFIG_FILES = {"us": Path("config.us.json"), "hk": Path("config.hk.json")}

            summary = _global_summary("us")
            notifications = summary["sections"]["notifications"]
            assert notifications["enabled"] is True
            assert notifications["channel"] == "feishu"
            assert notifications["target"] == "user:abc"
            assert notifications["include_cash_footer"] is False
            assert notifications["cash_footer_accounts"] == ["user1"]
            assert notifications["quiet_hours_beijing"] == {"start": "02:00", "end": "08:00"}
    finally:
        webui_server.BASE_DIR = old_base
        webui_server.CONFIG_FILES = old_config_files


def test_account_rows_expose_primary_and_fallback_visibility() -> None:
    old_base = webui_server.BASE_DIR
    old_config_files = dict(webui_server.CONFIG_FILES)
    try:
        with TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "secrets").mkdir()
            cfg_path = repo / "config.us.json"
            data_cfg_path = repo / "secrets" / "portfolio.sqlite.json"
            data_cfg_path.write_text(
                json.dumps(
                    {
                        "feishu": {
                            "app_id": "cli_a",
                            "app_secret": "secret_b",
                            "tables": {"holdings": "app_x/tbl_holdings"},
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            cfg_path.write_text(
                json.dumps(
                    {
                        "accounts": ["user1", "sy"],
                        "portfolio": {"data_config": "secrets/portfolio.sqlite.json", "source": "futu"},
                        "account_settings": {
                            "user1": {"type": "futu", "holdings_account": "lx"},
                            "sy": {"type": "external_holdings", "holdings_account": "sy"},
                        },
                        "trade_intake": {"account_mapping": {"futu": {"281756479859383816": "user1"}}},
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            webui_server.BASE_DIR = repo
            webui_server.CONFIG_FILES = {"us": Path("config.us.json"), "hk": Path("config.hk.json")}

            rows = _account_rows("us")
            user1 = next(row for row in rows if row["account_label"] == "user1")
            sy = next(row for row in rows if row["account_label"] == "sy")

            assert user1["primary_source"] == "futu"
            assert user1["primary_ready"] is True
            assert user1["fallback_enabled"] is True
            assert user1["fallback_ready"] is True

            assert sy["primary_source"] == "holdings"
            assert sy["primary_ready"] is True
            assert sy["fallback_enabled"] is True
            assert sy["fallback_source"] == "holdings"
    finally:
        webui_server.BASE_DIR = old_base
        webui_server.CONFIG_FILES = old_config_files


def test_repair_hint_maps_common_config_error() -> None:
    out = _repair_hint_from_error(
        {
            "code": "CONFIG_ERROR",
            "message": "runtime config not found: config.us.json",
            "hint": "Create the repo-local config file or pass config_path explicitly.",
        }
    )
    assert out is not None
    assert out["code"] == "CONFIG_ERROR"
    assert "repo-local config file" in out["summary"]
    assert any("om-agent init" in item for item in out["actions"])


def test_webui_frontend_shows_resolved_path_and_warning_copy() -> None:
    src = Path("scripts/webui/frontend/src/App.jsx").read_text(encoding="utf-8")
    assert "const [configModule, setConfigModule] = useState('symbols');" in src
    assert '<span className="Spacer ModuleTabsSpacer" />' in src
    assert '新增 {marketMeta.label} 标的' in src
    assert "账户管理" in src
    assert "运行结果" in src
    assert 'ToolbarSpacer' not in src
    assert "summary.resolvedPath || summary.path" in src
    assert "当前 WebUI 写入路径不是推荐 canonical runtime config" in src
    assert "summary.recommendedPath" in src
    assert "Feishu 推送" in src
    assert "notificationsPayloadFromGlobalForm" in src
    assert "Close Advice" in src
    assert "审计历史" in src
    assert "修复建议" in src
    assert "/api/history" in src


def test_deploy_safe_uses_env_configured_canonical_runtime_hash_guard() -> None:
    src = Path("scripts/deploy_safe.sh").read_text(encoding="utf-8")
    assert 'OM_CANONICAL_CONFIG_US' in src
    assert 'OM_CANONICAL_CONFIG_HK' in src
    assert 'runtime config hash guard disabled' in src

    docs = Path("docs/GUARDRAILS.md").read_text(encoding="utf-8")
    assert 'OM_CANONICAL_CONFIG_US' in docs
    assert 'OM_CANONICAL_CONFIG_HK' in docs
