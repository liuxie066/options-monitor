from __future__ import annotations

import hashlib
import json
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


GENERATED_KEY = "_generated"
GENERATED_SCHEMA_VERSION = "1.0"


class RuntimeConfigFreshnessError(Exception):
    """Raised when a runtime config is missing or stale against its sources."""

    def __init__(self, result: dict[str, Any]):
        self.result = result
        super().__init__(format_runtime_config_freshness_error(result))


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _payload_sha256(payload: Any) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _path_for_metadata(path: Path, *, repo_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


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

    command = ["./om", "config", "build", "--market", str(market)]
    sources = generated.get("sources") if isinstance(generated, dict) else []
    if isinstance(sources, list):
        source_by_role = {
            str(item.get("role") or ""): item
            for item in sources
            if isinstance(item, dict)
        }
        system = source_by_role.get("system") or {}
        if system.get("path"):
            command.extend(["--system-config", str(system["path"])])

        common = source_by_role.get("common_user") or {}
        if common.get("loaded") and common.get("path"):
            command.extend(["--common-user-config", str(common["path"])])
        elif common.get("enabled") and common.get("path") and repo_root is not None:
            common_path = _resolve_metadata_path(common.get("path"), repo_root=repo_root)
            if common_path is not None and common_path.exists():
                command.extend(["--common-user-config", str(common["path"])])
        elif common and common.get("enabled") is False:
            command.append("--no-common-user-config")

        user = source_by_role.get("market_user") or {}
        if user.get("path"):
            command.extend(["--user-config", str(user["path"])])

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
            "rebuild_command": rebuild_command,
            "errors": [
                {
                    "code": "missing_generated_metadata",
                    "message": "runtime config is missing generation metadata",
                }
            ],
        }

    generated_market = str(generated.get("market") or "").strip().lower()
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
            if not str(item.get("sha256") or "").strip():
                errors.append(
                    {
                        "code": "inline_source_fingerprint_missing",
                        "message": "inline runtime config source has no fingerprint",
                        "role": role,
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
    return "\n".join(lines)


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
    "RuntimeConfigFreshnessError",
    "build_generated_metadata",
    "build_inline_generated_metadata",
    "build_rebuild_command",
    "check_runtime_config_freshness",
    "ensure_runtime_config_freshness",
    "format_runtime_config_freshness_error",
]
