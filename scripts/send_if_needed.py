#!/usr/bin/env python3
"""Deprecated compatibility launcher for scheduled ticks.

The single-account scheduler/pipeline/send implementation was retired in
favor of the unified multi-account tick path.  Keep this filename so existing
cron jobs fail less abruptly while all business behavior runs through
``src.application.multi_account_tick.run_tick``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

try:
    from scripts.run_log import RunLogger
except Exception:
    from run_log import RunLogger  # type: ignore

from src.application.multi_account_tick import current_run_id, run_tick


LEGACY_IGNORED_OPTIONS = (
    "state_dir",
    "state",
    "report_dir",
)

LEGACY_UNSAFE_OPTIONS = (
    "channel",
    "target",
    "notification",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deprecated wrapper for ./om run tick",
    )
    parser.add_argument("--config", default="config.us.json")
    parser.add_argument("--accounts", nargs="+", default=None)
    parser.add_argument("--default-account", default=None)
    parser.add_argument("--market-config", default="auto", choices=["auto", "hk", "us", "all"])
    parser.add_argument("--no-send", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--opend-phone-verify-continue", action="store_true")

    # Legacy single-account flags.  They are consumed so existing cron command
    # lines still parse, but the unified tick path reads these concerns from
    # runtime config and shared state.
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--channel", default=None)
    parser.add_argument("--target", default=None)
    parser.add_argument("--state", default=None)
    parser.add_argument("--report-dir", default=None)
    parser.add_argument("--notification", default=None)
    return parser


def _resolve_config_path(config: str) -> Path:
    path = Path(config)
    if path.is_absolute():
        return path
    return (repo_root / path).resolve()


def _normalized_items(raw: Any) -> list[str]:
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip().lower()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _legacy_portfolio_account(config_path: Path) -> list[str]:
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(cfg, dict):
        return []
    if _normalized_items(cfg.get("accounts")):
        return []
    portfolio = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    account = str(portfolio.get("account") or "").strip().lower()
    return [account] if account else []


def build_multi_tick_argv(args: argparse.Namespace) -> list[str]:
    config_path = _resolve_config_path(str(args.config))
    out = ["--config", str(config_path), "--market-config", str(args.market_config or "auto")]

    accounts = _normalized_items(args.accounts)
    if not accounts:
        accounts = _legacy_portfolio_account(config_path)
    if accounts:
        out.extend(["--accounts", *accounts])

    default_account = str(args.default_account or "").strip().lower()
    if default_account:
        out.extend(["--default-account", default_account])

    for flag_name, cli_flag in (
        ("no_send", "--no-send"),
        ("smoke", "--smoke"),
        ("force", "--force"),
        ("debug", "--debug"),
        ("opend_phone_verify_continue", "--opend-phone-verify-continue"),
    ):
        if bool(getattr(args, flag_name, False)):
            out.append(cli_flag)
    return out


def _warn_ignored_legacy_options(args: argparse.Namespace) -> None:
    ignored = [
        f"--{name.replace('_', '-')}"
        for name in LEGACY_IGNORED_OPTIONS
        if getattr(args, name, None) not in (None, "")
    ]
    if ignored:
        sys.stderr.write(
            "[WARN] scripts/send_if_needed.py is deprecated; "
            f"ignored legacy options: {', '.join(ignored)}. "
            "Use ./om run tick or runtime config instead.\n"
        )


def _reject_unsafe_legacy_options(args: argparse.Namespace) -> bool:
    unsafe = [
        f"--{name.replace('_', '-')}"
        for name in LEGACY_UNSAFE_OPTIONS
        if getattr(args, name, None) not in (None, "")
    ]
    if not unsafe:
        return False
    sys.stderr.write(
        "[ERROR] scripts/send_if_needed.py is deprecated; "
        f"refusing ignored live-affecting options: {', '.join(unsafe)}. "
        "Move channel/target/notification routing into runtime config or use ./om run tick.\n"
    )
    return True


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if _reject_unsafe_legacy_options(args):
        return 2
    _warn_ignored_legacy_options(args)
    return int(run_tick(build_multi_tick_argv(args)))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        try:
            RunLogger(repo_root, run_id=current_run_id()).event(
                "run_error",
                "error",
                error_code=(getattr(exc, "error_code", None) or type(exc).__name__),
                message=str(exc),
            )
        except Exception:
            pass
        raise
