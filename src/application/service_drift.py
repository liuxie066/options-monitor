from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from src.application.service_deploy import (
    DEFAULT_ACCOUNTS,
    DEFAULT_MARKETS,
    load_service_profile,
    render_service_bundle,
)


SYSTEMD_REQUIRED_MAINTENANCE_UNITS = ("options-monitor-projection-verify.timer",)
LAUNCHD_REQUIRED_MAINTENANCE_UNITS = ("com.options-monitor.projection-verify",)


def service_drift(
    *,
    repo_root: str | Path | None = None,
    runtime_root: str | Path | None = None,
    profile_path: str | Path | None = None,
    profile: dict[str, Any] | None = None,
    confirm: bool = False,
    systemd_unit_root: str | Path | None = None,
    run_cmd: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Compare current-release expected services with profile and installed unit files.

    Dry-run is the default. Confirmed apply only writes missing unit files and the
    refreshed service profile, then enables missing timers. Long-running services
    are written when missing but are not enabled or restarted here.
    """

    initial = _load_profile_and_paths(
        repo_root=repo_root,
        runtime_root=runtime_root,
        profile_path=profile_path,
        profile=profile,
        systemd_unit_root=systemd_unit_root,
    )
    before = _build_drift(initial)
    operations: list[dict[str, Any]] = []
    apply_errors: list[str] = []
    changed = False

    if confirm and before.get("supported"):
        apply_result = _apply_missing_service_drift(initial, before=before, operations=operations, run_cmd=run_cmd)
        changed = bool(apply_result.get("changed"))
        apply_errors = [str(item) for item in apply_result.get("errors") or []]
        after = _build_drift(initial)
        out = {
            **after,
            "before": before,
            "confirmed": True,
            "changed": changed,
            "operations": operations,
            "apply_errors": apply_errors,
            "applied": apply_result,
        }
        if apply_errors:
            summary_raw = out.get("summary")
            summary = summary_raw if isinstance(summary_raw, dict) else {}
            out["summary"] = _summary_with_apply_errors(summary, apply_errors)
        return out

    return {
        **before,
        "confirmed": bool(confirm),
        "changed": False,
        "operations": operations,
        "apply_errors": apply_errors,
    }


def service_drift_status(
    *,
    repo_root: str | Path | None = None,
    runtime_root: str | Path | None = None,
    profile_path: str | Path | None = None,
    profile: dict[str, Any] | None = None,
    systemd_unit_root: str | Path | None = None,
) -> dict[str, Any]:
    try:
        return service_drift(
            repo_root=repo_root,
            runtime_root=runtime_root,
            profile_path=profile_path,
            profile=profile,
            confirm=False,
            systemd_unit_root=systemd_unit_root,
        )
    except Exception as exc:
        return {
            "checked": False,
            "supported": False,
            "reason": "service_drift_check_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "summary": {
                "ok": False,
                "status": "error",
                "error_count": 1,
                "warning_count": 0,
                "missing_required_units": [],
            },
        }


def _load_profile_and_paths(
    *,
    repo_root: str | Path | None,
    runtime_root: str | Path | None,
    profile_path: str | Path | None,
    profile: dict[str, Any] | None,
    systemd_unit_root: str | Path | None,
) -> dict[str, Any]:
    loaded_profile = dict(profile or {})
    runtime = Path(runtime_root or loaded_profile.get("runtime_root") or "/var/lib/options-monitor").expanduser()
    profile_file = Path(profile_path).expanduser() if profile_path else runtime / "service.profile.json"
    if not loaded_profile and profile_file.exists():
        loaded_profile = load_service_profile(profile_file)
        if runtime_root is None and loaded_profile.get("runtime_root"):
            runtime = Path(str(loaded_profile["runtime_root"])).expanduser()
    repo = Path(repo_root or loaded_profile.get("repo_root") or Path.cwd()).expanduser()
    provider = str(loaded_profile.get("service_provider") or loaded_profile.get("provider") or "").strip().lower()
    unit_root_raw = (
        systemd_unit_root
        or loaded_profile.get("systemd_unit_root")
        or os.environ.get("OM_SYSTEMD_UNIT_ROOT")
        or "/etc/systemd/system"
    )
    return {
        "profile": loaded_profile,
        "profile_path": profile_file,
        "repo_root": repo,
        "runtime_root": runtime,
        "provider": provider,
        "systemd_unit_root": Path(unit_root_raw).expanduser(),
    }


def _build_drift(ctx: dict[str, Any]) -> dict[str, Any]:
    profile = ctx["profile"]
    provider = str(ctx["provider"] or "").strip().lower()
    if provider not in {"systemd", "launchd"}:
        return {
            "checked": False,
            "supported": False,
            "reason": "unsupported_service_provider" if provider else "service_profile_missing",
            "provider": provider or None,
            "profile_path": str(ctx["profile_path"]),
            "repo_root": str(ctx["repo_root"]),
            "runtime_root": str(ctx["runtime_root"]),
            "summary": {"ok": True, "status": "skipped", "error_count": 0, "warning_count": 0},
        }

    if isinstance(profile.get("services"), list) and not profile.get("services"):
        return {
            "checked": True,
            "supported": True,
            "reason": "service_profile_has_no_services",
            "provider": provider,
            "profile_path": str(ctx["profile_path"]),
            "repo_root": str(ctx["repo_root"]),
            "runtime_root": str(ctx["runtime_root"]),
            "systemd_unit_root": str(ctx["systemd_unit_root"]) if provider == "systemd" else None,
            "expected_services": [],
            "profile_services": [],
            "installed_units": [],
            "missing_profile_units": [],
            "missing_installed_units": [],
            "extra_profile_units": [],
            "extra_installed_units": [],
            "mismatched_units": [],
            "required_units": [],
            "missing_required_units": [],
            "profile_content_changed": False,
            "manual_actions": [],
            "summary": {"ok": True, "status": "skipped", "error_count": 0, "warning_count": 0},
        }
    bundle = _expected_bundle_from_profile(
        profile,
        provider=provider,
        repo_root=ctx["repo_root"],
        runtime_root=ctx["runtime_root"],
    )
    expected_files = _expected_install_files(bundle, provider=provider)
    expected_services = _service_names_from_profile(_bundle_profile(bundle))
    profile_services = _service_names_from_profile(profile)
    installed_units = _installed_units(provider=provider, expected_files=expected_files, ctx=ctx)
    missing_profile_units = sorted(set(expected_services) - set(profile_services))
    extra_profile_units = sorted(set(profile_services) - set(expected_services))
    missing_installed_units = sorted(set(expected_files) - set(installed_units))
    extra_installed_units = sorted(set(installed_units) - set(expected_files))
    mismatched_units = _mismatched_units(provider=provider, expected_files=expected_files, ctx=ctx)
    required_units = _required_units(provider, expected_services)
    missing_required_units = sorted(
        unit
        for unit in required_units
        if unit in set(missing_profile_units) or unit in set(missing_installed_units)
    )
    profile_content_changed = _profile_content_changed(profile, bundle)
    manual_actions = _manual_actions(
        provider=provider,
        missing_installed_units=missing_installed_units,
        mismatched_units=mismatched_units,
        profile_path=ctx["profile_path"],
    )
    summary = _drift_summary(
        expected_services=expected_services,
        missing_required_units=missing_required_units,
        missing_profile_units=missing_profile_units,
        missing_installed_units=missing_installed_units,
        extra_profile_units=extra_profile_units,
        extra_installed_units=extra_installed_units,
        mismatched_units=mismatched_units,
        profile_content_changed=profile_content_changed,
    )
    return {
        "checked": True,
        "supported": True,
        "provider": provider,
        "profile_path": str(ctx["profile_path"]),
        "repo_root": str(ctx["repo_root"]),
        "runtime_root": str(ctx["runtime_root"]),
        "systemd_unit_root": str(ctx["systemd_unit_root"]) if provider == "systemd" else None,
        "expected_services": expected_services,
        "profile_services": profile_services,
        "installed_units": installed_units,
        "missing_profile_units": missing_profile_units,
        "missing_installed_units": missing_installed_units,
        "extra_profile_units": extra_profile_units,
        "extra_installed_units": extra_installed_units,
        "mismatched_units": mismatched_units,
        "required_units": required_units,
        "missing_required_units": missing_required_units,
        "profile_content_changed": profile_content_changed,
        "manual_actions": manual_actions,
        "summary": summary,
    }


def _expected_bundle_from_profile(
    profile: dict[str, Any],
    *,
    provider: str,
    repo_root: Path,
    runtime_root: Path,
) -> dict[str, Any]:
    config_paths_raw = profile.get("config_paths")
    config_paths = config_paths_raw if isinstance(config_paths_raw, dict) else {}
    feishu_ws_raw = profile.get("feishu_ws")
    feishu_ws = feishu_ws_raw if isinstance(feishu_ws_raw, dict) else {}
    services = _service_names_from_profile(profile)
    include_auto_upgrade = bool(
        isinstance(profile.get("auto_upgrade"), dict)
        and profile["auto_upgrade"].get("enabled")
        or "options-monitor-upgrade.timer" in services
        or "com.options-monitor.upgrade" in services
    )
    include_feishu_ws = bool(
        feishu_ws.get("enabled")
        or "options-monitor-feishu-ws.service" in services
        or "com.options-monitor.feishu-ws" in services
    )
    return render_service_bundle(
        target=provider,
        repo_root=repo_root,
        runtime_root=runtime_root,
        accounts=_profile_accounts(profile),
        markets=_profile_markets(profile),
        config_paths={str(key): str(value) for key, value in config_paths.items() if str(value or "").strip()},
        env_file=profile.get("env_file"),
        deploy_user=profile.get("deploy_user"),
        deploy_home=profile.get("deploy_home"),
        use_default_deploy_user=False,
        include_auto_upgrade=include_auto_upgrade,
        include_feishu_ws=include_feishu_ws,
        feishu_ws_config_key=str(feishu_ws.get("config_key") or "us"),
        include_content=True,
    )


def _bundle_profile(bundle: dict[str, Any]) -> dict[str, Any]:
    for item in bundle.get("files", []):
        if isinstance(item, dict) and item.get("kind") == "service_profile":
            try:
                payload = json.loads(str(item.get("content") or "{}"))
            except Exception:
                return {}
            return payload if isinstance(payload, dict) else {}
    return {}


def _expected_profile_content(bundle: dict[str, Any]) -> str:
    for item in bundle.get("files", []):
        if isinstance(item, dict) and item.get("kind") == "service_profile":
            return str(item.get("content") or "")
    return ""


def _expected_install_files(bundle: dict[str, Any], *, provider: str) -> dict[str, dict[str, Any]]:
    kinds = {"systemd_service", "systemd_timer"} if provider == "systemd" else {"launchd_plist"}
    out: dict[str, dict[str, Any]] = {}
    for item in bundle.get("files", []):
        if not isinstance(item, dict) or item.get("kind") not in kinds:
            continue
        name = _service_name_for_file(item, provider=provider)
        if name:
            out[name] = item
    return out


def _service_name_for_file(item: dict[str, Any], *, provider: str) -> str:
    if provider == "systemd":
        return Path(str(item.get("install_path") or item.get("relative_path") or "")).name
    install_path = str(item.get("install_path") or "")
    return Path(install_path).name.removesuffix(".plist")


def _service_names_from_profile(profile: dict[str, Any]) -> list[str]:
    services = profile.get("services")
    raw_items = services if isinstance(services, list) else []
    out: list[str] = []
    for item in raw_items:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("label") or "").strip()
        else:
            name = str(item or "").strip()
        if name and name not in out:
            out.append(name)
    return sorted(out)


def _profile_accounts(profile: dict[str, Any]) -> list[str]:
    values = profile.get("accounts")
    if isinstance(values, list):
        out = [str(item).strip() for item in values if str(item).strip()]
        if out:
            return out
    return list(DEFAULT_ACCOUNTS)


def _profile_markets(profile: dict[str, Any]) -> list[str]:
    values = profile.get("markets")
    if isinstance(values, list):
        out = [str(item).strip().lower() for item in values if str(item).strip().lower() in {"us", "hk"}]
        if out:
            return sorted(set(out), key=out.index)
    config_paths = profile.get("config_paths")
    if isinstance(config_paths, dict):
        out = [str(key).strip().lower() for key in config_paths if str(key).strip().lower() in {"us", "hk"}]
        if out:
            return sorted(set(out), key=out.index)
    services = " ".join(_service_names_from_profile(profile))
    inferred = [market for market in DEFAULT_MARKETS if f"-{market}" in services or f".{market}" in services]
    return inferred or list(DEFAULT_MARKETS)


def _installed_units(*, provider: str, expected_files: dict[str, dict[str, Any]], ctx: dict[str, Any]) -> list[str]:
    if provider == "systemd":
        root = Path(ctx["systemd_unit_root"])
        names = {path.name for path in root.glob("options-monitor*") if path.is_file()} if root.exists() else set()
        for name, item in expected_files.items():
            if _install_path(item, provider=provider, ctx=ctx).exists():
                names.add(name)
        return sorted(names)
    names: set[str] = set()
    for name, item in expected_files.items():
        if _install_path(item, provider=provider, ctx=ctx).exists():
            names.add(name)
    return sorted(names)


def _install_path(item: dict[str, Any], *, provider: str, ctx: dict[str, Any]) -> Path:
    raw = Path(str(item.get("install_path") or "")).expanduser()
    if provider == "systemd":
        return Path(ctx["systemd_unit_root"]) / raw.name
    return raw


def _mismatched_units(*, provider: str, expected_files: dict[str, dict[str, Any]], ctx: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for name, item in expected_files.items():
        path = _install_path(item, provider=provider, ctx=ctx)
        if not path.exists() or not path.is_file():
            continue
        try:
            actual = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if actual != str(item.get("content") or ""):
            out.append(name)
    return sorted(out)


def _profile_content_changed(profile: dict[str, Any], bundle: dict[str, Any]) -> bool:
    expected = _bundle_profile(bundle)
    keys = (
        "service_provider",
        "repo_root",
        "runtime_root",
        "accounts",
        "markets",
        "config_paths",
        "services",
        "env_file",
        "deploy_user",
        "deploy_home",
        "auto_upgrade",
        "feishu_ws",
        "restart",
    )
    return {key: profile.get(key) for key in keys if key in profile or key in expected} != {
        key: expected.get(key) for key in keys if key in profile or key in expected
    }


def _required_units(provider: str, expected_services: list[str]) -> list[str]:
    required = SYSTEMD_REQUIRED_MAINTENANCE_UNITS if provider == "systemd" else LAUNCHD_REQUIRED_MAINTENANCE_UNITS
    expected = set(expected_services)
    return sorted(unit for unit in required if unit in expected)


def _drift_summary(
    *,
    expected_services: list[str],
    missing_required_units: list[str],
    missing_profile_units: list[str],
    missing_installed_units: list[str],
    extra_profile_units: list[str],
    extra_installed_units: list[str],
    mismatched_units: list[str],
    profile_content_changed: bool,
) -> dict[str, Any]:
    warning_count = sum(
        1
        for values in (
            missing_profile_units,
            missing_installed_units,
            extra_profile_units,
            extra_installed_units,
            mismatched_units,
        )
        if values
    )
    if profile_content_changed:
        warning_count += 1
    error_count = 1 if missing_required_units else 0
    status = "error" if error_count else ("warn" if warning_count else "ok")
    return {
        "ok": status == "ok",
        "status": status,
        "error_count": error_count,
        "warning_count": warning_count,
        "expected_count": len(expected_services),
        "missing_required_units": missing_required_units,
        "missing_profile_count": len(missing_profile_units),
        "missing_installed_count": len(missing_installed_units),
        "extra_profile_count": len(extra_profile_units),
        "extra_installed_count": len(extra_installed_units),
        "mismatched_count": len(mismatched_units),
        "profile_content_changed": bool(profile_content_changed),
    }


def _summary_with_apply_errors(summary: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    out = dict(summary)
    out["ok"] = False
    out["status"] = "error"
    out["error_count"] = int(out.get("error_count") or 0) + len(errors)
    out["apply_error_count"] = len(errors)
    return out


def _manual_actions(
    *,
    provider: str,
    missing_installed_units: list[str],
    mismatched_units: list[str],
    profile_path: Path,
) -> list[str]:
    actions: list[str] = []
    if missing_installed_units:
        actions.append(f"./om service drift --profile-path {profile_path} --confirm")
    if provider == "systemd":
        long_running = [name for name in missing_installed_units if name.endswith(".service") and ("trade-intake" in name or "feishu-ws" in name)]
        actions.extend(f"manual_enable_long_running_service: sudo systemctl enable --now {name}" for name in long_running)
        actions.extend(f"manual_review_unit_content: sudo systemctl cat {name}" for name in mismatched_units)
    return actions


def _apply_missing_service_drift(
    ctx: dict[str, Any],
    *,
    before: dict[str, Any],
    operations: list[dict[str, Any]],
    run_cmd: Callable[..., Any],
) -> dict[str, Any]:
    provider = str(ctx["provider"])
    if provider != "systemd":
        return {
            "changed": False,
            "errors": [f"confirmed service drift apply is not implemented for provider: {provider}"],
            "written_units": [],
            "enabled_timers": [],
            "profile_written": False,
        }
    bundle = _expected_bundle_from_profile(
        ctx["profile"],
        provider=provider,
        repo_root=ctx["repo_root"],
        runtime_root=ctx["runtime_root"],
    )
    expected_files = _expected_install_files(bundle, provider=provider)
    missing = set(before.get("missing_installed_units") or [])
    written_units: list[str] = []
    errors: list[str] = []
    for name in sorted(missing):
        item = expected_files.get(name)
        if not item:
            continue
        path = _install_path(item, provider=provider, ctx=ctx)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(item.get("content") or ""), encoding="utf-8")
        except Exception as exc:
            errors.append(f"write {path}: {type(exc).__name__}: {exc}")
            continue
        written_units.append(name)
        operations.append({"operation": "write_unit", "path": str(path), "unit": name, "ok": True})

    profile_written = False
    if before.get("profile_content_changed"):
        try:
            ctx["profile_path"].parent.mkdir(parents=True, exist_ok=True)
            ctx["profile_path"].write_text(_expected_profile_content(bundle), encoding="utf-8")
        except Exception as exc:
            errors.append(f"write {ctx['profile_path']}: {type(exc).__name__}: {exc}")
        else:
            profile_written = True
            ctx["profile"] = _bundle_profile(bundle)
            operations.append({"operation": "write_profile", "path": str(ctx["profile_path"]), "ok": True})

    enabled_timers: list[str] = []
    if written_units:
        result = _run_systemctl(ctx, ["daemon-reload"], run_cmd=run_cmd)
        operations.append(result)
        if not result.get("ok"):
            errors.append(f"daemon-reload: {result.get('stderr') or result.get('stdout') or result.get('error') or result.get('returncode')}")
    for name in sorted(item for item in missing if item.endswith(".timer")):
        result = _run_systemctl(ctx, ["enable", "--now", name], run_cmd=run_cmd)
        operations.append(result)
        if result.get("ok"):
            enabled_timers.append(name)
        else:
            errors.append(f"enable {name}: {result.get('stderr') or result.get('stdout') or result.get('returncode')}")

    return {
        "changed": bool(written_units or profile_written or enabled_timers),
        "errors": errors,
        "written_units": written_units,
        "enabled_timers": enabled_timers,
        "profile_written": profile_written,
    }


def _run_systemctl(ctx: dict[str, Any], args: list[str], *, run_cmd: Callable[..., Any]) -> dict[str, Any]:
    command = [*_systemctl_prefix(ctx["profile"]), *args]
    try:
        proc = run_cmd(command, capture_output=True, text=True, timeout=60, check=False)
    except Exception as exc:
        return {
            "operation": "systemctl",
            "command": command,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    stdout = str(getattr(proc, "stdout", "") or "")
    stderr = str(getattr(proc, "stderr", "") or "")
    rc = int(getattr(proc, "returncode", 1))
    return {
        "operation": "systemctl",
        "command": command,
        "ok": rc == 0,
        "returncode": rc,
        "stdout": stdout[-2000:],
        "stderr": stderr[-2000:],
    }


def _systemctl_prefix(profile: dict[str, Any]) -> list[str]:
    restart = profile.get("restart")
    restart_profile = restart if isinstance(restart, dict) else {}
    prefix = restart_profile.get("command_prefix")
    if isinstance(prefix, list) and prefix:
        parts = [str(item).strip() for item in prefix if str(item).strip()]
        if parts:
            return parts
    deploy_user = str(profile.get("deploy_user") or "").strip()
    if deploy_user and deploy_user != "root":
        return ["sudo", "-n", "systemctl"]
    return ["systemctl"]


__all__ = [
    "service_drift",
    "service_drift_status",
]
