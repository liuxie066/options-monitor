from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src.application.healthcheck import build_wheel_activation_readiness
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.tool_execution import execute_tool
from src.application.wheel.config import build_wheel_policy_hash
from tests.test_agent_plugin_smoke import (
    _patch_healthcheck_dependencies,
    _public_cfg_with_futu,
)


def _wheel_config(
    *,
    account: str = "lx",
    generation: int = 1,
    activated_at_ms: int = 1_000,
    deactivated_at_ms: int | None = None,
) -> dict:
    return {
        "wheel": {
            "enabled": True,
            "accounts": [account],
            "activation_by_account": {
                account: {
                    "generation": generation,
                    "activated_at_ms": activated_at_ms,
                    "deactivated_at_ms": deactivated_at_ms,
                }
            },
        }
    }


def _write_activation_table(
    path: Path,
    *,
    market: str,
    account: str,
    generation: int,
    activated_at_ms: int,
    deactivated_at_ms: int | None,
    policy_hash: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE wheel_activation_windows (
              market TEXT NOT NULL,
              account TEXT NOT NULL,
              generation INTEGER NOT NULL,
              activated_at_ms INTEGER NOT NULL,
              deactivated_at_ms INTEGER,
              policy_hash TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO wheel_activation_windows (
              market, account, generation, activated_at_ms,
              deactivated_at_ms, policy_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                market,
                account,
                generation,
                activated_at_ms,
                deactivated_at_ms,
                policy_hash,
            ),
        )


def test_wheel_activation_readiness_matches_open_and_closed_windows(
    tmp_path: Path,
) -> None:
    open_config = _wheel_config()
    open_policy_hash = build_wheel_policy_hash(
        open_config,
        market="us",
        account="lx",
    )
    open_path = tmp_path / "open.sqlite3"
    _write_activation_table(
        open_path,
        market="us",
        account="lx",
        generation=1,
        activated_at_ms=1_000,
        deactivated_at_ms=None,
        policy_hash=open_policy_hash,
    )

    open_readiness = build_wheel_activation_readiness(
        config=open_config,
        market="US",
        accounts=["LX"],
        sqlite_path=open_path,
    )

    assert open_readiness["monitoring_gate"] == "enabled"
    assert open_readiness["reason_code"] is None
    assert open_readiness["storage_status"] == "available"
    assert open_readiness["enabled_account_count"] == 1
    assert open_readiness["accounts"]["lx"] == {
        "market": "us",
        "account": "lx",
        "generation": 1,
        "activated_at_ms": 1_000,
        "deactivated_at_ms": None,
        "policy_hash": open_policy_hash,
        "ready": True,
        "enabled_for_new_lifecycle": True,
        "monitoring_gate": "enabled",
        "reason_code": None,
        "identity_source": "durable_window",
        "descriptor": {
            "market": "us",
            "account": "lx",
            "generation": 1,
            "activated_at_ms": 1_000,
            "deactivated_at_ms": None,
            "policy_hash": open_policy_hash,
        },
        "durable_window": {
            "market": "us",
            "account": "lx",
            "generation": 1,
            "activated_at_ms": 1_000,
            "deactivated_at_ms": None,
            "policy_hash": open_policy_hash,
        },
    }

    closed_config = _wheel_config(deactivated_at_ms=2_000)
    closed_path = tmp_path / "closed.sqlite3"
    _write_activation_table(
        closed_path,
        market="us",
        account="lx",
        generation=1,
        activated_at_ms=1_000,
        deactivated_at_ms=2_000,
        policy_hash=open_policy_hash,
    )
    closed_readiness = build_wheel_activation_readiness(
        config=closed_config,
        market="us",
        accounts=["lx"],
        sqlite_path=closed_path,
    )
    assert closed_readiness["monitoring_gate"] == "disabled"
    assert closed_readiness["reason_code"] == "closed_window"
    assert closed_readiness["accounts"]["lx"]["deactivated_at_ms"] == 2_000


def test_wheel_activation_readiness_fails_closed_for_missing_and_mismatched_state(
    tmp_path: Path,
) -> None:
    config = _wheel_config()
    missing_path = tmp_path / "missing.sqlite3"

    missing = build_wheel_activation_readiness(
        config=config,
        market="us",
        accounts=["lx"],
        sqlite_path=missing_path,
    )

    assert missing["monitoring_gate"] == "disabled"
    assert missing["reason_code"] == "missing_window"
    assert missing["storage_status"] == "missing_database"
    assert missing_path.exists() is False

    policy_hash = build_wheel_policy_hash(config, market="us", account="lx")
    mismatch_path = tmp_path / "mismatch.sqlite3"
    _write_activation_table(
        mismatch_path,
        market="us",
        account="lx",
        generation=2,
        activated_at_ms=2_000,
        deactivated_at_ms=None,
        policy_hash=policy_hash,
    )
    mismatch = build_wheel_activation_readiness(
        config=config,
        market="us",
        accounts=["lx"],
        sqlite_path=mismatch_path,
    )
    assert mismatch["monitoring_gate"] == "config_mismatch"
    assert mismatch["reason_code"] == "descriptor_mismatch"
    assert mismatch["accounts"]["lx"]["generation"] == 2

    no_descriptor = build_wheel_activation_readiness(
        config={"wheel": {"enabled": True, "accounts": ["lx"]}},
        market="us",
        accounts=["lx"],
        sqlite_path=mismatch_path,
    )
    assert no_descriptor["monitoring_gate"] == "disabled"
    assert no_descriptor["reason_code"] == "missing_descriptor"


def test_runtime_status_and_healthcheck_expose_same_read_only_activation_readiness(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.us.json"
    data_config_path = tmp_path / "portfolio.runtime.json"
    data_config_path.write_text(
        json.dumps(
            {"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}
        ),
        encoding="utf-8",
    )
    config = _public_cfg_with_futu("portfolio.runtime.json")
    config["wheel"] = _wheel_config(account="user1")["wheel"]
    policy_hash = build_wheel_policy_hash(
        config,
        market="us",
        account="user1",
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")
    sqlite_path = tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    repo = SQLiteOptionPositionsRepository(sqlite_path)
    with repo._connect() as conn:
        conn.execute(
            """
            INSERT INTO wheel_activation_windows (
              market, account, generation, activated_at_ms, deactivated_at_ms,
              policy_hash, activation_request_id, activation_request_hash,
              deactivation_request_id, deactivation_request_hash
            ) VALUES ('us', 'user1', 1, 1000, NULL, ?, 'activation-test', ?, NULL, NULL)
            """,
            (policy_hash, "a" * 64),
        )

    _patch_healthcheck_dependencies(monkeypatch)
    runtime_status = execute_tool(
        "runtime_status",
        {"config_path": str(config_path), "accounts": ["user1"]},
    )
    healthcheck = execute_tool(
        "healthcheck",
        {"config_path": str(config_path), "accounts": ["user1"]},
    )

    runtime_readiness = runtime_status["data"]["wheel_activation_readiness"]
    health_readiness = healthcheck["data"]["wheel_activation_readiness"]
    assert runtime_readiness == health_readiness
    assert runtime_readiness["monitoring_gate"] == "enabled"
    assert runtime_readiness["accounts"]["user1"]["policy_hash"] == policy_hash
    assert runtime_status["data"]["summary"]["wheel_activation_monitoring_gate"] == "enabled"
    assert healthcheck["data"]["summary"]["wheel_activation_monitoring_gate"] == "enabled"
    readiness_check = next(
        item
        for item in healthcheck["data"]["checks"]
        if item["name"] == "wheel_activation_readiness"
    )
    assert readiness_check["status"] == "ok"
