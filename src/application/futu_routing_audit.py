from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from src.application.account_config import resolve_account_broker_binding_sets
from src.application.futu_quote_routing import resolve_shared_futu_quote_route, runtime_config_market
from src.application.trades.account_mapping import resolve_trade_intake_sources


def _mask_account_id(value: str) -> str:
    raw = str(value or "")
    if len(raw) <= 4:
        return "*" * len(raw)
    return f"{'*' * (len(raw) - 4)}{raw[-4:]}"


def build_futu_routing_audit(
    configs: list[tuple[str | None, str | Path, Mapping[str, Any]]],
) -> dict[str, Any]:
    route_inputs = [(config_key, config) for config_key, _path, config in configs]
    quote = resolve_shared_futu_quote_route(route_inputs)
    broker_bindings = resolve_account_broker_binding_sets(route_inputs)
    errors: list[dict[str, str]] = []
    warnings: list[str] = []
    if not quote.ok:
        for message in quote.errors:
            errors.append({"code": f"quote_route_{quote.status}", "scope": "quote", "message": message})

    broker_rows: list[dict[str, Any]] = []
    for account in sorted(broker_bindings):
        binding = broker_bindings[account]
        warnings.extend(binding.compatibility_warnings)
        for message in binding.errors:
            errors.append({"code": "broker_binding_conflict", "scope": f"broker:{account}", "message": message})
        broker_rows.append(
            {
                "account": account,
                "status": binding.status,
                "host": binding.host,
                "port": binding.port,
                "trd_env": binding.trd_env,
                "required_account_id_count": len(binding.required_account_ids),
                "masked_required_account_ids": [_mask_account_id(value) for value in binding.required_account_ids],
                "members": [
                    {
                        "config_key": member.config_key,
                        "market": member.market,
                        "authority_source": member.authority_source,
                        "account_id_count": len(member.account_ids),
                        "masked_account_ids": [_mask_account_id(value) for value in member.account_ids],
                    }
                    for member in binding.members
                ],
            }
        )

    source_rows: list[dict[str, Any]] = []
    for config_key, _path, config in configs:
        source_market = runtime_config_market(config)
        try:
            sources = resolve_trade_intake_sources(dict(config))
        except Exception as exc:
            errors.append(
                {"code": "trade_intake_source_invalid", "scope": f"trade_intake:{config_key or 'runtime'}", "message": str(exc)}
            )
            continue
        for source in sources:
            if not bool(source.get("enabled", True)):
                continue
            source_id = str(source.get("id") or "legacy")
            account = str(source.get("account") or "").strip().lower() or None
            source_ids = {str(value).strip() for value in list(source.get("futu_account_ids") or []) if str(value).strip()}
            applicable = [broker_bindings[account]] if account in broker_bindings else list(broker_bindings.values())
            source_errors: list[str] = []
            expected_source_ids: set[str] = set()
            if not applicable:
                source_errors.append("no applicable broker binding")
            for binding in applicable:
                member_ids = {
                    value
                    for member in binding.members
                    if member.market == source_market
                    for value in member.account_ids
                }
                expected_source_ids.update(member_ids or binding.required_account_ids)
                if not binding.ok:
                    source_errors.append(f"broker binding for {binding.account} is not ready")
                    continue
                if (str(source.get("host") or "").strip().lower(), int(source.get("port") or 0)) != (
                    binding.host,
                    binding.port,
                ):
                    source_errors.append(f"endpoint differs from broker binding for {binding.account}")
                if binding.trd_env != "REAL":
                    source_errors.append(f"direct trade intake requires REAL for {binding.account}")
            if source_ids != expected_source_ids:
                source_errors.append("account id set differs from broker binding member")
            status = "ok" if not source_errors else "conflict"
            for message in source_errors:
                errors.append(
                    {"code": "trade_intake_source_conflict", "scope": f"trade_intake:{config_key or 'runtime'}:{source_id}", "message": message}
                )
            source_rows.append(
                {
                    "source_id": source_id,
                    "account": account,
                    "status": status,
                    "host": str(source.get("host") or "").strip().lower() or None,
                    "port": int(source.get("port") or 0) or None,
                    "trd_env": "REAL",
                    "required_account_id_count": len(source_ids),
                }
            )

    return {
        "schema_version": "futu_routing_audit.v1",
        "ok": not errors,
        "configs": [
            {
                "config_path": f".../{Path(path).name}",
                "config_key": str(config_key or "").strip().lower() or None,
                "market": runtime_config_market(config),
            }
            for config_key, path, config in configs
        ],
        "quote": {
            "status": quote.status,
            "host": quote.host,
            "port": quote.port,
            "member_count": len(quote.members),
        },
        "broker_accounts": broker_rows,
        "trade_intake_sources": source_rows,
        "errors": errors,
        "warnings": list(dict.fromkeys(warnings)),
    }


__all__ = ["build_futu_routing_audit"]
