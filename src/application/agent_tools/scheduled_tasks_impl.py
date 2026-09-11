"""Safe adapter for the deployment-owned scheduled-task inventory."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from src.application.account_config import accounts_from_config
from src.application.agent_tool_config import load_runtime_config
from src.application.runtime_config_freshness import infer_runtime_config_market
from src.application.service_deploy import (
    load_service_profile,
    scheduled_tasks_from_profile,
)


_QUERY_CONTEXT: ContextVar[tuple[float | None, Callable[[], bool] | None]] = ContextVar(
    "scheduled_tasks_query_context", default=(None, None)
)


@contextmanager
def scheduled_tasks_query_context(
    *,
    deadline_monotonic: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[None]:
    token = _QUERY_CONTEXT.set((deadline_monotonic, cancelled))
    try:
        yield
    finally:
        _QUERY_CONTEXT.reset(token)


def _unavailable(*, market: str, observed_at: str, reason: str) -> dict[str, Any]:
    scope = {
        "market": market,
        "granularity": "market_deployment",
        "inventory_source": "service_profile",
        "coverage": "unavailable",
        "reasons": [reason],
    }
    return {
        "schema_version": "scheduled_tasks.output.v1",
        "scope": scope,
        "tasks": [],
        "count": 0,
        "observed_at": observed_at,
        "coverage": _coverage(status="unavailable", count=0, scope=scope),
        "freshness": {"status": "current", "as_of": observed_at},
        "availability": "unavailable",
        "reasons": [reason],
    }


def run_scheduled_tasks_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(
        config_key=payload.get("config_key"), config_path=payload.get("config_path")
    )
    market = infer_runtime_config_market(
        config=cfg,
        config_key=payload.get("config_key"),
        config_path=config_path,
    )
    if market not in {"us", "hk"}:
        market = "unknown"
    observed_at = datetime.now(timezone.utc).isoformat()
    runtime_root = config_path.resolve().parent
    try:
        profile = load_service_profile(runtime_root / "service.profile.json")
    except FileNotFoundError:
        value = _unavailable(market=market, observed_at=observed_at, reason="profile_missing")
        return value, ["profile_missing"], {"read_only": True}
    except Exception:
        value = _unavailable(market=market, observed_at=observed_at, reason="profile_unreadable")
        return value, ["profile_unreadable"], {"read_only": True}

    reason = _profile_binding_reason(
        profile,
        runtime_root=runtime_root,
        config_path=config_path.resolve(),
        market=market,
    )
    if reason:
        value = _unavailable(market=market, observed_at=observed_at, reason=reason)
        return value, [reason], {"read_only": True}

    try:
        authorized_accounts = accounts_from_config(cfg, fallback=())
    except ValueError:
        value = _unavailable(
            market=market,
            observed_at=observed_at,
            reason="config_accounts_invalid",
        )
        return value, ["config_accounts_invalid"], {"read_only": True}

    deadline_monotonic, cancelled = _QUERY_CONTEXT.get()
    inventory = scheduled_tasks_from_profile(
        profile,
        market=market,
        authorized_accounts=authorized_accounts,
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
    )
    reasons = list(inventory["reasons"])
    scope = {
        "market": market,
        "granularity": "market_deployment",
        "inventory_source": "service_profile",
        "coverage": inventory["coverage"],
        "reasons": reasons,
    }
    value = {
        "schema_version": "scheduled_tasks.output.v1",
        "scope": scope,
        "tasks": inventory["tasks"],
        "count": len(inventory["tasks"]),
        "observed_at": observed_at,
        "coverage": _coverage(
            status=inventory["coverage"], count=len(inventory["tasks"]), scope=scope
        ),
        "freshness": {"status": "current", "as_of": observed_at},
        "availability": inventory["availability"],
        "reasons": reasons,
    }
    return value, reasons, {"read_only": True}


def _coverage(
    *, status: str, count: int, scope: dict[str, Any]
) -> dict[str, Any]:
    complete = status == "complete"
    return {
        "status": status if status in {"complete", "partial"} else "unknown",
        "complete_for": "full_query",
        "included_count": count,
        "total_count": count if complete else None,
        "omitted_count": 0 if complete else None,
        "has_more": False,
        "scope": {
            "market": scope["market"],
            "granularity": scope["granularity"],
        },
    }


def _profile_binding_reason(
    profile: dict[str, Any],
    *,
    runtime_root: Path,
    config_path: Path,
    market: str,
) -> str | None:
    try:
        if Path(str(profile.get("runtime_root") or "")).expanduser().resolve() != runtime_root:
            return "profile_runtime_mismatch"
    except Exception:
        return "profile_runtime_mismatch"
    markets = profile.get("markets")
    if not isinstance(markets, list) or market not in {
        str(value).strip().lower() for value in markets
    }:
        return "profile_market_unbound"
    config_paths = profile.get("config_paths")
    if not isinstance(config_paths, dict) or not config_paths.get(market):
        return "profile_config_unbound"
    try:
        if Path(str(config_paths[market])).expanduser().resolve() != config_path:
            return "profile_config_mismatch"
    except Exception:
        return "profile_config_mismatch"
    return None


__all__ = ["run_scheduled_tasks_tool", "scheduled_tasks_query_context"]
