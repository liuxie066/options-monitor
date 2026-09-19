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


def test_effective_binding_survives_runtime_status_sanitizer(monkeypatch) -> None:
    from src.application.agent_tools.runtime_status_impl import _status_safe_wheel_activation_readiness
    from src.application.wheel import runtime_readiness

    config = _wheel_config()
    effective = build_wheel_policy_hash(config, market="us", account="lx")
    window = {"market": "us", "account": "lx", "generation": 1,
              "activated_at_ms": 1000, "deactivated_at_ms": None,
              "policy_hash": "a" * 64, "effective_policy_hash": effective,
              "policy_binding_revision": 1}
    reads = []

    def read(path, market, account):
        reads.append((market, account))
        return {"windows": [window], "source_status": "available"}

    monkeypatch.setattr(runtime_readiness, "read_wheel_activation_windows_read_only", read)
    result = _status_safe_wheel_activation_readiness(build_wheel_activation_readiness(
        config=config, market="us", accounts=["lx", "sy"], sqlite_path=None,
    ))
    assert result["ready"] is True
    assert reads == [("us", "lx")]
    assert set(result["accounts"]) == {"lx"}
    account = result["accounts"]["lx"]
    for identity in (account, account["durable_window"]):
        assert identity["policy_hash"] == "a" * 64
        assert identity["effective_policy_hash"] == effective
        assert identity["policy_binding_revision"] == 1
    assert account["descriptor"]["policy_hash"] == effective
    assert "effective_policy_hash" not in account["descriptor"]


def _readiness_us(config: dict, sqlite_path: Path | None) -> dict:
    """Readiness for account lx on the US market, for the given config and store."""
    return build_wheel_activation_readiness(
        config=config,
        market="us",
        accounts=["lx"],
        sqlite_path=sqlite_path,
    )


def _write_activation_table(
    path: Path,
    *,
    market: str = "us",
    account: str = "lx",
    generation: int = 1,
    activated_at_ms: int = 1_000,
    deactivated_at_ms: int | None = None,
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
    _write_activation_table(open_path, policy_hash=open_policy_hash)

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
        "effective_policy_hash": open_policy_hash,
        "policy_binding_revision": 0,
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
            "effective_policy_hash": open_policy_hash,
            "policy_binding_revision": 0,
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
    _write_activation_table(closed_path, deactivated_at_ms=2_000, policy_hash=open_policy_hash)
    closed_readiness = _readiness_us(closed_config, closed_path)
    assert closed_readiness["monitoring_gate"] == "disabled"
    assert closed_readiness["reason_code"] == "closed_window"
    assert closed_readiness["accounts"]["lx"]["deactivated_at_ms"] == 2_000
    assert closed_readiness["accounts"]["lx"]["policy_drift"] is False

    drift_path = tmp_path / "closed-drift.sqlite3"
    _write_activation_table(drift_path, deactivated_at_ms=2_000, policy_hash="f" * 64)
    closed_drift = _readiness_us(closed_config, drift_path)
    assert closed_drift["monitoring_gate"] == "disabled"
    assert closed_drift["reason_code"] == "closed_window"
    assert closed_drift["accounts"]["lx"]["policy_drift"] is True


def test_wheel_activation_readiness_fails_closed_for_missing_and_mismatched_state(
    tmp_path: Path,
) -> None:
    config = _wheel_config()
    missing_path = tmp_path / "missing.sqlite3"

    missing = _readiness_us(config, missing_path)

    assert missing["monitoring_gate"] == "disabled"
    assert missing["reason_code"] == "missing_database"
    assert missing["storage_status"] == "missing_database"
    assert missing_path.exists() is False

    no_table_path = tmp_path / "no-table.sqlite3"
    with sqlite3.connect(no_table_path) as conn:
        conn.execute("CREATE TABLE unrelated (value INTEGER)")
    no_table = _readiness_us(config, no_table_path)
    assert no_table["monitoring_gate"] == "disabled"
    assert no_table["reason_code"] == "missing_table"
    assert no_table["storage_status"] == "missing_table"

    policy_hash = build_wheel_policy_hash(config, market="us", account="lx")
    mismatch_path = tmp_path / "mismatch.sqlite3"
    _write_activation_table(
        mismatch_path, generation=2, activated_at_ms=2_000, policy_hash=policy_hash
    )
    mismatch = _readiness_us(config, mismatch_path)
    assert mismatch["monitoring_gate"] == "config_mismatch"
    assert mismatch["reason_code"] == "descriptor_mismatch"
    assert mismatch["accounts"]["lx"]["generation"] == 2

    no_descriptor = _readiness_us({"wheel": {"enabled": True, "accounts": ["lx"]}}, mismatch_path)
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


def test_policy_drift_and_remediation_survive_the_status_sanitizer(
    tmp_path: Path,
) -> None:
    from src.application.agent_tools.runtime_status_impl import (
        _status_safe_wheel_activation_readiness,
    )

    config = _wheel_config()
    drift_path = tmp_path / "drift.sqlite3"
    _write_activation_table(drift_path, policy_hash="f" * 64)

    readiness = _readiness_us(config, drift_path)
    assert readiness["monitoring_gate"] == "config_mismatch"
    assert readiness["accounts"]["lx"]["policy_drift"] is True
    assert "accept-policy --market us" in readiness["accounts"]["lx"]["remediation_command"]
    assert "accept-policy --market us" in readiness["remediation_command"]

    masked = _status_safe_wheel_activation_readiness(readiness)

    assert masked["remediation_command"] == readiness["remediation_command"]
    assert masked["accounts"]["lx"]["policy_drift"] is True
    assert masked["accounts"]["lx"]["remediation_command"] == (
        readiness["accounts"]["lx"]["remediation_command"]
    )
    # The masked view omits absolute paths on purpose; the operator's shell supplies them.
    assert str(tmp_path) not in masked["accounts"]["lx"]["remediation_command"]


def test_synthesized_refusals_state_policy_drift_is_false(tmp_path: Path) -> None:
    no_market = build_wheel_activation_readiness(
        config=_wheel_config(),
        market=None,
        accounts=["lx"],
        sqlite_path=tmp_path / "unused.sqlite3",
    )
    assert no_market["accounts"]["lx"]["policy_drift"] is False
    assert "remediation_command" not in no_market["accounts"]["lx"]

    available_path = tmp_path / "available.sqlite3"
    _write_activation_table(available_path, policy_hash="f" * 64)
    unparseable = _readiness_us(
        {"wheel": {"enabled": True, "accounts": ["lx"], "activation_by_account": "broken"}},
        available_path,
    )
    assert unparseable["accounts"]["lx"]["reason_code"] == "descriptor_mismatch"
    assert unparseable["accounts"]["lx"]["policy_drift"] is False
    assert "remediation_command" not in unparseable["accounts"]["lx"]

    no_descriptor = _readiness_us({"wheel": {"enabled": True, "accounts": ["lx"]}}, available_path)
    assert no_descriptor["accounts"]["lx"]["reason_code"] == "missing_descriptor"
    assert no_descriptor["accounts"]["lx"]["policy_drift"] is False
    assert "remediation_command" not in no_descriptor["accounts"]["lx"]

    missing = _readiness_us(_wheel_config(), tmp_path / "absent.sqlite3")
    assert missing["accounts"]["lx"]["policy_drift"] is False
    assert "remediation_command" not in missing["accounts"]["lx"]


@pytest.mark.parametrize("activation_runtime", ["mismatch"], indirect=True)
def test_wheel_activation_view_exposes_the_accept_policy_command(activation_runtime) -> None:
    config_path, _sqlite_path, _policy_hash, _state = activation_runtime
    response = execute_tool("runtime_status", {
        "config_path": str(config_path), "accounts": ["user1"], "view": "wheel_activation",
    })

    assert response["ok"] is True
    readiness = response["data"]["wheel_activation_readiness"]
    assert readiness["monitoring_gate"] == "config_mismatch"
    account = readiness["accounts"]["user1"]
    assert account["policy_drift"] is True
    assert "wheel activation accept-policy --market us" in readiness["remediation_command"]
    assert "--account user1" in account["remediation_command"]
    assert "--apply --confirm" in account["remediation_command"]


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
