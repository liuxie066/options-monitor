from __future__ import annotations

import argparse
import getpass
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domain.domain.position_advice_authority import (
    normalize_account_label,
    scope_for,
    validate_authority_policy,
)
from src.application.position_advice_authority_binding import (
    PositionAdviceIdentityBindingError,
    build_first_use_identity_binding_from_runtime,
)
from src.application.position_advice_authority_service import (
    apply_authority_change,
    authority_policy_path,
    plan_authority_change,
)
from src.application.position_advice_notification_authority import (
    resolve_notification_unknown,
)
from src.application.runtime_paths import resolve_runtime_root


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    payload = handle_position_advice_command(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("status") not in {"blocked", "error"} else 2


def handle_position_advice_command(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[3]
    runtime_root = resolve_runtime_root(
        repo_root=repo_root,
        runtime_root=getattr(args, "runtime_root", None),
    ).runtime_root
    if args.authority_command == "set":
        return _set_authority(
            args,
            repo_root=repo_root,
            runtime_root=runtime_root,
        )
    if args.authority_command == "resolve-notification":
        evidence = _read_json_object(
            Path(args.evidence),
            "notification resolution evidence",
        )
        return resolve_notification_unknown(
            base=runtime_root,
            normalized_account=args.account,
            receipt_id=args.receipt_id,
            resolution=args.resolution,
            evidence=evidence,
            actor=args.actor,
            resolved_at=_now(),
            confirm=bool(args.confirm),
            dry_run=not bool(args.confirm),
        )
    raise ValueError(f"unsupported position advice authority command: {args.authority_command}")


def _set_authority(
    args: argparse.Namespace,
    *,
    repo_root: Path,
    runtime_root: Path,
) -> dict[str, Any]:
    account = normalize_account_label(args.account)
    expected_hash = str(args.expected_policy_hash or "").strip()
    binding: dict[str, Any] | None = None
    if expected_hash == "absent":
        try:
            first_use = build_first_use_identity_binding_from_runtime(
                repo_root=repo_root,
                runtime_root=runtime_root,
                normalized_account=account,
                config_yaml_path=(
                    Path(args.config_yaml) if args.config_yaml else None
                ),
                now=_now(),
            )
        except PositionAdviceIdentityBindingError as exc:
            return {
                "schema_version": (
                    "position_advice_authority_change_plan.v1"
                ),
                "status": "blocked",
                "reason_codes": ["first_use_identity_binding_failed"],
                "failure_detail": str(exc),
                "portfolio_scope_id": scope_for(account),
                "target_mode": args.mode,
                "expected_policy_hash": expected_hash,
                "would_change": False,
                "applied": False,
                "dry_run": not bool(args.confirm),
                "runtime_root": str(runtime_root),
                "identity_binding_intent": exc.intent_evidence,
            }
        source = str(first_use["normalized_portfolio_source"])
        identity_hash = str(
            first_use["portfolio_account_identity_hash"]
        )
        binding = dict(first_use["identity_binding_evidence"])
    else:
        policy = _read_existing_policy(
            runtime_root=runtime_root,
            account=account,
        )
        source = str(policy["normalized_portfolio_source"])
        identity_hash = str(policy["portfolio_account_identity_hash"])
    promotion_evidence = (
        _read_json_object(Path(args.evidence), "promotion evidence")
        if args.evidence
        else None
    )
    kwargs = {
        "base": runtime_root,
        "normalized_account": account,
        "normalized_portfolio_source": source,
        "portfolio_account_identity_hash": identity_hash,
        "target_mode": args.mode,
        "expected_policy_hash": expected_hash,
        "actor": args.actor,
        "requested_at": _now(),
        "identity_binding_evidence": binding,
        "promotion_evidence": promotion_evidence,
    }
    if args.confirm:
        return apply_authority_change(
            **kwargs,
            confirm=True,
        )
    return {
        **plan_authority_change(**kwargs),
        "dry_run": True,
        "runtime_root": str(runtime_root),
    }


def _read_existing_policy(
    *,
    runtime_root: Path,
    account: str,
) -> dict[str, Any]:
    path = authority_policy_path(runtime_root, scope_for(account))
    policy = _read_json_object(path, "authority policy")
    reasons = validate_authority_policy(
        policy,
        expected_scope_id=scope_for(account),
    )
    if reasons:
        raise ValueError(
            "authority policy is invalid: " + ",".join(reasons)
        )
    if policy.get("normalized_account") != account:
        raise ValueError("authority policy account does not match CLI account")
    return policy


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="om position-advice",
        description="human-only Position Advice control-plane operations",
    )
    parser.add_argument("--runtime-root", default=None)
    authority = parser.add_subparsers(
        dest="position_advice_command",
        required=True,
    ).add_parser("authority")
    commands = authority.add_subparsers(
        dest="authority_command",
        required=True,
    )
    set_command = commands.add_parser(
        "set",
        help="dry-run or CAS-apply one account authority mode",
    )
    set_command.add_argument("--account", required=True)
    set_command.add_argument(
        "--mode",
        required=True,
        choices=("v1", "v2_shadow", "v2"),
    )
    set_command.add_argument("--expected-policy-hash", required=True)
    set_command.add_argument("--evidence", default=None)
    set_command.add_argument("--config-yaml", default=None)
    set_command.add_argument("--actor", default=getpass.getuser())
    _add_apply_mode(set_command)

    resolve = commands.add_parser(
        "resolve-notification",
        help="append a human delivery resolution receipt",
    )
    resolve.add_argument("--account", required=True)
    resolve.add_argument("--receipt-id", required=True)
    resolve.add_argument(
        "--resolution",
        required=True,
        choices=("delivered", "failed"),
    )
    resolve.add_argument("--evidence", required=True)
    resolve.add_argument("--actor", default=getpass.getuser())
    _add_apply_mode(resolve)
    return parser


def _add_apply_mode(parser: argparse.ArgumentParser) -> None:
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help="preview only; this is also the default",
    )
    modes.add_argument(
        "--confirm",
        action="store_true",
        help="apply the audited mutation",
    )


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    if not target.is_file() or target.is_symlink():
        raise ValueError(f"{label} is unavailable: {target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable: {target}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _now() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
