"""Offline opening-chain comparison using the formal planner and disk rate gates.

Run from the repo root with ``python -m scripts.benchmark_opening_chain_warmup``.
Only provider transport and the rate gate's clock/sleep are replaced. Both arms
start with empty caches, use the same two-account union and default limits, and
run formal merged specs through execute_required_data_opend. Timings represent
planning + chain + contract snapshot work, not a complete scheduled Tick.
Totals count provider DATA requests (chain, expiration, snapshot, market-state).
Connections have a synthetic 0.1 s cost; real SDK readiness probes, connection
and shutdown costs are not modeled. The fork fixture inherits a network trap
whose shared trace records attempts even if application code catches the error.
"""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Iterator
from unittest.mock import patch

import pandas as pd

from src.application import market_data_provider as providers
from src.application import opend_market_snapshot_fetching as snapshots
from src.application import opend_symbol_chain_fetching as chains
from src.application.opend_symbol_fetching import _filter_option_chain_for_request
from src.application import opend_utils
from src.application.multi_tick import required_data_prefetch as prefetch
from src.application.opend_fetch_config import opend_fetch_kwargs, resolve_opend_fetch_config
from src.application.opend_rate_gate import OpenDRateGate
from src.application.required_data_fetching import (
    build_fetch_request_from_spec,
    execute_required_data_opend,
)
from src.application.required_data_prefetch_planning import build_cross_account_prefetch_config
from src.infrastructure.futu_gateway_pool import ThreadLocalFutuGatewayPool
from src.application.futu_portfolio_context import build_futu_portfolio_context, build_futu_position_snapshot


TRADING_DATE = date(2026, 11, 12)
EXPIRATIONS = ["2026-12-04", "2026-12-18"]
SYMBOLS = {
    "US": ["ALFA", "BETA", "GAMMA", "DELTA", "EPSI", "ZETA"],
    "HK": ["0700.HK", "9988.HK", "0005.HK", "1299.HK", "3690.HK", "9618.HK"],
}


class VirtualClock:
    def __init__(self) -> None:
        self.value = multiprocessing.get_context("fork").Value("d", 1000.0)

    def now(self) -> float:
        with self.value.get_lock():
            return float(self.value.value)

    def advance(self, seconds: float) -> None:
        with self.value.get_lock():
            self.value.value += max(0.000001, float(seconds))


def representative_config(market: str = "US") -> dict[str, Any]:
    symbols = SYMBOLS[market]
    configs = {}
    for account, min_dte, max_dte, min_call in (
        ("alpha", 15, 30, 105.0), ("beta", 25, 45, 110.0),
    ):
        configs[account] = {"symbols": [
            {
                "symbol": symbol, "broker": market,
                "fetch": {"source": "futu", "host": "offline", "port": 11111},
                "sell_put": {"enabled": index % 3 != 1, "strategy": "insurance_underwriting",
                             "min_dte": min_dte, "max_dte": max_dte,
                             "min_strike": 85.0, "max_strike": 95.0},
                "sell_call": {"enabled": index % 3 != 0, "strategy": "insurance_underwriting",
                              "min_dte": min_dte, "max_dte": max_dte,
                              "min_strike": min_call, "max_strike": 125.0},
            }
            for index, symbol in enumerate(symbols)
        ]}
    # No portfolio transport is involved; these are declared offline holdings.
    contexts = {}
    observed = datetime.now(timezone.utc).isoformat()
    for account in configs:
        rows = [{"code": opend_utils.normalize_underlier(symbol).code,
                 "qty": 100, "cost_price": 100.0, "can_sell_qty": 100,
                 "sec_type": "STOCK"} for symbol in symbols]
        snapshot = build_futu_position_snapshot(
            rows=rows, broker_account_ref={"broker_id": "futu", "external_account_id": account,
                "environment": "REAL", "account_label": account, "broker_account_id": f"futu:REAL:{account}"},
            markets=[market], asset_types=["stock", "option"],
            observed_at_utc=observed, completeness="complete",
        )
        contexts[account] = build_futu_portfolio_context(
            balance_rows=[], position_rows=rows, account=account, futu_account_id=account,
            broker_account_identifiers={account}, trd_env="REAL", capacity_market=market.lower(),
            source_observed_at=observed, position_snapshot_input=snapshot,
        )
    return build_cross_account_prefetch_config(
        base_config={"symbols": []}, account_configs=configs,
        prepared_portfolio_contexts=contexts,
    )


class FakeProvider:
    name = "opend"
    trading_date = TRADING_DATE

    def __init__(self, root: Path, clock: VirtualClock, *, linger: bool = False) -> None:
        self.root = root
        self.clock = clock
        self.phase = "warmup"
        self.linger = linger
        self.parent_pid = os.getpid()
        self.thread_started = False

    def record(self, endpoint: str, **data: Any) -> None:
        record = {"phase": self.phase, "endpoint": endpoint, "at": self.clock.now(), "pid": os.getpid(), **data}
        with (self.root / "provider.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")

    def open_quote_gateway(self, **_kwargs: Any) -> Any:
        self.record("connect")
        self.clock.advance(0.1)
        if self.linger and not self.thread_started and os.getpid() != self.parent_pid:
            self.thread_started = True
            # Models a provider callback thread surviving gateway.close().
            threading.Thread(target=threading.Event().wait, daemon=False).start()
        return FakeGateway(self)

    def get_pooled_quote_gateway(self, pool: Any, **kwargs: Any) -> Any:
        return pool.get_gateway(**kwargs)

    def get_underlier_observations(self, *args: Any, **kwargs: Any) -> Any:
        return snapshots.get_underlier_observations_opend(*args, **kwargs)

    def discover_option_expirations(self, *args: Any, **kwargs: Any) -> Any:
        return chains.discover_option_expirations(*args, **kwargs)

    def fetch_option_chains(self, **kwargs: Any) -> Any:
        request = kwargs["request"]
        self.record("chain_request", symbol=request.symbol, expirations=request.expirations,
                    option_types=request.option_types)
        return chains.fetch_option_chains(**kwargs)

    def fetch_symbol_request(self, request: Any) -> dict[str, Any]:
        # The formal request adapter and chain-fetch stage are real; snapshot
        # transport below is fake, with the same endpoint limiter as the worker.
        self.record("formal_request", symbol=request.symbol,
                    expirations=request.explicit_expirations, option_types=request.option_types)
        gateway = self.open_quote_gateway()
        try:
            result = chains.fetch_symbol_option_chain(
                gateway=gateway, request=request, underlier_code=opend_utils.normalize_underlier(request.symbol).code,
                today=self.trading_date,
                explicit_expirations_norm=request.explicit_expirations,
                limits=request.limits,
            )
            frame, _sides = _filter_option_chain_for_request(
                chain=pd.DataFrame(result.rows), option_types=request.option_types,
                min_strike=request.min_strike, max_strike=request.max_strike,
                side_strike_windows=request.side_strike_windows,
            )
            rows = frame.drop(columns=["_ot"]).to_dict("records")
            codes = [row["code"] for row in rows]
            if codes:
                snapshots.rate_limited_opend_call(
                    base_dir=request.effective_base_dir, endpoint="market_snapshot",
                    **request.limits.market_snapshot.call_kwargs(),
                    call=lambda: gateway.get_snapshot(codes),
                )
            return {"rows": rows, "meta": result.fetch_meta}
        finally:
            gateway.close()


class FakeGateway:
    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider

    def is_connected(self) -> bool:
        return True

    def close(self) -> None:
        self.provider.record("close")

    def get_snapshot(self, codes: list[str]) -> pd.DataFrame:
        self.provider.record("snapshot", codes=list(codes))
        self.provider.clock.advance(0.2)
        return pd.DataFrame([
            {"code": code, "last_price": 100.0, "update_time": "2026-11-12 10:00:00-05:00",
             "sec_status": "NORMAL", "suspension": False}
            for code in codes
        ])

    def get_market_state(self, codes: list[str]) -> pd.DataFrame:
        self.provider.record("market_state", codes=list(codes))
        self.provider.clock.advance(0.2)
        return pd.DataFrame([{"code": code, "market_state": "MORNING"} for code in codes])

    def get_option_expiration_dates(self, code: str) -> pd.DataFrame:
        self.provider.record("expiration", code=code)
        self.provider.clock.advance(0.2)
        return pd.DataFrame({"strike_time": EXPIRATIONS})

    def get_option_chain(self, *, code: str, start: str, end: str,
                         option_type: Any = None, **_kwargs: Any) -> pd.DataFrame:
        scope = str(getattr(option_type, "value", option_type) or "both").upper()
        self.provider.record("chain", code=code, expiration=start, option_type=scope)
        self.provider.clock.advance(0.4)
        sides = ["PUT", "CALL"] if scope == "BOTH" else [scope]
        return pd.DataFrame([
            {"code": f"{code}.{start}.{side}.{strike}", "strike_time": start, "expiration": start,
             "option_type": side, "strike_price": strike, "lot_size": 100}
            for side in sides for strike in (90.0, 100.0, 110.0, 130.0)
        ])


@contextmanager
def offline_runtime(root: Path, *, linger: bool = False) -> Iterator[FakeProvider]:
    root.mkdir(parents=True, exist_ok=True)
    clock = VirtualClock()
    provider = FakeProvider(root, clock, linger=linger)
    real_gate_init = OpenDRateGate.__init__

    def gate_init(self: Any, **kwargs: Any) -> None:
        kwargs.update(clock=clock.now, sleep=clock.advance)
        real_gate_init(self, **kwargs)
        self._wall_clock = clock.now
        # Gates are exercised serially here: replace only their condition wait
        # with virtual elapsed time, retaining the real sliding-window checks.
        self._cv.wait = lambda timeout=None: clock.advance(timeout or 0.0)

    def reject_network(*_args: Any, **_kwargs: Any) -> None:
        provider.record("forbidden_network")
        raise AssertionError("offline benchmark attempted network transport")

    fork_context = multiprocessing.get_context("fork")
    with ExitStack() as stack:
        # A reused temporary path must not retain a gate bound to another
        # fixture's virtual clock. Restore the real registry on context exit.
        stack.enter_context(patch("src.application.option_chain_fetching._RATE_GATE_CACHE", {}))
        for target, replacement in (
            ("socket.socket.connect", reject_network),
            ("socket.create_connection", reject_network),
            ("src.application.market_data_provider.resolve_market_data_provider", lambda _cfg=None: provider),
            ("src.application.opend_symbol_chain_fetching.build_ready_futu_quote_gateway", provider.open_quote_gateway),
            ("src.infrastructure.futu_gateway.build_ready_futu_quote_gateway", provider.open_quote_gateway),
            ("src.application.multi_tick.required_data_prefetch.multiprocessing.get_context", lambda _mode: fork_context),
            ("src.application.opend_utils.get_trading_date", lambda _market: provider.trading_date),
            ("src.application.multi_tick.required_data_prefetch._gateway_pool", ThreadLocalFutuGatewayPool()),
            ("src.application.opend_rate_gate.OpenDRateGate.__init__", gate_init),
        ):
            stack.enter_context(patch(target, replacement))
        yield provider


def formal_plan(root: Path, config: dict[str, Any]) -> list[Any]:
    limits = prefetch._resolve_opend_fetch_cfg(config)
    symbols = prefetch.build_prefetch_symbol_plan(prefetch.resolve_watchlist_config(config)).symbol_cfgs
    observations: dict[Any, Any] = {}
    identities, failed = prefetch._prefill_spot_observation_cache(
        symbol_cfgs=symbols, base=root, opend_fetch_cfg=limits,
        snapshot_batch_size=200, spot_observation_cache=observations,
    )
    assert not failed, failed
    expirations: dict[Any, Any] = {}
    plans = [prefetch._build_prefetch_fetch_plan(
        item, base=root, shared_required=root / "output_shared/required_data",
        opend_fetch_cfg=limits, expiration_discovery_cache=expirations,
        spot_observation_cache=observations, planning_identities=identities,
    ) for item in symbols]
    assert all(plan.projection_outcome == "success_rows" for plan in plans), [plan.to_debug_dict() for plan in plans]
    return plans


def warmup_kwargs(root: Path, config: dict[str, Any], *, seconds: float = 5.0) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return dict(
        base=root, base_cfg=config, cfg_path=root / "config.us.json", accounts=["alpha", "beta"],
        markets_to_run=[config["symbols"][0]["broker"]], symbols_arg=None,
        next_formal_target_utc=(now + timedelta(seconds=seconds + 120)).isoformat(),
        worker_stop_at_utc=(now + timedelta(seconds=seconds)).isoformat(),
        lock_release_by_utc=(now + timedelta(seconds=seconds + 1)).isoformat(),
    )


def run_comparison(root: Path, *, warmup: bool, market: str = "US", linger: bool = False) -> dict[str, Any]:
    config = representative_config(market)
    with offline_runtime(root, linger=linger) as provider:
        total_started = provider.clock.now()
        summary = prefetch.warm_required_data_chain_cache(**warmup_kwargs(root, config)) if warmup else None
        warmup_seconds = provider.clock.now() - total_started
        # Same pre-opening lead for each arm; rate-state and day cache survive.
        provider.clock.advance(max(0.0, 120.0 - warmup_seconds))
        provider.phase = "formal"
        formal_started = provider.clock.now()
        plans = formal_plan(root, config)
        plan_debug = [plan.to_debug_dict() for plan in plans]
        payloads = [execute_required_data_opend(
            base=root, request=build_fetch_request_from_spec(
                spec=spec, chain_cache=True,
                opend_fetch_config=opend_fetch_kwargs(config),
                spot_override=plan.spot_reference,
                underlier_observation=plan.underlier_observation.to_dict(),
            ),
        ) for plan in plans for spec in plan.merged_specs]
        assert all(payload["meta"]["status"] == "ok" for payload in payloads), payloads
        records = [json.loads(line) for line in (root / "provider.jsonl").read_text().splitlines()]
        forbidden_network_calls = sum(row["endpoint"] == "forbidden_network" for row in records)
        assert forbidden_network_calls == 0, records
        counts = {phase: dict(Counter(row["endpoint"] for row in records if row["phase"] == phase))
                  for phase in ("warmup", "formal")}
        endpoint_names = {"snapshot", "market_state", "expiration", "chain"}
        total_calls = sum(row["endpoint"] in endpoint_names for row in records)
        return {
            "warmup": summary, "warmup_seconds": round(warmup_seconds, 3),
            "opening_seconds": round(provider.clock.now() - formal_started, 3),
            "counts": counts, "total_provider_data_calls": total_calls,
            "forbidden_network_calls": forbidden_network_calls,
            "total_chain_calls": sum(row["endpoint"] == "chain" for row in records),
            "opening_chain_calls": sum(row["endpoint"] == "chain" and row["phase"] == "formal" for row in records),
            "opening_cache_hits": sum(len(payload["meta"]["from_cache_expirations"]) for payload in payloads),
            "limits": resolve_opend_fetch_config(config),
            "plans": plan_debug, "records": records,
            "output_rows": [payload["rows"] for payload in payloads],
            "formal_open_at": formal_started,
            "opening_rate_gate_wait_seconds": round(sum(payload["meta"]["rate_gate_wait_sec"] for payload in payloads), 3),
            "chain_cache_bytes": sum(path.stat().st_size for path in (root / "cache/opend_option_chain").rglob("*.json")),
        }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="om-opening-chain-offline-") as raw:
        root = Path(raw)
        for market in SYMBOLS:
            for enabled in (False, True):
                result = run_comparison(root / market / ("warm" if enabled else "cold"), warmup=enabled, market=market)
                for key in ("plans", "records", "output_rows"):
                    result.pop(key)
                print(json.dumps({"market": market, **result}, indent=2))


if __name__ == "__main__":
    main()
