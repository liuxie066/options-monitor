from __future__ import annotations

from pathlib import Path
from typing import Any

from src.application.agent_tool_config import repo_base as agent_repo_base
from src.application.agent_tool_init_local import init_local_config


def init_runtime(
    *,
    market: str,
    futu_acc_id: str,
    account_label: str = "user1",
    config_path: str | Path | None = None,
    data_config_path: str | Path | None = None,
    symbols: list[str] | None = None,
    holdings_account: str | None = None,
    opend_host: str = "127.0.0.1",
    opend_port: int = 11111,
    force: bool = False,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "repo_root": agent_repo_base(),
        "market": str(market),
        "futu_acc_id": str(futu_acc_id),
        "account_label": str(account_label or "user1"),
        "config_path": config_path,
        "symbols": list(symbols or []),
        "holdings_account": holdings_account,
        "opend_host": str(opend_host),
        "opend_port": int(opend_port),
        "force": bool(force),
    }
    if data_config_path is not None:
        kwargs["data_config_path"] = data_config_path
    return init_local_config(**kwargs)
