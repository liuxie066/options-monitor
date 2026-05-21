from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import build_response, mask_path
from src.application.ai_cofunder.redaction import redact_value
from src.application.config_validator import validate_config
from src.application.runtime_paths import resolve_runtime_root
from src.application.setup import run_setup_check
from src.application.settings import diagnose_effective_settings
from src.application.tool_execution import execute_tool


SCHEMA_VERSION = "support_bundle.v1"
PATH_KEY_PARTS = ("path", "dir", "root", "file", "sqlite", "db", "executable")
LOCAL_ABSOLUTE_PATH_RE = re.compile(r"(?P<path>/(?:Users|Volumes|private|var|tmp)/[^'\"\n,}]+)")


def collect_support_bundle(
    *,
    repo_root: str | Path,
    config_key: str | None = None,
    config_path: str | Path | None = None,
    accounts: list[str] | None = None,
    profile_path: str | Path | None = None,
    env_file: str | Path | None = None,
    include_local_env_file: bool = True,
    include_healthcheck: bool = False,
    output_dir: str | Path | None = None,
    runtime_root: str | Path | None = None,
    execute_tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Collect a redacted, read-only diagnostic bundle for support handoff."""
    root = Path(repo_root).expanduser().resolve()
    now = (now_fn or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    tool_fn = execute_tool_fn or execute_tool
    targets = _config_targets(config_key=config_key, config_path=config_path)
    sections: dict[str, Any] = {}

    sections["environment"] = _capture(lambda: _environment_section(root=root))
    sections["setup_check"] = _capture(
        lambda: run_setup_check(
            repo_root=root,
            markets=[config_key] if config_key in {"us", "hk"} else None,
            env_file=env_file,
            include_local_env_file=include_local_env_file,
        )
    )
    sections["settings_doctor"] = _capture(
        lambda: diagnose_effective_settings(
            repo_root=root,
            env_file=env_file,
            include_local_env_file=include_local_env_file,
        )
    )
    sections["config_validate"] = _capture(lambda: _config_validate_section(targets))
    sections["runtime_status"] = _capture(lambda: _runtime_status_section(targets, accounts=accounts, profile_path=profile_path, tool_fn=tool_fn))
    if include_healthcheck:
        sections["healthcheck"] = _capture(lambda: _healthcheck_section(targets, accounts=accounts, profile_path=profile_path, tool_fn=tool_fn))
    else:
        sections["healthcheck"] = {"status": "skipped", "reason": "include_healthcheck=false"}

    raw_bundle = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": now.isoformat().replace("+00:00", "Z"),
        "input": {
            "config_key": config_key,
            "config_path": str(config_path) if config_path is not None else None,
            "accounts": list(accounts or []),
            "profile_path": str(profile_path) if profile_path is not None else None,
            "env_file": str(env_file) if env_file is not None else None,
            "include_local_env_file": bool(include_local_env_file),
            "include_healthcheck": bool(include_healthcheck),
        },
        "redaction": {
            "enabled": True,
            "rules": ["secret_key_names", "webhook_urls", "bearer_tokens", "long_numbers", "path_values", "local_absolute_paths"],
        },
        "sections": sections,
    }
    bundle = _redact_bundle(raw_bundle)
    summary = _summary(bundle)
    bundle["summary"] = summary

    destination = _write_bundle(
        bundle,
        repo_root=root,
        output_dir=output_dir,
        runtime_root=runtime_root,
        generated_at=now,
    )
    return {
        "summary": summary,
        "bundle_path": str(destination),
        "bundle_path_public": mask_path(destination),
        "schema_version": SCHEMA_VERSION,
        "redacted": True,
        "included_sections": sorted(bundle.get("sections", {}).keys()),
    }


def _environment_section(*, root: Path) -> dict[str, Any]:
    return {
        "repo_root": str(root),
        "version": _read_text(root / "VERSION"),
        "git": _git_snapshot(root),
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
        },
    }


def _git_snapshot(root: Path) -> dict[str, Any]:
    return {
        "commit": _git(root, "rev-parse", "HEAD"),
        "short_commit": _git(root, "rev-parse", "--short", "HEAD"),
        "branch": _git(root, "branch", "--show-current"),
        "exact_tag": _git(root, "describe", "--tags", "--exact-match"),
    }


def _config_targets(*, config_key: str | None, config_path: str | Path | None) -> list[dict[str, Any]]:
    if config_path is not None and str(config_path).strip():
        return [{"label": config_key or "custom", "config_key": config_key, "config_path": str(config_path)}]
    if config_key:
        return [{"label": config_key, "config_key": config_key}]
    return [{"label": "us", "config_key": "us"}, {"label": "hk", "config_key": "hk"}]


def _config_validate_section(targets: list[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for target in targets:
        item: dict[str, Any] = {"label": target["label"]}
        try:
            path, cfg = load_runtime_config(config_key=target.get("config_key"), config_path=target.get("config_path"))
            validate_config(dict(cfg))
        except SystemExit as exc:
            item.update({"ok": False, "error": str(exc)})
        except Exception as exc:
            item.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        else:
            item.update({"ok": True, "config_path": str(path)})
        items.append(item)
    return {
        "status": "ok" if all(bool(item.get("ok")) for item in items) else "error",
        "items": items,
    }


def _runtime_status_section(
    targets: list[dict[str, Any]],
    *,
    accounts: list[str] | None,
    profile_path: str | Path | None,
    tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    return _tool_section("runtime_status", targets, accounts=accounts, profile_path=profile_path, tool_fn=tool_fn)


def _healthcheck_section(
    targets: list[dict[str, Any]],
    *,
    accounts: list[str] | None,
    profile_path: str | Path | None,
    tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    return _tool_section("healthcheck", targets, accounts=accounts, profile_path=profile_path, tool_fn=tool_fn)


def _tool_section(
    tool_name: str,
    targets: list[dict[str, Any]],
    *,
    accounts: list[str] | None,
    profile_path: str | Path | None,
    tool_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for target in targets:
        payload = _target_payload(target, accounts=accounts, profile_path=profile_path)
        result = tool_fn(tool_name, payload)
        items.append({"label": target["label"], "payload": payload, "result": result})
    return {
        "status": "ok" if all(bool(item.get("result", {}).get("ok", False)) for item in items) else "error",
        "items": items,
    }


def _target_payload(target: dict[str, Any], *, accounts: list[str] | None, profile_path: str | Path | None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if target.get("config_key"):
        payload["config_key"] = target["config_key"]
    if target.get("config_path"):
        payload["config_path"] = target["config_path"]
    if accounts:
        payload["accounts"] = list(accounts)
    if profile_path:
        payload["profile_path"] = str(profile_path)
    return payload


def _capture(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:
        return {
            "status": "error",
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }


def _redact_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    return _mask_path_values(redact_value(payload))


def _mask_path_values(value: Any, *, key_hint: str = "") -> Any:
    if isinstance(value, dict):
        return {str(key): _mask_path_values(item, key_hint=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_path_values(item, key_hint=key_hint) for item in value]
    if isinstance(value, str):
        if value.startswith("env_file:"):
            prefix, raw_path = value.split(":", 1)
            return f"{prefix}:{mask_path(raw_path)}" if _looks_like_path(raw_path) else value
        if _is_path_key(key_hint) and _looks_like_path(value):
            return mask_path(value)
        return _mask_embedded_local_paths(value)
    return value


def _is_path_key(key: str) -> bool:
    lowered = str(key or "").lower()
    return any(part in lowered for part in PATH_KEY_PARTS)


def _looks_like_path(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and ("/" in text or text.startswith("~") or text.startswith("."))


def _mask_embedded_local_paths(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        raw = match.group("path").rstrip()
        return mask_path(raw) or "..."

    return LOCAL_ABSOLUTE_PATH_RE.sub(_replace, text)


def _summary(bundle: dict[str, Any]) -> dict[str, Any]:
    sections = bundle.get("sections")
    section_map: dict[str, Any] = sections if isinstance(sections, dict) else {}
    statuses: dict[str, str] = {}
    error_count = 0
    warning_count = 0
    for name, section in section_map.items():
        status = _section_status(section)
        statuses[name] = status
        if status == "error":
            error_count += 1
        if status == "warn":
            warning_count += 1
        warning_count += _nested_warning_count(section)
    return {
        "ok": error_count == 0,
        "diagnostic_ok": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "section_status": statuses,
    }


def _section_status(section: Any) -> str:
    if isinstance(section, dict):
        explicit = str(section.get("status") or "").strip().lower()
        if explicit in {"ok", "warn", "error", "skipped", "info"}:
            return explicit
        summary = section.get("summary")
        if isinstance(summary, dict):
            if summary.get("ok") is False:
                return "error"
            if int(summary.get("warning_count") or 0) > 0:
                return "warn"
            return "ok"
        if section.get("ok") is False:
            return "error"
    return "ok"


def _nested_warning_count(section: Any) -> int:
    if isinstance(section, dict):
        total = 0
        summary = section.get("summary")
        if isinstance(summary, dict):
            try:
                total += int(summary.get("warning_count") or 0)
            except Exception:
                pass
        warnings = section.get("warnings")
        if isinstance(warnings, list):
            total += len(warnings)
        return total
    return 0


def _write_bundle(
    bundle: dict[str, Any],
    *,
    repo_root: Path,
    output_dir: str | Path | None,
    runtime_root: str | Path | None,
    generated_at: datetime,
) -> Path:
    if output_dir is not None and str(output_dir).strip():
        directory = Path(output_dir).expanduser()
        if not directory.is_absolute():
            directory = (Path.cwd() / directory).resolve()
    else:
        runtime = resolve_runtime_root(repo_root=repo_root, runtime_root=runtime_root)
        directory = runtime.runtime_root / "support"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"options-monitor-support-{stamp}.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path.resolve()


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def support_bundle_response(**kwargs: Any) -> dict[str, Any]:
    data = collect_support_bundle(**kwargs)
    return build_response(tool_name="support.bundle", ok=True, data=data)
