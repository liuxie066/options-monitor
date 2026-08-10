from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import sys
from pathlib import Path

from src.application.cash_summary_footer import append_cash_summary_footer
from src.application.config_loader import load_config as load_runtime_pipeline_config
from src.application.config_loader import resolve_data_config_path
from src.application.config_sections import resolve_watchlist_config
from src.application.report_builders import build_symbols_digest, build_symbols_summary
from src.application.pipeline_reporting import (
    run_pipeline_alert_stage,
    run_pipeline_notification_stage,
)
from src.application.prepared_option_positions_context import (
    PreparedOptionPositionsContextError,
)
from src.infrastructure.logging_config import get_logger
from src.application.opend_fetch_config import opend_fetch_kwargs
from src.application.pipeline_symbol import process_symbol
from src.application.runtime_paths import resolve_runtime_root
from src.application.tick_run_workspace import (
    AccountRunConfigAuthority,
    AccountRunConfigError,
    load_retained_account_run_config,
)

from domain.storage.repositories import report_repo


LOG = get_logger("run_pipeline")
runtime_mode = "dev"
is_scheduled = False
stage = "all"
stage_only: str | None = None
shared_required_data: str | None = None


def log(msg: str) -> None:
    try:
        if msg.startswith("[WARN]"):
            LOG.warning(msg)
        elif msg.startswith("[INFO]"):
            LOG.info(msg)
        elif msg.startswith("[ERR]"):
            LOG.error(msg)
        else:
            LOG.info(msg)
    except Exception:
        print(msg)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run options-monitor pipeline")
    parser.add_argument("--config", required=True, help="Path to JSON config with symbols[].")
    parser.add_argument("--mode", default="dev", choices=["dev", "scheduled"], help="Runtime mode: dev (verbose) vs scheduled (fast)")
    parser.add_argument("--symbols", default=None, help="Comma-separated symbol whitelist; only process these symbols")
    parser.add_argument("--stage", default="all", choices=["fetch", "scan", "alert", "notify", "all"], help="Pipeline stage: fetch|scan|alert|notify|all (dev speed; runs up to this stage)")
    parser.add_argument("--stage-only", default=None, choices=["alert", "notify"], help="Run ONLY a late stage (no fetch/scan). Requires existing output files.")
    parser.add_argument("--refresh-multiplier-cache", action="store_true", help="Refresh output_shared/state/multiplier_cache.json via OpenD before running (best-effort).")
    parser.add_argument("--no-context", action="store_true", help="Skip portfolio/option_positions context fetch (dev speed). Useful when tuning filters only.")
    parser.add_argument("--shared-required-data", default=None, help="Path to shared required_data directory (contains raw/ and parsed/). If set, it is authoritative and fetch is skipped when artifacts exist.")
    parser.add_argument(
        "--required-data-snapshot-manifest",
        default=None,
        help="Internal: terminal run-scoped required-data snapshot manifest.",
    )
    parser.add_argument(
        "--prepared-portfolio-context-manifest",
        default=None,
        help="Internal: prepared account portfolio-context manifest.",
    )
    parser.add_argument(
        "--prepared-portfolio-context-manifest-sha256",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--prepared-option-positions-context-manifest",
        default=None,
        help="Internal: prepared account option-positions context manifest.",
    )
    parser.add_argument(
        "--prepared-option-positions-context-manifest-sha256",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--report-dir", default=None, help="Directory to write reports (symbols_summary/alerts/notification). Default: output_shared/reports")
    parser.add_argument("--state-dir", default=None, help="Directory to read/write state cache (portfolio_context/option_positions_context/rate_cache/etc). Default: output_shared/state")
    parser.add_argument("--shared-context-dir", default=None, help="Optional shared context cache directory for cross-account reuse within one tick")
    parser.add_argument(
        "--source-account-run-id",
        default=None,
        help=(
            "Internal account run id for immutable candidate capture"
        ),
    )
    parser.add_argument("--account-config-base", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--account-config-run-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--account-config-account", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--account-config-compatibility-path",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--account-config-sha256", default=None, help=argparse.SUPPRESS)
    return parser


def _load_account_config_authority(
    *,
    args: argparse.Namespace,
    cfg_path: Path,
) -> tuple[dict | None, str | None]:
    raw_values = (
        getattr(args, "account_config_base", None),
        getattr(args, "account_config_run_id", None),
        getattr(args, "account_config_account", None),
        getattr(args, "account_config_compatibility_path", None),
        getattr(args, "account_config_sha256", None),
    )
    if not any(value is not None for value in raw_values):
        return None, None
    if not all(value is not None and str(value).strip() for value in raw_values):
        raise SystemExit("[CONFIG_ERROR] account config authority is incomplete")

    digest = str(args.account_config_sha256).strip().lower()
    encoded = str(os.environ.get("OM_ACCOUNT_CONFIG_CANONICAL_B64") or "").strip()
    if not encoded:
        raise SystemExit(
            "[CONFIG_ERROR] ACCOUNT_CONFIG_CANONICAL_BYTES_MISSING: "
            "account config authority requires retained canonical bytes"
        )
    try:
        canonical_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise SystemExit(
            "[CONFIG_ERROR] ACCOUNT_CONFIG_CANONICAL_BYTES_INVALID: "
            "retained canonical bytes are not valid base64"
        ) from exc
    try:
        authority = AccountRunConfigAuthority(
            run_id=str(args.account_config_run_id),
            account=str(args.account_config_account),
            state_path=cfg_path,
            compatibility_path=Path(str(args.account_config_compatibility_path)),
            account_config_sha256=digest,
            canonical_bytes=canonical_bytes,
        )
        config = load_retained_account_run_config(
            authority=authority,
            base=Path(str(args.account_config_base)),
            run_id=str(args.account_config_run_id),
            account=str(args.account_config_account),
        )
    except AccountRunConfigError as exc:
        raise SystemExit(f"[CONFIG_ERROR] {exc.code}: {exc}") from exc
    return config, digest


def _want(step: str) -> bool:
    s = str(step or "").strip().lower()
    if not s:
        return False

    if stage_only is not None:
        if s == "alert":
            return stage_only == "alert"
        if s == "notify":
            return stage_only == "notify"
        return False

    order = {"fetch": 0, "scan": 1, "alert": 2, "notify": 3, "all": 3}
    cur = order.get(str(stage or "all"), 3)
    need = order.get(s)
    if need is None:
        return False
    return cur >= need


def main(argv: list[str] | None = None) -> int:
    global runtime_mode, is_scheduled, stage, stage_only, shared_required_data

    args = build_parser().parse_args(argv)

    runtime_mode = str(args.mode)
    is_scheduled = runtime_mode == "scheduled"
    stage = str(args.stage)
    stage_only = str(args.stage_only) if args.stage_only else None
    shared_required_data = str(args.shared_required_data) if getattr(args, "shared_required_data", None) else None

    repo_root = Path(__file__).resolve().parents[2]
    runtime_root = resolve_runtime_root(repo_root=repo_root).runtime_root
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (repo_root / cfg_path).resolve()

    authority_config, account_config_sha256 = _load_account_config_authority(
        args=args,
        cfg_path=cfg_path,
    )
    if (
        getattr(args, "prepared_portfolio_context_manifest", None)
        and authority_config is None
    ):
        raise SystemExit(
            "[CONFIG_ERROR] prepared portfolio context requires account config authority"
        )
    if (
        getattr(
            args,
            "prepared_option_positions_context_manifest",
            None,
        )
        and authority_config is None
    ):
        raise SystemExit(
            "[CONFIG_ERROR] prepared option context requires account config authority"
        )
    prepared_manifest = getattr(
        args,
        "prepared_portfolio_context_manifest",
        None,
    )
    prepared_manifest_sha256 = getattr(
        args,
        "prepared_portfolio_context_manifest_sha256",
        None,
    )
    if bool(prepared_manifest) != bool(prepared_manifest_sha256):
        raise SystemExit(
            "[CONFIG_ERROR] prepared portfolio context authority is incomplete"
        )
    prepared_option_manifest = getattr(
        args,
        "prepared_option_positions_context_manifest",
        None,
    )
    prepared_option_manifest_sha256 = getattr(
        args,
        "prepared_option_positions_context_manifest_sha256",
        None,
    )
    if bool(prepared_option_manifest) != bool(
        prepared_option_manifest_sha256
    ):
        raise SystemExit(
            "[CONFIG_ERROR] prepared option context authority is incomplete"
        )

    report_dir, state_dir = report_repo.prepare_dirs(
        base=runtime_root,
        report_dir=getattr(args, "report_dir", None),
        state_dir=getattr(args, "state_dir", None),
    )
    shared_context_dir = Path(args.shared_context_dir).resolve() if getattr(args, "shared_context_dir", None) else None

    if bool(getattr(args, "refresh_multiplier_cache", False)):
        try:
            from domain.domain.fetch_source import is_futu_fetch_source
            from src.application import multiplier_cache

            cache_path = multiplier_cache.default_cache_path(runtime_root)
            cfg0 = (
                dict(authority_config)
                if authority_config is not None
                else json.loads(cfg_path.read_text(encoding="utf-8"))
            )
            opend_kwargs = opend_fetch_kwargs(cfg0)
            syms = [
                item
                for item in resolve_watchlist_config(cfg0)
                if is_futu_fetch_source((item.get("fetch") or {}).get("source"))
            ]
            cache = multiplier_cache.load_cache(cache_path)
            for item in syms:
                sym = str(item.get("symbol") or "").strip().upper()
                fetch = item.get("fetch") or {}
                host = fetch.get("host") or "127.0.0.1"
                port = int(fetch.get("port") or 11111)
                refreshed = multiplier_cache.refresh_via_opend(
                    repo_base=runtime_root,
                    symbol=sym,
                    host=str(host),
                    port=int(port),
                    limit_expirations=1,
                    opend_fetch_config=opend_kwargs,
                )
                if refreshed.ok and refreshed.multiplier:
                    cache[sym] = {
                        "multiplier": int(refreshed.multiplier),
                        "as_of_utc": multiplier_cache.utc_now(),
                        "source": "opend",
                    }
            multiplier_cache.save_cache(cache_path, cache)
        except Exception:
            pass

    cfg = load_runtime_pipeline_config(
        base=repo_root,
        config_path=cfg_path,
        is_scheduled=is_scheduled,
        log=log,
        state_dir=state_dir,
        config_payload=authority_config,
    )
    py = sys.executable

    if "symbols" in cfg:
        top_n = cfg.get("outputs", {}).get("top_n_alerts", 3)
        runtime = cfg.get("runtime", {}) or {}
        symbol_timeout_sec = int(runtime.get("symbol_timeout_sec", 120))
        portfolio_timeout_sec = int(runtime.get("portfolio_timeout_sec", 60))

        if stage_only is not None:
            from src.application.pipeline_alert_steps import run_stage_only_alert_notify

            run_stage_only_alert_notify(
                report_dir=report_dir,
                stage_only=stage_only,
                want=_want,
                log=log,
            )
            return 0

        report_repo.ensure_report_dir(report_dir)

        from src.application.pipeline_watchlist import run_watchlist_pipeline_default

        required_data_dir = Path(shared_required_data).resolve() if shared_required_data else (runtime_root / "output_shared" / "required_data").resolve()

        try:
            summary_rows = run_watchlist_pipeline_default(
                py=py,
                base=runtime_root,
                cfg=cfg,
                report_dir=report_dir,
                state_dir=state_dir,
                shared_state_dir=shared_context_dir,
                required_data_dir=required_data_dir,
                is_scheduled=is_scheduled,
                top_n=top_n,
                symbol_timeout_sec=symbol_timeout_sec,
                portfolio_timeout_sec=portfolio_timeout_sec,
                want_scan=_want("scan"),
                no_context=bool(getattr(args, "no_context", False)),
                symbols_arg=getattr(args, "symbols", None),
                log=log,
                want_fn=_want,
                source_account_run_id=getattr(
                    args,
                    "source_account_run_id",
                    None,
                ),
                required_data_snapshot_manifest=(
                    Path(args.required_data_snapshot_manifest).resolve()
                    if getattr(args, "required_data_snapshot_manifest", None)
                    else None
                ),
                prepared_portfolio_context_manifest=(
                    Path(args.prepared_portfolio_context_manifest).resolve()
                    if getattr(args, "prepared_portfolio_context_manifest", None)
                    else None
                ),
                prepared_portfolio_context_manifest_sha256=(
                    str(
                        args.prepared_portfolio_context_manifest_sha256
                    ).strip().lower()
                    if getattr(
                        args,
                        "prepared_portfolio_context_manifest_sha256",
                        None,
                    )
                    else None
                ),
                prepared_option_positions_context_manifest=(
                    Path(
                        args.prepared_option_positions_context_manifest
                    ).resolve()
                    if getattr(
                        args,
                        "prepared_option_positions_context_manifest",
                        None,
                    )
                    else None
                ),
                prepared_option_positions_context_manifest_sha256=(
                    str(
                        args.prepared_option_positions_context_manifest_sha256
                    ).strip().lower()
                    if getattr(
                        args,
                        "prepared_option_positions_context_manifest_sha256",
                        None,
                    )
                    else None
                ),
                account_config_sha256=account_config_sha256,
            )
        except PreparedOptionPositionsContextError as exc:
            raise SystemExit(
                "[CONFIG_ERROR] "
                "ACCOUNT_CONFIG_PREPARED_OPTION_CONTEXT_INVALID: "
                f"{exc}"
            ) from exc

        symbols = [str(r.get("symbol")) for r in summary_rows if r.get("symbol")]

        if (stage_only is None) and (not _want("scan")):
            log(f"[INFO] stage={stage}: fetch done")
            return 0

        build_symbols_summary(summary_rows, report_dir, is_scheduled=is_scheduled)

        if not is_scheduled:
            build_symbols_digest(symbols, report_dir)

        changes_path = Path("/dev/null") if is_scheduled else (report_dir / "symbols_changes.txt").resolve()
        policy_json: str | None = None
        try:
            policy = cfg.get("alert_policy")
            if isinstance(policy, dict) and policy:
                policy_path = (state_dir / "alert_policy.json").resolve()
                report_repo.write_state_json_text(state_dir, "alert_policy.json", policy)
                policy_json = str(policy_path)
            elif isinstance(policy, str) and policy.strip():
                policy_json = policy.strip()
        except Exception:
            pass
        try:
            from domain.domain import alert_rules as _alert_rules
            from domain.domain.alert_policy import resolve_alert_policy as _resolve_alert_policy
            _raw_policy = cfg.get("alert_policy") if isinstance(cfg, dict) else None
            _alert_rules.set_active_alert_policy(
                _resolve_alert_policy(_raw_policy if isinstance(_raw_policy, dict) else None)
            )
        except Exception:
            pass
        if _want("alert"):
            run_pipeline_alert_stage(
                summary_input=(report_dir / "symbols_summary.csv").resolve(),
                output=(report_dir / "symbols_alerts.txt").resolve(),
                changes_output=changes_path,
                previous_summary=((state_dir / "symbols_summary_prev.csv").resolve() if not is_scheduled else None),
                state_dir=state_dir,
                update_snapshot=(not is_scheduled),
                policy_json=policy_json,
            )

        if _want("notify"):
            run_pipeline_notification_stage(
                alerts_input=(report_dir / "symbols_alerts.txt").resolve(),
                changes_input=changes_path,
                output=(report_dir / "symbols_notification.txt").resolve(),
                render_style="compact",
            )

        portfolio_cfg = cfg.get("portfolio", {}) or {}
        data_config = str(resolve_data_config_path(base=runtime_root, data_config=portfolio_cfg.get("data_config")))
        broker = str(portfolio_cfg.get("broker") or "富途")

        try:
            include_cash_footer = bool((cfg.get("notifications") or {}).get("include_cash_footer", True))
        except Exception:
            include_cash_footer = True

        if include_cash_footer and (not is_scheduled):
            append_cash_summary_footer(
                base=runtime_root,
                notification=report_dir / "symbols_notification.txt",
                config=cfg_path,
                data_config=data_config,
                market=str(broker),
            )

        notifications_cfg = cfg.get("notifications", {}) or {}
        if notifications_cfg.get("enabled", False):
            log("[INFO] notifications enabled; generated Compact compatibility notification bundle (not delivery evidence).")
        else:
            log("[INFO] notifications disabled; generated Compact compatibility notification bundle only.")
        if not is_scheduled:
            print("\n[DONE] Symbols pipeline finished")
            print(f"- {report_dir}/symbols_summary.csv")
            print(f"- {report_dir}/symbols_alerts.txt")
            print(f"- {report_dir}/symbols_changes.txt")
            print(f"- {report_dir}/symbols_notification.txt")
            print("")
        return 0

    top_n = cfg.get("outputs", {}).get("top_n_alerts", 3)
    process_symbol(py, runtime_root, cfg, top_n, report_dir=report_dir, state_dir=state_dir, is_scheduled=is_scheduled)
    print("\n[DONE] Single-symbol pipeline finished")
    print(f"- {state_dir}/opening_candidate_snapshot.json")
    return 0
