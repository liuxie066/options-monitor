#!/usr/bin/env python3
"""Offline business-entry read counts and local timings; never production latency."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import CancelledError
from contextlib import ExitStack, nullcontext, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 9, 4, tzinfo=timezone.utc)
OPTION = {"context_status": "available", "decision_snapshot_status": "trusted",
          "locked_shares_status": "available", "locked_shares_by_symbol": {},
          "cash_secured_total_by_ccy": {}, "cash_secured_by_symbol_by_ccy": {},
          "cash_secured_unavailable_by_symbol": {}}
CASES = (
    "scan_cold", "scan_warm", "cash_cli_cold", "cash_tool_cold", "cash_tool_warm",
    "cross_entry", "prepared", "prepared_new_run", "prepared_bad_hash",
    "prepared_bad_account", "account_isolation", "physical_account_isolation",
    "path_isolation", "stock_301_cash_fresh", "stock_option_61",
    "partial_stock", "positions_failure", "balance_failure", "timeout", "cancel",
    "assignment_pm_unavailable", "wheel_cli_cold", "wheel_tool_cold",
)


class IsolationError(RuntimeError):
    pass


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class Harness:
    def __init__(self, root: Path, stack: ExitStack):
        root = root.resolve()
        self.root = root
        self.counts = Counter()
        self.trace = []
        self.contexts = []
        self.stages = []
        self.preparation = {}
        self.mode = "ok"
        self.blocked = 0
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        previous_futu = sys.modules.get("futu")
        sys.modules["futu"] = SimpleNamespace(RET_OK=0)
        def restore_futu():
            if previous_futu is None:
                sys.modules.pop("futu", None)
            else:
                sys.modules["futu"] = previous_futu
        stack.callback(restore_futu)
        stack.enter_context(patch.dict("os.environ", {"OM_RUNTIME_ROOT": str(root)}))
        for obj, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                          (socket, "create_connection"), (urllib.request, "urlopen")):
            stack.enter_context(patch.object(obj, name, self.deny_network))
        from src.application import cash_headroom_query as cash
        from src.application import futu_portfolio_context as provider
        from src.application import pipeline_context as pipeline
        from src.application import portfolio_assignment_scenario as assignment
        from src.application import portfolio_context_service as reader
        from src.application import prepared_portfolio_context as prepared
        from src.application.agent_tools import materialization_impl, positions
        from src.application.wheel import capacity
        from src.infrastructure import exchange_rates as fx
        from src.infrastructure.futu_gateway import FutuGateway, _FutuAPIClient
        from src.interfaces.cli import wheel
        from tests.cash_evidence_helpers import cash_config, cash_portfolio
        self.cash, self.provider, self.pipeline = cash, provider, pipeline
        self.assignment, self.reader, self.prepared = assignment, reader, prepared
        self.materialization, self.positions, self.wheel = materialization_impl, positions, wheel
        self.cash_portfolio = cash_portfolio
        self.cfg = cash_config()
        self.cfg["portfolio"].update(account="lx", broker="富途", data_config=str(root / "data.json"))
        self.cfg["portfolio"]["futu"] = {"host": "offline.invalid", "port": 1}
        self.cfg["accounts"] = ["lx", "sy"]
        self.cfg["account_settings"]["sy"] = {"futu": {"account_id": "456", "trd_env": "REAL"}}
        self.state = root / "output_accounts/lx/state"
        self.repo = SimpleNamespace(read_lifecycle_account_rows=self.ledger_rows)
        self.fx_snapshot = {"calendar": fx.CALENDAR_EVIDENCE, "pairs": {
            pair: {"rate": rate, "source": "tencent_quote", "quote_at_utc": NOW.isoformat(),
                   "observed_at_utc": NOW.isoformat()}
            for pair, rate in (("USDCNY", 7.2), ("HKDCNY", 0.92))}}
        stack.enter_context(patch.object(fx, "_utc_now", lambda: NOW))
        self.fx = fx.project_exchange_rate_snapshot(self.fx_snapshot, purpose="capacity")
        for module in (cash, provider, pipeline, assignment, reader, prepared, capacity):
            if hasattr(module, "datetime"):
                stack.enter_context(patch.object(module, "datetime", Clock))
        actual_reader, actual_fetch = reader.load_account_portfolio_context, provider.fetch_futu_portfolio_context

        def read(**kwargs):
            self.ensure_path(kwargs["state_dir"])
            self.hit("shared_reader", str(kwargs.get("account")))
            result = actual_reader(**kwargs)
            self.contexts.append({"source": result.get("context_source"),
                                  "cash": result.get("cash_snapshot", {}).get("status"),
                                  "stock_errors": reader.portfolio_position_snapshot_errors(result)})
            return result

        def fetch(**kwargs):
            self.hit("portfolio_fetch", str(kwargs.get("account")))
            if self.mode in {"timeout", "cancel"}:
                raise TimeoutError("fixture") if self.mode == "timeout" else CancelledError("fixture")
            result = actual_fetch(**kwargs)
            if self.mode == "partial":
                result["position_snapshot_input"]["completeness"] = "partial"
            return result

        def ready(**_kwargs):
            self.hit("ready_factory_substitute")
            trade = SimpleNamespace(accinfo_query=self.balance, position_list_query=self.stock,
                                    close=lambda: None)
            backend = SimpleNamespace(_trade_client=trade, _quote_client=None,
                                      _ensure_trade_client=lambda: trade)
            return FutuGateway(_FutuAPIClient(backend, is_option_chain_cache_enabled=False), backend, "offline.invalid", 1)

        for module in (cash, pipeline, assignment, prepared, capacity):
            stack.enter_context(patch.object(module, "load_account_portfolio_context", read))
            stack.enter_context(patch.object(module, "fetch_futu_portfolio_context", fetch))
        self.read, self.fetch = read, fetch
        stack.enter_context(patch.object(provider, "build_ready_futu_broker_gateway", ready))
        stack.enter_context(patch.object(provider, "_fetch_market_exchange_rate_observation", self.fx_read))
        for module in (cash, pipeline):
            stack.enter_context(patch.object(module, "get_exchange_rates_or_fetch_latest", self.fx_read))
        stack.enter_context(patch.object(assignment, "current_exchange_rate_snapshot", self.fx_snapshot_read))
        for module in (wheel, positions):
            stack.enter_context(patch.object(module, "load_current_exchange_rate_snapshot", self.fx_read))
        stack.enter_context(patch.object(cash, "_load_option_position_records", self.cash_ledger))
        stack.enter_context(patch.object(cash, "build_option_positions_context", self.option))
        stack.enter_context(patch.object(pipeline, "load_option_positions_context", self.pipeline_option))
        stack.enter_context(patch.object(pipeline, "_persist_source_snapshot", self.source_snapshot))
        stack.enter_context(patch.object(prepared.state_repo, "append_source_snapshot_event", self.source_snapshot))
        for module in (pipeline, prepared):
            stack.enter_context(patch.object(module, "prepare_position_sizing_evidence", self.sizing))
        stack.enter_context(patch.object(capacity, "with_sqlite_repo_writer_lock", lambda _: nullcontext()))
        stack.enter_context(patch.object(capacity, "decision_state_snapshot_from_locked_rows", self.decision))
        stack.enter_context(patch.object(capacity, "build_context", self.option))
        stack.enter_context(patch.object(assignment, "_load_runtime_and_positions", self.assignment_ledger))
        stack.enter_context(patch.object(assignment, "_read_futu_quotes", self.quotes))
        stack.enter_context(patch.object(assignment, "read_portfolio_valuation_evidence", self.pm))

    def hit(self, name, detail=""):
        self.counts[name] += 1
        self.trace.append([name, detail])

    def ensure_path(self, path):
        if not Path(path).resolve().is_relative_to(self.root):
            raise IsolationError(f"fixture path escaped: {path}")

    def deny_network(self, *_args, **_kwargs):
        self.blocked += 1
        raise IsolationError("unexpected network access")

    def balance(self, **kwargs):
        self.hit("sdk_accinfo_substitute", str(kwargs["acc_id"]))
        if self.mode == "balance_failure":
            raise OSError("offline balance failure")
        return 0, [{"acc_id": kwargs["acc_id"], "trd_env": "REAL", "cn_cash": 1000}]

    def stock(self, **kwargs):
        self.hit("sdk_positions_substitute", str(kwargs["acc_id"]))
        if self.mode == "positions_failure":
            raise OSError("offline positions failure")
        return 0, [{"acc_id": kwargs["acc_id"], "trd_env": "REAL", "code": "US.NVDA",
                    "sec_type": "STOCK", "position_side": "LONG", "qty": 100,
                    "can_sell_qty": 100, "cost_price": 100, "currency": "USD"}]

    def fx_read(self, *_args, **_kwargs):
        self.hit("fx_fixture")
        return deepcopy(self.fx)

    def fx_snapshot_read(self, **_kwargs):
        self.hit("fx_fixture")
        return deepcopy(self.fx_snapshot)

    def option(self, *_args, **_kwargs):
        self.hit("option_context_fixture")
        return deepcopy(OPTION)

    def pipeline_option(self, **_kwargs):
        return self.option(), False

    def cash_ledger(self, *_args, **_kwargs):
        self.hit("ledger_fixture")
        return [], {"snapshot_status": "trusted"}

    def ledger_rows(self, **_kwargs):
        self.hit("ledger_fixture")
        return {"stored_position_lots": [], "trade_events": [], "account_wheel_events": []}

    def decision(self, *_args, **_kwargs):
        self.hit("decision_fixture")
        return {"snapshot_status": "trusted"}

    def source_snapshot(self, *_args, **_kwargs):
        self.hit("source_persistence_substitute")

    def sizing(self, *, context, **_kwargs):
        self.hit("sizing_substitute")
        return context

    def assignment_ledger(self, _accounts):
        self.hit("ledger_fixture")
        return [], str(self.root / "config.us.json"), self.cfg

    def quotes(self, _codes, **_kwargs):
        self.hit("quotes_fixture")
        return [{"code": "NVDA", "currency": "USD", "price_cny": "720"}], []

    def pm(self, **_kwargs):
        self.hit("pm_fixture")
        raise self.assignment.PortfolioEvidenceReadError("offline PM unavailable", code="FIXTURE_UNAVAILABLE")

    def seed(self, path, age=0):
        self.ensure_path(path)
        path.mkdir(parents=True, exist_ok=True)
        context = self.cash_portfolio({"source_observed_at": (NOW - timedelta(seconds=age)).isoformat(),
                                     "cash_source_observed_at": NOW.isoformat()}, evaluated_at=NOW)
        for field in ("source_as_of_utc", "observed_at_utc"):
            context["position_snapshot_input"][field] = (NOW - timedelta(seconds=age)).isoformat()
        (path / "portfolio_context.json").write_text(json.dumps(context), encoding="utf-8")

    def scan(self, **prepared):
        return self.pipeline.build_pipeline_context(py=sys.executable, base=self.root, cfg=self.cfg,
            report_dir=self.root / "reports", portfolio_timeout_sec=1, runtime={}, is_scheduled=bool(prepared),
            state_dir=self.state, shared_state_dir=self.root / "output_shared/state", log=lambda _: None,
            no_context=False, want_scan=True, **prepared)[0]

    def cash_query(self, tool=False):
        if not tool:
            return self.cash.query_sell_put_cash(runtime_config=self.cfg, account="lx", output_format="json",
                data_config=self.root / "data.json", base_dir=self.root, out_dir=self.state)
        return self.materialization.query_cash_headroom_tool({"account": "lx"},
            load_runtime_config=lambda **_: (self.root / "config.us.json", self.cfg),
            resolve_public_data_config_path=lambda *_: self.root / "data.json",
            normalize_broker=lambda _: "富途", resolve_output_root=lambda _: self.root / "tool_output",
            query_sell_put_cash=self.cash.query_sell_put_cash, repo_base=lambda: self.root,
            mask_path=lambda _: "fixture")[0]

    def wheel_facts(self, tool=False):
        instant = int(NOW.timestamp() * 1000)
        if tool:
            payload = {"runtime_root": str(self.root), "account": "lx", "request_id": "fixture"}
            coverage = self.positions._wheel_coverage(self.repo, self.cfg, payload, {"symbol": "NVDA"}, instant)
            cash = self.positions._wheel_cash_capacity(self.repo, self.cfg, payload, {}, instant)
        else:
            coverage = self.wheel._coverage(self.repo, self.cfg, runtime_root=self.root, account="lx",
                batch={"symbol": "NVDA"}, as_of_ms=instant, source_identity="fixture")
            cash = self.wheel._cash_capacity(self.repo, self.cfg, runtime_root=self.root, account="lx",
                branch={}, as_of_ms=instant)
        return {"coverage": coverage.get("status"), "cash": cash.get("status"),
                "coverage_reason": coverage.get("reason"), "cash_reason": cash.get("reason")}

    def prepare_setup(self, run_id):
        from src.application.tick_run_workspace import publish_account_run_config
        authority = publish_account_run_config(base=self.root, run_id=run_id, account="lx", config=self.cfg)
        harness = self

        class Worker:
            def __init__(self, command, **_kwargs):
                self.returncode = harness.prepared.run_worker(Path(command[-1]))

            def poll(self):
                return self.returncode

        kwargs = dict(base=self.root, repo_root=ROOT, run_id=run_id,
            account_config_authorities={"lx": authority},
            account_state_dirs={"lx": self.root / "output_runs" / run_id / "accounts/lx/state"},
            shared_state_dir=self.root / "output_runs" / run_id / "state", timeout_sec=1, popen_factory=Worker)
        self.preparation[run_id] = kwargs

    def stage(self, name, call):
        before = self.counts.copy()
        started = time.perf_counter()
        result = call()
        self.stages.append({"name": name, "counts": dict(self.counts - before),
                            "wall_ms": (time.perf_counter() - started) * 1000})
        return result

    def prepare(self, run_id):
        kwargs = self.preparation[run_id]
        manifest = self.stage("prepare_" + run_id,
                              lambda: self.prepared.prepare_portfolio_contexts(**kwargs)["lx"])
        return kwargs, manifest

    def execute(self, case):
        if case.startswith("scan_"):
            return [self.scan() is not None for _ in range(2)]
        if case.startswith("cash_"):
            return [self.cash_query("tool" in case)["cash_snapshot"]["status"] for _ in range(2)]
        if case == "cross_entry":
            self.scan()
            assignment = self.assignment.query_portfolio_assignment_scenario(["lx"])
            return {"assignment": assignment["status"], "wheel": self.wheel_facts(),
                    "tool_cash": self.cash_query(True)["cash_snapshot"]["status"]}
        if case.startswith("prepared"):
            kwargs, manifest = self.prepare("run-a")
            if case == "prepared_new_run":
                return [manifest["status"], self.prepare("run-b")[1]["status"]]
            args = dict(prepared_portfolio_context_manifest=Path(manifest["manifest_path"]),
                prepared_portfolio_context_run_id="run-a",
                prepared_portfolio_context_account_config_sha256=manifest["account_config_sha256"],
                prepared_portfolio_context_manifest_sha256=manifest["manifest_sha256"])
            if case == "prepared_bad_hash":
                args["prepared_portfolio_context_manifest_sha256"] = "0" * 64
            if case == "prepared_bad_account":
                self.cfg = deepcopy(self.cfg)
                self.cfg["portfolio"]["account"] = "sy"
            if case in {"prepared_bad_hash", "prepared_bad_account"}:
                try:
                    self.scan(**args)
                except self.prepared.PreparedPortfolioContextError:
                    return "rejected_without_fallback"
                raise AssertionError("invalid prepared evidence accepted")
            result = [self.stage("consume", lambda: self.scan(**args)) is not None for _ in range(2)]
            adopted = self.stage("adopt", lambda: self.prepared.prepare_portfolio_contexts(**kwargs)["lx"])
            return result + [adopted["status"]]
        if case.startswith("wheel_"):
            return self.wheel_facts(tool="tool" in case)
        if case == "assignment_pm_unavailable":
            self.cfg["portfolio"]["holdings"] = {"enabled": True}
            result = self.assignment.query_portfolio_assignment_scenario(["lx"])
            return {"status": result["status"], "warnings": result["warnings"]}
        required = ("stock",)
        if case == "stock_301_cash_fresh":
            required = ()
        if case == "stock_option_61":
            required = ("stock", "option")
        results = []
        for index in range(2):
            cfg = deepcopy(self.cfg)
            path, account = self.state, "lx"
            if index and case == "account_isolation":
                account = "sy"
            if index and case == "physical_account_isolation":
                cfg["account_settings"]["lx"]["futu"]["account_id"] = "789"
            if index and case == "path_isolation":
                path = self.root / "other/state"
            if index and case == "stock_301_cash_fresh":
                required = ("stock",)
            try:
                context = self.read(market="富途", account=account, state_dir=path, log=lambda _: None,
                    runtime_config=cfg, portfolio_source="futu", fetch_futu_portfolio_context_fn=self.fetch,
                    write_cache=True, required_position_asset_types=required, include_options="option" in required,
                    exchange_rate_observation=self.fx)
                results.append(context["cash_snapshot"]["status"])
            except (TimeoutError, CancelledError) as exc:
                results.append(type(exc).__name__)
        return results


def sample(case):
    with tempfile.TemporaryDirectory(prefix="om-business-baseline-") as directory, ExitStack() as stack:
        harness = Harness(Path(directory), stack)
        if case == "scan_warm":
            harness.seed(harness.state)
        if case == "cash_tool_warm":
            harness.seed(harness.root / "tool_output/query_cash_headroom")
        if case == "stock_301_cash_fresh":
            harness.seed(harness.state, 301)
        if case == "stock_option_61":
            harness.seed(harness.state, 61)
        if case.startswith("prepared"):
            harness.prepare_setup("run-a")
            if case == "prepared_new_run":
                harness.prepare_setup("run-b")
        harness.mode = {"partial_stock": "partial"}.get(case, case)
        harness.counts.clear()
        harness.trace.clear()
        started = time.perf_counter()
        with redirect_stdout(io.StringIO()):
            outcome = harness.execute(case)
        elapsed = (time.perf_counter() - started) * 1000
        if harness.blocked:
            raise IsolationError(f"{case}: {harness.blocked} blocked network attempts")
        tool_cache = harness.root / "tool_output/query_cash_headroom/portfolio_context.json"
        if case == "cash_tool_cold" and tool_cache.exists():
            raise AssertionError("read-only tool wrote portfolio cache")
        return {"counts": dict(harness.counts), "trace": harness.trace, "outcome": outcome,
                "contexts": harness.contexts, "stages": harness.stages, "blocked_network_attempts": harness.blocked, "wall_ms": elapsed}


def aggregate(samples):
    signatures = [{key: value for key, value in row.items() if key not in {"wall_ms", "stages"}}
                  | {"stages": [{key: value for key, value in stage.items() if key != "wall_ms"}
                                for stage in row["stages"]]} for row in samples]
    if any(signature != signatures[0] for signature in signatures[1:]):
        raise AssertionError("inconsistent count/trace/outcome samples")
    for index, stage in enumerate(signatures[0]["stages"]):
        times = [row["stages"][index]["wall_ms"] for row in samples]
        stage["timing_ms"] = {"median": statistics.median(times), "min": min(times), "max": max(times)}
    values = [row["wall_ms"] for row in samples]
    return {**signatures[0], "timing_ms": {"median": statistics.median(values), "min": min(values),
                                        "max": max(values)}, "samples": samples}


def run(samples=7):
    if isinstance(samples, bool) or not isinstance(samples, int) or not 1 <= samples <= 50:
        raise ValueError("samples must be an integer in 1..50")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()
    return {"schema_version": "business_fetch_baseline.v1", "source": {
        "commit": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}"),
        "dirty": bool(git("status", "--porcelain")), "python": platform.python_version(),
        "platform": platform.platform()}, "fixture": {"now": NOW.isoformat(), "stock_rows": 1,
        "option_rows": 0, "account_ids": ["123", "456", "789"], "samples": samples,
        "sdk": "substitute context; real gateway/client mapping", "prepared_worker": "synchronous substitute"},
        "cases": {case: aggregate([sample(case) for _ in range(samples)]) for case in CASES}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=7)
    args = parser.parse_args()
    print(json.dumps(run(args.samples), ensure_ascii=False, indent=2))
