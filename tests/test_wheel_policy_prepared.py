from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import yaml

from src.application import prepared_option_positions_context as prepared
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.tick_run_workspace import publish_account_run_config
from src.application.wheel.policy_binding import rebind_wheel_policy
from tests.test_wheel_activation_operation import REPO_ROOT, _build, _deployment
from tests.test_wheel_workflows import _assign_short_put


def test_rebind_preserves_frozen_run_and_new_run_reads_effective_policy(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    _seed, _lot, branch_id = _assign_short_put(root, wheel_start_enabled=True)
    ledger = root / "output_shared/state/option_positions.sqlite3"
    ledger.parent.mkdir(parents=True)
    with sqlite3.connect(root / "ledger.sqlite3") as source_db, sqlite3.connect(ledger) as target_db:
        source_db.backup(target_db)
    repo = SQLiteOptionPositionsRepository(ledger)
    source = root / "config.yaml"
    doc = yaml.safe_load(source.read_text())
    doc["markets"]["us"]["features"] = {"wheel": {
        "accounts": ["lx"],
        "activation_by_account": {"lx": {
            "generation": 1, "activated_at_ms": 500, "deactivated_at_ms": None,
        }},
        "call": {"min_dte": 7, "max_dte": 90},
        "put": {"min_dte": 7, "max_dte": 90},
    }}
    source.write_text(yaml.safe_dump(doc))
    _build(root)
    config_path = root / "config.us.json"
    config = json.loads(config_path.read_text())
    data_config = root / "portfolio.runtime.json"
    data_config.write_text("{}\n")
    config["portfolio"] = {**config.get("portfolio", {}), "account": "lx",
                           "broker": "富途", "data_config": str(data_config)}
    monkeypatch.setattr(prepared, "get_exchange_rates_or_fetch_latest", lambda **kwargs: {
        "timestamp": datetime.now(timezone.utc).isoformat(), "source": "test",
        "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
    })

    def prepare(run_id):
        authority = publish_account_run_config(base=root, run_id=run_id, account="lx", config=config)
        batch = prepared.prepare_option_positions_contexts(
            base=root, run_id=run_id, config_path=config_path, account_configs={"lx": config},
            account_config_authorities={"lx": authority}, run_state_dir=root / "output_runs" / run_id / "state",
        )
        assert batch.unavailable_by_account == {}
        return batch

    before = repo.read_lifecycle_account_rows(account="lx")
    original_window = repo.list_wheel_activation_windows(market="us", account="lx")
    old = prepare("before-binding")
    old_model = old.wheel_read_models_by_account["lx"]
    assert old_model["monitoring_gate"] == "config_mismatch"
    assert [row["wheel_branch_id"] for row in old_model["wheel_branches"]] == [branch_id]
    frozen_files = {p: p.read_bytes() for p in (root / "output_runs/before-binding").rglob("*") if p.is_file()}

    args = dict(repo_root=REPO_ROOT, market="us", account="lx", request_id="bind-b",
                actor="test", config_path=config_path)
    preview = rebind_wheel_policy(**args)
    result = rebind_wheel_policy(**args, apply_changes=True, expected_preview_hash=preview["preview_hash"])
    assert result["ready"] is True

    reused = prepare("before-binding")
    assert reused.ledger_read_count == 0
    assert reused.wheel_read_models_by_account["lx"] == old_model
    assert all(path.read_bytes() == content for path, content in frozen_files.items())
    fresh = prepare("after-binding")
    fresh_model = fresh.wheel_read_models_by_account["lx"]
    assert fresh_model["monitoring_gate"] == "enabled"
    assert [row["wheel_branch_id"] for row in fresh_model["wheel_branches"]] == [branch_id]
    assert repo.read_lifecycle_account_rows(account="lx") == before
    assert repo.list_wheel_activation_windows(market="us", account="lx") == original_window
