from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]


def test_auto_trade_intake_open_example_dry_run_without_pm_config() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/auto_trade_intake.py",
            "--config",
            "configs/examples/config.example.us.json",
            "--mode",
            "dry-run",
            "--deal-json",
            "configs/examples/auto_trade_intake.open.example.json",
        ],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "dry_run"
    assert payload["action"] == "open"
