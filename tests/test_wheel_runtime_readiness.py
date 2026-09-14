from __future__ import annotations

import json
import sqlite3

import pytest
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
              policy_hash TEXT NOT NULL,
              activation_request_id TEXT NOT NULL,
              activation_request_hash TEXT NOT NULL,
              deactivation_request_id TEXT,
              deactivation_request_hash TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO wheel_activation_windows (
              market, account, generation, activated_at_ms,
              deactivated_at_ms, policy_hash,
              activation_request_id, activation_request_hash,
              deactivation_request_id, deactivation_request_hash
            ) VALUES (?, ?, ?, ?, ?, ?, 'activate-test', ?, ?, ?)
            """,
            (
                market,
                account,
                generation,
                activated_at_ms,
                deactivated_at_ms,
                policy_hash,
                "a" * 64,
                "deactivate-test" if deactivated_at_ms is not None else None,
                "b" * 64 if deactivated_at_ms is not None else None,
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
    assert closed_readiness["accounts"]["lx"]["policy_drift"] is False

    drift_path = tmp_path / "closed-drift.sqlite3"
    _write_activation_table(
        drift_path,
        market="us",
        account="lx",
        generation=1,
        activated_at_ms=1_000,
        deactivated_at_ms=2_000,
        policy_hash="f" * 64,
    )
    closed_drift = build_wheel_activation_readiness(
        config=closed_config,
        market="us",
        accounts=["lx"],
        sqlite_path=drift_path,
    )
    assert closed_drift["monitoring_gate"] == "disabled"
    assert closed_drift["reason_code"] == "closed_window"
    assert closed_drift["accounts"]["lx"]["policy_drift"] is True


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
    assert missing["reason_code"] == "missing_database"
    assert missing["storage_status"] == "missing_database"
    assert missing_path.exists() is False

    no_table_path = tmp_path / "no-table.sqlite3"
    with sqlite3.connect(no_table_path) as conn:
        conn.execute("CREATE TABLE unrelated (value INTEGER)")
    no_table = build_wheel_activation_readiness(
        config=config,
        market="us",
        accounts=["lx"],
        sqlite_path=no_table_path,
    )
    assert no_table["monitoring_gate"] == "disabled"
    assert no_table["reason_code"] == "missing_table"
    assert no_table["storage_status"] == "missing_table"

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


@pytest.fixture
def activation_runtime(tmp_path: Path, monkeypatch, request):
    state = getattr(request, "param", "enabled")
    config_path = tmp_path / "config.us.json"
    data_config_path = tmp_path / "portfolio.runtime.json"
    data_config_path.write_text(
        json.dumps(
            {"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}
        ),
        encoding="utf-8",
    )
    config = _public_cfg_with_futu("portfolio.runtime.json")
    config["wheel"] = _wheel_config(account="user1", deactivated_at_ms=2000 if state == "disabled" else None)["wheel"]
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
            ) VALUES ('us', 'user1', 1, 1000, ?, ?, 'activation-test', ?, ?, ?)
            """,
            (2000 if state == "disabled" else None, "b" * 64 if state == "mismatch" else policy_hash, "a" * 64,
             "deactivation-test" if state == "disabled" else None, "c" * 64 if state == "disabled" else None),
        )

    # Finish fixture WAL writes before byte-level read-only assertions. A sqlite
    # connection context commits but does not close; delayed GC can checkpoint it.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    _patch_healthcheck_dependencies(monkeypatch)
    monkeypatch.setattr("src.application.agent_tools.diagnostics.repo_base", lambda: tmp_path)
    if state in {"missing_database", "missing_table", "unreadable"}:
        sqlite_path.unlink()
        if state == "missing_table":
            with sqlite3.connect(sqlite_path) as conn:
                conn.execute("CREATE TABLE unrelated (id INTEGER)")
        elif state == "unreadable":
            sqlite_path.write_bytes(b"invalid sqlite database")
    return config_path, sqlite_path, policy_hash, state


def test_bot_answers_wheel_activation_through_gateway_and_admission(activation_runtime, monkeypatch):
    from tests.test_bot_python_runtime import MODEL, answer, call, run_contract, script
    from src.application.bot.contracts import BotRequest, BotScope
    from src.application.bot.service import prepare_contract
    config_path, sqlite_path, _policy_hash, _state = activation_runtime
    before = sqlite_path.read_bytes()
    contract = prepare_contract(BotRequest(request_id="wheel", source_entry="test", user_message="Wheel 有没有正常激活？",
        explicit_scope=BotScope(config_path=str(config_path))))
    seen = []
    result = run_contract(contract, model_settings=MODEL, model_request=script([
        call("runtime_status", {"accounts":["user1"], "view":"wheel_activation"}),
        answer("Wheel 已正常激活，激活不代表已经扫描或成交。")], seen))
    observation = json.loads(seen[-1]["messages"][-1]["content"])
    assert observation["ok"], observation
    assert observation["data"]["wheel_activation_readiness"]["monitoring_gate"] == "enabled"
    assert result.ok and sqlite_path.read_bytes() == before


def test_wheel_activation_view_respects_requested_accounts(activation_runtime) -> None:
    config_path, _sqlite_path, _policy_hash, _state = activation_runtime
    response = execute_tool("runtime_status", {
        "config_path": str(config_path), "accounts": ["other"], "view": "wheel_activation",
    })
    assert response["ok"] is True
    assert response["data"]["scope"]["accounts"] == ["other"]
    readiness = response["data"]["wheel_activation_readiness"]
    assert readiness["accounts"] == {}
    assert readiness["monitoring_gate"] == "disabled"
    assert readiness["reason_code"] == "not_configured"
