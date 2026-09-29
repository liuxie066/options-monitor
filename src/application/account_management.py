from __future__ import annotations

from pathlib import Path
from typing import Any

from src.application.agent_tool_config import repo_base as agent_repo_base
from src.application.config_yaml_accounts import mutate_yaml_account_config


def add_account(
    *,
    market: str,
    account_label: str,
    account_type: str,
    config_path: str | Path | None = None,
    futu_acc_id: str | None = None,
    market_label: str | None = None,
    enabled: bool | None = None,
    trade_intake_enabled: bool | None = None,
    futu_host: str | None = None,
    futu_port: int | None = None,
    rebuild_runtime_root: str | Path | None = None,
    apply: bool = False,
    dry_run: bool | None = None,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    write_apply = bool(apply) if dry_run is None else not bool(dry_run)
    return mutate_yaml_account_config(
        repo_root=agent_repo_base(),
        action="add",
        market=str(market),
        account_label=str(account_label),
        account_type=str(account_type),
        config_path=config_path,
        futu_acc_id=futu_acc_id,
        market_label=market_label,
        enabled=enabled,
        trade_intake_enabled=trade_intake_enabled,
        futu_host=futu_host,
        futu_port=futu_port,
        rebuild_runtime_root=rebuild_runtime_root,
        apply=write_apply,
        expected_source_sha256=expected_source_sha256,
    )


def edit_account(
    *,
    market: str,
    account_label: str,
    config_path: str | Path | None = None,
    account_type: str | None = None,
    futu_acc_id: str | None = None,
    market_label: str | None = None,
    enabled: bool | None = None,
    trade_intake_enabled: bool | None = None,
    futu_host: str | None = None,
    futu_port: int | None = None,
    rebuild_runtime_root: str | Path | None = None,
    apply: bool = False,
    dry_run: bool | None = None,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    write_apply = bool(apply) if dry_run is None else not bool(dry_run)
    return mutate_yaml_account_config(
        repo_root=agent_repo_base(),
        action="edit",
        market=str(market),
        account_label=str(account_label),
        config_path=config_path,
        account_type=account_type,
        futu_acc_id=futu_acc_id,
        market_label=market_label,
        enabled=enabled,
        trade_intake_enabled=trade_intake_enabled,
        futu_host=futu_host,
        futu_port=futu_port,
        rebuild_runtime_root=rebuild_runtime_root,
        apply=write_apply,
        expected_source_sha256=expected_source_sha256,
    )


def remove_account(
    *,
    market: str,
    account_label: str,
    config_path: str | Path | None = None,
    rebuild_runtime_root: str | Path | None = None,
    apply: bool = False,
    dry_run: bool | None = None,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    write_apply = bool(apply) if dry_run is None else not bool(dry_run)
    return mutate_yaml_account_config(
        repo_root=agent_repo_base(),
        action="remove",
        market=str(market),
        account_label=str(account_label),
        config_path=config_path,
        rebuild_runtime_root=rebuild_runtime_root,
        apply=write_apply,
        expected_source_sha256=expected_source_sha256,
    )
