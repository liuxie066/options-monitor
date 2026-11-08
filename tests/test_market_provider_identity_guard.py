"""Offline regressions for provider identity and real Tick admission."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.application import market_data_provider as providers
from src.application.tick_guard_flow import TickGuardRequest, run_tick_guard_flow
from test_market_data_provider import RecordingProvider, install, no_live_io  # noqa: F401


def _config(provider, broker):
    settings = ({"type": "longbridge", "longbridge": {
        "account_id": "lb-1", "credentials_env_prefix": "OM_LB", "region": "ap", "trd_env": "REAL",
    }} if broker == "longbridge" else {"type": "futu", "futu": {
        "account_id": "123", "host": "broker.invalid", "port": 12346, "trd_env": "REAL",
    }})
    return {"market_data": {"provider": provider}, "accounts": ["user"],
            "account_settings": {"user": settings}, "portfolio": {"source": "auto"},
            "symbols": [{"symbol": "NVDA", "fetch": {"source": "futu", "host": "quote.invalid", "port": 12345}}]}


@pytest.mark.parametrize("provider,broker,expected", [
    ("fixture", "longbridge", set()),
    ("fixture", "futu", {("broker.invalid", 12346, "broker")}),
    ("opend", "longbridge", {("quote.invalid", 12345, "quote")}),
    ("opend", "futu", {("quote.invalid", 12345, "quote"), ("broker.invalid", 12346, "broker")}),
])
def test_tick_guard_probes_only_required_capabilities(monkeypatch, tmp_path, provider, broker, expected):
    install(monkeypatch, RecordingProvider(None))
    calls = []
    quiet = lambda *a, **kw: None
    request = TickGuardRequest(base=tmp_path, base_cfg=_config(provider, broker), accounts=["user"],
        default_account="user", market_config="auto", no_send=True, opend_phone_verify_continue=False,
        vpy=tmp_path / "unused", runlog=SimpleNamespace(safe_event=quiet),
        audit_helper=SimpleNamespace(audit=quiet, guard_mark_failure=quiet),
        complete_tick_idempotency_fn=quiet, allow_operational_side_effects=False,
        admit_project_run_fn=lambda *a: {"allowed": True}, apply_project_load_shed_fn=lambda accounts, guard: accounts,
        is_opend_phone_verify_pending_fn=lambda *a: False,
        run_opend_watchdog_fn=lambda **kw: calls.append(kw) or {"ok": True})
    result = run_tick_guard_flow(request)
    assert result.should_continue
    assert {(c["host"], c["port"], c["required_capability"]) for c in calls} == expected
    assert all(c["ensure"] is False for c in calls)


def test_unrelated_opend_phone_pending_does_not_block_alternate_longbridge(monkeypatch, tmp_path):
    install(monkeypatch, RecordingProvider(None))
    quiet = lambda *a, **kw: None
    result = run_tick_guard_flow(TickGuardRequest(base=tmp_path, base_cfg=_config("fixture", "longbridge"),
        accounts=["user"], default_account="user", market_config="auto", no_send=True,
        opend_phone_verify_continue=False, vpy=tmp_path / "unused", runlog=SimpleNamespace(safe_event=quiet),
        audit_helper=SimpleNamespace(audit=quiet, guard_mark_failure=quiet), complete_tick_idempotency_fn=quiet,
        allow_operational_side_effects=False, admit_project_run_fn=lambda *a: {"allowed": True},
        apply_project_load_shed_fn=lambda accounts, guard: accounts,
        is_opend_phone_verify_pending_fn=lambda *a: True,
        run_opend_watchdog_fn=lambda **kw: pytest.fail("unrequired OpenD probe")))
    assert result.should_continue


def test_alternate_source_receipt_and_cache_never_masquerade_as_opend(monkeypatch, tmp_path):
    import json
    from src.application.opend_symbol_outputs import finalize_required_data_quote_candidate, resolve_exact_fresh_required_data_quote_receipt
    from src.application.required_data_plan_identity import build_required_data_expected_fetch_contract
    from src.application.close_advice_quote_cache import quote_cache_metadata_path, validate_quote_cache_metadata
    from test_required_data_output_integrity import _contract, _payload, NOW, HOST, PORT

    install(monkeypatch, RecordingProvider(None))
    old_contract = _contract()
    plan = deepcopy(old_contract["fetch_plan"])
    plan["expiration_discovery"]["request_identity"]["source"] = "fixture"
    for spec in plan["merged_requests"]:
        spec["source"] = "fixture"
    contract = build_required_data_expected_fetch_contract(symbol="NVDA", fetch_plan=plan,
        source="fixture", host=HOST, port=PORT)
    payload = _payload()
    payload["meta"]["source"] = "fixture"
    now = NOW + timedelta(seconds=2)
    root = tmp_path / "required"
    result = finalize_required_data_quote_candidate(base=tmp_path, producer_root=root,
        producer_run_id="alternate", symbol="NVDA", expected_fetch_contract=contract,
        fetch_policy={"source": "fixture", "host": HOST, "port": PORT}, mode="fresh", payload=payload, now=now)
    assert result["quote_receipt"]["source_native_id"].startswith("fixture-required-data:")
    assert result["quote_receipt"]["broker"] != "futu"
    csv = root / "parsed" / "NVDA_required_data.csv"
    assert json.loads(quote_cache_metadata_path(csv).read_text())["source"] == "fixture"
    assert validate_quote_cache_metadata(csv_path=csv, symbol="NVDA", expected_source="fixture", now=now)["ok"]
    assert not validate_quote_cache_metadata(csv_path=csv, symbol="NVDA", now=now)["ok"]
    assert resolve_exact_fresh_required_data_quote_receipt(producer_root=root, symbol="NVDA", now=now,
        expected_fetch_contract=old_contract) is None


@pytest.mark.parametrize("complete,closed", [(True, False), (True, True), (False, False)])
def test_tick_calendar_routes_selected_provider_without_opend(monkeypatch, complete, closed):
    from src.application import multi_account_tick as tick
    from test_market_data_provider_consumers import ConsumerProvider

    provider = install(monkeypatch, ConsumerProvider(complete=complete))
    if closed:
        provider.get_trading_days_with_receipt = lambda gateway, **kw: {
            "retcode": 0, "coverage_complete": True, "pagination_complete": True,
            "page_count": 1, "rows": []}
    monkeypatch.setattr(tick, "resolve_futu_quote_route", lambda *a, **kw: pytest.fail("OpenD calendar route"))
    monkeypatch.setattr(tick, "trading_day_via_futu", lambda **kw: pytest.fail("OpenD calendar read"))
    result = tick._is_trading_day_guard_for_market(_config("fixture", "longbridge"), "US")
    assert result == ((not closed) if complete else None, "US")
    assert [name for name, _ in provider.calls][:2] == ["route", "open"]
    assert provider.closed == [True]


@pytest.mark.parametrize("age_minutes", [0, 60])
@pytest.mark.parametrize("selected,cached,refetch", [
    ("fixture", "opend", True), ("opend", "fixture", True),
    ("fixture", "fixture", False), ("opend", "opend", False),
])
def test_interactive_required_data_cache_is_source_specific(monkeypatch, tmp_path, selected, cached, refetch, age_minutes):
    from datetime import datetime, timezone
    from src.application.close_advice_quote_cache import publish_quote_cache_metadata
    from src.application import required_data_steps as steps

    install(monkeypatch, RecordingProvider(None))
    root = tmp_path / "required"
    (root / "parsed").mkdir(parents=True)
    (root / "parsed/NVDA_required_data.csv").write_text("symbol,dte\nNVDA,30\n")
    publish_quote_cache_metadata(csv_path=root / "parsed/NVDA_required_data.csv", symbol="NVDA",
        source=cached, source_run_id="fixture",
        observed_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes))
    calls = []
    def stop(request, **kw):
        calls.append(request)
        raise TimeoutError("source switched")
    monkeypatch.setattr(steps, "execute_required_data_opend", stop)
    def run():
        return steps.ensure_required_data(py="unused", base=tmp_path, symbol="NVDA", required_data_dir=root,
            limit_expirations=1, want_put=True, want_call=False, timeout_sec=1, is_scheduled=False,
            market_data_provider=selected)
    if refetch:
        with pytest.raises(TimeoutError, match="source switched"):
            run()
        assert calls[0].market_data_provider == selected
    else:
        assert run() is None
        assert calls == []


def test_close_advice_rejects_frozen_cache_after_source_switch(monkeypatch, tmp_path):
    from test_close_advice_required_data import _frozen_workspace
    from src.application.close_advice_runner import run_close_advice

    install(monkeypatch, RecordingProvider(None))
    frozen = _frozen_workspace(tmp_path)
    frozen[0]["market_data"] = {"provider": "fixture"}
    result = run_close_advice(**frozen.run_kwargs(tmp_path))
    assert result["snapshot_authority"] == "invalid"
    assert result["integrity_failure"]["reason"] == "required_data_snapshot_integrity_failed"


@pytest.mark.parametrize("provider,broker,pending", [
    ("opend", "longbridge", True), ("fixture", "futu", True), ("fixture", "futu", False),
])
def test_required_opend_failure_or_pending_blocks_tick(monkeypatch, tmp_path, provider, broker, pending):
    install(monkeypatch, RecordingProvider(None))
    quiet = lambda *a, **kw: None
    calls = []
    result = run_tick_guard_flow(TickGuardRequest(base=tmp_path, base_cfg=_config(provider, broker),
        accounts=["user"], default_account="user", market_config="auto", no_send=True,
        opend_phone_verify_continue=False, vpy=tmp_path / "unused", runlog=SimpleNamespace(safe_event=quiet),
        audit_helper=SimpleNamespace(audit=quiet, guard_mark_failure=quiet), complete_tick_idempotency_fn=quiet,
        allow_operational_side_effects=False, admit_project_run_fn=lambda *a: {"allowed": True},
        apply_project_load_shed_fn=lambda accounts, guard: accounts,
        is_opend_phone_verify_pending_fn=lambda *a: pending,
        run_opend_watchdog_fn=lambda **kw: calls.append(kw) or {"ok": False, "error_code": "OPEND_LOGIN_INVALID"}))
    assert not result.should_continue and result.return_code == 2
    assert len(calls) == (0 if pending else 1)
    if calls:
        assert calls[0]["required_capability"] == "broker"
        assert calls[0]["expected_account_ids"] == ["123"]


def _watchdog_cli_transport(monkeypatch, *, fail_constructor=False):
    from src.infrastructure import futu_gateway, opend_watchdog
    from test_futu_gateway_minimal import _BrokerClient

    calls = {name: [] for name in ("ports", "gateway", "sdk", "state", "accounts", "closed", "starts")}

    class TradeContext:
        def get_global_state(self):
            calls["state"].append(True)
            return 0, {"program_status_type": "READY", "trd_logined": True}

        def get_acc_list(self):
            calls["accounts"].append(True)
            return 0, [{"acc_id": account, "trd_env": environment} for account, environment in
                       [("123", "REAL"), ("456", "REAL"), ("789", "SIMULATE")]]

        def close(self):
            calls["closed"].append(True)

    class Backend:
        def __init__(self, **kwargs):
            self.host, self.port = kwargs["host"], kwargs["port"]
            self._trade_client = None

        def _ensure_trade_client(self):
            calls["sdk"].append((self.host, self.port))
            if fail_constructor:
                raise RuntimeError("synthetic SDK handshake unavailable")
            if self._trade_client is None:
                self._trade_client = TradeContext()
            return self._trade_client

    build = futu_gateway.build_ready_futu_broker_gateway

    def real_readiness_with_mock_sdk(**kwargs):
        calls["gateway"].append(kwargs)
        return build(**kwargs, backend_cls=Backend, client_cls=_BrokerClient)

    def forbidden_start():
        calls["starts"].append(True)
        raise AssertionError("CLI input must not start OpenD")

    monkeypatch.setattr(futu_gateway, "build_ready_futu_broker_gateway", real_readiness_with_mock_sdk)
    monkeypatch.setattr(opend_watchdog, "port_open", lambda host, port: calls["ports"].append((host, port)) or True)
    monkeypatch.setattr(opend_watchdog, "try_start_opend", forbidden_start)
    return calls


@pytest.mark.parametrize("identity_args", [[], ["--expected-account-id", ""],
                                          ["--expected-account-id", " \t "]],
                         ids=["absent", "empty", "whitespace"])
@pytest.mark.parametrize("fail_constructor", [False, True], ids=["sdk-available", "sdk-unavailable"])
def test_broker_watchdog_cli_missing_accounts_fail_before_sdk(monkeypatch, capsys, identity_args, fail_constructor):
    from src.infrastructure import opend_watchdog

    calls = _watchdog_cli_transport(monkeypatch, fail_constructor=fail_constructor)
    argv = ["--host", "isolated.invalid", "--port", "12346", "--required-capability", "broker",
            "--ensure", "--json", *identity_args]
    try:
        code = opend_watchdog.main(argv)
    except SystemExit as error:
        code = error.code
    output = capsys.readouterr()
    assert calls["sdk"] == []
    assert calls["ports"] == calls["gateway"] == calls["starts"] == []
    assert code == 2
    assert "--expected-account-id" in output.err
    assert output.out == ""


@pytest.mark.parametrize("accounts,environment,ready", [
    (["123"], "REAL", True), (["123", "456"], "REAL", True),
    (["123", "123"], "REAL", True), (["789"], "SIMULATE", True),
    (["999"], "REAL", False), (["123"], "SIMULATE", False),
])
def test_broker_watchdog_cli_keeps_real_account_readiness(monkeypatch, capsys, accounts, environment, ready):
    import json
    from src.infrastructure import opend_watchdog

    calls = _watchdog_cli_transport(monkeypatch)
    argv = ["--host", "isolated.invalid", "--port", "12346", "--required-capability", "broker",
            "--trd-env", environment, "--json"]
    for account in accounts:
        argv.extend(["--expected-account-id", account])
    assert opend_watchdog.main(argv) == (0 if ready else 2)
    output = capsys.readouterr()
    assert json.loads(output.out)["ok"] is ready
    assert not output.err
    assert len(calls["gateway"]) == 1
    assert calls["gateway"][0]["expected_account_ids"] == accounts
    assert calls["gateway"][0]["trd_env"] == environment
    assert calls["sdk"] == [("isolated.invalid", 12346)]
    assert calls["state"] == calls["accounts"] == calls["closed"] == [True]
    assert calls["starts"] == []


@pytest.mark.parametrize("capability,trade_ready,ready", [
    ("quote", False, True), ("both", True, True), ("both", False, False), (None, True, True),
])
def test_watchdog_cli_quote_and_both_keep_existing_behavior(monkeypatch, capsys, capability, trade_ready, ready):
    import json
    from src.infrastructure import opend_watchdog

    calls = _watchdog_cli_transport(monkeypatch)
    quote_calls = []

    def state(host, port, **kwargs):
        quote_calls.append((host, port, kwargs))
        return {"program_status_type": "READY", "qot_logined": True, "trd_logined": trade_ready}, None, None

    monkeypatch.setattr(opend_watchdog, "get_global_state", state)
    argv = ["--host", "isolated.invalid", "--port", "12346", "--json"]
    if capability is not None:
        argv.extend(["--required-capability", capability])
    assert opend_watchdog.main(argv) == (0 if ready else 2)
    output = capsys.readouterr()
    assert json.loads(output.out)["ok"] is ready
    assert not output.err
    assert len(quote_calls) == 1
    assert calls["sdk"] == calls["gateway"] == calls["starts"] == []


def test_broker_watchdog_subprocess_preserves_account_binding(monkeypatch, tmp_path):
    from src.infrastructure import external_services, opend_watchdog

    commands = []
    monkeypatch.setattr(external_services, "run_command", lambda command, **kw: commands.append(command))
    external_services.run_opend_watchdog(vpy=tmp_path / "python", base=tmp_path,
        host="broker.invalid", port=12346, ensure=False, required_capability="broker",
        expected_account_ids=["123"], trd_env="SIMULATE")
    args = opend_watchdog.build_parser().parse_args(commands[0][3:])
    assert args.expected_account_id == ["123"] and args.trd_env == "SIMULATE"
    assert args.required_capability == "broker" and not args.ensure
    calls = []
    monkeypatch.setattr(opend_watchdog, "run_watchdog_check", lambda **kw: calls.append(kw) or SimpleNamespace(ok=True))
    monkeypatch.setattr(opend_watchdog, "_emit", lambda *a: None)
    assert opend_watchdog.main(commands[0][3:]) == 0
    assert calls[0]["expected_account_ids"] == ["123"] and calls[0]["trd_env"] == "SIMULATE"
