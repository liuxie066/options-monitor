"""Render the operator command that clears one Wheel readiness refusal.

The gate is intentionally fail-closed, so the useful thing to publish alongside a
refusal is not another explanation but the exact command that resolves it.
"""
from __future__ import annotations

import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Expanded by the operator's shell, never by this process: the audit actor is whoever runs
# the command, and a literal string baked in at render time would be a lie in the ledger.
_ACTOR_TOKEN = '"$USER"'


def _quote(value: Any) -> str:
    return shlex.quote(str(value))


def build_accept_policy_command(
    *,
    market: str,
    account: str | None = None,
    config_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
    apply: bool = True,
) -> str:
    """Render the `accept-policy` invocation, with or without the write flags."""

    tokens = ["./om", "wheel", "activation", "accept-policy", "--market", _quote(market)]
    if account:
        tokens.extend(["--account", _quote(account)])
    if config_path is not None:
        tokens.extend(["--config", _quote(config_path)])
    if runtime_root is not None:
        tokens.extend(["--runtime-root", _quote(runtime_root)])
    tokens.extend(["--actor", _ACTOR_TOKEN])
    if apply:
        tokens.extend(["--apply", "--confirm"])
    return " ".join(tokens)


def policy_remediation(
    readiness: Mapping[str, Any],
    *,
    market: str | None,
    account: str | None = None,
    config_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
    apply: bool = True,
) -> dict[str, str]:
    """Return `remediation_command` only when a policy rebind can actually clear the gate.

    Both halves are required. `policy_drift is True` alone also holds for a closed window,
    where nothing can be accepted until the window reopens; `reason_code ==
    "descriptor_mismatch"` alone also holds for a broken activation boundary, which no
    rebind can fix. The conjunction is exactly "open window, intact boundary, stale policy".
    """

    if readiness.get("policy_drift") is not True:
        return {}
    if readiness.get("reason_code") != "descriptor_mismatch":
        return {}
    normalized_market = str(market or "").strip().lower()
    if normalized_market not in {"us", "hk"}:
        return {}
    return {
        "remediation_command": build_accept_policy_command(
            market=normalized_market,
            account=account,
            config_path=config_path,
            runtime_root=runtime_root,
            apply=apply,
        )
    }


__all__ = ["build_accept_policy_command", "policy_remediation"]
