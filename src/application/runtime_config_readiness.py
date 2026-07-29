from __future__ import annotations

from pathlib import Path
from typing import Any

from domain.domain.config_contract import ensure_runtime_schedule_matches_market
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_validator import validate_config
from src.application.runtime_config_freshness import (
    check_runtime_config_freshness,
    check_runtime_config_identity,
    infer_runtime_config_market,
)


def evaluate_runtime_config_readiness(
    config: dict[str, Any],
    *,
    repo_root: str | Path,
    runtime_config_path: str | Path,
    explicit_market: str | None = None,
    config_key: str | None = None,
) -> dict[str, Any]:
    path = Path(runtime_config_path).expanduser().resolve()
    root = Path(repo_root).expanduser().resolve()
    market = infer_runtime_config_market(
        explicit_market=explicit_market,
        config_key=config_key,
        config_path=path,
        config=config,
    )
    validation = _validation_readiness(config)
    identity = check_runtime_config_identity(
        config,
        explicit_market=explicit_market,
        config_key=config_key,
        runtime_config_path=path,
    )
    schedule = _schedule_readiness(config, path=path, market=market)
    freshness = (
        check_runtime_config_freshness(
            config,
            repo_root=root,
            market=market,
            runtime_config_path=path,
        )
        if market
        else {
            "ok": False,
            "market": None,
            "runtime_config_path": str(path),
            "errors": [{"code": "market_not_inferred", "message": "runtime config market could not be inferred"}],
        }
    )
    components = {
        "validation": validation,
        "identity": identity,
        "freshness": freshness,
        "schedule": schedule,
    }
    errors: list[dict[str, Any]] = []
    for name, result in components.items():
        if bool(result.get("ok")):
            continue
        component_errors = result.get("errors")
        if isinstance(component_errors, list) and component_errors:
            for item in component_errors:
                errors.append({"component": name, **(item if isinstance(item, dict) else {"message": str(item)})})
        else:
            errors.append({"component": name, "message": str(result.get("error") or f"{name} readiness failed")})
    return {
        "ok": not errors,
        "config_path": str(path),
        "repo_root": str(root),
        "market": market,
        **components,
        "errors": errors,
    }


def require_runtime_config_readiness(
    config: dict[str, Any],
    *,
    repo_root: str | Path,
    runtime_config_path: str | Path,
    explicit_market: str | None = None,
    config_key: str | None = None,
) -> dict[str, Any]:
    result = evaluate_runtime_config_readiness(
        config,
        repo_root=repo_root,
        runtime_config_path=runtime_config_path,
        explicit_market=explicit_market,
        config_key=config_key,
    )
    if result["ok"]:
        return result
    first = result["errors"][0] if result["errors"] else {}
    component = str(first.get("component") or "runtime")
    message = str(first.get("message") or "runtime config is not ready")
    raise AgentToolError(
        code="CONFIG_ERROR",
        message=f"runtime config {component} readiness failed: {message}",
        hint="Rebuild from config.yaml with `./om config build --source yaml --market <market>` and retry.",
        details=result,
    )


def _validation_readiness(config: dict[str, Any]) -> dict[str, Any]:
    try:
        validate_config(dict(config))
    except (AgentToolError, SystemExit) as exc:
        return {
            "ok": False,
            "errors": [{"code": "validation_failed", "message": str(getattr(exc, "message", exc))}],
        }
    except Exception as exc:
        return {
            "ok": False,
            "errors": [{"code": "validation_failed", "message": f"{type(exc).__name__}: {exc}"}],
        }
    return {"ok": True, "errors": []}


def _schedule_readiness(config: dict[str, Any], *, path: Path, market: str | None) -> dict[str, Any]:
    if not market:
        return {
            "ok": False,
            "validated": False,
            "errors": [{"code": "market_not_inferred", "message": "cannot validate schedule without a market"}],
        }
    try:
        contract = ensure_runtime_schedule_matches_market(
            config,
            config_path=path,
            market_config=market,
        )
    except SystemExit as exc:
        return {
            "ok": False,
            "validated": False,
            "market": market,
            "errors": [{"code": "schedule_mismatch", "message": str(exc)}],
        }
    return {"ok": bool(contract.get("validated")), **contract, "errors": []}


__all__ = [
    "evaluate_runtime_config_readiness",
    "require_runtime_config_readiness",
]
