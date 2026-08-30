from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

from src.application.account_config import normalize_account_label


_TOP1_PROFILE_FIELDS = {
    "enabled",
    "market",
    "account",
    "opend_binding",
    "advance_interval",
    "timeout_start_sec",
}


class StrategyLabContextError(ValueError):
    reason_code = "strategy_lab_context_invalid"


def _fail(message: str) -> NoReturn:
    raise StrategyLabContextError(message)


def _absolute_path(profile: Mapping[str, Any], key: str) -> Path:
    raw = str(profile.get(key) or "").strip()
    path = Path(raw).expanduser()
    if not raw or not path.is_absolute():
        _fail(f"profile {key} must be an absolute path")
    return path


def resolve_strategy_lab_runtime_context(
    profile: Mapping[str, Any],
    *,
    market: str,
) -> dict[str, Any]:
    """Resolve shared Strategy Lab paths without depending on the retired Top1 shell."""

    if not isinstance(profile, Mapping):
        _fail("service profile must be an object")
    market_key = str(market or "").strip().lower()
    markets = profile.get("markets")
    if market_key not in {"hk", "us"} or not isinstance(markets, list) or market_key not in markets:
        _fail("Strategy Lab market is absent from the service profile")
    repo_root = _absolute_path(profile, "repo_root")
    runtime_root = _absolute_path(profile, "runtime_root")
    config_paths = profile.get("config_paths")
    raw_config_path = config_paths.get(market_key) if isinstance(config_paths, Mapping) else None
    config_path = Path(str(raw_config_path or "")).expanduser()
    if not str(raw_config_path or "").strip() or not config_path.is_absolute():
        _fail(f"profile {market_key.upper()} runtime config path is invalid")
    artifact_root = runtime_root / "output_shared" / "research" / "strategy_lab"
    return {
        "profile": dict(profile),
        "repo_root": repo_root,
        "runtime_root": runtime_root,
        "config_path": config_path,
        "market": market_key,
        "artifact_root": artifact_root,
        "opend_limiter_root": runtime_root,
        "tick_lock_path": runtime_root / "locks" / f"tick-{market_key}.lock",
    }


def resolve_strategy_lab_context(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve the existing controlled Strategy Lab runtime binding."""

    runtime = resolve_strategy_lab_runtime_context(profile, market="hk")
    repo_root = runtime["repo_root"]
    runtime_root = runtime["runtime_root"]
    _absolute_path(profile, "env_file")
    raw = profile.get("strategy_lab_top1")
    top1 = dict(raw) if isinstance(raw, Mapping) else {}
    binding_raw = top1.get("opend_binding")
    binding = dict(binding_raw) if isinstance(binding_raw, Mapping) else {}
    try:
        account = normalize_account_label(top1.get("account"))
    except ValueError as exc:
        raise StrategyLabContextError(str(exc)) from exc
    accounts = profile.get("accounts")
    markets = profile.get("markets")
    valid = (
        profile.get("service_provider") == "systemd"
        and isinstance(accounts, list)
        and account in accounts
        and isinstance(markets, list)
        and "hk" in markets
        and set(top1) == _TOP1_PROFILE_FIELDS
        and top1.get("enabled") is True
        and top1.get("market") == "hk"
        and top1.get("account") == account
        and isinstance(binding.get("host"), str)
        and bool(binding["host"].strip())
        and type(binding.get("port")) is int
        and 0 < binding["port"] <= 65535
        and type(top1.get("advance_interval")) is int
        and top1["advance_interval"] > 0
        and type(top1.get("timeout_start_sec")) is int
        and top1["timeout_start_sec"] > 0
    )
    if not valid:
        _fail("Strategy Lab systemd profile binding is missing or invalid")
    config_hk = runtime["config_path"]
    artifact_root = runtime["artifact_root"]
    return {
        "profile": dict(profile),
        "repo_root": repo_root,
        "runtime_root": runtime_root,
        "config_hk": config_hk,
        "market": "hk",
        "account": account,
        "opend_binding": binding,
        "store_path": artifact_root / "experiments.sqlite3",
        "artifact_root": artifact_root,
        "opend_limiter_root": runtime_root,
        "tick_lock_path": runtime_root / "locks" / "tick-hk.lock",
    }


__all__ = [
    "StrategyLabContextError",
    "resolve_strategy_lab_context",
    "resolve_strategy_lab_runtime_context",
]
