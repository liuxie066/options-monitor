"""Offline boundary adapters for the whole Tick benchmark.

Only provider observations and process transport are replaced. Required-data
planning/finalization, pipeline, ledger, Brief and Tick owners execute real code.
No readiness or delivery confirmation is synthesized.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager, redirect_stdout, redirect_stderr
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

NOW = datetime(2026, 9, 28, 2, 0, 30, tzinfo=timezone.utc)
ACCOUNTS = ("lx", "sy")
SYMBOLS = ("3690.HK", "0700.HK")
RUN_ID = "offline-tick-20260928"


def seed_tick_inputs(base: Path, *, days=35, fill_dates=8, fills_per_date=3, preparation_inputs=None):
    from daily_brief_preparation_fixtures import seed_preparation_inputs
    from src.application.config_defaults import DEFAULT_CONFIG
    if preparation_inputs:
        shutil.copytree(preparation_inputs, base)
        meta = {"preparation_inputs": str(preparation_inputs), "history_days": days,
                "fill_dates": fill_dates, "fills_per_date": fills_per_date}
    else:
        meta = seed_preparation_inputs(base, days=days, fill_dates=fill_dates,
                                       fills_per_date=fills_per_date)
    original = json.loads((base / "benchmark_config.json").read_text())
    cfg = deepcopy(DEFAULT_CONFIG["defaults"])
    cfg.update(original)
    cfg.update({"accounts": list(ACCOUNTS), "_generated": {
        "schema_version": "1.0", "generator": "options-monitor", "source_format": "yaml", "market": "hk"}})
    cfg["account_settings"] = {"lx": {"type": "futu", "futu": {"account_id": "1001", "trd_env": "REAL", "host": "127.0.0.1", "port": 11111}},
                               "sy": {"type": "futu", "futu": {"account_id": "1002", "trd_env": "REAL", "host": "127.0.0.1", "port": 11111}}}
    cfg["runtime"] = {**DEFAULT_CONFIG["defaults"]["runtime"], "account_max_workers": 1, "prefetch_max_workers": 1}
    cfg["trade_intake"] = {"enabled": False}
    cfg["option_positions"] = {"auto_close": {"enabled": False}}
    cfg["wheel"] = {"accounts": []}
    cfg["close_advice"] = {"enabled": False}
    cfg["schedule"] = {"enabled": True, "timezone": "Asia/Hong_Kong",
        "run_window": {"start": "09:30", "end": "16:00", "breaks": []},
        "run_points": {"start_plus_min": 10, "hourly_minute": 0, "end_minus_min": 10}, "cron_interval_min": 10}
    cfg["notifications"].update(enabled=True, provider="feishu_app", channel="feishu_app")
    cfg["notifications"].pop("target", None)
    cfg["portfolio"] = {**DEFAULT_CONFIG["defaults"]["portfolio"], "source": "futu"}
    cfg["symbols"] = [{"symbol": symbol, "broker": "HK", "use": ["put_base"], "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
        "sell_put": {"enabled": True, "strategy": "insurance_underwriting", "min_dte": 20, "max_dte": 45,
                     "max_abs_delta": 0.35,
                     "min_iv_rv_ratio": 1.1, "min_iv_minus_rv": 0.05},
        "sell_call": {"enabled": False}} for symbol in SYMBOLS]
    cfg["opend"] = {"fetch": {"execution_mode": "inprocess", "max_workers": 1}}
    # Provider observations are immutable inputs, separate from measured runtime artifacts.
    from cash_evidence_helpers import cash_portfolio
    observations = {}
    for account, ident in (("lx", "1001"), ("sy", "1002")):
        observations[account] = cash_portfolio({"as_of_utc": NOW.isoformat(),
            "cash_by_currency": {"HKD": 480000.0, "USD": 18000.0},
            "stocks_by_symbol": {}, "filters": {"broker": "富途", "account": account},
            "capacity_authority": {"market": "hk"}},
            account=account, account_id=ident, evaluated_at=NOW)
    (base / "frozen_portfolio.json").write_text(json.dumps(observations, ensure_ascii=False, sort_keys=True))
    provider_spec = {"clock": NOW.isoformat(), "expirations": ["2026-10-23"], "put_dte": [20, 45],
        "spots": {"3690.HK": 75.0, "0700.HK": 440.0},
        "strike_windows": {"3690.HK": [60.0, 75.0], "0700.HK": [352.0, 440.0]},
        "premium_fraction_of_spot": 0.025, "earnings_events": [],
        "discovery_failure_symbol": "0700.HK", "failure_outcome": "provider_error", "failure_reason": "timeout",
        "failure_latency_seconds": 0, "accounts": list(ACCOUNTS)}
    (base / "frozen_provider_spec.json").write_text(json.dumps(provider_spec, sort_keys=True))
    # The seed's run artifacts belong to an isolated preparation fixture, not this Tick.
    shutil.rmtree(base / "output_runs")
    (base / "config.hk.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True))
    (base / "config.yaml").write_text("# Offline benchmark admission placeholder; admission excluded.\n")
    meta.update(accounts=list(ACCOUNTS), symbols=list(SYMBOLS), strategy_universe={"put_dte": [20, 45], "call": "disabled"},
                scenario_clock=NOW.isoformat(), pending_attribution_account="lx", sy_ledger="empty; account-isolation control")
    return meta


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls.fromtimestamp(NOW.timestamp(), tz=tz or timezone.utc)
        return value if tz else value.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return cls.now()


class FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(NOW.year, NOW.month, NOW.day)


class FrozenGateway:
    host = "127.0.0.1"
    port = 11111
    def close(self):
        pass
    def get_earnings_calendar(self, **kwargs):
        return []


class FrozenMarketProvider:
    name = "opend"
    def __init__(self, scenario, trace, spec):
        self.scenario, self.trace, self.spec = scenario, trace, spec
        if spec["clock"] != NOW.isoformat():
            raise AssertionError("fixture clock differs from sealed provider inputs")

    def open_quote_gateway(self, **kwargs):
        self.trace.append({"boundary": "open_quote_gateway", "fixture": True})
        return FrozenGateway()

    def get_pooled_quote_gateway(self, pool, **kwargs):
        return self.open_quote_gateway(**kwargs)

    def resolve_quote_route(self, config, **kwargs):
        from om_market_opend import OpenDMarketDataProvider
        return OpenDMarketDataProvider().resolve_quote_route(config, **kwargs)

    def get_underlier_observation(self, symbol, **kwargs):
        from src.application.opening_quote_evidence import OpeningUnderlierObservation
        from domain.domain.symbol_identity import resolve_symbol_identity
        identity = resolve_symbol_identity(symbol)
        if identity.canonical not in SYMBOLS:
            raise AssertionError(f"unfrozen underlier scope: {identity.canonical}")
        return OpeningUnderlierObservation(schema_version="opening_underlier_observation.v1", status="ready",
            code=identity.futu_code, market="HK",
            last_price=self.spec["spots"][identity.canonical],
            update_time="2026-09-28 10:00:30", observed_at_utc=NOW.isoformat(), age_seconds=0.0,
            market_state="MORNING", sec_status="NORMAL", suspension=False, reason_code=None)

    def get_underlier_observations(self, gateway, codes, **kwargs):
        return {code: self.get_underlier_observation(code.removeprefix("HK.")) for code in codes}

    def discover_option_expirations(self, symbol, **kwargs):
        from src.application.opend_symbol_chain_fetching import OptionExpirationDiscoveryResult
        from domain.domain.symbol_identity import resolve_symbol_identity
        if symbol not in SYMBOLS:
            raise AssertionError(f"unfrozen discovery scope: {symbol}")
        identity = {"symbol": symbol, "underlier": resolve_symbol_identity(symbol).futu_code,
                    "source": kwargs.get("source", "futu"), "host": kwargs.get("host", "127.0.0.1"),
                    "port": kwargs.get("port", 11111), "trading_date": "2026-09-28"}
        failed = self.scenario == "localized_discovery_failure" and symbol == self.spec["discovery_failure_symbol"]
        self.trace.append({"boundary": "discover_option_expirations", "symbol": symbol,
                           "outcome": "provider_error" if failed else "success_rows"})
        return OptionExpirationDiscoveryResult(outcome="provider_error" if failed else "success_rows",
            reason_code="timeout" if failed else None, expirations=[] if failed else list(self.spec["expirations"]),
            observed_at_utc=NOW.isoformat(), completed_at_utc=NOW.isoformat(), request_identity=identity,
            error="offline injected discovery timeout" if failed else None)

    def prefetch_earnings_calendars(self, **kwargs):
        from src.application.earnings_calendar import prefetch_market_earnings_calendars
        self.trace.append({"boundary": "earnings_sdk_fixture", "raw_events": 0})
        return prefetch_market_earnings_calendars(**kwargs, gateway_builder=lambda **kw: FrozenGateway())

    def fetch_symbol_request(self, request, **kwargs):
        self.trace.append({"boundary": "fetch_symbol_request_enter", "symbol": request.symbol})
        from test_required_data_prefetch_inprocess import _strict_success_rows_payload
        symbol = request.symbol
        if symbol not in SYMBOLS or request.option_types != "put" or request.explicit_expirations != self.spec["expirations"]:
            raise AssertionError("fetch scope differs from frozen HK put universe")
        spot = float(request.spot_override or (75 if symbol == "3690.HK" else 440))
        self.trace.append({"boundary": "fetch_scope", "symbol": symbol, "option_types": request.option_types,
            "explicit_expirations": list(request.explicit_expirations), "min_dte": request.min_dte, "max_dte": request.max_dte,
            "side_strike_windows": request.side_strike_windows, "spot": spot})
        rows = []
        bid = round(spot * self.spec["premium_fraction_of_spot"], 2)
        window = (request.side_strike_windows or {}).get("put", {})
        if [window.get("min_strike"), window.get("max_strike")] != self.spec["strike_windows"][symbol]:
            raise AssertionError("strike window differs from sealed provider universe")
        if spot != self.spec["spots"][symbol]:
            raise AssertionError("underlier price differs from sealed provider universe")
        strikes = sorted({round(spot * 0.90, 2), round(spot * 0.95, 2),
                          float(window.get("min_strike") or spot * 0.8),
                          float(window.get("max_strike") or spot)})
        from domain.domain.symbol_identity import resolve_symbol_identity
        code = resolve_symbol_identity(symbol).futu_code
        for expiration in request.explicit_expirations or ["2026-10-23"]:
            for index, strike in enumerate(strikes):
                rows.append({"symbol": symbol, "market": "HK", "option_type": "put", "contract_symbol": f"{code}261023P{int(strike*1000):08d}",
                    "expiration": expiration, "dte": (date.fromisoformat(expiration) - NOW.date()).days,
                    "strike": strike, "spot": spot, "bid": bid, "ask": bid + 0.1, "mid": bid + 0.05,
                    "last_price": bid + 0.05, "volume": 100, "open_interest": 1000, "implied_volatility": 0.6,
                    "in_the_money": False, "otm_pct": (spot - strike) / spot,
                    "delta": -0.2, "multiplier": 500, "currency": "HKD"})
        from conftest import phase2_opening_row
        rows = [phase2_opening_row({**row, "quote_update_time": "2026-09-28 10:00:30",
            "quote_observed_at_utc": NOW.isoformat(), "quote_age_seconds": 0,
            "snapshot_received_at_utc": NOW.isoformat(), "spot_update_time": "2026-09-28 10:00:30",
            "spot_observed_at_utc": NOW.isoformat(), "spot_age_seconds": 0,
            "decision_now_utc": NOW.isoformat()}) for row in rows]
        payload = _strict_success_rows_payload(symbol, rows, trading_date="2026-09-28",
                    meta={"underlier_observation": request.underlier_observation,
                          "source_observed_at": NOW.isoformat(), "completed_at_utc": NOW.isoformat()})
        rv = payload["meta"]["realized_volatility"]
        rv["qfq_history"].update(market="HK", cache_identity=f"HK:{code}:QFQ")
        rv["trading_calendar"]["market"] = "HK"
        self.trace.append({"boundary": "fetch_symbol_request", "symbol": symbol, "rows": len(rows)})
        return payload


def _cli_command(cmd, *, cwd, capture_output=False, text=False, **kwargs):
    if len(cmd) < 4 or cmd[1:3] != ["-m", "src.interfaces.cli.main"]:
        raise AssertionError(f"unapproved subprocess boundary: {cmd[:4]}")
    from src.interfaces.cli.main import main
    out, err = io.StringIO(), io.StringIO()
    with patch.dict(os.environ, kwargs.get("env") or {}), redirect_stdout(out), redirect_stderr(err):
        try:
            rc = main(cmd[3:])
        except SystemExit as exc:
            if isinstance(exc.code, (int, type(None))):
                rc = int(exc.code or 0)
            else:
                err.write(str(exc.code))
                rc = 1
    return subprocess.CompletedProcess(cmd, rc, out.getvalue() if text else out.getvalue().encode(),
                                       err.getvalue() if text else err.getvalue().encode())


def _inline_subprocess_run(cmd, **kwargs):
    if len(cmd) >= 4 and cmd[1:3] == ["-m", "src.application.close_advice_required_data"]:
        from src.application.close_advice_required_data import enrich_close_advice_required_data_plan
        enrich_close_advice_required_data_plan(plan_path=Path(cmd[3]), expected_run_id=cmd[4])
        return subprocess.CompletedProcess(cmd, 0, "", "")
    raise AssertionError(f"unhandled subprocess.run: {cmd[:4]}")


class InlinePortfolioWorker:
    def __init__(self, cmd, **kwargs):
        if cmd[1:3] != ["-m", "src.application.prepared_portfolio_context"]:
            raise AssertionError("unexpected prepared worker command")
        from src.application.prepared_portfolio_context import run_worker
        self.returncode = run_worker(Path(cmd[-1]))
    def poll(self):
        return self.returncode
    def wait(self, timeout=None):
        return self.returncode
    def terminate(self):
        raise AssertionError("inline completed worker cannot be terminated")
    kill = terminate


@contextmanager
def offline_tick_boundaries(base, *, scenario, trace):
    from src.application import multi_account_tick as tick, prepared_portfolio_context as prepared
    from src.application import tick_account_execution as execution
    from src.application import market_data_provider as market
    from src.infrastructure import external_services
    from src.application import tick_guard_flow
    from src.application import multi_tick_watchdog
    from src.application import pipeline_runtime, pipeline_context
    from src.application import daily_decision_brief_service, tick_notification_flow
    from src.infrastructure import exchange_rates
    # Load the real owners before freezing their imported clocks. Future imports
    # of datetime are handled by a second clock sweep before account work starts.
    for name in ("src.application.required_data_planning", "src.application.multi_tick.required_data_prefetch",
                 "src.application.opend_utils", "src.application.opend_symbol_chain_fetching",
                 "src.application.trades.attribution", "src.application.ledger.trade_attribution",
                 "src.application.positions.context_builder", "src.application.prepared_option_positions_context",
                 "src.interfaces.cli.main", "src.application.config_yaml"):
        importlib.import_module(name)
    spec = json.loads((base / "frozen_provider_spec.json").read_text())
    provider = FrozenMarketProvider(scenario, trace, spec)
    observations = json.loads((base / "frozen_portfolio.json").read_text())
    original_prepare = prepared.prepare_portfolio_contexts
    original_portfolio = prepared.fetch_futu_portfolio_context
    fx_pairs = {"schema_version": 2, "pairs": {pair: {"rate": rate, "source": "tencent_quote",
        "quote_at_utc": NOW.isoformat(), "observed_at_utc": NOW.isoformat()}
        for pair, rate in (("USDCNY", 7.2), ("HKDCNY", 0.92))}}
    def prepare(**kwargs):
        return original_prepare(**kwargs, popen_factory=InlinePortfolioWorker)
    def portfolio(*args, **kwargs):
        account = kwargs.get("account") or "lx"
        trace.append({"boundary": "portfolio_provider", "account": account})
        return deepcopy(observations[account])
    def forbidden_network(*args, **kwargs):
        trace.append({"boundary": "forbidden_network", "attempted": True})
        raise AssertionError("offline benchmark attempted a network connection")
    def forbidden_process(*args, **kwargs):
        import traceback
        trace.append({"boundary": "forbidden_subprocess", "attempted": True,
                      "command": args[0] if args else kwargs.get("args"), "stack": traceback.format_stack(limit=8)})
        raise AssertionError("offline benchmark attempted an unhandled child process")
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"OM_RUNTIME_ROOT": str(base), "PYTHONDONTWRITEBYTECODE": "1", "OM_FEISHU_BOT_USER_OPEN_ID": "ou_offline_tick_fixture", "OM_SECRET_BACKEND": "env", "OM_TRIGGER_SOURCE": "cron", "OM_TRIGGER_JOB_ID": "offline-tick-benchmark"}))
        stack.enter_context(patch.dict(market._BUILTIN_PROVIDERS, {"opend": provider}))
        stack.enter_context(patch.object(socket.socket, "connect", forbidden_network))
        stack.enter_context(patch.object(socket.socket, "connect_ex", forbidden_network))
        stack.enter_context(patch.object(socket.socket, "sendto", forbidden_network))
        stack.enter_context(patch.object(socket, "create_connection", forbidden_network))
        stack.enter_context(patch.object(socket, "getaddrinfo", forbidden_network))
        stack.enter_context(patch.object(subprocess, "Popen", forbidden_process))
        stack.enter_context(patch.object(subprocess, "run", _inline_subprocess_run))
        stack.enter_context(patch.object(external_services, "run_command", _cli_command))
        stack.enter_context(patch.object(exchange_rates, "fetch_market_exchange_rates", lambda **kwargs: deepcopy(fx_pairs)))
        stack.enter_context(patch("time.time", return_value=NOW.timestamp()))
        stack.enter_context(patch.object(tick, "trading_day_via_futu", lambda **kwargs: (True, "offline frozen HK calendar")))
        stack.enter_context(patch("src.infrastructure.run_log.create_run_id", return_value=RUN_ID))
        stack.enter_context(patch.object(execution, "prepare_portfolio_contexts", prepare))
        # Provider adapter is frozen; the prepared-worker envelope and validators remain real.
        for module in tuple(sys.modules.values()):
            if module is None or not getattr(module, "__name__", "").startswith(("src.", "domain.")):
                continue
            if getattr(module, "fetch_futu_portfolio_context", None) is original_portfolio:
                stack.enter_context(patch.object(module, "fetch_futu_portfolio_context", portfolio))
        # CLI config-origin freshness and live OpenD preflight are explicitly outside
        # this offline execution benchmark. There is no synthetic watchdog result.
        for name in ("ensure_runtime_canonical_config", "ensure_runtime_config_freshness"):
            if hasattr(tick, name):
                stack.enter_context(patch.object(tick, name, lambda *a, **k: None))
        stack.enter_context(patch.object(tick_guard_flow, "required_opend_probes", lambda *a, **k: []))
        stack.enter_context(patch.object(multi_tick_watchdog, "required_opend_probes", lambda *a, **k: []))
        for module in tuple(sys.modules.values()):
            if module is None or not getattr(module, "__name__", "").startswith(("src.", "domain.", "test_required_data_prefetch")):
                continue
            if getattr(module, "datetime", None) is datetime:
                stack.enter_context(patch.object(module, "datetime", FrozenDateTime))
            if getattr(module, "date", None) is date:
                stack.enter_context(patch.object(module, "date", FrozenDate))
        yield tick
