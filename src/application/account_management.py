from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.agent_plugin.config import repo_base as agent_repo_base
from scripts.agent_plugin.init_local import (
    add_account_to_local_config,
    edit_account_in_local_config,
    remove_account_from_local_config,
)


def add_account(
    *,
    market: str,
    account_label: str,
    account_type: str,
    config_path: str | Path | None = None,
    futu_acc_id: str | None = None,
    holdings_account: str | None = None,
) -> dict[str, Any]:
    return add_account_to_local_config(
        repo_root=agent_repo_base(),
        market=str(market),
        account_label=str(account_label),
        account_type=str(account_type),
        config_path=config_path,
        futu_acc_id=futu_acc_id,
        holdings_account=holdings_account,
    )


def edit_account(
    *,
    market: str,
    account_label: str,
    config_path: str | Path | None = None,
    account_type: str | None = None,
    futu_acc_id: str | None = None,
    holdings_account: str | None = None,
    clear_holdings_account: bool = False,
) -> dict[str, Any]:
    return edit_account_in_local_config(
        repo_root=agent_repo_base(),
        market=str(market),
        account_label=str(account_label),
        config_path=config_path,
        account_type=account_type,
        futu_acc_id=futu_acc_id,
        holdings_account=holdings_account,
        clear_holdings_account=bool(clear_holdings_account),
    )


def remove_account(
    *,
    market: str,
    account_label: str,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    return remove_account_from_local_config(
        repo_root=agent_repo_base(),
        market=str(market),
        account_label=str(account_label),
        config_path=config_path,
    )

