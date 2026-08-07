from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from src.application.service_deploy import (
    DEFAULT_FEISHU_AGENT_CREDENTIAL_ENV_FILE,
    DEFAULT_FEISHU_AGENT_CREDENTIAL_HELPER,
    DEFAULT_FEISHU_AGENT_CREDENTIAL_STORE,
    DEFAULT_FEISHU_HOLDINGS_CREDENTIAL_STORE,
    DEFAULT_ACCOUNTS,
    DEFAULT_MARKETS,
    FEISHU_AGENT_CREDENTIAL_DROPIN,
    FEISHU_AGENT_CREDENTIAL_SERVICE,
    load_service_profile,
    render_service_bundle,
    resolve_strategy_lab_recorder_endpoint_matches,
)


SYSTEMD_REQUIRED_MAINTENANCE_UNITS = (
    FEISHU_AGENT_CREDENTIAL_SERVICE,
    "options-monitor-position-advice-promotion.timer",
    "options-monitor-projection-verify.timer",
)
LAUNCHD_REQUIRED_MAINTENANCE_UNITS = (
    "com.options-monitor.position-advice-promotion",
    "com.options-monitor.projection-verify",
)
LEGACY_STRATEGY_LAB_RECORDER_HOST = "127.0.0.1"
LEGACY_STRATEGY_LAB_RECORDER_PORT = 11111
LEGACY_FEISHU_AGENT_CREDENTIAL_UNIT_PATH = Path(
    "/usr/lib/systemd/system/options-monitor-feishu-agent-credential.service"
)


def service_drift(
    *,
    repo_root: str | Path | None = None,
    runtime_root: str | Path | None = None,
    profile_path: str | Path | None = None,
    profile: dict[str, Any] | None = None,
    confirm: bool = False,
    systemd_unit_root: str | Path | None = None,
    run_cmd: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Compare current-release expected services with profile and installed unit files.

    Dry-run is the default. Confirmed apply writes missing or changed unit files
    and profile-owned support files, repairs expected timer and credential
    activation, retires extra managed assets, and refreshes the service profile.
    Long-running expected services are not enabled or restarted here.
    """

    initial = _load_profile_and_paths(
        repo_root=repo_root,
        runtime_root=runtime_root,
        profile_path=profile_path,
        profile=profile,
        systemd_unit_root=systemd_unit_root,
    )
    command_runner = run_cmd or subprocess.run
    initial["run_cmd"] = command_runner
    initial["run_cmd_injected"] = run_cmd is not None
    before = _build_drift(initial)
    operations: list[dict[str, Any]] = []
    apply_errors: list[str] = []
    changed = False

    if confirm and before.get("supported"):
        apply_result = _apply_service_drift(initial, before=before, operations=operations, run_cmd=command_runner)
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


def _effective_profile_with_legacy_feishu_credential(
    profile: dict[str, Any],
    *,
    ctx: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if str(ctx.get("provider") or "").strip().lower() != "systemd":
        return profile, []
    if "feishu_agent_credential" in profile:
        return profile, []

    profile_services = set(_service_names_from_profile(profile))
    inferred_from_profile = FEISHU_AGENT_CREDENTIAL_SERVICE in profile_services
    unit_path = LEGACY_FEISHU_AGENT_CREDENTIAL_UNIT_PATH
    dropin_root = Path(ctx["systemd_unit_root"])
    legacy_dropins = sorted(
        path
        for path in dropin_root.glob(
            f"options-monitor-*.service.d/{FEISHU_AGENT_CREDENTIAL_DROPIN}"
        )
        if path.is_file()
    ) if dropin_root.exists() else []
    inferred_from_legacy = unit_path.is_file() and bool(legacy_dropins)
    if not inferred_from_profile and not inferred_from_legacy:
        return profile, []

    effective = dict(profile)
    effective["feishu_agent_credential"] = {
        "enabled": True,
        "service_name": FEISHU_AGENT_CREDENTIAL_SERVICE,
        "helper_path": str(DEFAULT_FEISHU_AGENT_CREDENTIAL_HELPER),
        "agent_store": str(DEFAULT_FEISHU_AGENT_CREDENTIAL_STORE),
        "holdings_store": str(DEFAULT_FEISHU_HOLDINGS_CREDENTIAL_STORE),
        "runtime_env_file": str(DEFAULT_FEISHU_AGENT_CREDENTIAL_ENV_FILE),
    }
    return effective, [
        {
            "code": "legacy_feishu_agent_credential_inferred",
            "source": "profile_service" if inferred_from_profile else "installed_assets",
            "unit_path": str(unit_path),
            "dropin_count": len(legacy_dropins),
        }
    ]


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
            "expected_managed_files": [],
            "installed_managed_files": [],
            "missing_managed_files": [],
            "extra_managed_files": [],
            "mismatched_managed_files": [],
            "mode_mismatched_managed_files": [],
            "activation_states": {},
            "active_states": {},
            "execution_states": {},
            "activation_drift_units": [],
            "execution_drift_units": [],
            "required_units": [],
            "missing_required_units": [],
            "profile_content_changed": False,
            "manual_actions": [],
            "summary": {"ok": True, "status": "skipped", "error_count": 0, "warning_count": 0},
        }
    effective_profile, profile_compatibility_warnings = _effective_profile_with_legacy_feishu_credential(
        profile,
        ctx=ctx,
    )
    ctx["effective_profile"] = effective_profile
    try:
        bundle = _expected_bundle_from_profile(
            effective_profile,
            provider=provider,
            repo_root=ctx["repo_root"],
            runtime_root=ctx["runtime_root"],
        )
    except ValueError as exc:
        error = str(exc)
        if not error.startswith("strategy_lab_recorder_binding_"):
            raise
        return {
            "checked": True,
            "supported": False,
            "reason": "strategy_lab_recorder_binding_invalid",
            "error": error,
            "provider": provider,
            "profile_path": str(ctx["profile_path"]),
            "repo_root": str(ctx["repo_root"]),
            "runtime_root": str(ctx["runtime_root"]),
            "systemd_unit_root": str(ctx["systemd_unit_root"]) if provider == "systemd" else None,
            "compatibility_warnings": [],
            "summary": {
                "ok": False,
                "status": "error",
                "error_count": 1,
                "warning_count": 0,
                "missing_required_units": [],
            },
        }
    compatibility_warnings_raw = bundle.get("compatibility_warnings")
    compatibility_warnings = list(profile_compatibility_warnings)
    if isinstance(compatibility_warnings_raw, list):
        compatibility_warnings.extend(
            dict(item)
            for item in compatibility_warnings_raw
            if isinstance(item, dict)
        )
    expected_files = _expected_install_files(bundle, provider=provider)
    expected_managed_files = _expected_managed_files(bundle, provider=provider)
    installed_managed_files = _installed_managed_files(
        provider=provider,
        expected_files=expected_managed_files,
        ctx=ctx,
    )
    expected_services = _service_names_from_profile(_bundle_profile(bundle))
    profile_services = _service_names_from_profile(profile)
    installed_units = _installed_units(provider=provider, expected_files=expected_files, ctx=ctx)
    missing_profile_units = sorted(set(expected_services) - set(profile_services))
    extra_profile_units = sorted(set(profile_services) - set(expected_services))
    missing_installed_units = sorted(set(expected_files) - set(installed_units))
    extra_installed_units = sorted(set(installed_units) - set(expected_files))
    extra_managed_files = sorted(
        set(installed_managed_files) - set(expected_managed_files)
    )
    mismatched_units = _mismatched_units(provider=provider, expected_files=expected_files, ctx=ctx)
    (
        missing_managed_files,
        mismatched_managed_files,
        mode_mismatched_managed_files,
    ) = _managed_file_status(
        provider=provider,
        expected_files=expected_managed_files,
        ctx=ctx,
    )
    activation_states = _activation_states(
        provider=provider,
        expected_files=expected_files,
        installed_units=installed_units,
        ctx=ctx,
    )
    active_states = _active_states(
        provider=provider,
        expected_files=expected_files,
        installed_units=installed_units,
        ctx=ctx,
    )
    execution_states = _execution_states(
        provider=provider,
        expected_files=expected_files,
        installed_units=installed_units,
        ctx=ctx,
    )
    activation_drift_units = sorted(
        name
        for name in set(activation_states) | set(active_states)
        if activation_states.get(name) in {"disabled", "masked"}
        or active_states.get(name) in {"inactive", "failed", "deactivating"}
    )
    execution_drift_units = sorted(
        name
        for name, state in execution_states.items()
        if state != "success"
    )
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
        activation_drift_units=activation_drift_units,
        execution_drift_units=execution_drift_units,
        extra_installed_units=extra_installed_units,
        extra_managed_files=extra_managed_files,
        missing_managed_files=missing_managed_files,
        mismatched_managed_files=mismatched_managed_files,
        mode_mismatched_managed_files=mode_mismatched_managed_files,
        profile_path=ctx["profile_path"],
    )
    summary = _drift_summary(
        expected_services=expected_services,
        missing_required_units=missing_required_units,
        missing_profile_units=missing_profile_units,
        missing_installed_units=missing_installed_units,
        extra_profile_units=extra_profile_units,
        extra_installed_units=extra_installed_units,
        extra_managed_files=extra_managed_files,
        mismatched_units=mismatched_units,
        missing_managed_files=missing_managed_files,
        mismatched_managed_files=mismatched_managed_files,
        mode_mismatched_managed_files=mode_mismatched_managed_files,
        activation_drift_units=activation_drift_units,
        execution_drift_units=execution_drift_units,
        profile_content_changed=profile_content_changed,
        compatibility_warning_count=len(compatibility_warnings),
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
        "expected_managed_files": sorted(expected_managed_files),
        "installed_managed_files": installed_managed_files,
        "missing_managed_files": missing_managed_files,
        "extra_managed_files": extra_managed_files,
        "mismatched_managed_files": mismatched_managed_files,
        "mode_mismatched_managed_files": mode_mismatched_managed_files,
        "activation_states": activation_states,
        "active_states": active_states,
        "execution_states": execution_states,
        "activation_drift_units": activation_drift_units,
        "execution_drift_units": execution_drift_units,
        "required_units": required_units,
        "missing_required_units": missing_required_units,
        "profile_content_changed": profile_content_changed,
        "compatibility_warnings": compatibility_warnings,
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
    opend_raw = profile.get("opend")
    opend = opend_raw if isinstance(opend_raw, dict) else {}
    feishu_ws_raw = profile.get("feishu_ws")
    feishu_ws = feishu_ws_raw if isinstance(feishu_ws_raw, dict) else {}
    wechat_clawbot_raw = profile.get("wechat_clawbot")
    wechat_clawbot = wechat_clawbot_raw if isinstance(wechat_clawbot_raw, dict) else {}
    strategy_lab_recorder_raw = profile.get("strategy_lab_recorder")
    strategy_lab_recorder = strategy_lab_recorder_raw if isinstance(strategy_lab_recorder_raw, dict) else {}
    quality_monitoring_raw = profile.get("quality_monitoring")
    quality_monitoring = quality_monitoring_raw if isinstance(quality_monitoring_raw, dict) else {}
    feishu_agent_credential_raw = profile.get("feishu_agent_credential")
    feishu_agent_credential = (
        feishu_agent_credential_raw
        if isinstance(feishu_agent_credential_raw, dict)
        else {}
    )
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
    include_wechat_clawbot = bool(
        wechat_clawbot.get("enabled")
        or "options-monitor-wechat-clawbot.service" in services
        or "com.options-monitor.wechat-clawbot" in services
    )
    include_opend = bool(
        opend.get("enabled")
        or "options-monitor-opend.service" in services
        or "com.options-monitor.opend" in services
    )
    include_strategy_lab_recorder = bool(
        strategy_lab_recorder.get("enabled")
        or "options-monitor-strategy-lab-build.timer" in services
        or "options-monitor-strategy-lab-sample.timer" in services
        or "options-monitor-strategy-lab-settle.timer" in services
        or "com.options-monitor.strategy-lab-build" in services
        or "com.options-monitor.strategy-lab-sample" in services
        or "com.options-monitor.strategy-lab-settle" in services
    )
    include_quality_monitoring = bool(
        quality_monitoring.get("enabled")
        or any(name.startswith("options-monitor-quality-") for name in services)
    )
    include_feishu_agent_credential = bool(
        feishu_agent_credential.get("enabled")
        or FEISHU_AGENT_CREDENTIAL_SERVICE in services
    )
    market_values = _profile_markets(profile)
    feishu_ws_config_key = str(feishu_ws.get("config_key") or "").strip() or None
    if include_feishu_ws and feishu_ws_config_key is None and len([market for market in market_values if market in {"us", "hk"}]) != 1:
        include_feishu_ws = False
    wechat_clawbot_config_key = str(wechat_clawbot.get("config_key") or "").strip() or None
    wechat_clawbot_allowed_senders = str(wechat_clawbot.get("allowed_senders") or "").strip() or None
    wechat_clawbot_allowed_senders_configured = bool(
        wechat_clawbot_allowed_senders
        or wechat_clawbot.get("allowed_senders_configured")
        or str(wechat_clawbot.get("allowed_senders_source") or "").strip() == "config_yaml"
    )
    if include_wechat_clawbot and wechat_clawbot_config_key is None and len([market for market in market_values if market in {"us", "hk"}]) != 1:
        include_wechat_clawbot = False
    if include_wechat_clawbot and not wechat_clawbot_allowed_senders_configured:
        include_wechat_clawbot = False
    accounts = _profile_accounts(profile)
    recorder_source = str(strategy_lab_recorder.get("source") or "opend").strip().lower() or "opend"
    binding_raw = strategy_lab_recorder.get("binding")
    binding = binding_raw if isinstance(binding_raw, dict) else {}
    recorder_account = str(binding.get("account") or "").strip().lower() or None
    opend_root, opend_executable = _profile_opend_render_values(opend)
    render_kwargs: dict[str, Any] = {
        "target": provider,
        "repo_root": repo_root,
        "runtime_root": runtime_root,
        "accounts": accounts,
        "markets": market_values,
        "config_paths": {str(key): str(value) for key, value in config_paths.items() if str(value or "").strip()},
        "config_yaml": _profile_config_yaml(profile),
        "env_file": profile.get("env_file"),
        "deploy_user": profile.get("deploy_user"),
        "deploy_home": profile.get("deploy_home"),
        "use_default_deploy_user": False,
        "include_auto_upgrade": include_auto_upgrade,
        "include_opend": include_opend,
        "opend_root": opend_root,
        "opend_executable": opend_executable,
        "include_feishu_ws": include_feishu_ws,
        "feishu_ws_config_key": feishu_ws_config_key,
        "include_wechat_clawbot": include_wechat_clawbot,
        "wechat_clawbot_config_key": wechat_clawbot_config_key,
        "wechat_clawbot_label": str(wechat_clawbot.get("label") or "default"),
        "wechat_clawbot_allowed_senders": wechat_clawbot_allowed_senders,
        "include_strategy_lab_recorder": include_strategy_lab_recorder,
        "strategy_lab_recorder_source": recorder_source,
        "strategy_lab_recorder_max_datasets": int(strategy_lab_recorder.get("max_datasets") or 5),
        "strategy_lab_recorder_mark_stale_hours": int(strategy_lab_recorder.get("mark_stale_hours") or 2),
        "include_quality_monitoring": include_quality_monitoring,
        "include_feishu_agent_credential": include_feishu_agent_credential,
        "feishu_agent_credential_helper_path": (
            feishu_agent_credential.get("helper_path")
            or DEFAULT_FEISHU_AGENT_CREDENTIAL_HELPER
        ),
        "feishu_agent_credential_store": (
            feishu_agent_credential.get("agent_store")
            or DEFAULT_FEISHU_AGENT_CREDENTIAL_STORE
        ),
        "feishu_holdings_credential_store": (
            feishu_agent_credential.get("holdings_store")
            or DEFAULT_FEISHU_HOLDINGS_CREDENTIAL_STORE
        ),
        "feishu_agent_credential_env_file": (
            feishu_agent_credential.get("runtime_env_file")
            or DEFAULT_FEISHU_AGENT_CREDENTIAL_ENV_FILE
        ),
        "include_content": True,
    }
    compatibility_warnings: list[dict[str, Any]] = []
    if include_strategy_lab_recorder and recorder_source == "opend" and recorder_account is None:
        try:
            matching_accounts = resolve_strategy_lab_recorder_endpoint_matches(
                repo_root=repo_root,
                runtime_root=runtime_root,
                accounts=accounts,
                markets=market_values,
                config_paths=render_kwargs["config_paths"],
                config_yaml=render_kwargs["config_yaml"],
                include_auto_upgrade=include_auto_upgrade,
                host=LEGACY_STRATEGY_LAB_RECORDER_HOST,
                port=LEGACY_STRATEGY_LAB_RECORDER_PORT,
            )
        except ValueError as exc:
            raise ValueError(f"strategy_lab_recorder_binding_invalid: {exc}") from exc
        if len(matching_accounts) != 1:
            matches = ",".join(matching_accounts) if matching_accounts else "none"
            raise ValueError(
                "strategy_lab_recorder_binding_unresolved: legacy endpoint "
                f"{LEGACY_STRATEGY_LAB_RECORDER_HOST}:{LEGACY_STRATEGY_LAB_RECORDER_PORT} "
                f"matched {matches}; expected exactly one selected Futu account"
            )
        recorder_account = matching_accounts[0]
        compatibility_warnings.append({
            "code": "legacy_strategy_lab_recorder_binding_inferred",
            "account": recorder_account,
            "host": LEGACY_STRATEGY_LAB_RECORDER_HOST,
            "port": LEGACY_STRATEGY_LAB_RECORDER_PORT,
        })

    try:
        bundle = render_service_bundle(
            **render_kwargs,
            strategy_lab_recorder_account=recorder_account,
        )
    except ValueError as exc:
        if include_strategy_lab_recorder and recorder_source == "opend":
            raise ValueError(f"strategy_lab_recorder_binding_invalid: {exc}") from exc
        raise
    if compatibility_warnings:
        bundle["compatibility_warnings"] = compatibility_warnings
    return bundle


def _profile_opend_render_values(opend: dict[str, Any]) -> tuple[str | None, str | None]:
    services_raw = opend.get("services")
    services = [item for item in services_raw if isinstance(item, dict)] if isinstance(services_raw, list) else []
    if not services or not any(str(item.get("account") or "").strip() for item in services):
        root = str(opend.get("root") or "").strip() or None
        executable = str(opend.get("executable") or "").strip() or None
        return root, executable

    relative_executables: list[str] = []
    absolute_executables: list[str] = []
    for item in services:
        root_raw = str(item.get("root") or "").strip()
        executable_raw = str(item.get("executable") or "").strip()
        if not root_raw or not executable_raw:
            continue
        root_path = Path(root_raw)
        executable_path = Path(executable_raw)
        absolute_executables.append(executable_raw)
        try:
            relative_executables.append(str(executable_path.relative_to(root_path)))
        except ValueError:
            relative_executables.append("")
    if relative_executables and all(relative_executables) and len(set(relative_executables)) == 1:
        return None, relative_executables[0]
    if absolute_executables and len(absolute_executables) == len(services) and len(set(absolute_executables)) == 1:
        return None, absolute_executables[0]
    return None, None


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


def _expected_managed_files(bundle: dict[str, Any], *, provider: str) -> dict[str, dict[str, Any]]:
    if provider != "systemd":
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in bundle.get("files", []):
        if not isinstance(item, dict) or item.get("kind") not in {
            "systemd_dropin",
            "systemd_executable",
        }:
            continue
        install_path = str(item.get("install_path") or "").strip()
        if install_path:
            out[install_path] = item
    return out


def _installed_managed_files(
    *,
    provider: str,
    expected_files: dict[str, dict[str, Any]],
    ctx: dict[str, Any],
) -> list[str]:
    if provider != "systemd":
        return []
    installed = {
        key
        for key, item in expected_files.items()
        if _install_path(item, provider=provider, ctx=ctx).is_file()
    }
    root = Path(ctx["systemd_unit_root"])
    if root.exists():
        for path in root.glob(
            f"options-monitor-*.service.d/{FEISHU_AGENT_CREDENTIAL_DROPIN}"
        ):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            installed.add(str(Path("/etc/systemd/system") / relative))
    return sorted(installed)


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


def _profile_config_yaml(profile: dict[str, Any]) -> str | None:
    raw_authoring = profile.get("config_authoring")
    authoring = raw_authoring if isinstance(raw_authoring, dict) else {}
    if str(authoring.get("source") or "").strip().lower() == "yaml":
        config_yaml = str(authoring.get("config_yaml") or "").strip()
        if config_yaml:
            return config_yaml
    return None


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
        if item.get("kind") == "systemd_executable":
            return raw
        if item.get("kind") == "systemd_dropin":
            live_root = Path("/etc/systemd/system")
            try:
                relative = raw.relative_to(live_root)
            except ValueError:
                return raw
            return Path(ctx["systemd_unit_root"]) / relative
        return Path(ctx["systemd_unit_root"]) / raw.name
    return raw


def _managed_path_from_key(key: str, *, ctx: dict[str, Any]) -> Path:
    raw = Path(key).expanduser()
    live_root = Path("/etc/systemd/system")
    try:
        relative = raw.relative_to(live_root)
    except ValueError:
        return raw
    return Path(ctx["systemd_unit_root"]) / relative


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


def _managed_file_status(
    *,
    provider: str,
    expected_files: dict[str, dict[str, Any]],
    ctx: dict[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    missing: list[str] = []
    mismatched: list[str] = []
    mode_mismatched: list[str] = []
    for key, item in expected_files.items():
        path = _install_path(item, provider=provider, ctx=ctx)
        if not path.exists() or not path.is_file():
            missing.append(key)
            continue
        try:
            actual = path.read_text(encoding="utf-8")
        except Exception:
            mismatched.append(key)
            continue
        if actual != str(item.get("content") or ""):
            mismatched.append(key)
        expected_mode = item.get("mode")
        if expected_mode is not None:
            try:
                actual_mode = path.stat().st_mode & 0o777
            except OSError:
                mode_mismatched.append(key)
            else:
                if actual_mode != int(expected_mode):
                    mode_mismatched.append(key)
    return sorted(missing), sorted(mismatched), sorted(mode_mismatched)


def _activation_states(
    *,
    provider: str,
    expected_files: dict[str, dict[str, Any]],
    installed_units: list[str],
    ctx: dict[str, Any],
) -> dict[str, str]:
    if provider != "systemd" or not _live_systemctl_enabled(ctx):
        return {}
    installed = set(installed_units)
    states: dict[str, str] = {}
    for name in sorted(expected_files):
        if (
            not name.endswith(".timer")
            and name != FEISHU_AGENT_CREDENTIAL_SERVICE
        ) or name not in installed:
            continue
        result = _run_systemctl(ctx, ["is-enabled", name], run_cmd=ctx.get("run_cmd") or subprocess.run)
        stdout = str(result.get("stdout") or "").strip().lower()
        stderr = str(result.get("stderr") or "").strip().lower()
        state = (stdout or stderr).splitlines()[0].strip() if (stdout or stderr) else ""
        if state not in {"enabled", "enabled-runtime", "disabled", "masked", "masked-runtime", "static", "indirect", "generated", "transient"}:
            state = "unknown"
        if state == "masked-runtime":
            state = "masked"
        states[name] = state
    return states


def _active_states(
    *,
    provider: str,
    expected_files: dict[str, dict[str, Any]],
    installed_units: list[str],
    ctx: dict[str, Any],
) -> dict[str, str]:
    if provider != "systemd" or not _live_systemctl_enabled(ctx):
        return {}
    installed = set(installed_units)
    states: dict[str, str] = {}
    for name in sorted(expected_files):
        if not name.endswith(".timer") or name not in installed:
            continue
        result = _run_systemctl(ctx, ["is-active", name], run_cmd=ctx.get("run_cmd") or subprocess.run)
        stdout = str(result.get("stdout") or "").strip().lower()
        stderr = str(result.get("stderr") or "").strip().lower()
        state = (stdout or stderr).splitlines()[0].strip() if (stdout or stderr) else ""
        if state not in {
            "active",
            "reloading",
            "inactive",
            "failed",
            "activating",
            "deactivating",
            "maintenance",
            "refreshing",
        }:
            state = "unknown"
        states[name] = state
    return states


def _execution_states(
    *,
    provider: str,
    expected_files: dict[str, dict[str, Any]],
    installed_units: list[str],
    ctx: dict[str, Any],
) -> dict[str, str]:
    if provider != "systemd" or not _live_systemctl_enabled(ctx):
        return {}
    installed = set(installed_units)
    if (
        FEISHU_AGENT_CREDENTIAL_SERVICE not in expected_files
        or FEISHU_AGENT_CREDENTIAL_SERVICE not in installed
    ):
        return {}
    result = _run_systemctl(
        ctx,
        [
            "show",
            "--property=Result",
            "--value",
            FEISHU_AGENT_CREDENTIAL_SERVICE,
        ],
        run_cmd=ctx.get("run_cmd") or subprocess.run,
    )
    if not result.get("ok"):
        return {FEISHU_AGENT_CREDENTIAL_SERVICE: "query_failed"}
    state = str(result.get("stdout") or "").strip().lower().splitlines()
    value = state[0].strip() if state else ""
    return {FEISHU_AGENT_CREDENTIAL_SERVICE: value or "unknown"}


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
        "opend",
        "feishu_ws",
        "wechat_clawbot",
        "strategy_lab_recorder",
        "quality_monitoring",
        "position_advice_promotion",
        "feishu_agent_credential",
        "restart",
    )
    return {key: profile.get(key) for key in keys if key in profile or key in expected} != {
        key: expected.get(key) for key in keys if key in profile or key in expected
    }


def _live_systemctl_enabled(ctx: dict[str, Any]) -> bool:
    """Use host systemctl only for the live unit root or an injected runner."""

    return bool(ctx.get("run_cmd_injected")) or Path(ctx["systemd_unit_root"]) == Path("/etc/systemd/system")


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
    extra_managed_files: list[str],
    mismatched_units: list[str],
    missing_managed_files: list[str],
    mismatched_managed_files: list[str],
    mode_mismatched_managed_files: list[str],
    activation_drift_units: list[str],
    execution_drift_units: list[str],
    profile_content_changed: bool,
    compatibility_warning_count: int = 0,
) -> dict[str, Any]:
    warning_count = sum(
        1
        for values in (
            missing_profile_units,
            missing_installed_units,
            extra_profile_units,
            extra_installed_units,
            extra_managed_files,
            mismatched_units,
            missing_managed_files,
            mismatched_managed_files,
            mode_mismatched_managed_files,
            activation_drift_units,
            execution_drift_units,
        )
        if values
    )
    if profile_content_changed:
        warning_count += 1
    warning_count += max(0, int(compatibility_warning_count))
    error_count = (
        int(bool(missing_required_units))
        + int(bool(activation_drift_units))
        + int(bool(missing_managed_files))
        + int(bool(mismatched_managed_files))
        + int(bool(mode_mismatched_managed_files))
        + int(bool(extra_managed_files))
        + int(bool(execution_drift_units))
    )
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
        "extra_managed_file_count": len(extra_managed_files),
        "mismatched_count": len(mismatched_units),
        "managed_file_drift_count": (
            len(missing_managed_files)
            + len(mismatched_managed_files)
            + len(mode_mismatched_managed_files)
        ),
        "missing_managed_file_count": len(missing_managed_files),
        "mismatched_managed_file_count": len(mismatched_managed_files),
        "mode_mismatched_managed_file_count": len(mode_mismatched_managed_files),
        "activation_drift_count": len(activation_drift_units),
        "execution_drift_count": len(execution_drift_units),
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
    activation_drift_units: list[str],
    execution_drift_units: list[str],
    extra_installed_units: list[str],
    extra_managed_files: list[str],
    missing_managed_files: list[str],
    mismatched_managed_files: list[str],
    mode_mismatched_managed_files: list[str],
    profile_path: Path,
) -> list[str]:
    actions: list[str] = []
    if (
        missing_installed_units
        or mismatched_units
        or activation_drift_units
        or execution_drift_units
        or extra_installed_units
        or extra_managed_files
        or missing_managed_files
        or mismatched_managed_files
        or mode_mismatched_managed_files
    ):
        actions.append(f"./om service drift --profile-path {profile_path} --confirm")
    if provider == "systemd":
        long_running = [
            name
            for name in missing_installed_units
            if name.endswith(".service")
            and ("trade-intake" in name or "feishu-ws" in name or "wechat-clawbot" in name)
        ]
        actions.extend(f"manual_enable_long_running_service: sudo systemctl enable --now {name}" for name in long_running)
        actions.extend(f"manual_review_unit_content: sudo systemctl cat {name}" for name in mismatched_units)
        actions.extend(
            f"manual_enable_timer: sudo systemctl enable --now {name}"
            for name in activation_drift_units
            if name.endswith(".timer")
        )
        actions.extend(
            f"manual_enable_service: sudo systemctl enable --now {name}"
            for name in activation_drift_units
            if not name.endswith(".timer")
        )
        actions.extend(f"manual_restart_failed_oneshot: sudo systemctl start {name}" for name in execution_drift_units)
        actions.extend(f"manual_retire_unit: sudo systemctl disable --now {name}" for name in extra_installed_units)
        actions.extend(
            f"manual_retire_managed_file: sudo rm -- {path}"
            for path in extra_managed_files
        )
    return actions


def _apply_service_drift(
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
            "written_managed_files": [],
            "retired_managed_files": [],
            "enabled_timers": [],
            "enabled_services": [],
            "started_services": [],
            "restarted_timers": [],
            "retired_units": [],
            "profile_written": False,
        }
    bundle = _expected_bundle_from_profile(
        ctx.get("effective_profile") or ctx["profile"],
        provider=provider,
        repo_root=ctx["repo_root"],
        runtime_root=ctx["runtime_root"],
    )
    expected_files = _expected_install_files(bundle, provider=provider)
    expected_managed_files = _expected_managed_files(bundle, provider=provider)
    missing = set(before.get("missing_installed_units") or [])
    mismatched = set(before.get("mismatched_units") or [])
    units_to_write = missing | mismatched
    missing_managed = set(before.get("missing_managed_files") or [])
    mismatched_managed = set(before.get("mismatched_managed_files") or [])
    mode_mismatched_managed = set(before.get("mode_mismatched_managed_files") or [])
    managed_files_to_write = (
        missing_managed | mismatched_managed | mode_mismatched_managed
    )
    extra_managed = set(before.get("extra_managed_files") or [])
    written_units: list[str] = []
    written_managed_files: list[str] = []
    retired_managed_files: list[str] = []
    errors: list[str] = []
    for name in sorted(units_to_write):
        item = expected_files.get(name)
        if not item:
            continue
        path = _install_path(item, provider=provider, ctx=ctx)
        write_result = _write_text_with_sudo_fallback(
            path,
            str(item.get("content") or ""),
            ctx=ctx,
            run_cmd=run_cmd,
        )
        operations.append({**write_result, "unit": name})
        if not write_result.get("ok"):
            errors.append(f"write {path}: {write_result.get('error') or write_result.get('stderr') or write_result.get('returncode')}")
            continue
        written_units.append(name)

    for key in sorted(managed_files_to_write):
        item = expected_managed_files.get(key)
        if not item:
            continue
        path = _install_path(item, provider=provider, ctx=ctx)
        write_result = _write_text_with_sudo_fallback(
            path,
            str(item.get("content") or ""),
            ctx=ctx,
            run_cmd=run_cmd,
            mode=(int(item["mode"]) if item.get("mode") is not None else None),
        )
        operations.append({**write_result, "managed_file": key})
        if not write_result.get("ok"):
            errors.append(
                f"write {path}: "
                f"{write_result.get('error') or write_result.get('stderr') or write_result.get('returncode')}"
            )
            continue
        written_managed_files.append(key)

    for key in sorted(extra_managed):
        path = _managed_path_from_key(key, ctx=ctx)
        delete_result = _delete_unit_with_sudo_fallback(path, run_cmd=run_cmd)
        operations.append({**delete_result, "managed_file": key})
        if not delete_result.get("ok"):
            errors.append(
                f"delete {path}: "
                f"{delete_result.get('error') or delete_result.get('stderr') or delete_result.get('returncode')}"
            )
            continue
        retired_managed_files.append(key)

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
    enabled_services: list[str] = []
    started_services: list[str] = []
    restarted_timers: list[str] = []
    retired_units: list[str] = []
    activation_drift = set(before.get("activation_drift_units") or [])
    execution_drift = set(before.get("execution_drift_units") or [])
    extra_installed = set(before.get("extra_installed_units") or [])
    live_systemctl = _live_systemctl_enabled(ctx)
    systemd_definition_changed = bool(
        written_units
        or any(
            expected_managed_files.get(key, {}).get("kind") == "systemd_dropin"
            for key in written_managed_files
        )
        or bool(retired_managed_files)
    )
    if systemd_definition_changed and live_systemctl:
        result = _run_systemctl(ctx, ["daemon-reload"], run_cmd=run_cmd)
        operations.append(result)
        if not result.get("ok"):
            errors.append(f"daemon-reload: {result.get('stderr') or result.get('stdout') or result.get('error') or result.get('returncode')}")
    for name in sorted(
        item
        for item in activation_drift
        if live_systemctl and before.get("activation_states", {}).get(item) == "masked"
    ):
        result = _run_systemctl(ctx, ["unmask", name], run_cmd=run_cmd)
        operations.append(result)
        if not result.get("ok"):
            errors.append(f"unmask {name}: {result.get('stderr') or result.get('stdout') or result.get('returncode')}")
    for name in sorted(
        item
        for item in missing | activation_drift
        if live_systemctl
        and (
            item.endswith(".timer")
            or item == FEISHU_AGENT_CREDENTIAL_SERVICE
        )
    ):
        result = _run_systemctl(ctx, ["enable", "--now", name], run_cmd=run_cmd)
        operations.append(result)
        if result.get("ok"):
            if name.endswith(".timer"):
                enabled_timers.append(name)
            else:
                enabled_services.append(name)
        else:
            errors.append(f"enable {name}: {result.get('stderr') or result.get('stdout') or result.get('returncode')}")
    credential_refresh_needed = bool(
        FEISHU_AGENT_CREDENTIAL_SERVICE in mismatched
        or FEISHU_AGENT_CREDENTIAL_SERVICE in execution_drift
        or any(
            expected_managed_files.get(key, {}).get("kind") == "systemd_executable"
            for key in written_managed_files
        )
    )
    if (
        live_systemctl
        and credential_refresh_needed
        and FEISHU_AGENT_CREDENTIAL_SERVICE not in missing
        and FEISHU_AGENT_CREDENTIAL_SERVICE not in activation_drift
    ):
        result = _run_systemctl(
            ctx,
            ["start", FEISHU_AGENT_CREDENTIAL_SERVICE],
            run_cmd=run_cmd,
        )
        operations.append(result)
        if result.get("ok"):
            started_services.append(FEISHU_AGENT_CREDENTIAL_SERVICE)
        else:
            errors.append(
                f"start {FEISHU_AGENT_CREDENTIAL_SERVICE}: "
                f"{result.get('stderr') or result.get('stdout') or result.get('returncode')}"
            )
    for name in sorted(
        item
        for item in mismatched
        if live_systemctl and item.endswith(".timer") and item in written_units
    ):
        result = _run_systemctl(ctx, ["restart", name], run_cmd=run_cmd)
        operations.append(result)
        if result.get("ok"):
            restarted_timers.append(name)
        else:
            errors.append(f"restart {name}: {result.get('stderr') or result.get('stdout') or result.get('returncode')}")
    retired_paths_changed = False
    for name in sorted(extra_installed):
        if live_systemctl:
            stop_result = _run_systemctl(ctx, ["disable", "--now", name], run_cmd=run_cmd)
            operations.append(stop_result)
            if not stop_result.get("ok"):
                errors.append(
                    f"retire {name}: "
                    f"{stop_result.get('stderr') or stop_result.get('stdout') or stop_result.get('returncode')}"
                )
                continue
        path = Path(ctx["systemd_unit_root"]) / name
        delete_result = _delete_unit_with_sudo_fallback(path, run_cmd=run_cmd)
        operations.append({**delete_result, "unit": name})
        if not delete_result.get("ok"):
            errors.append(f"delete {path}: {delete_result.get('error') or delete_result.get('stderr') or delete_result.get('returncode')}")
            continue
        retired_units.append(name)
        retired_paths_changed = True
    if retired_paths_changed and live_systemctl:
        result = _run_systemctl(ctx, ["daemon-reload"], run_cmd=run_cmd)
        operations.append(result)
        if not result.get("ok"):
            errors.append(f"daemon-reload after retire: {result.get('stderr') or result.get('stdout') or result.get('returncode')}")

    return {
        "changed": bool(
            written_units
            or written_managed_files
            or retired_managed_files
            or profile_written
            or enabled_timers
            or enabled_services
            or started_services
            or restarted_timers
            or retired_units
        ),
        "errors": errors,
        "written_units": written_units,
        "written_managed_files": written_managed_files,
        "retired_managed_files": retired_managed_files,
        "enabled_timers": enabled_timers,
        "enabled_services": enabled_services,
        "started_services": started_services,
        "restarted_timers": restarted_timers,
        "retired_units": retired_units,
        "profile_written": profile_written,
    }


def _delete_unit_with_sudo_fallback(path: Path, *, run_cmd: Callable[..., Any]) -> dict[str, Any]:
    try:
        path.unlink()
        return {"operation": "delete_unit", "path": str(path), "ok": True, "sudo_fallback": False}
    except FileNotFoundError:
        return {"operation": "delete_unit", "path": str(path), "ok": True, "sudo_fallback": False, "already_missing": True}
    except Exception as exc:
        first_error = f"{type(exc).__name__}: {exc}"
    command = ["sudo", "-n", "rm", "--", str(path)]
    try:
        proc = run_cmd(command, capture_output=True, text=True, timeout=60, check=False)
    except Exception as exc:
        return {
            "operation": "delete_unit",
            "path": str(path),
            "ok": False,
            "error": f"{first_error}; sudo fallback failed: {type(exc).__name__}: {exc}",
            "sudo_fallback": True,
            "command": command,
        }
    rc = int(getattr(proc, "returncode", 1))
    return {
        "operation": "delete_unit",
        "path": str(path),
        "ok": rc == 0,
        "error": None if rc == 0 else first_error,
        "sudo_fallback": True,
        "command": command,
        "returncode": rc,
        "stdout": str(getattr(proc, "stdout", "") or "")[-2000:],
        "stderr": str(getattr(proc, "stderr", "") or "")[-2000:],
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
    result = {
        "operation": "systemctl",
        "command": command,
        "ok": rc == 0,
        "returncode": rc,
        "stdout": stdout[-2000:],
        "stderr": stderr[-2000:],
    }
    if result["ok"] or not command or command[0] == "sudo":
        return result
    if not _looks_like_systemctl_permission_error(stdout=stdout, stderr=stderr):
        return result
    retry_command = ["sudo", "-n", *command]
    try:
        proc = run_cmd(retry_command, capture_output=True, text=True, timeout=60, check=False)
    except Exception as exc:
        return {
            **result,
            "ok": False,
            "sudo_fallback": True,
            "sudo_command": retry_command,
            "sudo_error": f"{type(exc).__name__}: {exc}",
        }
    retry_stdout = str(getattr(proc, "stdout", "") or "")
    retry_stderr = str(getattr(proc, "stderr", "") or "")
    retry_rc = int(getattr(proc, "returncode", 1))
    return {
        "operation": "systemctl",
        "command": retry_command,
        "initial_command": command,
        "sudo_fallback": True,
        "ok": retry_rc == 0,
        "returncode": retry_rc,
        "stdout": retry_stdout[-2000:],
        "stderr": retry_stderr[-2000:],
    }


def _write_text_with_sudo_fallback(
    path: Path,
    content: str,
    *,
    ctx: dict[str, Any],
    run_cmd: Callable[..., Any],
    mode: int | None = None,
) -> dict[str, Any]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if mode is not None:
            path.chmod(mode)
        return {"operation": "write_unit", "path": str(path), "ok": True, "sudo_fallback": False}
    except Exception as exc:
        first_error = f"{type(exc).__name__}: {exc}"

    if str(ctx.get("provider") or "") != "systemd":
        return {"operation": "write_unit", "path": str(path), "ok": False, "error": first_error, "sudo_fallback": False}

    mkdir_command = ["sudo", "-n", "install", "-d", str(path.parent)]
    write_command = ["sudo", "-n", "sh", "-c", 'cat > "$1"', "sh", str(path)]
    try:
        mkdir_proc = run_cmd(mkdir_command, capture_output=True, text=True, timeout=60, check=False)
        mkdir_rc = int(getattr(mkdir_proc, "returncode", 1))
        if mkdir_rc != 0:
            return {
                "operation": "write_unit",
                "path": str(path),
                "ok": False,
                "error": first_error,
                "sudo_fallback": True,
                "sudo_command": mkdir_command,
                "returncode": mkdir_rc,
                "stdout": str(getattr(mkdir_proc, "stdout", "") or "")[-2000:],
                "stderr": str(getattr(mkdir_proc, "stderr", "") or "")[-2000:],
            }
        write_proc = run_cmd(write_command, input=content, capture_output=True, text=True, timeout=60, check=False)
        write_rc = int(getattr(write_proc, "returncode", 1))
        chmod_proc = None
        chmod_command = None
        if write_rc == 0 and mode is not None:
            chmod_command = ["sudo", "-n", "chmod", f"{mode:04o}", str(path)]
            chmod_proc = run_cmd(
                chmod_command,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
    except Exception as exc:
        return {
            "operation": "write_unit",
            "path": str(path),
            "ok": False,
            "error": f"{first_error}; sudo fallback failed: {type(exc).__name__}: {exc}",
            "sudo_fallback": True,
            "sudo_command": write_command,
        }
    chmod_rc = int(getattr(chmod_proc, "returncode", 0)) if chmod_proc is not None else 0
    ok = write_rc == 0 and chmod_rc == 0
    if ok:
        error = None
    elif write_rc != 0:
        error = first_error
    else:
        error = f"chmod failed: {path}"
    return {
        "operation": "write_unit",
        "path": str(path),
        "ok": ok,
        "error": error,
        "sudo_fallback": True,
        "sudo_command": chmod_command or write_command,
        "returncode": chmod_rc if write_rc == 0 else write_rc,
        "stdout": str(
            getattr(chmod_proc if chmod_proc is not None else write_proc, "stdout", "")
            or ""
        )[-2000:],
        "stderr": str(
            getattr(chmod_proc if chmod_proc is not None else write_proc, "stderr", "")
            or ""
        )[-2000:],
    }


def _looks_like_systemctl_permission_error(*, stdout: str, stderr: str) -> bool:
    text = f"{stdout}\n{stderr}".lower()
    return any(marker in text for marker in ("access denied", "permission denied", "interactive authentication required"))


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
