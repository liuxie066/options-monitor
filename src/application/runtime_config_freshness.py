from __future__ import annotations

import hashlib
import json
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.config_defaults import DEFAULT_CONFIG_REF, default_config_sha256
from src.application.config_primitives import file_sha256 as _file_sha256
from src.application.config_primitives import path_for_metadata as _path_for_metadata


GENERATED_KEY = "_generated"
GENERATED_SCHEMA_VERSION = "1.0"
RUNTIME_MARKETS = {"us", "hk"}
RUNTIME_CONFIG_MARKET_BY_NAME = {
    "config.us.json": "us",
    "config.hk.json": "hk",
}


class RuntimeConfigFreshnessError(Exception):
    """Raised when a runtime config is missing or stale against its sources."""

    def __init__(self, result: dict[str, Any]):
        self.result = result
        super().__init__(format_runtime_config_freshness_error(result))


class RuntimeConfigIdentityError(Exception):
    """Raised when a runtime config does not match the requested runtime identity."""

    def __init__(self, result: dict[str, Any]):
        self.result = result
        super().__init__(format_runtime_config_identity_error(result))


def _normalize_runtime_market(raw: Any) -> str | None:
    text = str(raw or "").strip().lower()
    return text if text in RUNTIME_MARKETS else None


def market_from_runtime_config_path(path: str | Path | None) -> str | None:
    if path is None:
        return None
    return RUNTIME_CONFIG_MARKET_BY_NAME.get(Path(path).name)


def _metadata_market(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    return _normalize_runtime_market(payload.get("market"))


def infer_runtime_config_market(
    *,
    explicit_market: str | None = None,
    config_key: str | None = None,
    config_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
) -> str | None:
    """Infer the intended market without silently defaulting to US."""
    explicit = _normalize_runtime_market(explicit_market)
    if explicit:
        return explicit
    keyed = _normalize_runtime_market(config_key)
    if keyed:
        return keyed
    path_market = market_from_runtime_config_path(config_path)
    if path_market:
        return path_market
    if isinstance(config, dict):
        generated_market = _metadata_market(config.get(GENERATED_KEY))
        resolved_market = _metadata_market(config.get("_resolved"))
        return generated_market or resolved_market
    return None


def _identity_rebuild_command(
    *,
    expected_market: str | None,
    runtime_config_path: str | Path | None,
    generated: dict[str, Any] | None,
) -> str | None:
    market = expected_market
    if not market and isinstance(generated, dict):
        market = _normalize_runtime_market(generated.get("market"))
    if not market:
        market = market_from_runtime_config_path(runtime_config_path)
    if not market:
        return None
    return build_rebuild_command(
        market=market,
        runtime_config_path=runtime_config_path,
        generated=generated,
    )


def check_runtime_config_identity(
    config: dict[str, Any],
    *,
    explicit_market: str | None = None,
    config_key: str | None = None,
    runtime_config_path: str | Path | None = None,
    required_source_format: str | None = "yaml",
    require_generated: bool = True,
) -> dict[str, Any]:
    expected_market = infer_runtime_config_market(
        explicit_market=explicit_market,
        config_key=config_key,
        config_path=runtime_config_path,
        config=config,
    )
    generated = config.get(GENERATED_KEY) if isinstance(config, dict) else None
    generated_dict = generated if isinstance(generated, dict) else None
    rebuild_command = _identity_rebuild_command(
        expected_market=expected_market,
        runtime_config_path=runtime_config_path,
        generated=generated_dict,
    )
    errors: list[dict[str, Any]] = []

    path_market = market_from_runtime_config_path(runtime_config_path)
    if expected_market and path_market and path_market != expected_market:
        errors.append(
            {
                "code": "path_market_mismatch",
                "message": "runtime config filename does not match requested market",
                "expected": expected_market,
                "actual": path_market,
            }
        )

    if not expected_market:
        errors.append(
            {
                "code": "market_not_inferred",
                "message": "runtime config market could not be inferred",
            }
        )

    if not isinstance(generated, dict):
        if require_generated:
            errors.append(
                {
                    "code": "missing_generated_metadata",
                    "message": "runtime config is missing generation metadata",
                }
            )
        return {
            "ok": not errors,
            "market": expected_market,
            "runtime_config_path": str(runtime_config_path) if runtime_config_path is not None else None,
            "required_source_format": required_source_format,
            "rebuild_command": rebuild_command,
            "errors": errors,
        }

    generated_market = _metadata_market(generated)
    if not generated_market:
        errors.append(
            {
                "code": "generated_market_missing",
                "message": "runtime config generation metadata is missing market",
            }
        )
    elif expected_market and generated_market != expected_market:
        errors.append(
            {
                "code": "market_mismatch",
                "message": "runtime config was generated for another market",
                "expected": expected_market,
                "actual": generated_market,
            }
        )

    resolved = config.get("_resolved") if isinstance(config, dict) else None
    if isinstance(resolved, dict) and "market" in resolved:
        resolved_market = _metadata_market(resolved)
        if not resolved_market:
            errors.append(
                {
                    "code": "resolved_market_invalid",
                    "message": "runtime config resolved metadata has an invalid market",
                }
            )
        elif expected_market and resolved_market != expected_market:
            errors.append(
                {
                    "code": "resolved_market_mismatch",
                    "message": "runtime config resolved metadata does not match requested market",
                    "expected": expected_market,
                    "actual": resolved_market,
                }
            )

    required_format = str(required_source_format or "").strip().lower() or None
    source_format = str(generated.get("source_format") or "").strip().lower()
    if required_format:
        if not source_format:
            errors.append(
                {
                    "code": "source_format_missing",
                    "message": "runtime config generation metadata is missing source_format",
                    "expected": required_format,
                }
            )
        elif source_format != required_format:
            errors.append(
                {
                    "code": "source_format_mismatch",
                    "message": "runtime config was generated from an unsupported source format",
                    "expected": required_format,
                    "actual": source_format,
                }
            )

    return {
        "ok": not errors,
        "market": expected_market,
        "runtime_config_path": str(runtime_config_path) if runtime_config_path is not None else None,
        "generated": generated,
        "source_format": source_format or None,
        "required_source_format": required_format,
        "rebuild_command": rebuild_command,
        "errors": errors,
    }


def _payload_sha256(payload: Any) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _resolve_metadata_path(raw: Any, *, repo_root: Path) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = repo_root.resolve() / path
    return path.resolve()


def build_rebuild_command(
    *,
    market: str,
    runtime_config_path: str | Path | None,
    generated: dict[str, Any] | None = None,
    repo_root: Path | None = None,
) -> str:
    custom_command = generated.get("rebuild_command") if isinstance(generated, dict) else None
    if isinstance(custom_command, str) and custom_command.strip():
        return custom_command.strip()

    command = ["./om", "config", "build", "--source", "yaml", "--market", str(market)]
    if runtime_config_path is not None:
        command.extend(["--output", str(runtime_config_path)])
    return " ".join(shlex.quote(part) for part in command)


def build_generated_metadata(
    *,
    repo_root: Path,
    market: str,
    system_config_path: Path,
    user_config_path: Path,
    common_user_config_path: Path | None,
    common_user_config_loaded: bool,
    common_user_config_enabled: bool,
    common_user_config_auto_candidate: bool,
) -> dict[str, Any]:
    version_path = repo_root / "VERSION"
    version = version_path.read_text(encoding="utf-8").strip() if version_path.exists() else None

    def source(
        *,
        role: str,
        path: Path | None,
        loaded: bool,
        optional: bool = False,
        enabled: bool = True,
        auto_candidate: bool = False,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "role": role,
            "loaded": bool(loaded),
            "optional": bool(optional),
            "enabled": bool(enabled),
        }
        if auto_candidate:
            item["auto_candidate"] = True
        if path is not None:
            resolved = path.resolve()
            item["path"] = _path_for_metadata(resolved, repo_root=repo_root)
            item["sha256"] = _file_sha256(resolved) if loaded else None
        return item

    return {
        "schema_version": GENERATED_SCHEMA_VERSION,
        "generator": "options-monitor",
        "source_format": "legacy",
        "version": version,
        "market": str(market),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": [
            source(role="system", path=system_config_path, loaded=True),
            source(
                role="common_user",
                path=common_user_config_path,
                loaded=common_user_config_loaded,
                optional=True,
                enabled=common_user_config_enabled,
                auto_candidate=common_user_config_auto_candidate,
            ),
            source(role="market_user", path=user_config_path, loaded=True),
        ],
    }


def build_inline_generated_metadata(
    *,
    repo_root: Path,
    market: str,
    system_config_path: Path,
    user_config: dict[str, Any],
    user_config_ref: str,
    rebuild_command: str | None = None,
) -> dict[str, Any]:
    version_path = repo_root / "VERSION"
    version = version_path.read_text(encoding="utf-8").strip() if version_path.exists() else None
    generated: dict[str, Any] = {
        "schema_version": GENERATED_SCHEMA_VERSION,
        "generator": "options-monitor",
        "source_format": "legacy",
        "version": version,
        "market": str(market),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": [
            {
                "role": "system",
                "loaded": True,
                "optional": False,
                "enabled": True,
                "path": _path_for_metadata(system_config_path.resolve(), repo_root=repo_root),
                "sha256": _file_sha256(system_config_path.resolve()),
            },
            {
                "role": "common_user",
                "loaded": False,
                "optional": True,
                "enabled": False,
            },
            {
                "role": "market_user",
                "loaded": True,
                "optional": False,
                "enabled": True,
                "inline": True,
                "ref": str(user_config_ref),
                "sha256": _payload_sha256(user_config),
            },
        ],
    }
    if rebuild_command is not None and str(rebuild_command).strip():
        generated["rebuild_command"] = str(rebuild_command).strip()
    return generated


def check_runtime_config_freshness(
    config: dict[str, Any],
    *,
    repo_root: Path,
    market: str,
    runtime_config_path: str | Path | None = None,
) -> dict[str, Any]:
    generated = config.get(GENERATED_KEY)
    rebuild_command = build_rebuild_command(
        market=str(market),
        runtime_config_path=runtime_config_path,
        generated=generated if isinstance(generated, dict) else None,
        repo_root=repo_root,
    )
    errors: list[dict[str, Any]] = []

    if not isinstance(generated, dict):
        return {
            "ok": False,
            "market": str(market),
            "runtime_config_path": str(runtime_config_path) if runtime_config_path is not None else None,
            "source_format": None,
            "rebuild_command": rebuild_command,
            "errors": [
                {
                    "code": "missing_generated_metadata",
                    "message": "runtime config is missing generation metadata",
                }
            ],
        }

    generated_market = str(generated.get("market") or "").strip().lower()
    source_format = str(generated.get("source_format") or "").strip().lower() or None
    expected_market = str(market or "").strip().lower()
    if generated_market != expected_market:
        errors.append(
            {
                "code": "market_mismatch",
                "message": "runtime config was generated for another market",
                "expected": expected_market,
                "actual": generated_market,
            }
        )

    sources = generated.get("sources")
    source_items = sources if isinstance(sources, list) else []
    roles = {
        str(item.get("role") or ""): item
        for item in source_items
        if isinstance(item, dict)
    }
    for required_role in ("system", "market_user"):
        if required_role not in roles:
            errors.append(
                {
                    "code": "missing_source_record",
                    "message": f"runtime config generation metadata is missing {required_role} source",
                    "role": required_role,
                }
            )

    for item in source_items:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        loaded = bool(item.get("loaded"))
        enabled = bool(item.get("enabled", True))
        inline = bool(item.get("inline"))
        if loaded and inline:
            actual_inline_sha = str(item.get("sha256") or "").strip()
            if not actual_inline_sha:
                errors.append(
                    {
                        "code": "inline_source_fingerprint_missing",
                        "message": "inline runtime config source has no fingerprint",
                        "role": role,
                    }
                )
            elif role == "system" and str(item.get("ref") or "").strip() == DEFAULT_CONFIG_REF:
                expected_inline_sha = default_config_sha256()
                if actual_inline_sha != expected_inline_sha:
                    errors.append(
                        {
                            "code": "inline_source_changed",
                            "message": "inline runtime config source changed after generation",
                            "role": role,
                            "ref": DEFAULT_CONFIG_REF,
                            "expected_sha256": actual_inline_sha,
                            "actual_sha256": expected_inline_sha,
                        }
                    )
            continue
        path = _resolve_metadata_path(item.get("path"), repo_root=repo_root)
        if path is None:
            if loaded:
                errors.append(
                    {
                        "code": "source_path_missing",
                        "message": "loaded source has no path",
                        "role": role,
                    }
                )
            continue

        if loaded:
            if not path.exists():
                errors.append(
                    {
                        "code": "source_missing",
                        "message": "runtime config source file is missing",
                        "role": role,
                        "path": str(path),
                    }
                )
                continue
            current_sha = _file_sha256(path)
            expected_sha = str(item.get("sha256") or "")
            if current_sha != expected_sha:
                errors.append(
                    {
                        "code": "source_changed",
                        "message": "runtime config source file changed after generation",
                        "role": role,
                        "path": str(path),
                        "expected_sha256": expected_sha,
                        "current_sha256": current_sha,
                    }
                )
        elif role == "common_user" and enabled and path.exists():
            errors.append(
                {
                    "code": "optional_source_appeared",
                    "message": "optional common user config appeared after runtime config generation",
                    "role": role,
                    "path": str(path),
                }
            )

    return {
        "ok": not errors,
        "market": expected_market,
        "runtime_config_path": str(runtime_config_path) if runtime_config_path is not None else None,
        "generated": generated,
        "source_format": source_format,
        "rebuild_command": rebuild_command,
        "errors": errors,
    }


def format_runtime_config_freshness_error(result: dict[str, Any]) -> str:
    errors = result.get("errors") if isinstance(result.get("errors"), list) else []
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    lines = ["[CONFIG_ERROR] runtime config is stale"]
    if first.get("code") == "missing_generated_metadata":
        lines[0] = "[CONFIG_ERROR] runtime config is missing generation metadata"
    elif first.get("code") == "market_mismatch":
        lines[0] = "[CONFIG_ERROR] runtime config market does not match requested market"

    if result.get("market"):
        lines.append(f"market: {result['market']}")
    if result.get("runtime_config_path"):
        lines.append(f"runtime_config: {result['runtime_config_path']}")
    if first:
        lines.append(f"reason: {first.get('message') or first.get('code')}")
        if first.get("role"):
            lines.append(f"changed_source: {first['role']} {first.get('path') or ''}".rstrip())
    if result.get("rebuild_command"):
        lines.append(f"rebuild: {result['rebuild_command']}")
    if str(result.get("source_format") or "").strip().lower() == "legacy":
        lines.append("migrate: ./om config migrate-yaml --output config.yaml --apply")
    return "\n".join(lines)


def format_runtime_config_identity_error(result: dict[str, Any]) -> str:
    errors = result.get("errors") if isinstance(result.get("errors"), list) else []
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    lines = ["[CONFIG_ERROR] runtime config identity is invalid"]
    if first.get("code") == "missing_generated_metadata":
        lines[0] = "[CONFIG_ERROR] runtime config is missing generation metadata"
    elif first.get("code") in {"market_mismatch", "path_market_mismatch", "resolved_market_mismatch"}:
        lines[0] = "[CONFIG_ERROR] runtime config market does not match requested market"
    elif first.get("code") in {"source_format_missing", "source_format_mismatch"}:
        lines[0] = "[CONFIG_ERROR] runtime config was not generated from config.yaml"

    if result.get("market"):
        lines.append(f"market: {result['market']}")
    if result.get("runtime_config_path"):
        lines.append(f"runtime_config: {result['runtime_config_path']}")
    if first:
        lines.append(f"reason: {first.get('message') or first.get('code')}")
        if first.get("expected") is not None:
            lines.append(f"expected: {first['expected']}")
        if first.get("actual") is not None:
            lines.append(f"actual: {first['actual'] or '<missing>'}")
    if result.get("rebuild_command"):
        lines.append(f"rebuild: {result['rebuild_command']}")
    return "\n".join(lines)


def ensure_runtime_config_identity(
    config: dict[str, Any],
    *,
    explicit_market: str | None = None,
    config_key: str | None = None,
    runtime_config_path: str | Path | None = None,
    required_source_format: str | None = "yaml",
    require_generated: bool = True,
) -> dict[str, Any]:
    result = check_runtime_config_identity(
        config,
        explicit_market=explicit_market,
        config_key=config_key,
        runtime_config_path=runtime_config_path,
        required_source_format=required_source_format,
        require_generated=require_generated,
    )
    if not result.get("ok"):
        raise RuntimeConfigIdentityError(result)
    return result


def ensure_runtime_config_freshness(
    config: dict[str, Any],
    *,
    repo_root: Path,
    market: str,
    runtime_config_path: str | Path | None = None,
) -> dict[str, Any]:
    result = check_runtime_config_freshness(
        config,
        repo_root=repo_root,
        market=market,
        runtime_config_path=runtime_config_path,
    )
    if not result.get("ok"):
        raise RuntimeConfigFreshnessError(result)
    return result


__all__ = [
    "GENERATED_KEY",
    "RUNTIME_CONFIG_MARKET_BY_NAME",
    "RUNTIME_MARKETS",
    "RuntimeConfigFreshnessError",
    "RuntimeConfigIdentityError",
    "build_generated_metadata",
    "build_inline_generated_metadata",
    "build_rebuild_command",
    "check_runtime_config_identity",
    "check_runtime_config_freshness",
    "ensure_runtime_config_identity",
    "ensure_runtime_config_freshness",
    "format_runtime_config_freshness_error",
    "format_runtime_config_identity_error",
    "infer_runtime_config_market",
    "market_from_runtime_config_path",
]
