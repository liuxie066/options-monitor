from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import AgentToolError, mask_path
from src.application.futu_portfolio_context import infer_futu_portfolio_settings
from src.application.strategy_lab.historical_data.cache import HistoricalDataCache
from src.application.strategy_lab.historical_data.contracts import HistoricalDataRequest, historical_snapshot_summary
from src.application.strategy_lab.historical_data.futu_provider import (
    FutuHistoricalFetchOptions,
    FutuHistoricalMarketDataProvider,
    normalize_historical_symbols,
)


SCHEMA_VERSION = "strategy_lab_historical_fetch.v1"


def fetch_historical_data_tool(
    payload: Mapping[str, Any],
    *,
    base: Path | None = None,
    provider_factory: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    payload_dict = dict(payload)
    repo_root = Path(base or Path.cwd()).resolve()
    provider_name = str(payload_dict.get("provider") or "futu").strip().lower()
    if provider_name != "futu":
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"unsupported historical data provider: {provider_name}",
            hint="Currently supported provider: futu.",
        )

    request = _request_from_payload(payload_dict)
    options = _futu_options_from_payload(payload_dict, base=repo_root)
    dry_run = _truthy(payload_dict.get("dry_run")) or not _truthy(payload_dict.get("confirm"))
    cache = HistoricalDataCache(base=repo_root, cache_dir=payload_dict.get("output_dir") or payload_dict.get("cache_dir"))
    output_path = cache.cache_dir / f"futu-{request.fingerprint}.json"
    warnings: list[str] = []

    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "provider": provider_name,
        "dry_run": bool(dry_run),
        "write_applied": False,
        "backup_path": None,
        "audit_id": None,
        "rollback_hint": None,
        "request": request.to_dict(),
        "fetch_options": {
            "host": options.host,
            "port": options.port,
            "max_count": options.max_count,
            "max_pages": options.max_pages,
            "rate_limit": {
                "max_wait_sec": options.max_wait_sec,
                "window_sec": options.window_sec,
                "max_calls": options.max_calls,
            },
            "retry": {
                "max_attempts": options.retry_max_attempts,
                "time_budget_sec": options.retry_time_budget_sec,
                "no_retry": options.no_retry,
            },
        },
        "output": {
            "written": False,
            "snapshot_path": cache.relative(output_path),
        },
        "snapshot": None,
    }
    if dry_run:
        warnings.append("historical_fetch_dry_run_no_opend_call")
        return data, warnings, {"base": mask_path(repo_root)}

    provider = (provider_factory or FutuHistoricalMarketDataProvider)(
        base=repo_root,
        options=options,
    )
    snapshot = provider.fetch(request)
    path = cache.write_snapshot(snapshot)
    summary = historical_snapshot_summary(snapshot)
    data.update(
        {
            "dry_run": False,
            "write_applied": True,
            "rollback_hint": "Remove the listed Strategy Lab historical snapshot if this fetched dataset is no longer needed.",
            "output": {
                "written": True,
                "snapshot_path": cache.relative(path),
            },
            "snapshot": summary,
        }
    )
    warnings.extend(snapshot.warnings)
    return data, warnings, {"base": mask_path(repo_root)}


def _request_from_payload(payload: dict[str, Any]) -> HistoricalDataRequest:
    asset_type = str(payload.get("asset_type") or "underlying").strip().lower()
    symbols = normalize_historical_symbols(payload.get("symbols") or payload.get("symbol"), asset_type=asset_type)
    if not symbols:
        raise AgentToolError(code="INPUT_ERROR", message="historical fetch requires at least one symbol")
    try:
        return HistoricalDataRequest(
            symbols=symbols,
            start_date=str(payload.get("start_date") or payload.get("start") or ""),
            end_date=str(payload.get("end_date") or payload.get("end") or ""),
            asset_type=asset_type,
            timeframe=str(payload.get("timeframe") or "1d"),
            provider="futu",
            adjusted=_truthy(payload.get("adjusted")),
            fields=tuple(str(item) for item in _list(payload.get("fields") or payload.get("field"))),
        )
    except ValueError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc


def _futu_options_from_payload(payload: dict[str, Any], *, base: Path) -> FutuHistoricalFetchOptions:
    settings = _futu_settings_from_config(payload)
    host = payload.get("host") or settings.get("host") or "127.0.0.1"
    port = payload.get("port") or settings.get("port") or 11111
    return FutuHistoricalFetchOptions(
        host=str(host),
        port=_int(port, default=11111),
        max_count=max(1, _int(payload.get("max_count"), default=1000)),
        max_pages=max(1, _int(payload.get("max_pages"), default=20)),
        max_wait_sec=max(0.0, _float(payload.get("max_wait_sec"), default=30.0)),
        window_sec=max(0.1, _float(payload.get("window_sec"), default=30.0)),
        max_calls=max(1, _int(payload.get("max_calls"), default=30)),
        retry_max_attempts=max(1, _int(payload.get("retry_max_attempts"), default=4)),
        retry_time_budget_sec=max(0.0, _float(payload.get("retry_time_budget_sec"), default=20.0)),
        retry_base_delay_sec=max(0.0, _float(payload.get("retry_base_delay_sec"), default=0.8)),
        retry_max_delay_sec=max(0.0, _float(payload.get("retry_max_delay_sec"), default=6.0)),
        no_retry=_truthy(payload.get("no_retry")),
        adjustment=_text(payload.get("adjustment")),
    )


def _futu_settings_from_config(payload: dict[str, Any]) -> dict[str, Any]:
    if not (payload.get("config_key") or payload.get("config_path")):
        return {}
    try:
        _path, cfg = load_runtime_config(
            config_key=_text(payload.get("config_key")),
            config_path=payload.get("config_path"),
        )
    except AgentToolError:
        raise
    except Exception as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=f"failed to load runtime config: {exc}") from exc
    return infer_futu_portfolio_settings(cfg, account=_text(payload.get("account"), lower=True))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out: list[Any] = []
        for item in value:
            out.extend(_list(item))
        return out
    return [part.strip() for part in str(value).replace("|", ",").split(",") if part.strip()]


def _text(value: Any, *, lower: bool = False) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text.lower() if lower else text


def _int(value: Any, *, default: int) -> int:
    try:
        if value in (None, ""):
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _float(value: Any, *, default: float) -> float:
    try:
        if value in (None, ""):
            return float(default)
        return float(value)
    except Exception:
        return float(default)
