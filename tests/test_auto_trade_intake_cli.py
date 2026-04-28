from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
import tempfile


BASE = Path(__file__).resolve().parents[1]


def test_auto_trade_intake_open_example_dry_run_without_explicit_data_config() -> None:
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


def test_auto_trade_intake_open_dry_run_accepts_futu_option_code_with_lookup_fields() -> None:
    payload_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            payload_path = f.name
            json.dump(
                {
                    "deal_id": "example-open-pop-20260528-150p-1",
                    "order_id": "example-order-open-pop-1",
                    "futu_account_id": "REAL_12345678",
                    "code": "HK.POP260528P150000",
                    "stock_name": "泡泡玛特",
                    "trd_side": "SELL_SHORT",
                    "qty": 1,
                    "price": 6.3,
                    "multiplier": 1000,
                    "create_time": "2026-04-28 10:15:56",
                },
                f,
                ensure_ascii=False,
            )
        result = subprocess.run(
            [
                sys.executable,
                "scripts/auto_trade_intake.py",
                "--config",
                "configs/examples/config.example.us.json",
                "--mode",
                "dry-run",
                "--deal-json",
                payload_path,
            ],
            cwd=str(BASE),
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        if payload_path:
            Path(payload_path).unlink(missing_ok=True)

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "dry_run"
    assert payload["action"] == "open"
    assert payload["account"] == "user1"
