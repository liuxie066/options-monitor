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


def test_agent_launcher_healthcheck_minimal_config() -> None:
    base = _ensure_repo_on_path()
    vpy = (base / ".venv" / "bin" / "python").resolve()
    with tempfile.TemporaryDirectory() as td:
        cfg_dir = Path(td)
        (cfg_dir / "config.us.json").write_text(
            json.dumps(
                {
                    "accounts": ["lx"],
                    "portfolio": {"market": "富途"},
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
                            "fetch": {"source": "yahoo", "limit_expirations": 2},
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
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        env = dict(**{"OM_CONFIG_DIR": str(cfg_dir)})
        p = subprocess.run(
            [str(vpy), "scripts/cli/om_agent_cli.py", "run", "--tool", "healthcheck", "--input-json", '{"config_key":"us"}'],
            cwd=str(base),
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, **env},
        )
    payload = json.loads(p.stdout)
    assert payload["ok"] is True
    assert payload["data"]["config"]["accounts"] == ["lx"]
    assert any("portfolio.pm_config is not configured" in x for x in payload["warnings"])


def main() -> None:
    test_scanners_require_multiplier()
    test_cash_cap_is_best_effort()
    test_agent_launcher_spec_contract()
    test_agent_launcher_healthcheck_minimal_config()
    print('OK (smoke)')


if __name__ == '__main__':
    main()
