from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.account_config import cash_footer_accounts_from_config
from scripts.config_loader import load_config, resolve_data_config_path, resolve_watchlist_config
from scripts.report_builders import build_symbols_digest, build_symbols_summary
from src.application.pipeline_reporting import (
    run_pipeline_alert_stage,
    run_pipeline_notification_stage,
)

try:
    from domain.storage.repositories import report_repo
except Exception:
    from scripts.domain.storage.repositories import report_repo  # type: ignore


LOG = __import__("scripts.logging_config", fromlist=["get_logger"]).get_logger("run_pipeline")
RUNTIME_MODE = "dev"
IS_SCHEDULED = False
STAGE = "all"
STAGE_ONLY: str | None = None
SHARED_REQUIRED_DATA: str | None = None


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
    parser.add_argument("--report-dir", default=None, help="Directory to write reports (symbols_summary/alerts/notification). Default: output/reports")
    parser.add_argument("--state-dir", default=None, help="Directory to read/write state cache (portfolio_context/option_positions_context/rate_cache/etc). Default: output/state")
    parser.add_argument("--shared-context-dir", default=None, help="Optional shared context cache directory for cross-account reuse within one tick")
    return parser


def _want(step: str) -> bool:
    s = str(step or "").strip().lower()
    if not s:
        return False

    if STAGE_ONLY is not None:
        if s == "alert":
            return STAGE_ONLY == "alert"
        if s == "notify":
            return STAGE_ONLY == "notify"
        return False

    order = {"fetch": 0, "scan": 1, "alert": 2, "notify": 3, "all": 3}
    cur = order.get(str(STAGE or "all"), 3)
    need = order.get(s)
    if need is None:
        return False
    return cur >= need


def main(argv: list[str] | None = None) -> int:
    global RUNTIME_MODE, IS_SCHEDULED, STAGE, STAGE_ONLY, SHARED_REQUIRED_DATA

    args = build_parser().parse_args(argv)

    RUNTIME_MODE = str(args.mode)
    IS_SCHEDULED = RUNTIME_MODE == "scheduled"
    STAGE = str(args.stage)
    STAGE_ONLY = str(args.stage_only) if args.stage_only else None
    SHARED_REQUIRED_DATA = str(args.shared_required_data) if getattr(args, "shared_required_data", None) else None

    base = Path(__file__).resolve().parents[2]
    cfg_path = Path(args.config)

    report_dir, state_dir = report_repo.prepare_dirs(
        base=base,
        report_dir=getattr(args, "report_dir", None),
        state_dir=getattr(args, "state_dir", None),
    )
    shared_context_dir = Path(args.shared_context_dir).resolve() if getattr(args, "shared_context_dir", None) else None

    if bool(getattr(args, "refresh_multiplier_cache", False)):
        try:
            from domain.domain.fetch_source import is_futu_fetch_source
            from scripts import multiplier_cache

            cache_path = multiplier_cache.default_cache_path(base)
            cfg0 = json.loads(cfg_path.read_text(encoding="utf-8"))
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
                    repo_base=base,
                    symbol=sym,
                    host=str(host),
                    port=int(port),
                    limit_expirations=1,
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

    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()

    cfg = load_config(
        base=base,
        config_path=cfg_path,
        is_scheduled=IS_SCHEDULED,
        log=log,
    )

    py = sys.executable

    if "symbols" in cfg:
        top_n = cfg.get("outputs", {}).get("top_n_alerts", 3)
        runtime = cfg.get("runtime", {}) or {}
        symbol_timeout_sec = int(runtime.get("symbol_timeout_sec", 120))
        portfolio_timeout_sec = int(runtime.get("portfolio_timeout_sec", 60))

        if STAGE_ONLY is not None:
            from scripts.pipeline_alert_steps import run_stage_only_alert_notify

            run_stage_only_alert_notify(
                report_dir=report_dir,
                stage_only=STAGE_ONLY,
                want=_want,
                log=log,
            )
            return 0

        report_repo.ensure_report_dir(report_dir)

        from scripts.pipeline_watchlist import run_watchlist_pipeline_default

        required_data_dir = Path(SHARED_REQUIRED_DATA).resolve() if SHARED_REQUIRED_DATA else (base / "output").resolve()

        summary_rows = run_watchlist_pipeline_default(
            py=py,
            base=base,
            cfg=cfg,
            report_dir=report_dir,
            state_dir=state_dir,
            shared_state_dir=shared_context_dir,
            required_data_dir=required_data_dir,
            is_scheduled=IS_SCHEDULED,
            top_n=top_n,
            symbol_timeout_sec=symbol_timeout_sec,
            portfolio_timeout_sec=portfolio_timeout_sec,
            want_scan=_want("scan"),
            no_context=bool(getattr(args, "no_context", False)),
            symbols_arg=getattr(args, "symbols", None),
            log=log,
            want_fn=_want,
        )

        symbols = [r.get("symbol") for r in summary_rows if r.get("symbol")]

        if (STAGE_ONLY is None) and (not _want("scan")):
            log(f"[INFO] stage={STAGE}: fetch done")
            return 0

        build_symbols_summary(summary_rows, report_dir, is_scheduled=IS_SCHEDULED)

        if not IS_SCHEDULED:
            build_symbols_digest(symbols, report_dir)

        changes_path = Path("/dev/null") if IS_SCHEDULED else (report_dir / "symbols_changes.txt").resolve()
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
        if _want("alert"):
            run_pipeline_alert_stage(
                summary_input=(report_dir / "symbols_summary.csv").resolve(),
                output=(report_dir / "symbols_alerts.txt").resolve(),
                changes_output=changes_path,
                previous_summary=((state_dir / "symbols_summary_prev.csv").resolve() if not IS_SCHEDULED else None),
                state_dir=state_dir,
                update_snapshot=(not IS_SCHEDULED),
                policy_json=policy_json,
            )

        if _want("notify"):
            run_pipeline_notification_stage(
                alerts_input=(report_dir / "symbols_alerts.txt").resolve(),
                changes_input=changes_path,
                output=(report_dir / "symbols_notification.txt").resolve(),
            )

            if IS_SCHEDULED and (report_dir == (base / "output" / "reports").resolve()):
                try:
                    import glob

                    keep = {
                        (report_dir / "symbols_summary.csv").resolve(),
                        (report_dir / "symbols_notification.txt").resolve(),
                    }
                    patterns = [
                        str((report_dir / "*sell_put_candidates*.csv").resolve()),
                        str((report_dir / "*sell_call_candidates*.csv").resolve()),
                        str((report_dir / "*sell_put_alerts*.txt").resolve()),
                        str((report_dir / "*sell_call_alerts*.txt").resolve()),
                        str((report_dir / "symbols_summary.txt").resolve()),
                        str((report_dir / "symbols_digest.txt").resolve()),
                        str((report_dir / "symbols_alerts.txt").resolve()),
                        str((report_dir / "symbols_changes.txt").resolve()),
                    ]
                    for pattern in patterns:
                        for fp in glob.glob(pattern):
                            candidate = Path(fp).resolve()
                            if candidate in keep:
                                continue
                            try:
                                if candidate.exists():
                                    candidate.unlink()
                            except Exception:
                                pass
                except Exception:
                    pass

        portfolio_cfg = cfg.get("portfolio", {}) or {}
        data_config = str(resolve_data_config_path(base=base, data_config=portfolio_cfg.get("data_config")))
        broker = str(portfolio_cfg.get("broker") or "富途")

        try:
            include_cash_footer = bool((cfg.get("notifications") or {}).get("include_cash_footer", True))
        except Exception:
            include_cash_footer = True

        if include_cash_footer and (not IS_SCHEDULED):
            cash_footer_accounts = cash_footer_accounts_from_config(cfg)
            run_cmd(
                [
                    py,
                    "scripts/append_cash_summary.py",
                    "--config",
                    str(cfg_path),
                    "--data-config",
                    str(data_config),
                    "--market",
                    str(broker),
                    "--accounts",
                    *cash_footer_accounts,
                    "--notification",
                    str((report_dir / "symbols_notification.txt").as_posix()),
                ],
                cwd=base,
                is_scheduled=IS_SCHEDULED,
            )

        notifications_cfg = cfg.get("notifications", {}) or {}
        if notifications_cfg.get("enabled", False):
            log("[INFO] notifications enabled in config; pipeline prepared notification text for sending.")
        else:
            log("[INFO] notifications disabled; generated notification text only.")
        if not IS_SCHEDULED:
            print("\n[DONE] Symbols pipeline finished")
            print(f"- {report_dir}/symbols_summary.csv")
            print(f"- {report_dir}/symbols_alerts.txt")
            print(f"- {report_dir}/symbols_changes.txt")
            print(f"- {report_dir}/symbols_notification.txt")
            print("")
        return 0

    top_n = cfg.get("outputs", {}).get("top_n_alerts", 3)
    process_symbol(py, base, cfg, top_n, report_dir=report_dir, state_dir=state_dir, is_scheduled=IS_SCHEDULED)
    print("\n[DONE] Single-symbol pipeline finished")
    print(f"- {report_dir}/{{symbol}}_sell_put_candidates*.csv / alerts.txt")
    print(f"- {report_dir}/{{symbol}}_sell_call_candidates.csv / alerts.txt")
    return 0
