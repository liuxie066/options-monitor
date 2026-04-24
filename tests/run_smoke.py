#!/usr/bin/env python3
"""Small smoke checks (fast, no OpenD).

Usage:
  ./.venv/bin/python tests/run_smoke.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _ensure_repo_on_path() -> Path:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return base


def test_scanners_require_multiplier() -> None:
    _ensure_repo_on_path()

    import pandas as pd
    from scripts.scan_sell_put import compute_metrics as put_metrics
    from scripts.scan_sell_call import compute_metrics as call_metrics

    put_row = pd.Series({'mid': 1.0, 'strike': 90.0, 'spot': 100.0, 'dte': 14, 'currency': 'HKD'})
    assert put_metrics(put_row) is None

    call_row = pd.Series({'mid': 1.0, 'strike': 110.0, 'spot': 100.0, 'dte': 14, 'currency': 'HKD'})
    assert call_metrics(call_row, avg_cost=80.0) is None


def test_cash_cap_is_best_effort() -> None:
    _ensure_repo_on_path()

    from scripts.pipeline_steps import derive_put_max_strike_from_cash

    # This is best-effort and depends on a local multiplier cache.
    ctx = {
        'cash_by_currency': {'HKD': 100000.0},
        'option_ctx': {'cash_secured_total_by_ccy': {'HKD': 0.0}},
    }
    out = derive_put_max_strike_from_cash('0700.HK', ctx, None, None)
    assert (out is None) or (float(out) >= 0.0)


def test_agent_launcher_spec_contract() -> None:
    base = _ensure_repo_on_path()
    vpy = (base / ".venv" / "bin" / "python").resolve()
    p = subprocess.run(
        [str(vpy), "scripts/cli/om_agent_cli.py", "spec"],
        cwd=str(base),
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(p.stdout)
    assert payload["schema_version"] == "1.0"
    assert any(str(x.get("name")) == "manage_symbols" for x in payload.get("tools", []))
    assert any(str(x.get("name")) == "prepare_close_advice_inputs" for x in payload.get("tools", []))
    assert any(str(x.get("name")) == "close_advice" for x in payload.get("tools", []))
    assert any(str(x.get("name")) == "get_close_advice" for x in payload.get("tools", []))


def test_agent_launcher_init_minimal_config() -> None:
    base = _ensure_repo_on_path()
    vpy = (base / ".venv" / "bin" / "python").resolve()
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.us.json"
        data_cfg_path = Path(td) / "portfolio.sqlite.json"
        p = subprocess.run(
            [
                str(vpy),
                "scripts/cli/om_agent_cli.py",
                "init",
                "--market",
                "us",
                "--futu-acc-id",
                "281756479859383816",
                "--config-path",
                str(cfg_path),
                "--data-config-path",
                str(data_cfg_path),
                "--symbol",
                "NVDA",
            ],
            cwd=str(base),
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ},
        )
        payload = json.loads(p.stdout)
        assert payload["ok"] is True
        assert payload["data"]["account_label"] == "user1"
        assert cfg_path.exists()
        assert data_cfg_path.exists()
        assert Path(payload["data"]["config_path"]).name == "config.us.json"
        assert Path(payload["data"]["data_config_path"]).name == "portfolio.sqlite.json"


def test_agent_launcher_add_external_holdings_account() -> None:
    base = _ensure_repo_on_path()
    vpy = (base / ".venv" / "bin" / "python").resolve()
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.us.json"
        data_cfg_path = Path(td) / "portfolio.sqlite.json"
        init_p = subprocess.run(
            [
                str(vpy),
                "scripts/cli/om_agent_cli.py",
                "init",
                "--market",
                "us",
                "--futu-acc-id",
                "281756479859383816",
                "--config-path",
                str(cfg_path),
                "--data-config-path",
                str(data_cfg_path),
                "--symbol",
                "NVDA",
            ],
            cwd=str(base),
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ},
        )
        assert json.loads(init_p.stdout)["ok"] is True

        add_p = subprocess.run(
            [
                str(vpy),
                "scripts/cli/om_agent_cli.py",
                "add-account",
                "--market",
                "us",
                "--config-path",
                str(cfg_path),
                "--account-label",
                "ext1",
                "--account-type",
                "external_holdings",
                "--holdings-account",
                "Feishu EXT",
            ],
            cwd=str(base),
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ},
        )
        payload = json.loads(add_p.stdout)
        current = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert payload["ok"] is True
        assert current["account_settings"]["ext1"]["type"] == "external_holdings"
        assert current["account_settings"]["ext1"]["holdings_account"] == "Feishu EXT"
        assert current["portfolio"]["source_by_account"]["ext1"] == "holdings"


def test_agent_launcher_add_futu_account_with_holdings_fallback() -> None:
    base = _ensure_repo_on_path()
    vpy = (base / ".venv" / "bin" / "python").resolve()
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.us.json"
        data_cfg_path = Path(td) / "portfolio.sqlite.json"
        init_p = subprocess.run(
            [
                str(vpy),
                "scripts/cli/om_agent_cli.py",
                "init",
                "--market",
                "us",
                "--futu-acc-id",
                "281756479859383816",
                "--config-path",
                str(cfg_path),
                "--data-config-path",
                str(data_cfg_path),
                "--symbol",
                "NVDA",
            ],
            cwd=str(base),
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ},
        )
        assert json.loads(init_p.stdout)["ok"] is True

        add_p = subprocess.run(
            [
                str(vpy),
                "scripts/cli/om_agent_cli.py",
                "add-account",
                "--market",
                "us",
                "--config-path",
                str(cfg_path),
                "--account-label",
                "sy",
                "--account-type",
                "futu",
                "--futu-acc-id",
                "381756479859383816",
                "--holdings-account",
                "sy",
            ],
            cwd=str(base),
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ},
        )
        payload = json.loads(add_p.stdout)
        current = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert payload["ok"] is True
        assert payload["data"]["holdings_account"] == "sy"
        assert current["account_settings"]["sy"]["type"] == "futu"
        assert current["account_settings"]["sy"]["holdings_account"] == "sy"
        assert current["portfolio"]["source_by_account"]["sy"] == "futu"


def test_agent_launcher_edit_account_updates_type_and_mappings() -> None:
    base = _ensure_repo_on_path()
    vpy = (base / ".venv" / "bin" / "python").resolve()
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.us.json"
        data_cfg_path = Path(td) / "portfolio.sqlite.json"
        subprocess.run(
            [
                str(vpy), "scripts/cli/om_agent_cli.py", "init",
                "--market", "us",
                "--futu-acc-id", "281756479859383816",
                "--config-path", str(cfg_path),
                "--data-config-path", str(data_cfg_path),
                "--symbol", "NVDA",
            ],
            cwd=str(base), capture_output=True, text=True, check=True, env={**os.environ},
        )
        subprocess.run(
            [
                str(vpy), "scripts/cli/om_agent_cli.py", "add-account",
                "--market", "us",
                "--config-path", str(cfg_path),
                "--account-label", "ext1",
                "--account-type", "external_holdings",
                "--holdings-account", "Feishu EXT",
            ],
            cwd=str(base), capture_output=True, text=True, check=True, env={**os.environ},
        )

        edit_p = subprocess.run(
            [
                str(vpy), "scripts/cli/om_agent_cli.py", "edit-account",
                "--market", "us",
                "--config-path", str(cfg_path),
                "--account-label", "ext1",
                "--account-type", "futu",
                "--futu-acc-id", "381756479859383816",
                "--holdings-account", "sy",
            ],
            cwd=str(base), capture_output=True, text=True, check=True, env={**os.environ},
        )
        payload = json.loads(edit_p.stdout)
        current = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert payload["ok"] is True
        assert payload["data"]["account_type"] == "futu"
        assert payload["data"]["holdings_account"] == "sy"
        assert current["account_settings"]["ext1"]["type"] == "futu"
        assert current["account_settings"]["ext1"]["holdings_account"] == "sy"
        assert current["trade_intake"]["account_mapping"]["futu"]["381756479859383816"] == "ext1"
        assert current["portfolio"]["source_by_account"]["ext1"] == "futu"


def test_agent_launcher_remove_account_updates_runtime_config() -> None:
    base = _ensure_repo_on_path()
    vpy = (base / ".venv" / "bin" / "python").resolve()
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.us.json"
        data_cfg_path = Path(td) / "portfolio.sqlite.json"
        subprocess.run(
            [
                str(vpy), "scripts/cli/om_agent_cli.py", "init",
                "--market", "us",
                "--futu-acc-id", "281756479859383816",
                "--config-path", str(cfg_path),
                "--data-config-path", str(data_cfg_path),
                "--symbol", "NVDA",
            ],
            cwd=str(base), capture_output=True, text=True, check=True, env={**os.environ},
        )
        subprocess.run(
            [
                str(vpy), "scripts/cli/om_agent_cli.py", "add-account",
                "--market", "us",
                "--config-path", str(cfg_path),
                "--account-label", "sy",
                "--account-type", "futu",
                "--futu-acc-id", "381756479859383816",
            ],
            cwd=str(base), capture_output=True, text=True, check=True, env={**os.environ},
        )

        remove_p = subprocess.run(
            [
                str(vpy), "scripts/cli/om_agent_cli.py", "remove-account",
                "--market", "us",
                "--config-path", str(cfg_path),
                "--account-label", "user1",
            ],
            cwd=str(base), capture_output=True, text=True, check=True, env={**os.environ},
        )
        payload = json.loads(remove_p.stdout)
        current = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert payload["ok"] is True
        assert payload["data"]["removed_account"] == "user1"
        assert current["accounts"] == ["sy"]
        assert current["portfolio"]["account"] == "sy"
        assert "user1" not in current["trade_intake"]["account_mapping"]["futu"].values()


def test_agent_launcher_spec_prefers_broker_field() -> None:
    base = _ensure_repo_on_path()
    vpy = (base / ".venv" / "bin" / "python").resolve()
    p = subprocess.run(
        [str(vpy), "scripts/cli/om_agent_cli.py", "spec"],
        cwd=str(base),
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(p.stdout)
    tool = next(item for item in payload["tools"] if item["name"] == "query_cash_headroom")
    assert "broker" in tool["input_schema"]
    assert "data_config" in tool["input_schema"]


def main() -> None:
    test_scanners_require_multiplier()
    test_cash_cap_is_best_effort()
    test_agent_launcher_spec_contract()
    test_agent_launcher_spec_prefers_broker_field()
    test_agent_launcher_init_minimal_config()
    test_agent_launcher_add_external_holdings_account()
    test_agent_launcher_add_futu_account_with_holdings_fallback()
    test_agent_launcher_edit_account_updates_type_and_mappings()
    test_agent_launcher_remove_account_updates_runtime_config()
    print('OK (smoke)')


if __name__ == '__main__':
    main()
