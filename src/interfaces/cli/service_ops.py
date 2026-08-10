from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.service_cleanup import service_cleanup
from src.application.service_deploy import (
    load_service_profile,
    render_service_bundle,
    service_preflight,
    service_status_from_profile,
    write_service_bundle,
)
from src.application.service_drift import migrate_service_credentials, service_drift
from src.application.service_upgrade import service_rollback, service_upgrade, service_upgrade_check, service_upgrade_verify
from src.application.write_contract import attach_write_contract


def add_service_update_commands(subparsers: Any) -> None:
    service = subparsers.add_parser("service", help="render and inspect platform service definitions")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    service_render = service_sub.add_parser("render", help="render systemd or launchd service files")
    service_render.add_argument("--target", required=True, choices=("systemd", "launchd"))
    service_render.add_argument("--repo-root", default=None)
    service_render.add_argument("--runtime-root", default=None)
    service_render.add_argument("--accounts", nargs="+", default=None)
    service_render.add_argument("--markets", nargs="+", choices=("us", "hk"), default=None)
    service_render.add_argument("--config-us", default=None)
    service_render.add_argument("--config-hk", default=None)
    service_render.add_argument(
        "--config-yaml",
        required=True,
        help="YAML authoring source recorded in service.profile.json for update rebuilds",
    )
    service_render.add_argument(
        "--env-file",
        default=None,
        help="service env-file path for ordinary settings; secret entries require the explicit env compatibility backend",
    )
    service_render.add_argument(
        "--deploy-user",
        default=None,
        help="systemd User= identity; also accepted from OM_DEPLOY_USER/DEPLOY_USER",
    )
    service_render.add_argument("--deploy-home", default=None, help="systemd HOME environment; defaults to /home/<deploy-user>")
    service_render.add_argument("--timeout", dest="timeout_seconds", type=int, default=600)
    service_render.add_argument(
        "--include-auto-upgrade",
        action="store_true",
        help="render an opt-in daily auto-upgrade service/timer",
    )
    service_render.add_argument(
        "--include-opend",
        action="store_true",
        help="render the long-running Futu OpenD gateway service",
    )
    service_render.add_argument("--opend-root", default=None, help="Futu OpenD installation/current directory")
    service_render.add_argument("--opend-executable", default=None, help="Futu OpenD executable name or absolute path")
    service_render.add_argument(
        "--include-feishu-ws",
        action="store_true",
        help="render the long-running Feishu long-connection inbound service",
    )
    service_render.add_argument("--feishu-ws-config-key", default=None, choices=("us", "hk"))
    service_render.add_argument(
        "--include-wechat-clawbot",
        action="store_true",
        help="render the long-running WeChat ClawBot inbound polling service",
    )
    service_render.add_argument("--wechat-clawbot-config-key", default=None, choices=("us", "hk"))
    service_render.add_argument("--wechat-clawbot-label", default=None)
    service_render.add_argument(
        "--wechat-clawbot-allowed-senders",
        default=None,
        help="comma-separated WeChat inbound allowlist, e.g. wechat:<from_user_id>",
    )
    service_render.add_argument(
        "--include-strategy-lab-recorder",
        action="store_true",
        help="render opt-in Strategy Lab dataset build, mark sampling, and settlement timers",
    )
    service_render.add_argument(
        "--include-quality-monitoring",
        action="store_true",
        help="render opt-in systemd quality API, refresh, recheck, and day-end reconciliation units",
    )
    service_render.add_argument(
        "--include-feishu-agent-credential",
        action="store_true",
        help="render the deprecated encrypted Feishu env materializer during migration only",
    )
    service_render.add_argument(
        "--include-secret-credentials",
        action="store_true",
        help="render per-unit systemd credential drop-ins using the selected delivery mode",
    )
    service_render.add_argument(
        "--secret-credential-delivery",
        default="load-credential-encrypted",
        choices=("load-credential-encrypted", "runtime-files"),
        help=(
            "credential injection mode: native systemd encrypted credentials or "
            "an explicit Incus-compatible tmpfs runtime-file materializer"
        ),
    )
    service_render.add_argument(
        "--secret-credential-store-root",
        default="/etc/credstore.encrypted",
        help="encrypted systemd credential store root",
    )
    service_render.add_argument(
        "--strategy-lab-recorder-source",
        default="opend",
        choices=("local", "opend"),
        help="mark sampling source for Strategy Lab recorder timers",
    )
    service_render.add_argument(
        "--strategy-lab-recorder-account",
        default=None,
        help="Futu account whose OpenD endpoint owns Strategy Lab mark sampling",
    )
    service_render.add_argument(
        "--strategy-lab-recorder-max-datasets",
        type=int,
        default=5,
        help="maximum datasets sampled per recorder run",
    )
    service_render.add_argument("--strategy-lab-recorder-mark-stale-hours", type=int, default=2)
    service_render.add_argument("--output-dir", default=None, help="write rendered files under this directory")
    service_render.add_argument("--no-content", action="store_true", help="omit file contents from JSON output")
    service_preflight_cmd = service_sub.add_parser(
        "preflight",
        help="check Linux runtime root before installing/running services",
    )
    service_preflight_cmd.add_argument("--runtime-root", default="/var/lib/options-monitor")
    service_preflight_cmd.add_argument("--accounts", nargs="+", default=None)
    service_preflight_cmd.add_argument("--config-us", default=None)
    service_preflight_cmd.add_argument("--config-hk", default=None)
    service_preflight_cmd.add_argument("--env-file", default=None)
    service_status = service_sub.add_parser("status", help="summarize a rendered service profile")
    service_status.add_argument("--profile-path", required=True)
    service_status.add_argument("--include-service-status", action="store_true")
    service_drift_cmd = service_sub.add_parser(
        "drift",
        help="compare expected service units with profile and installed units",
    )
    service_drift_cmd.add_argument("--repo-root", default=None)
    service_drift_cmd.add_argument("--runtime-root", default="/var/lib/options-monitor")
    service_drift_cmd.add_argument("--profile-path", default=None)
    service_drift_cmd.add_argument("--confirm", action="store_true", help="write missing/changed units and profile, then reload affected timers")
    service_drift_cmd.add_argument("--yes", action="store_true", help="non-interactive confirmation; emits an audit_id")
    service_credential_migrate = service_sub.add_parser(
        "credentials-migrate",
        help="migrate deprecated shared secret env delivery to per-unit systemd credentials",
    )
    service_credential_migrate.add_argument("--repo-root", default=None)
    service_credential_migrate.add_argument("--runtime-root", default="/var/lib/options-monitor")
    service_credential_migrate.add_argument("--profile-path", default=None)
    service_credential_migrate.add_argument(
        "--secret-credential-delivery",
        required=True,
        choices=("load-credential-encrypted", "runtime-files"),
        help="explicit target delivery; use runtime-files for restricted Incus/LXC",
    )
    service_credential_migrate.add_argument(
        "--secret-credential-store-root",
        default=None,
        help="override the encrypted credential store root recorded by the profile",
    )
    service_credential_migrate.add_argument(
        "--confirm",
        action="store_true",
        help="validate credentials, reconcile units, restart active consumers, and retire legacy runtime files",
    )
    service_credential_migrate.add_argument(
        "--yes",
        action="store_true",
        help="non-interactive confirmation; emits an audit_id",
    )
    service_cleanup_cmd = service_sub.add_parser("cleanup", help="dry-run or clean old releases and selected caches")
    service_cleanup_cmd.add_argument("--repo-root", default=None)
    service_cleanup_cmd.add_argument("--releases-root", default=None)
    service_cleanup_cmd.add_argument("--runtime-root", default="/var/lib/options-monitor")
    service_cleanup_cmd.add_argument("--keep-releases", type=int, default=2)
    service_cleanup_cmd.add_argument("--include-apt-cache", action="store_true")
    service_cleanup_cmd.add_argument("--journal-vacuum-size", default=None)
    service_cleanup_cmd.add_argument("--cleanup-downloads", action="store_true")
    service_cleanup_cmd.add_argument("--cleanup-pip-cache", action="store_true")
    service_cleanup_cmd.add_argument("--cleanup-output-runs", action="store_true")
    service_cleanup_cmd.add_argument("--output-runs-keep-days", type=int, default=14)
    service_cleanup_cmd.add_argument("--output-runs-keep-count", type=int, default=200)
    service_cleanup_cmd.add_argument("--cleanup-runtime-logs", action="store_true")
    service_cleanup_cmd.add_argument("--runtime-logs-keep-days", type=int, default=14)
    service_cleanup_cmd.add_argument("--expected-output-runs-plan-sha256", default=None)
    service_cleanup_cmd.add_argument("--confirm", action="store_true", help="delete planned paths; without this the command is a dry run")
    service_cleanup_cmd.add_argument("--yes", action="store_true", help="non-interactive confirmation; emits an audit_id")

    update = subparsers.add_parser("update", help="check, apply, or roll back released versions")
    update_sub = update.add_subparsers(dest="update_command", required=True)
    update_check = update_sub.add_parser("check", help="check whether a newer released version is available")
    update_check.add_argument("--repo-root", default=None)
    update_check.add_argument("--runtime-root", default="/var/lib/options-monitor")
    update_check.add_argument("--cache-root", default=None)
    update_check.add_argument("--remote-name", default="origin")
    update_verify = update_sub.add_parser("verify", help="compact read-only release/runtime verification")
    update_verify.add_argument("--repo-root", default=None)
    update_verify.add_argument("--runtime-root", default="/var/lib/options-monitor")
    update_verify.add_argument("--cache-root", default=None)
    update_verify.add_argument("--remote-name", default="origin")
    update_verify.add_argument(
        "--no-check-latest",
        action="store_true",
        help="skip git tag/latest-version lookup for a faster local runtime verification",
    )
    update_apply = update_sub.add_parser("apply", help="upgrade a current symlink to a released version")
    update_apply.add_argument("--repo-root", default=None)
    update_apply.add_argument("--runtime-root", default="/var/lib/options-monitor")
    update_apply.add_argument("--releases-root", default=None)
    update_apply.add_argument("--cache-root", default=None)
    update_apply.add_argument("--target-version", default=None)
    update_apply.add_argument("--remote-name", default="origin")
    update_apply.add_argument("--auto", action="store_true")
    update_apply.add_argument("--allow-major", action="store_true")
    update_apply.add_argument("--confirm", action="store_true", help="apply upgrade; without this the command is a dry run")
    update_apply.add_argument("--yes", action="store_true", help="non-interactive confirmation; emits an audit_id")
    update_apply.add_argument(
        "--no-restart-services",
        action="store_true",
        help=(
            "skip long-running service restart and health checks; timer drift "
            "is still repaired unless --preserve-activation-state is set"
        ),
    )
    update_apply.add_argument(
        "--preserve-activation-state",
        action="store_true",
        help=(
            "preserve pre-existing inactive, disabled, or masked systemd timers "
            "during service reconcile"
        ),
    )
    update_apply.add_argument(
        "--cleanup-after-upgrade",
        action="store_true",
        help="clean old releases after a fully successful confirmed upgrade",
    )
    update_apply.add_argument("--cleanup-keep-releases", type=int, default=2)
    update_rollback = update_sub.add_parser("rollback", help="switch current symlink back to a prior released version")
    update_rollback.add_argument("--repo-root", default=None)
    update_rollback.add_argument("--runtime-root", default="/var/lib/options-monitor")
    update_rollback.add_argument("--releases-root", default=None)
    update_rollback.add_argument("--to-version", default=None)
    update_rollback.add_argument("--confirm", action="store_true", help="apply rollback; without this the command is a dry run")
    update_rollback.add_argument("--yes", action="store_true", help="non-interactive confirmation; emits an audit_id")
    update_rollback.add_argument(
        "--no-restart-services",
        action="store_true",
        help=(
            "skip long-running service restart and health checks; timer drift "
            "is still repaired unless --preserve-activation-state is set"
        ),
    )
    update_rollback.add_argument(
        "--preserve-activation-state",
        action="store_true",
        help=(
            "preserve pre-existing inactive, disabled, or masked systemd timers "
            "during service reconcile"
        ),
    )


def _confirmed(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "confirm", False) or getattr(args, "yes", False))


def _service_write_contract(data: dict[str, Any], *, confirmed: bool, rollback_hint: str | None = None) -> dict[str, Any]:
    return attach_write_contract(
        data,
        dry_run=not bool(confirmed),
        write_applied=bool(confirmed and data.get("changed", False)),
        backup_path=data.get("backup_path"),
        rollback_hint=rollback_hint,
    )


def _config_paths_from_args(args: argparse.Namespace) -> dict[str, str]:
    return {
        key: value
        for key, value in {
            "us": args.config_us,
            "hk": args.config_hk,
        }.items()
        if value
    }


def handle_service_update_command(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
    load_service_profile_fn: Callable[..., dict[str, Any]] = load_service_profile,
    render_service_bundle_fn: Callable[..., dict[str, Any]] = render_service_bundle,
    service_preflight_fn: Callable[..., dict[str, Any]] = service_preflight,
    service_status_from_profile_fn: Callable[..., dict[str, Any]] = service_status_from_profile,
    write_service_bundle_fn: Callable[..., list[str]] = write_service_bundle,
    service_drift_fn: Callable[..., dict[str, Any]] = service_drift,
    migrate_service_credentials_fn: Callable[..., dict[str, Any]] = migrate_service_credentials,
    service_cleanup_fn: Callable[..., dict[str, Any]] = service_cleanup,
    service_upgrade_check_fn: Callable[..., dict[str, Any]] = service_upgrade_check,
    service_upgrade_verify_fn: Callable[..., dict[str, Any]] = service_upgrade_verify,
    service_upgrade_fn: Callable[..., dict[str, Any]] = service_upgrade,
    service_rollback_fn: Callable[..., dict[str, Any]] = service_rollback,
) -> dict[str, Any]:
    if args.command == "service" and args.service_command == "render":
        bundle = render_service_bundle_fn(
            target=args.target,
            repo_root=args.repo_root,
            runtime_root=args.runtime_root,
            accounts=args.accounts,
            markets=args.markets,
            config_paths=_config_paths_from_args(args),
            config_yaml=args.config_yaml,
            env_file=args.env_file,
            deploy_user=args.deploy_user,
            deploy_home=args.deploy_home,
            timeout_seconds=args.timeout_seconds,
            include_auto_upgrade=bool(args.include_auto_upgrade),
            include_opend=bool(args.include_opend),
            opend_root=args.opend_root,
            opend_executable=args.opend_executable,
            include_feishu_ws=bool(args.include_feishu_ws),
            feishu_ws_config_key=args.feishu_ws_config_key,
            include_wechat_clawbot=bool(args.include_wechat_clawbot),
            wechat_clawbot_config_key=args.wechat_clawbot_config_key,
            wechat_clawbot_label=args.wechat_clawbot_label,
            wechat_clawbot_allowed_senders=args.wechat_clawbot_allowed_senders,
            include_strategy_lab_recorder=bool(args.include_strategy_lab_recorder),
            strategy_lab_recorder_source=args.strategy_lab_recorder_source,
            strategy_lab_recorder_account=args.strategy_lab_recorder_account,
            strategy_lab_recorder_max_datasets=args.strategy_lab_recorder_max_datasets,
            strategy_lab_recorder_mark_stale_hours=args.strategy_lab_recorder_mark_stale_hours,
            include_quality_monitoring=bool(args.include_quality_monitoring),
            include_feishu_agent_credential=bool(args.include_feishu_agent_credential),
            include_secret_credentials=bool(args.include_secret_credentials),
            secret_credential_delivery=args.secret_credential_delivery,
            secret_credential_store_root=args.secret_credential_store_root,
            include_content=(not bool(args.no_content)) or bool(args.output_dir),
        )
        if args.output_dir:
            bundle["written_files"] = write_service_bundle_fn(bundle, args.output_dir)
            if bool(args.no_content):
                for item in bundle.get("files", []):
                    if isinstance(item, dict):
                        item.pop("content", None)
        return build_response(tool_name="service.render", ok=True, data=bundle)

    if args.command == "service" and args.service_command == "preflight":
        data = service_preflight_fn(
            runtime_root=args.runtime_root,
            env_file=args.env_file,
            accounts=args.accounts,
            config_paths=_config_paths_from_args(args),
        )
        return build_response(tool_name="service.preflight", ok=bool(data["summary"]["ok"]), data=data)

    if args.command == "service" and args.service_command == "status":
        profile = load_service_profile_fn(args.profile_path)
        data = service_status_from_profile_fn(profile, include_status=bool(args.include_service_status))
        return build_response(tool_name="service.status", ok=True, data=data)

    if args.command == "service" and args.service_command == "drift":
        confirmed = _confirmed(args)
        data = service_drift_fn(
            repo_root=args.repo_root or repo_base_fn(),
            runtime_root=args.runtime_root,
            profile_path=args.profile_path,
            confirm=confirmed,
        )
        data = _service_write_contract(
            data,
            confirmed=confirmed,
            rollback_hint="remove written units or restore the previous service.profile.json",
        )
        ok = bool(data.get("summary", {}).get("ok", True))
        return build_response(tool_name="service.drift", ok=ok, data=data)

    if args.command == "service" and args.service_command == "credentials-migrate":
        confirmed = _confirmed(args)
        data = migrate_service_credentials_fn(
            repo_root=args.repo_root or repo_base_fn(),
            runtime_root=args.runtime_root,
            profile_path=args.profile_path,
            secret_credential_delivery=args.secret_credential_delivery,
            secret_credential_store_root=args.secret_credential_store_root,
            confirm=confirmed,
        )
        data = _service_write_contract(
            data,
            confirmed=confirmed,
            rollback_hint=(
                "restore the reported service.profile.json backup and run service drift; "
                "encrypted credential files are not modified"
            ),
        )
        return build_response(
            tool_name="service.credentials-migrate",
            ok=bool(data.get("ok")),
            data=data,
        )

    if args.command == "service" and args.service_command == "cleanup":
        confirmed = _confirmed(args)
        data = service_cleanup_fn(
            repo_root=args.repo_root or repo_base_fn(),
            releases_root=args.releases_root,
            runtime_root=args.runtime_root,
            keep_releases=args.keep_releases,
            include_apt_cache=bool(args.include_apt_cache),
            journal_vacuum_size=args.journal_vacuum_size,
            cleanup_downloads=bool(args.cleanup_downloads),
            cleanup_pip_cache=bool(args.cleanup_pip_cache),
            cleanup_output_runs=bool(args.cleanup_output_runs),
            output_runs_keep_days=args.output_runs_keep_days,
            output_runs_keep_count=args.output_runs_keep_count,
            cleanup_runtime_logs=bool(args.cleanup_runtime_logs),
            runtime_logs_keep_days=args.runtime_logs_keep_days,
            expected_output_runs_plan_sha256=args.expected_output_runs_plan_sha256,
            confirm=confirmed,
        )
        data = _service_write_contract(
            data,
            confirmed=confirmed,
            rollback_hint="restore deleted release/cache paths from external backup",
        )
        return build_response(tool_name="service.cleanup", ok=bool(data.get("ok")), data=data)

    if args.command == "update" and args.update_command == "check":
        data = service_upgrade_check_fn(
            repo_root=args.repo_root or repo_base_fn(),
            runtime_root=args.runtime_root,
            cache_root=args.cache_root,
            remote_name=args.remote_name,
        )
        return build_response(tool_name="update.check", ok=bool(data.get("ok")), data=data)

    if args.command == "update" and args.update_command == "verify":
        data = service_upgrade_verify_fn(
            repo_root=args.repo_root or repo_base_fn(),
            runtime_root=args.runtime_root,
            cache_root=args.cache_root,
            remote_name=args.remote_name,
            check_latest=not bool(args.no_check_latest),
        )
        return build_response(tool_name="update.verify", ok=bool(data.get("ok")), data=data)

    if args.command == "update" and args.update_command == "apply":
        confirmed = _confirmed(args)
        data = service_upgrade_fn(
            repo_root=args.repo_root or repo_base_fn(),
            runtime_root=args.runtime_root,
            releases_root=args.releases_root,
            cache_root=args.cache_root,
            target_version=args.target_version,
            remote_name=args.remote_name,
            confirm=confirmed,
            auto=bool(args.auto),
            allow_major=bool(args.allow_major),
            restart_services=not bool(args.no_restart_services),
            preserve_activation_state=bool(args.preserve_activation_state),
            cleanup_after_upgrade=bool(args.cleanup_after_upgrade),
            cleanup_keep_releases=args.cleanup_keep_releases,
        )
        data = _service_write_contract(data, confirmed=confirmed, rollback_hint="./om update rollback --confirm")
        return build_response(tool_name="update.apply", ok=bool(data.get("ok")), data=data)

    if args.command == "update" and args.update_command == "rollback":
        confirmed = _confirmed(args)
        data = service_rollback_fn(
            repo_root=args.repo_root or repo_base_fn(),
            runtime_root=args.runtime_root,
            releases_root=args.releases_root,
            to_version=args.to_version,
            confirm=confirmed,
            restart_services=not bool(args.no_restart_services),
            preserve_activation_state=bool(args.preserve_activation_state),
        )
        data = _service_write_contract(data, confirmed=confirmed, rollback_hint="./om update apply --confirm")
        return build_response(tool_name="update.rollback", ok=bool(data.get("ok")), data=data)

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported service/update command: {args.command}")
