from __future__ import annotations

from typing import Any, Callable


def run_healthcheck_tool(
    payload: dict[str, Any],
    *,
    load_runtime_config: Callable[..., tuple[Any, dict[str, Any]]],
    validate_runtime_config: Callable[..., Any],
    normalize_accounts: Callable[..., list[str]],
    accounts_from_config: Callable[..., list[str]],
    resolve_data_config_ref: Callable[[dict[str, Any], dict[str, Any]], str | None],
    resolve_public_data_config_path: Callable[[dict[str, Any], dict[str, Any]], Any],
    read_json_object_or_empty: Callable[[Any], dict[str, Any]],
    mask_path: Callable[[Any], str],
    list_account_config_views: Callable[[dict[str, Any]], list[Any]],
    mask_account_id: Callable[[Any], str],
    infer_futu_portfolio_settings: Callable[[dict[str, Any]], dict[str, Any]],
    run_futu_doctor: Callable[..., dict[str, Any]],
    healthcheck_symbols_for_futu: Callable[[dict[str, Any]], list[str]],
    write_tools_enabled: Callable[[], bool],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(
        config_key=payload.get("config_key"),
        config_path=payload.get("config_path"),
    )
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []
    validate_runtime_config(cfg, allow_empty_symbols=True)
    checks.append({"name": "runtime_config", "status": "ok", "message": "config validation passed"})

    accounts = normalize_accounts(payload.get("accounts"), fallback=tuple(accounts_from_config(cfg)))
    checks.append(
        {
            "name": "accounts",
            "status": "ok",
            "message": f"resolved {len(accounts)} account(s)",
            "value": accounts,
        }
    )

    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    data_config_ref = resolve_data_config_ref(payload, portfolio_cfg)
    data_config_path = resolve_public_data_config_path(payload, portfolio_cfg)
    if data_config_path.exists():
        checks.append(
            {
                "name": "data_config",
                "status": "ok",
                "message": ("portfolio.data_config found" if data_config_ref else "repo-local SQLite data config found"),
                "value": mask_path(data_config_path),
            }
        )
    else:
        checks.append(
            {
                "name": "data_config",
                "status": "error",
                "message": ("portfolio.data_config missing" if data_config_ref else "portfolio.data_config not configured"),
            }
        )
        warnings.append(
            "Minimal public setup requires a repo-local SQLite data config at secrets/portfolio.sqlite.json."
        )

    data_cfg = read_json_object_or_empty(data_config_path) if data_config_path.exists() else {}
    feishu_cfg = data_cfg.get("feishu") if isinstance(data_cfg.get("feishu"), dict) else {}
    feishu_tables = feishu_cfg.get("tables") if isinstance(feishu_cfg.get("tables"), dict) else {}
    feishu_ready = bool(str(feishu_cfg.get("app_id") or "").strip()) and bool(str(feishu_cfg.get("app_secret") or "").strip())
    holdings_ref = str(feishu_tables.get("holdings") or "").strip()
    holdings_ready = feishu_ready and ("/" in holdings_ref)

    mapping_errors: list[str] = []
    fallback_warnings: list[str] = []
    mapping_preview: dict[str, dict[str, Any]] = {}
    primary_errors: list[str] = []
    primary_preview: dict[str, dict[str, Any]] = {}
    fallback_preview: dict[str, dict[str, Any]] = {}
    account_views = {item.account: item for item in list_account_config_views(cfg)}
    for account in accounts:
        account_view = account_views[account]
        source_plan = account_view.portfolio_source_plan
        account_type = account_view.account_type
        mapped_ids = account_view.futu_acc_ids
        primary_preview[account] = {
            "type": account_type,
            "source": source_plan.primary_source,
            "ready": False,
        }
        mapping_preview[account] = {
            "type": account_type,
            "futu_account_ids": [mask_account_id(x) for x in mapped_ids],
        }
        fallback_enabled = bool(source_plan.fallback_source) or account_type == "external_holdings"
        fallback_preview[account] = {
            "enabled": fallback_enabled,
            "source": (source_plan.fallback_source or ("holdings" if fallback_enabled else None)),
        }
        if fallback_enabled:
            mapping_preview[account]["holdings_account"] = source_plan.holdings_account
            mapping_preview[account]["holdings_fallback_ready"] = bool(holdings_ready)
            fallback_preview[account]["holdings_account"] = source_plan.holdings_account
            fallback_preview[account]["ready"] = bool(holdings_ready)
        if account_type == "futu":
            if not mapped_ids:
                mapping_errors.append(f"{account}: missing trade_intake.account_mapping.futu entry")
                primary_errors.append(f"{account}: missing trade_intake.account_mapping.futu entry")
                continue
            for acc_id in mapped_ids:
                if str(acc_id).startswith("REAL_"):
                    mapping_errors.append(f"{account}: placeholder futu acc_id {acc_id}")
                    primary_errors.append(f"{account}: placeholder futu acc_id {acc_id}")
                elif not str(acc_id).isdigit():
                    mapping_errors.append(f"{account}: futu acc_id must be digits only")
                    primary_errors.append(f"{account}: futu acc_id must be digits only")
            primary_preview[account]["futu_account_ids"] = [mask_account_id(x) for x in mapped_ids]
            primary_preview[account]["ready"] = not any(msg.startswith(f"{account}:") for msg in primary_errors)
            if fallback_enabled and not holdings_ready:
                fallback_warnings.append(
                    f"{account}: holdings fallback configured but feishu.app_id/app_secret/tables.holdings is incomplete in portfolio.data_config"
                )
            continue

        if not feishu_ready:
            mapping_errors.append(f"{account}: external_holdings requires feishu.app_id/app_secret in portfolio.data_config")
            primary_errors.append(f"{account}: external_holdings requires feishu.app_id/app_secret in portfolio.data_config")
        if "/" not in holdings_ref:
            mapping_errors.append(f"{account}: external_holdings requires feishu.tables.holdings in portfolio.data_config")
            primary_errors.append(f"{account}: external_holdings requires feishu.tables.holdings in portfolio.data_config")
        primary_preview[account]["holdings_account"] = source_plan.holdings_account
        primary_preview[account]["ready"] = bool(holdings_ready)

    checks.append(
        {
            "name": "account_primary_paths",
            "status": ("error" if primary_errors else "ok"),
            "message": ("; ".join(primary_errors) if primary_errors else f"resolved primary account paths for {len(accounts)} account(s)"),
            "value": primary_preview,
        }
    )
    checks.append(
        {
            "name": "account_fallback_paths",
            "status": ("warn" if fallback_warnings else "ok"),
            "message": ("; ".join(fallback_warnings) if fallback_warnings else "resolved fallback account paths"),
            "value": fallback_preview,
        }
    )
    checks.append(
        {
            "name": "account_mapping",
            "status": ("error" if mapping_errors else "ok"),
            "message": ("; ".join(mapping_errors) if mapping_errors else f"resolved account setup for {len(accounts)} account(s)"),
            "value": mapping_preview,
        }
    )
    if mapping_errors:
        warnings.append("Use `./om-agent add-account --account-type futu|external_holdings` and complete the matching mapping/config fields.")
    warnings.extend(fallback_warnings)

    futu_settings = infer_futu_portfolio_settings(cfg)
    futu_host = str(futu_settings.get("host") or "").strip()
    try:
        futu_port = int(futu_settings.get("port") or 0)
    except Exception:
        futu_port = 0
    if futu_host and futu_port > 0:
        checks.append(
            {
                "name": "opend_endpoint",
                "status": "ok",
                "message": "resolved OpenD endpoint from runtime config",
                "value": {"host": futu_host, "port": futu_port},
            }
        )
        doctor = run_futu_doctor(
            host=futu_host,
            port=futu_port,
            symbols=healthcheck_symbols_for_futu(cfg),
            timeout_sec=int(payload.get("timeout_sec") or 20),
        )
        doctor_ok = bool(doctor.get("ok"))
        if doctor_ok:
            doctor_message = "Futu/OpenD dependency check passed"
        else:
            watchdog = doctor.get("watchdog") if isinstance(doctor.get("watchdog"), dict) else {}
            doctor_message = str(
                watchdog.get("message")
                or watchdog.get("error")
                or doctor.get("message")
                or doctor.get("watchdog_raw")
                or "doctor_futu failed"
            )
            warnings.append("OpenD is a required dependency for the public install flow.")
        checks.append(
            {
                "name": "opend_doctor",
                "status": ("ok" if doctor_ok else "error"),
                "message": doctor_message,
                "value": {"host": futu_host, "port": futu_port},
            }
        )
    else:
        checks.append(
            {
                "name": "opend_endpoint",
                "status": "error",
                "message": "OpenD host/port not found in symbols[].fetch",
            }
        )
        warnings.append("Set symbols[].fetch.source=futu and keep host/port configured for the public install flow.")

    opend_ready = bool(
        futu_host and futu_port > 0 and any(item.get("name") == "opend_doctor" and item.get("status") == "ok" for item in checks)
    )
    account_paths: dict[str, dict[str, Any]] = {}
    for account in accounts:
        primary = dict(primary_preview.get(account) or {})
        fallback = dict(fallback_preview.get(account) or {})
        primary_source = str(primary.get("source") or "").strip()
        primary_ok = bool(primary.get("ready")) and opend_ready if primary_source == "futu" else bool(primary.get("ready"))

        fallback_source = str(fallback.get("source") or "").strip()
        fallback_ok = bool(fallback.get("ready")) if fallback.get("enabled") and fallback_source == "holdings" else False

        account_paths[account] = {
            "type": str(primary.get("type") or ""),
            "primary": {
                "source": (primary_source or None),
                "ok": bool(primary_ok),
                **({"futu_account_ids": primary.get("futu_account_ids")} if primary.get("futu_account_ids") is not None else {}),
                **({"holdings_account": primary.get("holdings_account")} if primary.get("holdings_account") is not None else {}),
            },
            "fallback": {
                "enabled": bool(fallback.get("enabled")),
                "source": (fallback_source or None),
                "ok": bool(fallback_ok),
                **({"holdings_account": fallback.get("holdings_account")} if fallback.get("holdings_account") is not None else {}),
            },
        }

    tools = {
        "healthcheck": {"available": True, "mode": "read"},
        "scan_opportunities": {"available": True, "mode": "read_with_local_cache"},
        "query_cash_headroom": {"available": True, "mode": "read_with_local_cache"},
        "get_portfolio_context": {"available": True, "mode": "read_with_local_cache"},
        "prepare_close_advice_inputs": {"available": True, "mode": "read_with_local_cache"},
        "close_advice": {"available": True, "mode": "read_with_local_cache"},
        "get_close_advice": {"available": True, "mode": "read_with_local_cache"},
        "manage_symbols": {"available": True, "mode": ("write" if write_tools_enabled() else "read_preview_only")},
        "preview_notification": {"available": True, "mode": "read"},
    }
    critical = [item for item in checks if item["status"] == "error"]
    return (
        {
            "config": {
                "config_path": mask_path(config_path),
                "accounts": accounts,
            },
            "account_paths": account_paths,
            "checks": checks,
            "tools": tools,
            "summary": {
                "ok": not critical,
                "critical_count": len(critical),
                "warning_count": len(warnings) + len([item for item in checks if item["status"] == "warn"]),
            },
        },
        warnings,
        {"config_path": mask_path(config_path)},
    )
