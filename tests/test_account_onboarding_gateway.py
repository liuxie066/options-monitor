"""Bounded account metadata through the real adapter and a fake SDK only."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from src.infrastructure import futu_gateway as mod


@pytest.fixture
def sdk(monkeypatch):
    state = SimpleNamespace(
        rows=[], ret=0, ready={"program_status_type": "READY", "trd_logined": True},
        calls=[], closed=0, init_error=None, query_error=None,
    )

    class Trade:
        def __init__(self, **kwargs):
            state.calls.append(("trade", kwargs))
            if state.init_error:
                raise state.init_error

        def set_sync_query_connect_timeout(self, timeout):
            assert timeout == 8

        def get_sync_conn_id(self):
            return 1

        def get_global_state(self):
            state.calls.append("state")
            return 0, state.ready

        def get_acc_list(self):
            state.calls.append("metadata")
            if state.query_error:
                raise state.query_error
            return state.ret, state.rows

        def close(self):
            state.closed += 1

    def no_quote(**kwargs):
        pytest.fail("account metadata must never construct a quote context")

    monkeypatch.setitem(sys.modules, "futu", SimpleNamespace(
        RET_OK=0, OpenSecTradeContext=Trade, OpenQuoteContext=no_quote,
    ))
    monkeypatch.setattr(mod, "port_open", lambda host, port: True)
    monkeypatch.setattr(
        mod, "_start_futu_query_context",
        lambda kind, kwargs, *, deadline=None, cancelled=None: mod._FutuQueryContext(
            mod._create_futu_sdk_context(kind, kwargs),
            deadline=deadline, cancelled=cancelled,
        ),
    )
    return state


def row(account_id="912340001", **overrides):
    return {"acc_id": account_id, "trd_env": "TrdEnv.SIMULATE", "trdmarket_auth": ["TrdMarket.US"],
            "acc_type": "AccType.MARGIN", "sim_acc_type": "OPTION", **overrides}


def candidates(**kwargs):
    gateway = mod.build_futu_gateway(host="synthetic", port=12345)
    try:
        return gateway.get_account_candidates(trd_env="SIMULATE", expected_market="us", **kwargs)
    finally:
        gateway.close()
        gateway.close()


def test_candidate_sdk_dataframe_lossless_identity_environment_and_market(sdk):
    sdk.rows = pd.DataFrame([
        row(912340001), row("922340001"), row("932340001", trd_env="REAL"),
        row("942340001", trdmarket_auth=["HK"]), row(952340001.0),
        row(True), row("00912340001"),
    ])
    result = candidates()
    assert result["status"] == "ok"
    assert [c["account_id"] for c in result["candidates"]] == ["912340001", "922340001"]
    assert all(c["masked_account_id"] == "*****0001" for c in result["candidates"])
    assert result["candidates"][0]["acc_type"] == "MARGIN"
    assert result["identity_unknown_count"] == 3
    assert sdk.calls == [("trade", {"host": "synthetic", "port": 12345}), "state", "metadata"]
    assert sdk.closed == 1


@pytest.mark.parametrize("rows,status", [
    ([], "empty"), ([row(trd_env="REAL")], "no_match"),
    ([row(trdmarket_auth=[])], "no_match"), ([row(trdmarket_auth=["HK"])], "no_match"),
    ([row(trdmarket_auth=None)], "permission_unavailable"),
    ([row(trdmarket_auth="['US']")], "permission_unavailable"),
    ([row(900000001.0)], "identity_unavailable"),
])
def test_empty_mismatch_and_unknown_are_distinct(sdk, rows, status):
    sdk.rows = rows
    result = candidates()
    assert result["status"] == status
    assert result["candidates"] == []
    assert sdk.closed == 1


def test_bounded_candidates_report_truncation_and_do_not_expose_arbitrary_sdk_text(sdk):
    sdk.rows = [row(str(900000000 + i), acc_type="912340001", sim_acc_type="garbage 912340001")
                for i in range(30)]
    result = candidates(max_candidates=2)
    assert len(result["candidates"]) == 2
    assert result["truncated"] is True
    assert result["candidates"][0]["acc_type"] is None
    assert result["candidates"][0]["sim_acc_type"] is None
    assert sdk.calls.count("metadata") == 1


@pytest.mark.parametrize("limit", [0, -1, 21, True, 1.5])
def test_invalid_limit_never_constructs_sdk(sdk, limit):
    with pytest.raises(mod.FutuGatewayError, match="invalid selection"):
        candidates(max_candidates=limit)
    assert sdk.calls == []


@pytest.mark.parametrize("scope", [
    {"trd_env": "unknown", "expected_market": "US"},
    {"trd_env": "REAL", "expected_market": "unknown"},
])
def test_invalid_scope_never_constructs_sdk(sdk, scope):
    gateway = mod.build_futu_gateway()
    with pytest.raises(mod.FutuGatewayError, match="invalid selection"):
        gateway.get_account_candidates(**scope)
    assert sdk.calls == []


@pytest.mark.parametrize("phase", ["init", "query"])
def test_sdk_failure_preserves_mapping_without_id_in_exception_or_log(sdk, phase, caplog):
    if phase == "init":
        sdk.init_error = RuntimeError("phone verification account 912340001")
    else:
        sdk.ret, sdk.rows = -1, "phone verification account 912340001"
    with pytest.raises(mod.FutuGatewayNeed2FAError) as caught:
        candidates()
    assert "912340001" not in str(caught.value) + caplog.text
    assert caught.value.raw_error is None
    assert caught.value.__suppress_context__
    assert sdk.closed == (1 if phase == "query" else 0)


def test_not_logged_in_is_not_an_empty_account_list(sdk):
    sdk.ready["trd_logined"] = False
    with pytest.raises(mod.FutuGatewayAuthExpiredError):
        candidates()
    assert "metadata" not in sdk.calls
    assert sdk.closed == 1


@pytest.mark.parametrize("bad_rows", [None, "unavailable", [None], {"acc_id": "912340001"}])
def test_malformed_success_is_not_a_valid_empty_list(sdk, bad_rows):
    sdk.rows = bad_rows
    with pytest.raises(mod.FutuGatewayError):
        candidates()
    assert sdk.closed == 1


def test_hk_real_metadata_scope_does_not_borrow_simulate_us_account(sdk):
    sdk.rows = [row(), row("987650001", trd_env="TrdEnv.REAL", trdmarket_auth="TrdMarket.HK")]
    gateway = mod.build_futu_gateway()
    try:
        result = gateway.get_account_candidates(trd_env="TrdEnv.REAL", expected_market="hk")
    finally:
        gateway.close()
    assert result["candidates"][0]["account_id"] == "987650001"
    assert len(result["candidates"]) == 1


@pytest.mark.parametrize("failure", [KeyboardInterrupt, TimeoutError])
def test_cancel_or_query_timeout_closes_sdk_in_application_facade(sdk, failure):
    from src.application.futu_doctor import discover_account_candidates

    sdk.query_error = failure()
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            discover_account_candidates(host="synthetic", port=12345, market="us", trd_env="SIMULATE")
    else:
        result = discover_account_candidates(host="synthetic", port=12345, market="us", trd_env="SIMULATE")
        assert result["status"] == "unavailable"
    assert sdk.closed == 1


def test_public_helper_through_doctor_and_sdk_selects_only_confirmed_metadata(sdk, capsys):
    from src.interfaces.cli.account_selection import select_account

    sdk.rows = [row("810001111"), row("820001111")]
    answers = iter(["yes", "2", "yes"])
    selected = select_account(host="synthetic", port=12345, market="us", trd_env="SIMULATE",
                              input_fn=lambda prompt: next(answers))
    assert selected.account_id == "820001111"
    assert selected.validation == "metadata_matched"
    assert sdk.calls == [("trade", {"host": "synthetic", "port": 12345}), "state", "metadata"]
    assert sdk.closed == 1
    output = capsys.readouterr().out
    assert "810001111" not in output and "820001111" not in output


def test_public_helper_manual_path_never_creates_sdk(sdk):
    from src.interfaces.cli.account_selection import select_account

    answers = iter(["manual", "810001111"])
    selected = select_account(host="synthetic", port=12345, market="us", trd_env="REAL",
                              input_fn=lambda prompt: next(answers))
    assert selected.account_id == "810001111"
    assert selected.validation == "pending_validation"
    assert sdk.calls == []


def test_default_limit_is_finite(sdk):
    sdk.rows = [row(str(810000000 + i)) for i in range(30)]
    result = candidates()
    assert len(result["candidates"]) == mod.FUTU_ACCOUNT_CANDIDATE_LIMIT == 20
    assert result["truncated"] is True


@pytest.mark.parametrize("ready", [None, {}, {"program_status_type": "STARTING", "trd_logined": True}])
def test_unavailable_global_state_is_not_an_empty_list(sdk, ready):
    sdk.ready = ready
    with pytest.raises(mod.FutuGatewayError):
        candidates()
    assert "metadata" not in sdk.calls
    assert sdk.closed == 1
