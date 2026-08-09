from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from src.application.secret_store.registry import legacy_secret_env_names


DEFAULT_LOCAL_ENV_FILE = Path(".env/options-monitor.env")
ENV_FILE_POINTER = "OM_ENV_FILE"
DEPRECATED_ENV_SETTINGS: dict[str, str] = {
    "OM_FEISHU_ACK_REACTION": "Use assistant config inbound.feishu_ws.ack_reaction.",
    "OM_FEISHU_REPLY_MAX_CHARS": "Use assistant config inbound.feishu_ws.max_reply_chars.",
    "OM_FEISHU_WS_QUEUE_SIZE": "Use assistant config inbound.feishu_ws.queue_size.",
}

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_NAME_PARTS = ("SECRET", "TOKEN", "PASSWORD", "PRIVATE_KEY", "API_KEY")
_REGISTERED_SECRET_ENV_NAMES = legacy_secret_env_names()
_PATH_ENV_NAMES = frozenset(
    {
        ENV_FILE_POINTER,
        "OM_DATA_CONFIG",
        "OM_INBOUND_AUDIT_DB",
        "OM_OUTPUT_DIR",
        "OM_RUNTIME_ROOT",
    }
)
_PUBLIC_LITERAL_ENV_NAMES = frozenset(
    {
        *DEPRECATED_ENV_SETTINGS,
        "OM_AGENT_ENABLE_WRITE_TOOLS",
        "OM_INBOUND_CONFIRM_TTL_SECONDS",
        "OM_INBOUND_MONITOR_RUN_ENABLED",
        "OM_INBOUND_OPERATIONS_ENABLED",
        "OM_INBOUND_REQUIRE_ALLOWLIST",
        "OM_INBOUND_SYMBOL_WRITE_ENABLED",
        "OM_INBOUND_TRADE_WRITE_ENABLED",
        "OM_INBOUND_UPGRADE_WRITE_ENABLED",
    }
)
_PUBLIC_ENV_FILE = "<configured-env-file>"


@dataclass(frozen=True)
class SettingSource:
    source: str
    path: str | None = None

    def public_value(self) -> str:
        # Source paths are machine-local metadata, not part of a public setting value.
        return self.source


@dataclass(frozen=True)
class EffectiveEnv:
    values: dict[str, str]
    sources: dict[str, SettingSource]
    env_file: Path | None = None
    env_file_loaded: bool = False
    warnings: tuple[str, ...] = ()
    env_file_duplicate_keys: tuple[dict[str, Any], ...] = ()

    def get(self, name: str, default: str = "") -> str:
        return str(self.values.get(name, default) or "")

    def source_of(self, name: str) -> SettingSource | None:
        return self.sources.get(name)


def bootstrap_process_env(
    *,
    repo_root: str | Path | None = None,
    env_file: str | Path | None = None,
    include_local_env_file: bool = True,
) -> EffectiveEnv:
    """Load ordinary env-file settings into ``os.environ`` for CLI processes.

    Unit tests usually call CLI main functions with an explicit argv list, so the
    CLI entrypoints call this only for process invocations. That keeps local
    ignored settings from leaking into deterministic tests while making terminal
    usage behave like the deployed service. Secret-looking names are loaded only
    when the compatibility backend is explicitly selected with
    ``OM_SECRET_BACKEND=env``; secure backends never silently ingest them.
    """
    effective = build_effective_env(
        repo_root=repo_root,
        env_file=env_file,
        include_local_env_file=include_local_env_file,
    )
    if effective.env_file_loaded:
        env_secret_compatibility = effective.get("OM_SECRET_BACKEND").strip().lower() == "env"
        for key, source in effective.sources.items():
            if source.source == "env_file":
                if _is_secret_name(key) and not env_secret_compatibility:
                    continue
                os.environ[key] = effective.values[key]
        if effective.env_file is not None:
            os.environ[ENV_FILE_POINTER] = str(effective.env_file)
    return effective


def build_effective_env(
    *,
    repo_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
    include_local_env_file: bool = False,
) -> EffectiveEnv:
    """Build the process settings environment with explicit source tracking.

    Precedence is intentionally simple:
    1. the supplied/process environment provides the base values;
    2. an explicit env file, OM_ENV_FILE, or opted-in repo-local env file
       overlays that base.

    Runtime code should consume this effective mapping instead of deciding
    ad hoc which environment source to trust.
    """
    base_env = {str(k): str(v) for k, v in (environ if environ is not None else os.environ).items()}
    values = dict(base_env)
    sources = {key: SettingSource("process_env") for key in values}
    warnings: list[str] = []

    resolved_env_file = _resolve_env_file(
        repo_root=Path(repo_root).expanduser() if repo_root is not None else None,
        environ=base_env,
        env_file=env_file,
        include_local_env_file=include_local_env_file,
    )
    loaded = False
    duplicate_keys: tuple[dict[str, Any], ...] = ()
    if resolved_env_file is not None:
        try:
            parsed_file = _parse_env_file_with_metadata(resolved_env_file.read_text(encoding="utf-8"))
            file_values = parsed_file.values
            duplicate_keys = parsed_file.duplicate_keys
        except FileNotFoundError:
            warnings.append(f"env file not found: {resolved_env_file}")
            file_values = {}
        except OSError as exc:
            warnings.append(f"failed to read env file: {resolved_env_file}: {exc}")
            file_values = {}
        except ValueError as exc:
            warnings.append(f"failed to parse env file: {resolved_env_file}: {exc}")
            file_values = {}
        else:
            loaded = True
        for key, value in file_values.items():
            values[key] = value
            sources[key] = SettingSource("env_file", str(resolved_env_file))

    return EffectiveEnv(
        values=values,
        sources=sources,
        env_file=resolved_env_file,
        env_file_loaded=loaded,
        warnings=tuple(warnings),
        env_file_duplicate_keys=duplicate_keys,
    )


@dataclass(frozen=True)
class _ParsedEnvFile:
    values: dict[str, str]
    duplicate_keys: tuple[dict[str, Any], ...] = ()


def parse_env_file(text: str) -> dict[str, str]:
    return _parse_env_file_with_metadata(text).values


def _parse_env_file_with_metadata(text: str) -> _ParsedEnvFile:
    out: dict[str, str] = {}
    seen: dict[str, list[tuple[int, str]]] = {}
    for lineno, raw_line in enumerate(str(text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ValueError(f"invalid env line {lineno}: missing '='")
        raw_key, raw_value = line.split("=", 1)
        key = raw_key.strip()
        if not _KEY_RE.match(key):
            raise ValueError(f"invalid env line {lineno}: invalid key {key!r}")
        value = _parse_env_value(raw_value.strip())
        out[key] = value
        seen.setdefault(key, []).append((lineno, value))

    duplicates: list[dict[str, Any]] = []
    for key, entries in seen.items():
        if len(entries) <= 1:
            continue
        raw_values = [value for _lineno, value in entries]
        public_values: list[str] = []
        for value in raw_values:
            redacted = _redacted_value(key, value) or ""
            if redacted not in public_values:
                public_values.append(redacted)
        item: dict[str, Any] = {
            "name": key,
            "count": len(entries),
            "lines": [lineno for lineno, _value in entries],
            "conflict": len(set(raw_values)) > 1,
            "values": public_values,
        }
        if key in DEPRECATED_ENV_SETTINGS:
            item["migration_target"] = _deprecated_env_migration_target(key)
            item["hint"] = DEPRECATED_ENV_SETTINGS[key]
        duplicates.append(item)
    return _ParsedEnvFile(values=out, duplicate_keys=tuple(duplicates))


def _deprecated_env_migration_target(name: str) -> str | None:
    targets = {
        "OM_FEISHU_ACK_REACTION": "inbound.feishu_ws.ack_reaction",
        "OM_FEISHU_REPLY_MAX_CHARS": "inbound.feishu_ws.max_reply_chars",
        "OM_FEISHU_WS_QUEUE_SIZE": "inbound.feishu_ws.queue_size",
    }
    return targets.get(name)


def inspect_effective_settings(
    *,
    repo_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
    include_local_env_file: bool = True,
) -> dict[str, Any]:
    effective = build_effective_env(
        repo_root=repo_root,
        environ=environ,
        env_file=env_file,
        include_local_env_file=include_local_env_file,
    )
    from src.application.secret_store import inspect_secret_credentials

    secret_credentials = inspect_secret_credentials(environ=effective.values)
    keys = [
        "OM_RUNTIME_ROOT",
        "OM_DATA_CONFIG",
        "OM_OUTPUT_DIR",
        "OM_FEISHU_APP_ID",
        "OM_FEISHU_APP_SECRET",
        "OM_FEISHU_HOLDINGS_TABLE",
        "OM_FEISHU_BOT_APP_ID",
        "OM_FEISHU_BOT_APP_SECRET",
        "OM_FEISHU_BOT_USER_OPEN_ID",
        "OM_FEISHU_BOT_ALLOWED_OPEN_IDS",
        "OM_INBOUND_AUDIT_DB",
        "OM_INBOUND_REQUIRE_ALLOWLIST",
        "OM_INBOUND_OPERATIONS_ENABLED",
        "OM_INBOUND_TRADE_WRITE_ENABLED",
        "OM_INBOUND_SYMBOL_WRITE_ENABLED",
        "OM_INBOUND_UPGRADE_WRITE_ENABLED",
        "OM_INBOUND_MONITOR_RUN_ENABLED",
        "OM_INBOUND_ADMIN_OPEN_IDS",
        "OM_INBOUND_CONFIRM_TTL_SECONDS",
        "OM_AGENT_ENABLE_WRITE_TOOLS",
        "OM_LLM_API_KEY",
        "DEEPSEEK_API_KEY",
        "MOONSHOT_API_KEY",
        "KIMI_API_KEY",
    ]
    entries: dict[str, dict[str, Any]] = {}
    secret_summary = secret_credentials.get("summary") if isinstance(secret_credentials, dict) else {}
    secret_backend = secret_summary.get("backend") if isinstance(secret_summary, dict) else None
    for key in keys:
        value = effective.get(key)
        source = effective.source_of(key)
        secret_name = _is_secret_name(key)
        active_env_secret = not secret_name or secret_backend == "env"
        entries[key] = {
            "configured": bool(value) and active_env_secret,
            **({"legacy_env_configured": bool(value)} if secret_name else {}),
            "value": _redacted_value(key, value),
            "source": (
                source.public_value()
                if source and active_env_secret
                else "ignored_legacy_env"
                if source
                else None
            ),
            "secret": secret_name,
        }
    return {
        "env_file": _PUBLIC_ENV_FILE if effective.env_file is not None else None,
        "env_file_loaded": effective.env_file_loaded,
        "warnings": _public_env_warnings(effective.warnings),
        "entries": entries,
        "secret_credentials": secret_credentials,
    }


def diagnose_effective_settings(
    *,
    repo_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
    include_local_env_file: bool = True,
) -> dict[str, Any]:
    effective = build_effective_env(
        repo_root=repo_root,
        environ=environ,
        env_file=env_file,
        include_local_env_file=include_local_env_file,
    )
    from src.application.secret_store import FEISHU_BOT_APP_SECRET, inspect_secret_credentials

    secret_credentials = inspect_secret_credentials(environ=effective.values)
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, message: str, value: Any | None = None) -> None:
        item: dict[str, Any] = {"name": name, "status": status, "message": message}
        if value is not None:
            item["value"] = value
        checks.append(item)

    if effective.env_file is None:
        add(
            "env_file",
            "warn",
            "no env file is loaded; process environment is the only settings source",
            {"default_local_env_file": "<repo-local-env-file>"},
        )
    elif effective.env_file_loaded:
        add("env_file", "ok", "env file loaded", _PUBLIC_ENV_FILE)
    else:
        add("env_file", "error", "env file was selected but could not be loaded", _PUBLIC_ENV_FILE)

    secret_summary = secret_credentials.get("summary") if isinstance(secret_credentials, dict) else {}
    secret_summary = secret_summary if isinstance(secret_summary, dict) else {}
    add(
        "secret_backend",
        "ok" if secret_summary.get("ok") else "error",
        (
            f"secret backend {secret_summary.get('backend')} is available"
            if secret_summary.get("ok")
            else str(secret_summary.get("error") or "secret backend is unavailable")
        ),
        {
            "backend": secret_summary.get("backend"),
            "configured_count": secret_summary.get("configured_count", 0),
            "values_exposed": False,
            "warnings": list(secret_summary.get("warnings") or []),
        },
    )

    for warning in effective.warnings:
        status = "error" if ("not found" in warning or "failed" in warning) else "warn"
        add("env_file_warning", status, _public_env_warning(warning))

    deprecated = [
        {"name": name, "hint": hint, "migration_target": _deprecated_env_migration_target(name)}
        for name, hint in DEPRECATED_ENV_SETTINGS.items()
        if effective.get(name)
    ]
    if deprecated:
        add("deprecated_env", "warn", "deprecated env settings are configured but ignored by runtime code", deprecated)
    else:
        add("deprecated_env", "ok", "no deprecated Feishu behavior env settings configured")

    duplicate_deprecated = [
        item
        for item in effective.env_file_duplicate_keys
        if str(item.get("name") or "") in DEPRECATED_ENV_SETTINGS
    ]
    if duplicate_deprecated:
        add(
            "deprecated_env_duplicates",
            "warn",
            "deprecated env settings are defined multiple times; migrate once to runtime config and remove env duplicates",
            {
                "duplicates": duplicate_deprecated,
                "action": "set the runtime config key shown in migration_target and delete the deprecated env key; this tool does not overwrite runtime config",
            },
        )
    else:
        add("deprecated_env_duplicates", "ok", "no duplicate deprecated env settings found")

    if effective.env_file_duplicate_keys:
        add(
            "duplicate_env_keys",
            "warn",
            "env file defines one or more keys multiple times; the last value wins",
            {"duplicates": list(effective.env_file_duplicate_keys)},
        )
    else:
        add("duplicate_env_keys", "ok", "no duplicate env keys found")

    bot_app_id = effective.get("OM_FEISHU_BOT_APP_ID")
    secret_items = secret_credentials.get("credentials") if isinstance(secret_credentials, dict) else []
    bot_secret_configured = any(
        isinstance(item, dict)
        and item.get("logical_name") == FEISHU_BOT_APP_SECRET
        and bool(item.get("configured"))
        for item in (secret_items if isinstance(secret_items, list) else [])
    )
    bot_user_open_id = effective.get("OM_FEISHU_BOT_USER_OPEN_ID")
    bot_allowed = _split_csv(effective.get("OM_FEISHU_BOT_ALLOWED_OPEN_IDS")) or (
        [bot_user_open_id] if bot_user_open_id else []
    )
    missing_bot_creds = [
        name
        for name, value in (
            ("OM_FEISHU_BOT_APP_ID", bot_app_id),
            (FEISHU_BOT_APP_SECRET, bot_secret_configured),
        )
        if not value
    ]
    if missing_bot_creds:
        add("feishu_bot_credentials", "warn", "Feishu Bot credentials are incomplete", {"missing": missing_bot_creds})
    else:
        add("feishu_bot_credentials", "ok", "Feishu Bot credentials are configured")
    if bot_allowed:
        add("feishu_bot_recipients", "ok", "Feishu Bot sender/recipient ids are configured", {"allowed_open_ids_count": len(bot_allowed)})
    else:
        add(
            "feishu_bot_recipients",
            "warn",
            "Feishu Bot has no allowed sender or default recipient open_id",
            {"missing": ["OM_FEISHU_BOT_ALLOWED_OPEN_IDS", "OM_FEISHU_BOT_USER_OPEN_ID"]},
        )

    runtime_root = effective.get("OM_RUNTIME_ROOT")
    add(
        "runtime_root",
        "ok" if runtime_root else "info",
        "runtime root is configured from settings" if runtime_root else "runtime root will use code default",
        _redacted_value("OM_RUNTIME_ROOT", runtime_root) if runtime_root else None,
    )

    write_gates = {
        "operations_enabled": _truthy(effective.get("OM_INBOUND_OPERATIONS_ENABLED")),
        "trade_write_enabled": _truthy(effective.get("OM_INBOUND_TRADE_WRITE_ENABLED")),
        "symbol_write_enabled": _truthy(effective.get("OM_INBOUND_SYMBOL_WRITE_ENABLED")),
        "upgrade_write_enabled": _truthy(effective.get("OM_INBOUND_UPGRADE_WRITE_ENABLED")),
        "monitor_run_enabled": _truthy(effective.get("OM_INBOUND_MONITOR_RUN_ENABLED")),
        "agent_write_tools_enabled": _truthy(effective.get("OM_AGENT_ENABLE_WRITE_TOOLS")),
    }
    add("write_gates", "info", "write gates are explicit settings and default to disabled", write_gates)
    missing_trade_write = [
        name
        for name, enabled in (
            ("OM_INBOUND_OPERATIONS_ENABLED", write_gates["operations_enabled"]),
            ("OM_INBOUND_TRADE_WRITE_ENABLED", write_gates["trade_write_enabled"]),
        )
        if not enabled
    ]
    if missing_trade_write:
        add(
            "inbound_trade_write_readiness",
            "warn",
            "manual trade inbound is readable but trade writes are not enabled",
            {
                "missing_enabled_env": missing_trade_write,
                "action": "set OM_INBOUND_OPERATIONS_ENABLED=1 and OM_INBOUND_TRADE_WRITE_ENABLED=1 only after confirming the Feishu sender allowlist/admin sender",
            },
        )
    else:
        add("inbound_trade_write_readiness", "ok", "manual trade inbound write gates are enabled")
    missing_upgrade_write = [
        name
        for name, enabled in (
            ("OM_INBOUND_OPERATIONS_ENABLED", write_gates["operations_enabled"]),
            ("OM_INBOUND_UPGRADE_WRITE_ENABLED", write_gates["upgrade_write_enabled"]),
        )
        if not enabled
    ]
    if missing_upgrade_write:
        add(
            "inbound_upgrade_write_readiness",
            "warn",
            "inbound immediate upgrade is not enabled",
            {
                "missing_enabled_env": missing_upgrade_write,
                "action": "set OM_INBOUND_OPERATIONS_ENABLED=1 and OM_INBOUND_UPGRADE_WRITE_ENABLED=1 only after confirming the Feishu sender allowlist/admin sender",
            },
        )
    else:
        add("inbound_upgrade_write_readiness", "ok", "inbound immediate upgrade write gates are enabled")
    missing_monitor_run = [
        name
        for name, enabled in (
            ("OM_INBOUND_OPERATIONS_ENABLED", write_gates["operations_enabled"]),
            ("OM_INBOUND_MONITOR_RUN_ENABLED", write_gates["monitor_run_enabled"]),
        )
        if not enabled
    ]
    if missing_monitor_run:
        add(
            "inbound_monitor_run_readiness",
            "warn",
            "inbound monitor run is not enabled",
            {
                "missing_enabled_env": missing_monitor_run,
                "action": "set OM_INBOUND_OPERATIONS_ENABLED=1 and OM_INBOUND_MONITOR_RUN_ENABLED=1 only after confirming the sender allowlist/admin sender",
            },
        )
    else:
        add("inbound_monitor_run_readiness", "ok", "inbound monitor run gates are enabled")

    error_count = sum(1 for item in checks if item.get("status") == "error")
    warning_count = sum(1 for item in checks if item.get("status") == "warn")
    return {
        "summary": {
            "ok": error_count == 0,
            "error_count": error_count,
            "warning_count": warning_count,
        },
        "env_file": _PUBLIC_ENV_FILE if effective.env_file is not None else None,
        "env_file_loaded": effective.env_file_loaded,
        "checks": checks,
    }


def explain_effective_setting(
    key: str,
    *,
    repo_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
    include_local_env_file: bool = True,
) -> dict[str, Any]:
    env_name = _setting_key_to_env_name(key)
    effective = build_effective_env(
        repo_root=repo_root,
        environ=environ,
        env_file=env_file,
        include_local_env_file=include_local_env_file,
    )
    value = effective.get(env_name)
    source = effective.source_of(env_name)
    if _is_secret_name(env_name):
        from src.application.secret_store import credential_specs, inspect_secret_credentials

        spec = next(
            (
                candidate
                for candidate in credential_specs()
                if env_name in candidate.legacy_env_names
            ),
            None,
        )
        if spec is not None:
            secret_credentials = inspect_secret_credentials(
                environ=effective.values,
                specs=(spec,),
            )
            summary = secret_credentials.get("summary")
            summary = summary if isinstance(summary, dict) else {}
            credential_items = secret_credentials.get("credentials")
            credential_items = (
                credential_items if isinstance(credential_items, list) else []
            )
            credential = (
                credential_items[0]
                if credential_items and isinstance(credential_items[0], dict)
                else {}
            )
            backend = str(summary.get("backend") or "") or None
            configured = bool(credential.get("configured"))
            return {
                "key": key,
                "env_name": env_name,
                "credential_name": spec.logical_name,
                "configured": configured,
                "legacy_env_configured": bool(value),
                "value": "<redacted>" if configured or value else None,
                "source": (
                    source.public_value()
                    if backend == "env" and source is not None
                    else credential.get("source")
                ),
                "secret_backend": backend,
                **({"credential_error": summary.get("error")} if summary.get("error") else {}),
                "secret": True,
                "env_file": _PUBLIC_ENV_FILE if effective.env_file is not None else None,
                "env_file_loaded": effective.env_file_loaded,
                "warnings": _public_env_warnings(effective.warnings),
            }
    return {
        "key": key,
        "env_name": env_name,
        "configured": bool(value),
        "value": _redacted_value(env_name, value),
        "source": source.public_value() if source else None,
        "secret": _is_secret_name(env_name),
        "env_file": _PUBLIC_ENV_FILE if effective.env_file is not None else None,
        "env_file_loaded": effective.env_file_loaded,
        "warnings": _public_env_warnings(effective.warnings),
    }


def _resolve_env_file(
    *,
    repo_root: Path | None,
    environ: Mapping[str, str],
    env_file: str | Path | None,
    include_local_env_file: bool,
) -> Path | None:
    raw = str(env_file or "").strip()
    if not raw:
        raw = str(environ.get(ENV_FILE_POINTER) or "").strip()
    if raw:
        return _resolve_path(raw, base=repo_root)
    if include_local_env_file and repo_root is not None:
        candidate = (repo_root / DEFAULT_LOCAL_ENV_FILE).resolve()
        if candidate.exists():
            return candidate
    return None


def _resolve_path(raw: str | Path, *, base: Path | None) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute() and base is not None:
        path = (base / path).resolve()
    return path.resolve()


def _parse_env_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    quote = value[0]
    if quote in {"'", '"'} and len(value) >= 2 and value[-1] == quote:
        inner = value[1:-1]
        if quote == '"':
            inner = inner.replace(r"\"", '"').replace(r"\\", "\\")
        return inner
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def _setting_key_to_env_name(key: str) -> str:
    normalized = str(key or "").strip()
    aliases = {
        "runtime.root": "OM_RUNTIME_ROOT",
        "data.config": "OM_DATA_CONFIG",
        "output.dir": "OM_OUTPUT_DIR",
        "feishu.holdings.app_id": "OM_FEISHU_APP_ID",
        "feishu.holdings.app_secret": "OM_FEISHU_APP_SECRET",
        "feishu.holdings.table": "OM_FEISHU_HOLDINGS_TABLE",
        "feishu.bot.app_id": "OM_FEISHU_BOT_APP_ID",
        "feishu.bot.app_secret": "OM_FEISHU_BOT_APP_SECRET",
        "feishu.bot.user_open_id": "OM_FEISHU_BOT_USER_OPEN_ID",
        "feishu.bot.allowed_open_ids": "OM_FEISHU_BOT_ALLOWED_OPEN_IDS",
        "inbound.audit_db": "OM_INBOUND_AUDIT_DB",
        "inbound.require_allowlist": "OM_INBOUND_REQUIRE_ALLOWLIST",
        "inbound.operations_enabled": "OM_INBOUND_OPERATIONS_ENABLED",
        "inbound.trade_write_enabled": "OM_INBOUND_TRADE_WRITE_ENABLED",
        "inbound.symbol_write_enabled": "OM_INBOUND_SYMBOL_WRITE_ENABLED",
        "inbound.upgrade_write_enabled": "OM_INBOUND_UPGRADE_WRITE_ENABLED",
        "inbound.monitor_run_enabled": "OM_INBOUND_MONITOR_RUN_ENABLED",
        "inbound.admin_open_ids": "OM_INBOUND_ADMIN_OPEN_IDS",
        "inbound.confirm_ttl_seconds": "OM_INBOUND_CONFIRM_TTL_SECONDS",
        "agent.write_tools_enabled": "OM_AGENT_ENABLE_WRITE_TOOLS",
        "assistant.llm.api_key": "OM_LLM_API_KEY",
    }
    if normalized in aliases:
        return aliases[normalized]
    if normalized.startswith("OM_") and _KEY_RE.match(normalized):
        return normalized
    raise ValueError(f"unknown effective setting key: {key}")


def _split_csv(value: str) -> list[str]:
    out: list[str] = []
    for raw in str(value or "").split(","):
        item = raw.strip()
        if item and item not in out:
            out.append(item)
    return out


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_secret_name(name: str) -> bool:
    upper = str(name or "").upper()
    return upper in _REGISTERED_SECRET_ENV_NAMES or any(
        part in upper for part in _SECRET_NAME_PARTS
    )


def _redacted_value(name: str, value: str) -> str | None:
    if not value:
        return None
    if _is_secret_name(name):
        return "<redacted>"
    if str(name or "").upper() in _PATH_ENV_NAMES:
        return "<configured-path>"
    if str(name or "").upper() in _PUBLIC_LITERAL_ENV_NAMES:
        return value
    # Unknown env-backed values may be stable account, tenant, table, app, or
    # recipient identifiers. Default to full masking instead of guessing from
    # length or a provider-specific prefix.
    return "<redacted>"


def _public_env_warning(message: str) -> str:
    normalized = str(message or "")
    if normalized.startswith("env file not found:"):
        return "env file not found"
    if normalized.startswith("failed to read env file:"):
        return "failed to read env file"
    if normalized.startswith("failed to parse env file:"):
        return "failed to parse env file"
    return "env settings warning"


def _public_env_warnings(messages: tuple[str, ...]) -> list[str]:
    return [_public_env_warning(message) for message in messages]
