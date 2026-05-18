from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile

from src.application.layered_config import build_layered_runtime_config


BASE = Path(__file__).resolve().parents[1]


def _write_runtime_config(tmp_path: Path) -> Path:
    cfg, _meta = build_layered_runtime_config(
        repo_root=BASE,
        market="us",
        user_config_path=BASE / "configs" / "examples" / "user.example.us.json",
    )
    path = tmp_path / "config.us.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _write_open_deal_payload(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "deal_id": "example-open-0700hk-20260429-480p-1",
                "order_id": "example-order-open-1",
                "trd_acc_id": "REAL_12345678",
                "code": "0700.HK",
                "option_type": "PUT",
                "side": "SELL",
                "position_effect": "OPEN",
                "qty": 2,
                "price": 3.93,
                "strike": 480,
                "multiplier": 100,
                "expiration": "20260429",
                "currency": "HKD",
                "create_time": "2026-04-09 13:10:25",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_auto_trade_intake_open_example_dry_run_without_explicit_data_config(tmp_path: Path) -> None:
    config_path = _write_runtime_config(tmp_path)
    deal_path = _write_open_deal_payload(tmp_path / "auto_trade_intake.open.json")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.application.trades.auto_intake",
            "--config",
            str(config_path),
            "--mode",
            "dry-run",
            "--deal-json",
            str(deal_path),
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


def test_auto_trade_intake_once_defaults_state_paths_to_runtime_root(tmp_path: Path) -> None:
    config_path = _write_runtime_config(tmp_path)
    runtime_root = tmp_path / "runtime"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.application.trades.auto_intake",
            "--config",
            str(config_path),
            "--mode",
            "dry-run",
            "--once",
        ],
        cwd=str(BASE),
        env={**dict(os.environ), "OM_RUNTIME_ROOT": str(runtime_root)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["state_path"] == str((runtime_root / "output" / "state" / "auto_trade_intake_state.json").resolve())
    assert payload["audit_path"] == str((runtime_root / "output" / "state" / "auto_trade_intake_audit.jsonl").resolve())
    assert payload["status_path"] == str((runtime_root / "output" / "state" / "auto_trade_intake_status.json").resolve())


def test_auto_trade_intake_open_dry_run_accepts_futu_option_code_with_lookup_fields(tmp_path: Path) -> None:
    config_path = _write_runtime_config(tmp_path)
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
                "-m",
                "src.application.trades.auto_intake",
                "--config",
                str(config_path),
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
