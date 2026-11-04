from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import time

import pytest

from src.application import short_put_roll as service
from src.application.opening_quote_evidence import normalize_option_observation, normalize_underlier_observation
from test_close_advice_required_data import _position

NOW = datetime(2026, 11, 4, 15, tzinfo=timezone.utc)
OLD_EXPIRY = "2026-11-14"
NEW_EXPIRY = "2026-12-14"


def quote(strike, expiry, *, bid=5.5, ask=5.6, **changes):
    return {
        "symbol": "NVDA", "market": "US", "currency": "USD", "option_type": "put",
        "contract_symbol": f"US.NVDA{expiry.replace('-', '')[2:]}P{int(strike * 1000):08d}",
        "strike": strike, "expiration": expiry, "dte": (datetime.fromisoformat(expiry).date() - NOW.date()).days,
        "spot": 98.0, "bid": bid, "ask": ask, "multiplier": 100, "chain_multiplier": 100,
        "snapshot_multiplier": 100, "price_tick": 0.01, "option_standard_type": "STANDARD",
        "stock_owner": "US.NVDA", "stock_type": "DRVT", "option_sec_status": "NORMAL",
        "option_suspension": False, "underlier_observation_status": "ready", "underlier_sec_status": "NORMAL",
        "underlier_suspension": False, "opening_contract_status": "ready", "market_state": "MORNING",
        "spot_observed_at_utc": (NOW - timedelta(seconds=4)).isoformat(),
        "snapshot_requested_at_utc": (NOW - timedelta(seconds=3)).isoformat(),
        "snapshot_received_at_utc": (NOW - timedelta(seconds=2)).isoformat(), **changes,
    }


class Provider:
    name = "offline"

    def __init__(self):
        self.requests = []
        self.open_calls = 0
        self.closed = 0
        self.spot = 98
        self.market_state = "MORNING"
        self.right = "LV1"
        self.before_fetch = None
        self.rows = [quote(100, OLD_EXPIRY, bid=4.8, ask=5), quote(95, NEW_EXPIRY)]
        self.meta = {"expiration_enumeration_complete": True, "query_scope_validated": True,
            "available_expirations": [OLD_EXPIRY, NEW_EXPIRY],
            "expiration_statuses": {OLD_EXPIRY: "fetched", NEW_EXPIRY: "fetched"}}

    def resolve_quote_route(self, *_args, **_kwargs):
        return SimpleNamespace(ok=True, host="offline.invalid", port=11111)

    def get_underlier_observation(self, *_args, **_kwargs):
        return normalize_underlier_observation(code="US.NVDA", market="US",
            snapshot_row={"code": "US.NVDA", "last_price": self.spot, "update_time": "2026-11-04 09:59:56", "sec_status": "NORMAL", "suspension": False},
            market_state_row={"code": "US.NVDA", "market_state": self.market_state}, now_utc=NOW)

    def open_quote_gateway(self, **_kwargs):
        self.open_calls += 1
        return self

    def fetch_symbol_request(self, request):
        self.requests.append(request)
        if self.before_fetch:
            self.before_fetch()
        return {"rows": self.rows, "meta": self.meta, "expirations": [OLD_EXPIRY, NEW_EXPIRY]}

    def get_market_state(self, _gateway, codes):
        return [{"code": codes[0], "market_state": self.market_state}]

    def get_quote_rights(self, _gateway, *, market):
        return {"status": "ok", "market": market, "underlier_right": self.right, "option_right": self.right}

    def close(self):
        self.closed += 1


@pytest.fixture
def setup_service(monkeypatch):
    import om_position_advice_plugin as policy

    provider = Provider()
    rows = [_position(account="sample", lot_id="old", expiration=OLD_EXPIRY)]
    cfg = {"accounts": ["sample"], "portfolio": {"account": "sample", "broker": "富途"},
        "close_advice": {"enabled": True}}
    monkeypatch.setattr(service, "_read_holdings", lambda **_: deepcopy(rows))
    monkeypatch.setattr(service, "resolve_market_data_provider", lambda _: provider)
    monkeypatch.setattr(service, "position_advice_plugin_status", lambda _: "available")
    monkeypatch.setattr(service, "require_position_advice_plugin", lambda: policy)
    monkeypatch.setattr(service, "assert_quality_allows", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "rate_limited_opend_call", lambda **kwargs: kwargs["call"]())
    monkeypatch.setattr("socket.create_connection", lambda *_args, **_kwargs: pytest.fail("unexpected network"))
    return provider, rows, cfg


def run(tmp_path, setup_service, payload=None, **kwargs):
    _provider, _rows, cfg = setup_service
    return service.short_put_roll_candidates({"account": "sample", "lot_id": "old", "max_new_expiration": NEW_EXPIRY,
        **(payload or {})}, config=cfg, config_path=tmp_path / "config.us.json", base_dir=Path(__file__).resolve().parents[1],
        runtime_root=tmp_path, now_utc=NOW, **kwargs)


def test_found_uses_actual_lot_qty_and_bid_ask_without_opening_policy(tmp_path, setup_service):
    provider, rows, _ = setup_service
    rows[0]["fields"]["premium_open"] = None
    rows[0]["fields"]["contracts_open"] = 3
    rows[0]["fields"]["contracts_opened"] = 3
    provider.rows[1]["delta"] = -0.8
    provider.rows[1]["implied_volatility"] = None
    result = run(tmp_path, setup_service)
    assert result["outcome"] == "found"
    assert result["coverage"]["status"] == "complete"
    candidate = result["candidates"][0]
    assert candidate["contracts"] == 3
    assert candidate["net_credit"] == pytest.approx(150 - candidate["old_buy_fee"]["amount"] - candidate["new_sell_fee"]["amount"])
    assert candidate["new_otm_pct"] == pytest.approx(3 / 98)
    assert candidate["incremental_annualized_return"] == pytest.approx(candidate["net_credit"] / 28500 * 365 / 40)
    assert candidate["evidence"]["new_bid"] == 5.5
    request = provider.requests[0]
    assert request.include_realized_volatility is False
    assert request.limit_expirations is None
    assert request.query_filters[0]["min_strike"] == request.query_filters[0]["max_strike"] == 100
    assert request.query_filters[1]["max_strike"] == 98
    assert provider.closed == 1
    assert json.loads(Path(result["report_path"]).read_text())["outcome"] == "found"
    assert result["scope"]["max_new_expiration"] == NEW_EXPIRY


@pytest.mark.parametrize("strike,expected", [(95, "found"), (94.999, "not_applicable")])
def test_old_five_percent_boundary_skips_chain_outside(tmp_path, setup_service, strike, expected):
    provider, rows, _ = setup_service
    provider.spot = 100
    rows[0]["fields"]["contract_key"]["strike"] = str(strike)
    provider.rows = [quote(strike, OLD_EXPIRY, spot=100, bid=4.8, ask=5), quote(90, NEW_EXPIRY, spot=100)]
    result = run(tmp_path, setup_service)
    assert result["outcome"] == expected
    assert len(provider.requests) == (1 if expected == "found" else 0)
    if expected == "not_applicable":
        assert result["coverage"] is None
        assert result["reason_codes"] == ["old_position_outside_roll_scope"]
        assert provider.open_calls == 0


def normalized_quote(provider, strike, expiry, *, bid, ask, **chain_changes):
    """Use the provider's real buy/sell observation qualification."""
    row = quote(strike, expiry, bid=bid, ask=ask)
    observation = normalize_option_observation(expected_owner="US.NVDA", market="US", currency="USD",
        chain_row={"code": row["contract_symbol"], "option_type": "PUT", "lot_size": 100,
            "option_standard_type": "STANDARD", "stock_owner": "US.NVDA", "stock_type": "DRVT",
            "suspension": False, **chain_changes},
        snapshot_row={"code": row["contract_symbol"], "bid_price": bid, "ask_price": ask,
            "price_spread": 0.01, "option_contract_size": 100, "sec_status": "NORMAL", "suspension": False},
        underlier_observation=provider.get_underlier_observation(),
        snapshot_requested_at_utc=NOW - timedelta(seconds=3),
        snapshot_received_at_utc=NOW - timedelta(seconds=2), now_utc=NOW)
    row.update(opening_contract_status=observation.status, opening_contract_reason_codes=list(observation.reason_codes))
    return row


def test_old_buyback_uses_positive_ask_even_when_real_normalizer_excludes_zero_bid(tmp_path, setup_service):
    provider, _, _ = setup_service
    provider.rows[0] = normalized_quote(provider, 100, OLD_EXPIRY, bid=0, ask=5)
    assert provider.rows[0]["opening_contract_reason_codes"] == ["option_no_current_bid"]
    result = run(tmp_path, setup_service)
    assert result["outcome"] == "found", result
    assert result["coverage"]["status"] == "complete"
    assert result["candidates"][0]["old_close_cost"] > 500


@pytest.mark.parametrize("change", [{"option_standard_type": "NON_STANDARD"}, {"stock_owner": "US.OTHER"}])
def test_buyback_side_keeps_contract_identity_requirements(tmp_path, setup_service, change):
    provider, _, _ = setup_service
    provider.rows[0] = normalized_quote(provider, 100, OLD_EXPIRY, bid=0, ask=5, **change)
    result = run(tmp_path, setup_service)
    assert result["outcome"] == "unable"
    assert result["candidates"] == []


def test_new_sell_still_requires_positive_bid(tmp_path, setup_service):
    provider, _, _ = setup_service
    provider.rows[1] = normalized_quote(provider, 95, NEW_EXPIRY, bid=0, ask=5.6)
    result = run(tmp_path, setup_service)
    assert result["outcome"] == "none"
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["excluded"] == 1


def test_strictly_otm_new_put_and_no_lower_or_earlier_rows(tmp_path, setup_service):
    provider, _, _ = setup_service
    provider.rows = [provider.rows[0], quote(98, NEW_EXPIRY), quote(99, NEW_EXPIRY), quote(100, NEW_EXPIRY),
        quote(95, "2026-11-10")]
    result = run(tmp_path, setup_service)
    assert result["outcome"] == "none"
    assert result["coverage"]["discovered"] == 0
    assert result["candidates"] == []


@pytest.mark.parametrize("kind", ["partial", "none", "unable"])
def test_complete_empty_is_distinct_from_unassessed_and_partial(tmp_path, setup_service, kind):
    provider, _, _ = setup_service
    if kind == "none":
        provider.rows[1].update(bid=4.9, ask=5)
    elif kind == "unable":
        provider.rows[1].update(snapshot_received_at_utc=(NOW - timedelta(seconds=301)).isoformat())
    else:
        provider.rows.append(quote(94, NEW_EXPIRY, snapshot_received_at_utc=(NOW - timedelta(seconds=301)).isoformat()))
    result = run(tmp_path, setup_service)
    assert result["outcome"] == ("found" if kind == "partial" else kind)
    assert result["coverage"]["status"] == ("complete" if kind == "none" else "partial" if kind == "partial" else "unavailable")
    if kind != "none":
        assert result["coverage"]["unassessed"] == 1


def test_incomplete_enumeration_never_proves_no_opportunity(tmp_path, setup_service):
    provider, _, _ = setup_service
    provider.rows[1].update(bid=4.9, ask=5)
    provider.meta["expiration_enumeration_complete"] = False
    result = run(tmp_path, setup_service)
    assert result["outcome"] == "unable"
    assert result["coverage"]["status"] == "partial"
    assert "enumeration_incomplete" in result["reason_codes"]


@pytest.mark.parametrize("change", ["old_missing", "old_stale", "rights", "new_permission_time"])
def test_both_legs_need_current_quote_evidence(tmp_path, setup_service, change):
    provider, _, _ = setup_service
    if change == "old_missing":
        provider.rows.pop(0)
    elif change == "old_stale":
        provider.rows[0]["snapshot_received_at_utc"] = (NOW - timedelta(seconds=301)).isoformat()
    elif change == "rights":
        provider.right = "BMP"
    else:
        provider.rows[1]["snapshot_received_at_utc"] = (NOW + timedelta(seconds=1)).isoformat()
    result = run(tmp_path, setup_service)
    assert result["outcome"] == "unable"
    assert not result["candidates"]
    assert result["reason_codes"]


def test_unsupported_broker_does_not_use_futu_fees_or_query(tmp_path, setup_service):
    provider, _, cfg = setup_service
    cfg["account_settings"] = {"sample": {"type": "longbridge", "longbridge": {
        "account_id": "offline-account", "credentials_env_prefix": "OFFLINE_LB"}}}
    result = run(tmp_path, setup_service)
    assert result["reason_codes"] == ["unsupported_broker_fee_model"]
    assert result["outcome"] == "unable"
    assert not provider.requests


@pytest.mark.parametrize("state", ["disabled", "unavailable"])
def test_plugin_unavailable_never_fetches(tmp_path, setup_service, monkeypatch, state):
    provider, _, _ = setup_service
    monkeypatch.setattr(service, "position_advice_plugin_status", lambda _: state)
    result = run(tmp_path, setup_service)
    assert result["status"] == state
    assert not provider.requests


@pytest.mark.parametrize("failure", ["unimportable", "old_plugin"])
def test_admitted_plugin_still_needs_roll_exports(tmp_path, setup_service, monkeypatch, failure):
    def unavailable():
        raise service.PositionAdviceUnavailableError("offline incompatible package")

    monkeypatch.setattr(service, "require_position_advice_plugin",
        unavailable if failure == "unimportable" else lambda: SimpleNamespace())
    result = run(tmp_path, setup_service)
    assert result["outcome"] == "unable"
    assert result["reason_codes"] == ["position_advice_unavailable" if failure == "unimportable" else "position_advice_roll_unavailable"]
    assert not setup_service[0].requests


@pytest.mark.parametrize("problem", ["missing_identity", "conflicting_quotes", "non_object"])
def test_malformed_candidates_cannot_turn_into_complete_empty(tmp_path, setup_service, problem):
    provider, _, _ = setup_service
    candidate = provider.rows.pop()
    if problem == "missing_identity":
        candidate.pop("symbol")
        provider.rows.append(candidate)
    elif problem == "conflicting_quotes":
        provider.rows.extend([candidate, {**candidate, "ask": 9}])
    else:
        provider.rows.append(None)
    result = run(tmp_path, setup_service)
    assert result["outcome"] == "unable"
    assert result["coverage"]["status"] == "unavailable"
    assert result["coverage"]["unassessed"] == result["coverage"]["discovered"] == 1
    assert result["coverage"]["evaluated"] == result["coverage"]["excluded"] == 0


@pytest.mark.parametrize("failure", [OSError, TimeoutError, RuntimeError])
def test_provider_failure_is_unavailable_and_closes_gateway(tmp_path, setup_service, failure):
    provider, _, _ = setup_service

    def fail():
        raise failure("offline dependency failed")

    provider.before_fetch = fail
    result = run(tmp_path, setup_service)
    assert result["outcome"] == "unable"
    assert result["reason_codes"] == ["quote_request_timeout" if failure is TimeoutError else "quote_dependency_unavailable"]
    assert provider.closed == 1


@pytest.mark.parametrize("lower", [98, 99])
def test_minimum_strike_must_leave_a_strictly_otm_range(tmp_path, setup_service, lower):
    provider, _, _ = setup_service
    with pytest.raises(ValueError, match="below the current spot"):
        run(tmp_path, setup_service, {"min_new_strike": lower})
    assert provider.open_calls == 0
    assert not provider.requests
    assert not list(tmp_path.rglob("result.json"))


@pytest.mark.parametrize("failed_file", ["request.json", "result.json"])
def test_report_write_error_is_an_execution_error(tmp_path, setup_service, monkeypatch, failed_file):
    provider, _, _ = setup_service
    write = service.atomic_write_json
    captured = []

    def fail_write(path, payload):
        captured.append((Path(path), payload))
        if Path(path).name == failed_file:
            raise OSError("offline storage failure")
        return write(path, payload)

    monkeypatch.setattr(service, "atomic_write_json", fail_write)
    with pytest.raises(service.AgentToolError) as failure:
        run(tmp_path, setup_service)
    assert failure.value.code == "REPORT_WRITE_FAILED"
    assert isinstance(failure.value.__cause__, OSError)
    assert provider.closed == 1
    assert len(provider.requests) == 1
    assert not list(tmp_path.rglob("result.json"))
    if failed_file == "result.json":
        assert captured[-1][1]["outcome"] == "found"
        assert captured[-1][1]["displayed"] == 1
        assert captured[0][0].is_file()


@pytest.mark.parametrize("output_format", ["text", "json"])
@pytest.mark.parametrize("failure", ["request.json", "result.json", "min_strike"])
def test_real_cli_reports_service_execution_and_range_errors(
    tmp_path, setup_service, monkeypatch, capsys, output_format, failure,
):
    from contextlib import nullcontext
    from src.application.agent_tools import close_advice
    from src.interfaces.cli import main as cli

    provider, _, config = setup_service
    monkeypatch.setattr(cli, "command_environment", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(close_advice, "load_runtime_config", lambda **_: (tmp_path / "config.us.json", config))
    monkeypatch.setattr("src.application.runtime_paths.resolve_runtime_root", lambda **_:
        SimpleNamespace(source="argument", runtime_root=tmp_path))
    call_service = service.short_put_roll_candidates
    monkeypatch.setattr(service, "short_put_roll_candidates", lambda *args, **kwargs:
        call_service(*args, **kwargs, now_utc=NOW))
    write = service.atomic_write_json

    def fail_write(path, payload):
        if Path(path).name == failure:
            raise OSError("offline storage failure")
        return write(path, payload)

    monkeypatch.setattr(service, "atomic_write_json", fail_write)
    request_path = tmp_path / "input.json"
    payload = {"account": "sample", "lot_id": "old", "max_new_expiration": NEW_EXPIRY}
    if failure == "min_strike":
        payload["min_new_strike"] = 99
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    assert cli.main(["position-advice", "roll-down", "--request-file", str(request_path),
        "--format", output_format]) == 2
    output = capsys.readouterr().out
    expected_code = "INPUT_ERROR" if failure == "min_strike" else "REPORT_WRITE_FAILED"
    if output_format == "json":
        envelope = json.loads(output)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == expected_code
        assert not envelope.get("data")
    else:
        assert f"执行失败 [{expected_code}]" in output
        assert "独立报告：" not in output
    assert "quote_dependency_unavailable" not in output
    assert not list(tmp_path.rglob("result.json"))
    assert provider.closed == (0 if failure == "min_strike" else 1)


def test_account_and_market_scope_rejected_before_quotes(tmp_path, setup_service):
    provider, _, _ = setup_service
    with pytest.raises((ValueError, service.AgentToolError)):
        run(tmp_path, setup_service, {"account": "other"})
    with pytest.raises(service.AgentToolError, match="scope"):
        run(tmp_path, setup_service, {"config_key": "hk"})
    assert not provider.requests


@pytest.mark.parametrize("payload", [{"max_new_expiration": "bad"}, {"max_new_expiration": "2026-11-10"},
    {"limit": True}, {"limit": 51}, {"min_new_strike": float("nan")}, {"min_new_strike": 101}])
def test_invalid_requests_do_not_fetch(tmp_path, setup_service, payload):
    provider, _, _ = setup_service
    with pytest.raises((ValueError, service.AgentToolError)):
        run(tmp_path, setup_service, payload)
    assert not provider.requests


def test_cancel_mid_scan_is_explicit_and_closes_gateway(tmp_path, setup_service):
    provider, _, _ = setup_service
    flag = [False]
    provider.before_fetch = lambda: flag.__setitem__(0, True)
    result = run(tmp_path, setup_service, cancelled=lambda: flag[0], deadline=time.monotonic() + 10)
    assert result["reason_codes"] == ["cancelled"]
    assert result["outcome"] == "unable"
    assert not result["candidates"]
    assert provider.closed == 1


@pytest.mark.parametrize("missing_candidate", [False, True])
def test_real_provider_query_and_normalization_keep_scope_and_snapshot_gaps(tmp_path, setup_service, monkeypatch, missing_candidate):
    import pandas as pd
    from src.application import opend_symbol_fetching as fetching
    from src.application.opend_market_snapshot_fetching import MarketSnapshotFetchResult
    from src.application.option_chain_fetching import OptionChainFetchResult

    provider, _, _ = setup_service
    source_rows = [quote(100, OLD_EXPIRY, bid=0, ask=5), quote(95, NEW_EXPIRY)]
    chain_rows = [{"code": row["contract_symbol"], "strike_time": row["expiration"],
        "strike_price": row["strike"], "option_type": "PUT", "lot_size": 100,
        "option_standard_type": "STANDARD", "stock_owner": "US.NVDA", "stock_type": "DRVT",
        "suspension": False} for row in source_rows]
    chain_requests = []

    def chains(*, request, **_kwargs):
        chain_requests.append(request)
        return OptionChainFetchResult(rows=chain_rows, frame=pd.DataFrame(chain_rows),
            from_cache_expirations=[], fetched_expirations=[OLD_EXPIRY, NEW_EXPIRY], opend_call_count=2,
            rate_gate_wait_sec=0, status="ok", error_code=None, errors=[],
            expiration_statuses={OLD_EXPIRY: "fetched", NEW_EXPIRY: "fetched"}, source_outcome="success_rows")

    def snapshots(*, option_codes, **_kwargs):
        snapshot_rows = {row["contract_symbol"]: {"code": row["contract_symbol"], "bid_price": row["bid"],
            "ask_price": row["ask"], "price_spread": 0.01, "option_contract_size": 100,
            "sec_status": "NORMAL", "suspension": False,
            "snapshot_requested_at_utc": (NOW - timedelta(seconds=3)).isoformat(),
            "snapshot_received_at_utc": (NOW - timedelta(seconds=2)).isoformat()} for row in source_rows}
        if missing_candidate:
            snapshot_rows.pop(source_rows[1]["contract_symbol"])
        missing = frozenset(option_codes) - snapshot_rows.keys()
        return MarketSnapshotFetchResult(snap_map=snapshot_rows, errors=[], requested_codes=frozenset(option_codes),
            returned_codes=frozenset(snapshot_rows), missing_codes=frozenset(missing), unexpected_codes=frozenset(),
            complete=not missing)

    def real_fetch(request):
        provider.requests.append(request)
        return fetching.fetch_symbol_request(request)

    monkeypatch.setattr(fetching, "list_option_expirations_with_gateway", lambda *_args, **_kwargs:
        [OLD_EXPIRY, NEW_EXPIRY, "2027-01-15"])
    monkeypatch.setattr(fetching, "fetch_option_chains", chains)
    monkeypatch.setattr(fetching, "fetch_option_snapshots", snapshots)
    monkeypatch.setattr("src.application.opening_quote_evidence._utc_now", lambda value=None: value or NOW)
    monkeypatch.setattr(provider, "fetch_symbol_request", real_fetch)
    result = run(tmp_path, setup_service)
    assert result["coverage"] is not None, result["reason_codes"]
    assert result["outcome"] == ("unable" if missing_candidate else "found"), result
    assert chain_requests[0].expirations == [OLD_EXPIRY, NEW_EXPIRY]
    assert "enumerated_expirations" in result["scope"], result
    assert result["scope"]["enumerated_expirations"] == [OLD_EXPIRY, NEW_EXPIRY]
    assert result["coverage"]["enumeration_complete"] is True
    assert result["outcome"] == ("unable" if missing_candidate else "found"), result
    assert result["coverage"]["status"] == ("unavailable" if missing_candidate else "complete")
    assert result["coverage"]["unassessed"] == int(missing_candidate)
    assert provider.closed == 1


def test_limit_is_applied_after_yield_ranking(tmp_path, setup_service):
    provider, _, _ = setup_service
    provider.rows.append(quote(94, NEW_EXPIRY, bid=6, ask=6.1))
    result = run(tmp_path, setup_service, {"limit": 1})
    assert result["coverage"]["matched"] == 2
    assert result["displayed"] == result["omitted"] == 1
    assert result["candidates"][0]["strike"] == 94


@pytest.mark.parametrize("stop", ["cancelled", "budget_exhausted"])
def test_already_stopped_request_never_reads_ledger_or_fetches(tmp_path, setup_service, monkeypatch, stop):
    provider, _, _ = setup_service
    monkeypatch.setattr(service, "_read_holdings", lambda **_: pytest.fail("stopped request read ledger"))
    result = run(tmp_path, setup_service, cancelled=lambda: stop == "cancelled",
        deadline=time.monotonic() - 1 if stop == "budget_exhausted" else time.monotonic() + 10)
    assert result["outcome"] == "unable"
    assert result["reason_codes"] == [stop]
    assert provider.open_calls == 0


def _wire_tool(tmp_path, setup_service, monkeypatch):
    from src.application.agent_tools import close_advice
    from src.application.tool_execution import execute_tool

    _, _, config = setup_service
    monkeypatch.setattr(close_advice, "load_runtime_config", lambda **_: (tmp_path / "config.us.json", config))
    monkeypatch.setattr(close_advice, "repo_base", lambda: Path(__file__).resolve().parents[1])
    monkeypatch.setattr("src.application.runtime_paths.resolve_runtime_root",
        lambda **_: SimpleNamespace(source="explicit", runtime_root=tmp_path))
    original = service.short_put_roll_candidates
    monkeypatch.setattr(service, "short_put_roll_candidates", lambda *args, **kwargs: original(*args, **kwargs, now_utc=NOW))
    return execute_tool


def test_public_tool_calls_real_service_and_preserves_report_and_input_contract(tmp_path, setup_service, monkeypatch):
    execute = _wire_tool(tmp_path, setup_service, monkeypatch)
    payload = {"config_key": "us", "account": "sample", "lot_id": "old", "max_new_expiration": NEW_EXPIRY}
    result = execute("short_put_roll_candidates", payload)
    assert result["ok"] is True
    assert result["data"]["outcome"] == "found"
    assert Path(result["data"]["report_path"]).is_file()
    assert result["meta"]["notifications_sent"] is False
    setup_service[0].requests.clear()
    for rejected in ({key: value for key, value in payload.items() if key != "max_new_expiration"},
                     {**payload, "contracts": 10}, {**payload, "refresh_quotes": False}):
        failure = execute("short_put_roll_candidates", rejected)
        assert failure["ok"] is False
        assert failure["error"]["code"] == "INPUT_ERROR"
    assert not setup_service[0].requests


def test_quality_gate_remains_specific_through_tool_envelope(tmp_path, setup_service, monkeypatch):
    from src.application.quality.gate import QualityGateBlocked

    execute = _wire_tool(tmp_path, setup_service, monkeypatch)

    def blocked(*_args, **_kwargs):
        raise QualityGateBlocked("close_advice", "fixture_block", ("position_lots",))

    monkeypatch.setattr(service, "assert_quality_allows", blocked)
    result = execute("short_put_roll_candidates", {"account": "sample", "lot_id": "old", "max_new_expiration": NEW_EXPIRY})
    assert result["ok"] is False
    assert result["error"]["code"] == "QUALITY_GATE_BLOCKED"
    assert not setup_service[0].requests


def test_registered_tool_requires_explicit_expiry_and_has_no_trade_effects():
    from src.application.agent_tool_registry import AGENT_TOOL_REGISTRY
    tool = AGENT_TOOL_REGISTRY["short_put_roll_candidates"]
    assert tool.read_only is True
    assert "max_new_expiration" in tool.input_json_schema()["required"]
    assert "lot_id" in tool.input_json_schema()["required"]
    assert tool.side_effects == ("fetches_market_data", "writes_isolated_request_reports")
