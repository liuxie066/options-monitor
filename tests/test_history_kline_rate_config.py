"""Synthetic acquisition tests; real history limiter, retained storage seams."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from src.application import opend_fetch_config as config
from src.application import opend_symbol_fetching as fetching
from src.application import required_data_fetching as requests
from src.application import required_data_planning as planning
from src.application import required_data_steps as steps
from src.application import symbol_monitoring as monitoring
from src.application.multi_tick import required_data_prefetch as scheduled
from src.application.opening_quote_evidence import OpeningUnderlierObservation
from src.application.opend_symbol_chain_fetching import SymbolOptionChainResult
from src.application.opend_market_snapshot_fetching import MarketSnapshotFetchResult


def _runtime(strict=False):
    limits = {"history_kline": {"max_calls": 1, "window_sec": 60, "max_wait_sec": 2}} if strict else {}
    return {"runtime": {"prefetch_max_workers": 1, "opend_rate_limits": limits}}


def _symbol():
    return {"symbol": "NVDA", "fetch": {"source": "futu", "limit_expirations": 1},
            "sell_put": {"enabled": True, "min_dte": 10, "max_dte": 40,
                         "min_strike": 80, "max_strike": 100},
            "sell_call": {"enabled": False}}


class HistoryProvider:
    def __init__(self, today):
        self.today = today
        self.calls = []
        self.calendar_calls = []
        self.closed = 0
        self.failure = None
        self.empty = False

    def get_trading_days(self, **kwargs):
        self.calendar_calls.append(kwargs)
        return [{"time": d.date().isoformat()} for d in
                pd.bdate_range(kwargs["start"], kwargs["end"])]

    def request_history_kline(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        if self.empty:
            return {"data": [], "page_req_key": None}
        days = pd.bdate_range(kwargs["start"], self.today - timedelta(days=1))
        rows = [{"time_key": d.date().isoformat(),
                 "close": 100 * math.exp(0.001 * i + 0.01 * math.sin(i))}
                for i, d in enumerate(days)]
        midpoint = len(rows) // 2
        return {"data": rows[:midpoint] if kwargs["page_req_key"] is None else rows[midpoint:],
                "page_req_key": "second" if kwargs["page_req_key"] is None else None}

    def close(self):
        self.closed += 1


@pytest.fixture
def acquisition(monkeypatch, tmp_path):
    from src.application import opend_rate_gate, short_vol_metrics, opend_utils
    today = date(2026, 10, 16)
    expiry = (today + timedelta(days=28)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    provider = HistoryProvider(today)
    observation = OpeningUnderlierObservation(
        schema_version="opening_underlier_observation.v1", code="US.NVDA", market="US",
        last_price=100, update_time=now, observed_at_utc=now, age_seconds=0,
        market_state="MORNING", sec_status="NORMAL", suspension=False,
        status="ready", reason_code=None)
    state = SimpleNamespace(base=tmp_path, today=today, expiry=expiry, provider=provider,
                            observation=observation, payloads=[], requests=[], writes=[],
                            publications=[], empty_chain=False, commands=[], prune=[])

    def retained_json(path, payload, **_kwargs):
        path = Path(path)
        assert path.resolve().is_relative_to(tmp_path.resolve())
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, default=str)
        version = tmp_path / "writer_versions" / f"{len(state.writes):04d}.json"
        version.parent.mkdir(parents=True, exist_ok=True)
        with version.open("x") as stream:
            stream.write(text)
        path.write_text(text)
        state.writes.append({"path": str(path), "payload": payload})

    monkeypatch.setattr(opend_rate_gate, "atomic_write_json", retained_json)
    monkeypatch.setattr(short_vol_metrics, "atomic_write_json", retained_json)
    monkeypatch.setattr(opend_utils, "load_runtime_symbol_aliases", lambda *_a, **_k: {})
    monkeypatch.setattr(opend_utils, "get_trading_date", lambda *_a, **_k: today)
    monkeypatch.setattr(planning, "list_option_expirations", lambda *_a, **_k: [expiry])
    monkeypatch.setattr(planning, "get_underlier_spot", lambda *_a, **_k: observation)
    monkeypatch.setattr(fetching, "build_ready_futu_quote_gateway", lambda **_k: provider)
    monkeypatch.setattr(fetching, "get_underlier_observation_opend", lambda *_a, **_k: observation)

    def chain(**_kwargs):
        rows = [] if state.empty_chain else [{"code": "US.NVDA261113P95000",
            "strike_time": expiry, "strike_price": 95, "option_type": "PUT",
            "lot_size": 100, "stock_owner": "US.NVDA"}]
        return SymbolOptionChainResult(rows=rows, expirations_all=[expiry], expirations_pick=[expiry],
            fetch_meta={"status": "ok", "source_outcome": "success_empty" if not rows else "success_rows",
                        "reason_code": "no_contract_rows" if not rows else None,
                        "expiration_statuses": {expiry: "empty" if not rows else "fetched"},
                        "source_observed_at": now, "completed_at_utc": now})

    def snapshots(**kwargs):
        codes = frozenset(kwargs["option_codes"])
        rows = {code: {"code": code, "stock_owner": "US.NVDA", "stock_type": "DRVT",
            "option_standard_type": "STANDARD", "sec_status": "NORMAL", "suspension": False,
            "option_contract_size": 100, "bid_price": 2, "ask_price": 2.1, "last_price": 2.05,
            "bid_vol": 10, "ask_vol": 10, "volume": 100, "option_open_interest": 100,
            "price_spread": .01, "update_time": now} for code in codes}
        return MarketSnapshotFetchResult(snap_map=rows, errors=[], requested_codes=codes,
            returned_codes=codes, missing_codes=frozenset(), unexpected_codes=frozenset(),
            complete=True, requested_at_utc=now, received_at_utc=now)

    monkeypatch.setattr(fetching, "fetch_symbol_option_chain", chain)
    monkeypatch.setattr(fetching, "fetch_option_snapshots", snapshots)
    real_fetch = fetching.fetch_symbol_request

    def observed(request, **kwargs):
        state.requests.append(request)
        payload = real_fetch(request, **kwargs)
        state.payloads.append(payload)
        return payload

    monkeypatch.setattr(fetching, "fetch_symbol_request", observed)
    monkeypatch.setattr(requests, "fetch_symbol_request", observed)

    def publish(**kwargs):
        state.publications.append(kwargs)
        payload = kwargs.get("payload")
        if payload is not None:
            assert payload["meta"]["status"] == "ok"
        return {"evidence": {"synthetic_publication_seam": True}}

    monkeypatch.setattr(steps, "finalize_required_data_quote_candidate", publish)
    monkeypatch.setattr(steps, "finalize_unplanned_required_data_candidate", publish)
    monkeypatch.setattr(scheduled, "finalize_required_data_quote_candidate", publish)
    monkeypatch.setattr(scheduled.state_repo, "append_source_snapshot_event", lambda *_a, **_k: None)
    monkeypatch.setattr(scheduled, "find_fresh_required_data_quote_receipts", lambda **_k: [])
    monkeypatch.setattr(scheduled, "prefetch_market_earnings_calendars", lambda **_k: {})
    monkeypatch.setattr(scheduled.futu_gateway, "build_ready_futu_quote_gateway", lambda **_k: provider)
    monkeypatch.setattr(scheduled, "get_underlier_observations_opend",
                        lambda *_a, **_k: {"US.NVDA": observation})
    pool = SimpleNamespace(get_gateway=lambda **_k: provider, mark_success=lambda: None,
        mark_failure=lambda *_a: None, close_current_thread=lambda: None,
        close_registered=lambda: None)
    monkeypatch.setattr(scheduled, "_gateway_pool", pool)
    yield state
    (tmp_path / "acquisition-evidence.json").write_text(json.dumps({
        "requests": [{key: getattr(request, key) for key in (
            "symbol", "history_kline_max_calls", "history_kline_window_sec",
            "history_kline_max_wait_sec", "include_realized_volatility")}
            for request in state.requests],
        "history_calls": provider.calls, "calendar_calls": provider.calendar_calls,
        "owned_close_count": provider.closed, "payloads": state.payloads,
        "writer_versions": state.writes, "publication_seam_calls": len(state.publications),
        "commands": state.commands, "prune_seam_calls": state.prune,
    }, indent=2, default=str))


def _ordinary(state, strict=False):
    def prefilter(**kwargs):
        return SimpleNamespace(want_put=kwargs["want_put"], want_call=kwargs["want_call"],
                               sp=kwargs["sp"], cc=kwargs["cc"], stock=None)
    noop = lambda **_k: None
    deps = monitoring.SymbolMonitoringDependencies(
        build_converter_fn=noop, apply_prefilters_fn=prefilter, apply_multiplier_cache_fn=noop,
        ensure_required_data_fn=steps.ensure_required_data, run_sell_put_scan_fn=noop,
        empty_sell_put_summary_fn=noop, run_sell_call_scan_fn=noop,
        empty_sell_call_summary_fn=noop, run_combo_yield_scan_fn=noop,
        empty_combo_yield_summary_fn=noop)
    return monitoring.run_symbol_monitoring(inputs=monitoring.SymbolMonitoringInputs(
        py=sys.executable, base=state.base, symbol_cfg=_symbol(), top_n=1, portfolio_ctx=None,
        usd_per_cny_exchange_rate=None, cny_per_hkd_exchange_rate=None, timeout_sec=20,
        required_data_dir=state.base / "required", report_dir=state.base / "reports",
        state_dir=None, is_scheduled=False, runtime_config=_runtime(strict), fetch_only=True), deps=deps)


def _scheduled(state, strict=False, mode=None):
    cfg = _runtime(strict)
    cfg["symbols"] = [_symbol()]
    if mode:
        cfg["runtime"]["prefetch"] = {"execution_mode": mode}
    return scheduled.prefetch_required_data(vpy=Path(sys.executable), base=state.base,
        cfg=cfg, shared_required=state.base / "required", force_refresh=True)


def _assert_history(state, strict):
    assert len(state.requests) == 1
    request = state.requests[0]
    assert (request.history_kline_max_calls, request.history_kline_window_sec,
            request.history_kline_max_wait_sec) == ((1, 60, 2) if strict else (60, 30, 30))
    assert len(state.provider.calls) == (1 if strict else 2)
    payload = state.payloads[-1]
    rv = payload["meta"]["realized_volatility"]
    if strict:
        assert payload["meta"]["status"] == "error"
        assert payload["meta"]["error_code"] == "REQUIRED_REALIZED_VOLATILITY_INCOMPLETE"
        assert "OptionChainRateLimitExceeded" in rv["qfq_history"]["reason"]
    else:
        assert payload["meta"]["status"] == "ok", payload["meta"]
        assert rv["status"] == "ok", rv
        row = payload["rows"][0]
        assert row["term_matched_rv_status"] == "ok"
        assert row["term_matched_rv"] > 0
        assert row["realized_volatility_20"] > 0
        assert row["term_matched_rv_input_hash"]
    limiter = state.base / "output_shared/state/opend_history_kline_limiter.json"
    assert limiter.exists()
    persisted = json.loads(limiter.read_text())
    assert persisted["max_calls"] == (1 if strict else 60)
    assert persisted["window_sec"] == (60 if strict else 30)
    assert len(persisted["timestamps"]) == (1 if strict else 2)
    assert any(item["path"] == str(limiter) for item in state.writes)
    return payload


@pytest.mark.parametrize("strict", [False, True])
def test_ordinary_facade_enforces_configured_history_budget(acquisition, strict):
    if strict:
        with pytest.raises(RuntimeError, match="realized|volatility|RV"):
            _ordinary(acquisition, strict)
    else:
        assert _ordinary(acquisition, strict) == []
    _assert_history(acquisition, strict)
    assert acquisition.provider.closed == 1


@pytest.mark.parametrize("strict", [False, True])
def test_scheduled_top_level_enforces_configured_history_budget(acquisition, strict):
    result = _scheduled(acquisition, strict)
    assert result["execution_mode"] == "inprocess"
    assert result["submitted_count"] == result["completed_count"] == 1
    _assert_history(acquisition, strict)
    if strict:
        assert result["errors"] == 1
        assert not acquisition.publications
    else:
        assert result["fetched_ok"] == 1
        assert len(acquisition.publications) == 1


def test_subprocess_command_and_actual_cli_preserve_budget(acquisition, monkeypatch):
    from src.application import opend_symbol_fetching_cli as cli

    def execute(_self, intent):
        acquisition.commands.append(intent.cmd)
        return {"ok": False, "status": "error", "symbol": "NVDA", "source": "futu",
                "tool_name": "required_data_prefetch", "message": "command capture only"}

    monkeypatch.setattr(scheduled.ToolExecutionService, "execute", execute)
    _scheduled(acquisition, True, "subprocess")
    cmd, = acquisition.commands
    assert cmd[:3] == [sys.executable, "-m", "src.application.opend_symbol_fetching_cli"]
    for flag, value in (("--history-kline-max-calls", "1"),
                        ("--history-kline-window-sec", "60.0"),
                        ("--history-kline-max-wait-sec", "2.0")):
        assert cmd[cmd.index(flag) + 1] == value
    monkeypatch.setattr(cli, "resolve_runtime_root", lambda **_k: SimpleNamespace(runtime_root=acquisition.base))
    monkeypatch.setattr(cli, "prune_chain_cache", lambda *a: acquisition.prune.append(a))
    monkeypatch.setattr(cli, "save_outputs", lambda *_a, **_k:
                        (acquisition.base / "unpublished.json", acquisition.base / "unpublished.csv"))
    monkeypatch.setattr(cli, "append_metrics_json", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "fetch_symbol_request", fetching.fetch_symbol_request)
    monkeypatch.setattr(sys, "argv", [cmd[2], *cmd[3:]])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert len(acquisition.prune) == 1
    _assert_history(acquisition, True)


@pytest.mark.parametrize("strict", [False, True])
def test_kwargs_request_contract_and_discovery_isolation(acquisition, strict):
    cfg = _runtime(strict)
    cfg["runtime"]["option_chain_fetch"] = {"max_calls": 7, "window_sec": 9, "max_wait_sec": 11}
    flat = config.opend_fetch_kwargs(cfg)
    selected = config.filter_opend_fetch_kwargs({**flat, "unknown": "reject"})
    assert selected == flat
    assert not any(k.startswith("history") for k in config.opend_discovery_kwargs(cfg))
    required = requests.RequiredDataFetchRequest(symbol="NVDA", limit_expirations=1, **selected)
    actual = fetching.FetchSymbolRequest(symbol="NVDA", **selected)
    for key in ("history_kline_max_calls", "history_kline_window_sec", "history_kline_max_wait_sec"):
        assert getattr(required, key) == getattr(actual, key) == flat[key]
    assert (actual.option_chain_max_calls, actual.option_chain_window_sec, actual.max_wait_sec) == (7, 9, 11)
    assert not actual.include_realized_volatility


@pytest.mark.parametrize("mode", ["no_rv", "no_contracts"])
def test_no_history_when_not_required_or_no_contracts(acquisition, mode):
    acquisition.empty_chain = mode == "no_contracts"
    payload = fetching.fetch_symbol_request(fetching.FetchSymbolRequest(
        symbol="NVDA", base_dir=acquisition.base, spot_override=100,
        underlier_observation=acquisition.observation.to_dict(),
        explicit_expirations=[acquisition.expiry], trading_date=acquisition.today.isoformat(),
        include_realized_volatility=mode != "no_rv", **config.opend_fetch_kwargs(_runtime(True))))
    assert payload["meta"]["status"] == "ok"
    assert not acquisition.provider.calls
    assert not acquisition.provider.calendar_calls
    assert not acquisition.writes


@pytest.mark.parametrize("failure", ["provider", "cancel"])
def test_ordinary_failure_and_cancellation_do_not_become_success(acquisition, failure):
    acquisition.provider.failure = KeyboardInterrupt("synthetic cancel") if failure == "cancel" else RuntimeError("synthetic provider failure")
    expected = KeyboardInterrupt if failure == "cancel" else RuntimeError
    with pytest.raises(expected):
        _ordinary(acquisition)
    assert acquisition.provider.closed == 1
    assert not acquisition.publications
    assert len(acquisition.provider.calls) == 1


def test_empty_history_preserves_explicit_partial_term_evidence(acquisition):
    acquisition.provider.empty = True
    assert _ordinary(acquisition) == []
    payload = acquisition.payloads[-1]
    assert payload["meta"]["realized_volatility"]["status"] == "partial"
    assert payload["rows"][0]["term_matched_rv_status"] != "ok"
    assert payload["rows"][0]["term_matched_rv"] is None
    assert len(acquisition.provider.calls) == 1


def test_scheduled_cancellation_propagates_without_cleanup_claim(acquisition):
    acquisition.provider.failure = KeyboardInterrupt("synthetic cancel")
    with pytest.raises(KeyboardInterrupt, match="synthetic cancel"):
        _scheduled(acquisition)
    assert len(acquisition.provider.calls) == 1
    assert not acquisition.publications


def test_provider_rate_error_blocks_next_transport(acquisition):
    from src.application.opend_call_coordinator import rate_limited_opend_call
    from src.application.option_chain_fetching import OptionChainRateLimitExceeded
    calls = []

    def fail():
        calls.append("first")
        raise RuntimeError("rate limit")

    kwargs = dict(base_dir=acquisition.base, endpoint="history_kline", max_calls=3,
                  window_sec=60, max_wait_sec=.01)
    with pytest.raises(RuntimeError, match="rate limit"):
        rate_limited_opend_call(**kwargs, call=fail)
    with pytest.raises(OptionChainRateLimitExceeded):
        rate_limited_opend_call(**kwargs, call=lambda: calls.append("unexpected"))
    assert calls == ["first"]
    assert acquisition.writes


def test_shared_multiplier_consumer_accepts_expanded_kwargs_without_rv(acquisition):
    from src.application.multiplier_cache import refresh_via_opend
    result = refresh_via_opend(repo_base=acquisition.base, symbol="NVDA",
                              opend_fetch_config=config.opend_fetch_kwargs(_runtime(True)))
    assert result.ok, result.error
    assert result.multiplier == 100
    assert not acquisition.requests[0].include_realized_volatility
    assert acquisition.requests[0].history_kline_max_calls == 1
    assert not acquisition.provider.calls


@pytest.mark.parametrize("frozen", [False, True])
def test_required_data_reuse_avoids_history_fetch(acquisition, monkeypatch, frozen):
    root = acquisition.base / "reuse"
    parsed = root / "parsed/NVDA_required_data.csv"
    parsed.parent.mkdir(parents=True)
    content = b"symbol,dte\nNVDA,28\n"
    parsed.write_bytes(content)
    if frozen:
        monkeypatch.setattr(steps, "resolve_frozen_required_data_csv_bytes",
                            lambda **_k: ({"synthetic_frozen_read": True}, content))
    result = steps.ensure_required_data(py=sys.executable, base=acquisition.base, symbol="NVDA",
        required_data_dir=root, limit_expirations=1, want_put=True, want_call=False,
        timeout_sec=10, is_scheduled=False, min_dte=10,
        required_data_snapshot_manifest=root / "manifest.json" if frozen else None,
        opend_fetch_config=config.opend_fetch_kwargs(_runtime(True)))
    assert result == ({"synthetic_frozen_read": True} if frozen else None)
    assert not acquisition.requests
    assert not acquisition.provider.calls
    assert not acquisition.writes
