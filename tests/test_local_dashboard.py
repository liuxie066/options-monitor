from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_dashboard_module():
    path = Path("scripts/local_dashboard.py").resolve()
    spec = importlib.util.spec_from_file_location("local_dashboard_under_test", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_dashboard_saves_xueqiu_cookie_without_echoing_value(tmp_path, monkeypatch) -> None:
    dashboard = _load_dashboard_module()
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_RUNTIME_ROOT=/tmp/runtime\nXUEQIU_COOKIE=old-cookie\n", encoding="utf-8")
    monkeypatch.setenv("OM_DASHBOARD_ENV_FILE", str(env_file))
    monkeypatch.delenv("XUEQIU_COOKIE", raising=False)

    result = dashboard._save_env_setting(env_file, "XUEQIU_COOKIE", "new-cookie-value")
    page = dashboard._settings_page(result).decode("utf-8")

    assert result["ok"] is True
    assert "new-cookie-value" not in result["stdout"]
    assert "new-cookie-value" not in page
    text = env_file.read_text(encoding="utf-8")
    assert text.count("XUEQIU_COOKIE=") == 1
    assert 'XUEQIU_COOKIE="new-cookie-value"' in text
    assert "OM_RUNTIME_ROOT=/tmp/runtime" in text


def test_dashboard_saves_robinhood_token_without_echoing_value(tmp_path, monkeypatch) -> None:
    dashboard = _load_dashboard_module()
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_RUNTIME_ROOT=/tmp/runtime\n", encoding="utf-8")
    monkeypatch.setenv("OM_DASHBOARD_ENV_FILE", str(env_file))
    monkeypatch.delenv("ROBINHOOD_AUTH_TOKEN", raising=False)

    result = dashboard._save_env_setting(env_file, "ROBINHOOD_AUTH_TOKEN", "robinhood-secret-token")
    page = dashboard._settings_page(result).decode("utf-8")

    assert result["ok"] is True
    assert "robinhood-secret-token" not in result["stdout"]
    assert "robinhood-secret-token" not in page
    assert 'ROBINHOOD_AUTH_TOKEN="robinhood-secret-token"' in env_file.read_text(encoding="utf-8")


def test_dashboard_settings_page_mentions_blogger_link(tmp_path, monkeypatch) -> None:
    dashboard = _load_dashboard_module()
    env_file = tmp_path / "options-monitor.env"
    monkeypatch.setenv("OM_DASHBOARD_ENV_FILE", str(env_file))
    monkeypatch.delenv("XUEQIU_COOKIE", raising=False)
    monkeypatch.delenv("ROBINHOOD_AUTH_TOKEN", raising=False)

    page = dashboard._settings_page().decode("utf-8")

    assert "Options Monitor Settings" in page
    assert "https://xueqiu.com/u/1247347556#/stock" in page
    assert "Xueqiu cookie: not saved" in page
    assert "Robinhood token: not saved" in page
    assert "Save And Test Cookie" in page
    assert "Save And Test Token" in page


def test_dashboard_home_is_simplified_recommendation_flow(tmp_path, monkeypatch) -> None:
    dashboard = _load_dashboard_module()
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text('XUEQIU_COOKIE="cookie"\nROBINHOOD_AUTH_TOKEN="token"\n', encoding="utf-8")
    monkeypatch.setenv("OM_DASHBOARD_ENV_FILE", str(env_file))

    page = dashboard._page().decode("utf-8")

    assert "Generate Option Recommendations" in page
    assert "Generate Recommendations" in page
    assert "Tier-A target basis required" in page
    assert "no in-the-money puts" in page
    assert "IV at least 40%" in page
    assert "at least 15% out-of-the-money" in page
    assert "Cash is assumed unlimited" in page
    assert "ranked by annualized yield" in page
    assert "Portfolio NAV for risk guardrails" not in page
    assert "Xueqiu: ready" in page
    assert "Robinhood: ready" in page
    assert "Config Starter" not in page
    assert "Validate US" not in page
    assert "Preview Option Chain" not in page
    assert "Run US Scan" not in page
