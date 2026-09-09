from __future__ import annotations

import logging
import sqlite3
import sys
import threading
from types import SimpleNamespace

import pytest

from src.infrastructure.futu_trade_push import OpenDTradePushListener


@pytest.fixture(autouse=True)
def _open_port(monkeypatch):
    """Tests mock the futu SDK; keep the port pre-check passing."""

    from src.infrastructure import futu_trade_push as mod

    monkeypatch.setattr(mod, "port_open", lambda host, port: True)
    yield


def test_trade_push_listener_isolates_callback_exception(monkeypatch) -> None:
    class _FakeData:
        def to_dict(self, orient: str) -> list[dict]:
            assert orient == "records"
            return [{"deal_id": "bad"}, {"deal_id": "good"}]

    class _FakeHandlerBase:
        def on_recv_rsp(self, _rsp_pb):
            return 0, _FakeData()

    class _FakeContext:
        def __init__(self, **_kwargs):
            self.handler = None

        def set_sync_query_connect_timeout(self, timeout):
            assert timeout == 2
            self.connect_timeout = timeout

        def set_handler(self, handler):
            self.handler = handler

        def start(self):
            return None

        def close(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(OpenSecTradeContext=_FakeContext, TradeDealHandlerBase=_FakeHandlerBase),
    )
    seen: list[str] = []

    def _callback(row: dict) -> None:
        seen.append(str(row["deal_id"]))
        if row["deal_id"] == "bad":
            raise RuntimeError("boom")

    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=_callback)
    _ctx, handler = listener._build_default_context()

    ret, _data = handler.on_recv_rsp(None)

    assert ret == 0
    assert seen == ["bad", "good"]


def test_trade_push_listener_health_uses_existing_trade_context(monkeypatch) -> None:
    class _FakeHandlerBase:
        pass

    class _FakeContext:
        instances = 0

        def __init__(self, **_kwargs):
            type(self).instances += 1

        def set_sync_query_connect_timeout(self, timeout):
            assert timeout == 2
            self.connect_timeout = timeout

        def set_handler(self, _handler):
            return None

        def start(self):
            return None

        def get_global_state(self):
            return 0, {"program_status_type": "READY", "trd_logined": True, "qot_logined": False}

        def close(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(OpenSecTradeContext=_FakeContext, TradeDealHandlerBase=_FakeHandlerBase),
    )
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=lambda _row: None)

    listener.start()
    listener.check_health()

    assert _FakeContext.instances == 1


def test_trade_push_listener_health_raises_terminal_phone_verification(monkeypatch) -> None:
    from src.infrastructure.futu_trade_push import TradeIntakeAuthRequired

    class _FakeHandlerBase:
        pass

    class _FakeContext:
        def __init__(self, **_kwargs):
            return None

        def set_sync_query_connect_timeout(self, timeout):
            assert timeout == 2
            self.connect_timeout = timeout

        def set_handler(self, _handler):
            return None

        def start(self):
            return None

        def get_global_state(self):
            return -1, "需要手机验证码"

        def close(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(OpenSecTradeContext=_FakeContext, TradeDealHandlerBase=_FakeHandlerBase),
    )
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=lambda _row: None)
    listener.start()

    with pytest.raises(TradeIntakeAuthRequired) as _caught:
        listener.check_health()
    exc = _caught.value
    assert exc.error_code == "OPEND_NEEDS_PHONE_VERIFY"
    assert "需要手机验证码" in exc.detail


def test_trade_push_listener_health_keeps_disconnect_retryable(monkeypatch) -> None:
    class _FakeHandlerBase:
        pass

    class _FakeContext:
        def __init__(self, **_kwargs):
            return None

        def set_sync_query_connect_timeout(self, timeout):
            assert timeout == 2
            self.connect_timeout = timeout

        def set_handler(self, _handler):
            return None

        def start(self):
            return None

        def get_global_state(self):
            raise ConnectionResetError("connection reset")

        def close(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(OpenSecTradeContext=_FakeContext, TradeDealHandlerBase=_FakeHandlerBase),
    )
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=lambda _row: None)
    listener.start()

    with pytest.raises(RuntimeError) as _caught:
        listener.check_health()
    exc = _caught.value
    assert "OPEND_API_ERROR" in str(exc)


def test_trade_push_listener_detects_auth_while_constructor_blocks(monkeypatch) -> None:
    from src.infrastructure.futu_trade_push import TradeIntakeAuthRequired

    release_constructor = threading.Event()

    class _FakeHandlerBase:
        pass

    class _BlockingContext:
        def __init__(self, **_kwargs):
            logging.getLogger("FTConsoleLog").warning(
                "[open_context_base.py:407] _init_connect_sync: init connect fail: "
                "msg=需要手机验证码 context=<futu.trade.open_trade_context.OpenSecTradeContext object>"
            )
            release_constructor.wait(5)

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(OpenSecTradeContext=_BlockingContext, TradeDealHandlerBase=_FakeHandlerBase),
    )
    sdk_logger = logging.getLogger("FTConsoleLog")
    handlers_before = list(sdk_logger.handlers)
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=lambda _row: None)

    try:
        with pytest.raises(TradeIntakeAuthRequired) as caught:
            listener.start()
        assert caught.value.error_code == "OPEND_NEEDS_PHONE_VERIFY"
    finally:
        release_constructor.set()

    assert list(sdk_logger.handlers) == handlers_before


def test_trade_push_listener_cancels_blocked_constructor_and_removes_handler(monkeypatch) -> None:
    from src.infrastructure.futu_trade_push import TradeIntakeStartCancelled

    release_constructor = threading.Event()

    class _FakeHandlerBase:
        pass

    class _BlockingContext:
        def __init__(self, **_kwargs):
            release_constructor.wait(5)

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(OpenSecTradeContext=_BlockingContext, TradeDealHandlerBase=_FakeHandlerBase),
    )
    sdk_logger = logging.getLogger("FTConsoleLog")
    handlers_before = list(sdk_logger.handlers)
    cancel_event = threading.Event()
    cancel_event.set()
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=lambda _row: None)

    try:
        listener.start(cancel_event=cancel_event)
    except TradeIntakeStartCancelled:
        pass
    else:
        raise AssertionError("expected cancelled construction")
    finally:
        release_constructor.set()

    assert list(sdk_logger.handlers) == handlers_before


def test_trade_push_listener_constructor_error_removes_handler(monkeypatch) -> None:
    class _FakeHandlerBase:
        pass

    class _FailingContext:
        def __init__(self, **_kwargs):
            raise ConnectionRefusedError("refused")

    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(OpenSecTradeContext=_FailingContext, TradeDealHandlerBase=_FakeHandlerBase),
    )
    sdk_logger = logging.getLogger("FTConsoleLog")
    handlers_before = list(sdk_logger.handlers)
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=lambda _row: None)

    with pytest.raises(RuntimeError) as _caught:
        listener.start()
    exc = _caught.value
    assert "failed to initialize" in str(exc)

    assert list(sdk_logger.handlers) == handlers_before


def test_listener_raises_typed_unreachable_when_port_closed(monkeypatch) -> None:
    from src.infrastructure import futu_trade_push as mod
    from src.infrastructure.futu_gateway import FutuGatewayUnreachableError

    monkeypatch.setattr(mod, "port_open", lambda host, port: False)
    monkeypatch.setitem(
        sys.modules,
        "futu",
        SimpleNamespace(OpenSecTradeContext=object, TradeDealHandlerBase=object),
    )

    listener = OpenDTradePushListener(host="127.0.0.9", port=11119, on_deal=lambda payload: None)
    with pytest.raises(FutuGatewayUnreachableError):
        listener._build_default_context()


def _mock_sdk_rows(monkeypatch, *, rows, accounts, stop=None, response=None, handler_base=None):
    class Frame:
        def __init__(self, values):
            self.values = values

        def to_dict(self, orient):
            assert orient == "records"
            return self.values

    class HandlerBase:
        def on_recv_rsp(self, _rsp):
            return 0, Frame(rows)

    class Context:
        def __init__(self, **_kwargs):
            self.account_reads = 0

        def get_acc_list(self):
            self.account_reads += 1
            return (0, Frame(accounts)) if accounts is not None else (-1, "unavailable")

        def set_sync_query_connect_timeout(self, timeout):
            assert timeout == 2
            self.connect_timeout = timeout

        def set_handler(self, handler):
            self.handler = handler

        def start(self):
            self.handler.on_recv_rsp(response)
            if stop is not None:
                stop.set()

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "futu", SimpleNamespace(
        OpenSecTradeContext=Context, TradeDealHandlerBase=handler_base or HandlerBase,
        TrdEnv=SimpleNamespace(to_string2=lambda value: {0: "SIMULATE", 1: "REAL"}.get(value, "N/A")),
    ))


def _push_response(**fields):
    return SimpleNamespace(s2c=SimpleNamespace(header=SimpleNamespace(
        HasField=lambda key: key in fields, **fields,
    )))


@pytest.mark.parametrize("physical,environment", [(123, "REAL"), (456, "SIMULATE")])
def test_push_header_selects_exact_visible_account_before_conversion(monkeypatch, physical, environment):
    response = _push_response(accID=physical, trdEnv=1 if environment == "REAL" else 0)
    row = {"deal_id": "fill-1", "order_id": "order-1", "trd_env": environment}
    _mock_sdk_rows(monkeypatch, rows=[row], response=response, accounts=[
        {"acc_id": "123", "trd_env": "REAL"}, {"acc_id": "456", "trd_env": "SIMULATE"},
    ])
    seen = []
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=seen.append)
    listener.start()
    assert seen == [{**row, "acc_id": str(physical), "environment": environment,
                     "broker_account_id": f"futu:{environment}:{physical}",
                     "external_id_namespace": "futu.deal", "external_order_namespace": "futu.order",
                     "_trade_intake_push_header": {"accID": physical, "trdEnv": 1 if environment == "REAL" else 0}}]


@pytest.mark.parametrize("header,row,accounts,error", [
    ({"accID": 123, "trdEnv": 1}, {"acc_id": "456"}, [{"acc_id": "123", "trd_env": "REAL"}], "conflict:push_physical_account"),
    ({"accID": 123, "trdEnv": 1}, {"trd_env": "SIMULATE"}, [{"acc_id": "123", "trd_env": "REAL"}], "conflict:push_trd_env"),
    ({"accID": 123, "trdEnv": 0}, {}, [{"acc_id": "123", "trd_env": "REAL"}], "conflict:push_trd_env"),
    ({"accID": 123, "trdEnv": 1}, {}, [], "missing:source_account_environment"),
    ({"accID": 123, "trdEnv": 1}, {}, None, "missing:source_account_environment"),
    ({"trdEnv": 1}, {"acc_id": "123"}, [{"acc_id": "123", "trd_env": "REAL"}], "missing:push_header_physical_account"),
    ({"accID": 123}, {}, [{"acc_id": "123", "trd_env": "REAL"}], "missing:push_header_environment"),
    ({"accID": 0, "trdEnv": 1}, {}, [], "invalid:push_header_physical_account"),
    ({"accID": "123", "trdEnv": 1}, {}, [], "invalid:push_header_physical_account"),
    ({"accID": 123, "trdEnv": 7}, {}, [], "invalid:push_header_environment"),
    ({"accID": 123, "trdEnv": True}, {}, [], "invalid:push_header_environment"),
    ({"accID": 123, "trdEnv": 1}, {"acc_id": "123.0"}, [], "invalid:push_physical_account"),
])
def test_push_header_rejects_missing_invalid_conflicting_identity(monkeypatch, header, row, accounts, error):
    row = {"deal_id": "fill-1", **row}
    _mock_sdk_rows(monkeypatch, rows=[row], accounts=accounts, response=_push_response(**header))
    seen = []
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=seen.append)
    listener.start()
    assert len(seen) == 1
    assert {key: seen[0][key] for key in row} == row
    assert error in seen[0]["_trade_intake_source_identity_errors"]
    assert seen[0]["_trade_intake_push_header"] == header
    assert "broker_account_id" not in seen[0]


def test_real_sdk_protobuf_header_survives_dataframe_conversion(monkeypatch, tmp_path):
    import os.path

    # Importing the SDK opens its logger; keep even that I/O in test storage.
    join = os.path.join
    monkeypatch.setattr(os.path, "join", lambda *parts: str(tmp_path / "sdk-log")
                        if parts[-1:] == (".com.futunn.FutuOpenD/Log",) else join(*parts))
    futu = pytest.importorskip("futu")
    from futu.common.pb import Trd_UpdateOrderFill_pb2

    response = Trd_UpdateOrderFill_pb2.Response()
    response.retType = 0
    response.s2c.header.accID = 123
    response.s2c.header.trdEnv = 1
    response.s2c.header.trdMarket = 2
    deal = response.s2c.orderFill
    deal.fillID = 321
    deal.fillIDEx = "321"
    deal.orderIDEx = "order-1"
    deal.trdSide = 1
    deal.code = "NVDA"
    deal.name = "NVIDIA"
    deal.qty = 1
    deal.price = 100
    deal.createTime = "2026-09-09 10:00:00"
    deal.secMarket = 2
    assert response.IsInitialized()
    ret, frame = futu.TradeDealHandlerBase().on_recv_rsp(response)
    assert ret == 0
    assert "acc_id" not in frame.columns
    _mock_sdk_rows(monkeypatch, rows=None, accounts=[{"acc_id": "123", "trd_env": "REAL"}],
                   response=response, handler_base=futu.TradeDealHandlerBase)
    seen = []
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=seen.append)
    listener.start()
    assert len(seen) == 1
    assert seen[0]["acc_id"] == "123"
    assert seen[0]["broker_account_id"] == "futu:REAL:123"
    assert "_trade_intake_source_identity_errors" not in seen[0]


def test_start_wait_hook_runs_repeatedly_before_initialization_and_stops(monkeypatch):
    from src.infrastructure.futu_trade_push import TradeIntakeStartCancelled

    constructed = threading.Event()
    release_constructor = threading.Event()
    worker_finished = threading.Event()
    stop = threading.Event()
    calls = []
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=lambda _: None)

    def build():
        constructed.set()
        try:
            assert release_constructor.wait(5)
            raise RuntimeError("test constructor released")
        finally:
            worker_finished.set()

    def on_wait():
        assert constructed.is_set()
        assert not release_constructor.is_set()
        assert not stop.is_set()
        calls.append(len(calls))
        if len(calls) == 3:
            stop.set()

    monkeypatch.setattr(listener, "_build_default_context", build)
    try:
        with pytest.raises(TradeIntakeStartCancelled):
            listener.start(cancel_event=stop, on_wait=on_wait)
        assert calls == [0, 1, 2]
    finally:
        release_constructor.set()
        assert worker_finished.wait(2)


def test_push_binds_only_matching_account_snapshot_and_preserves_order_alias(monkeypatch):
    row = {"acc_id": "000123", "deal_id": "fill-1", "orderID": "order-1", "trd_env": "TrdEnv.REAL"}
    _mock_sdk_rows(monkeypatch, rows=[row], accounts=[{"acc_id": "000123", "trd_env": "REAL"}])
    seen = []
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=seen.append)
    ctx, handler = listener._build_default_context()
    handler.on_recv_rsp(None)
    assert ctx.account_reads == 1
    assert seen == [{**row, "environment": "REAL", "broker_account_id": "futu:REAL:000123",
                     "external_id_namespace": "futu.deal", "external_order_namespace": "futu.order"}]
    assert "environment" not in row


@pytest.mark.parametrize("field,value", [
    ("environment", "SIMULATE"), ("trd_env", "TrdEnv.SIMULATE"),
    ("broker_account_id", "futu:SIMULATE:123"),
    ("external_id_namespace", "other.deal"), ("execution_id_namespace", "other.deal"),
    ("external_order_namespace", "other.order"), ("order_id_namespace", "other.order"),
])
def test_push_known_identity_conflicts_are_retained_without_silent_overwrite(monkeypatch, field, value):
    row = {"acc_id": "123", "deal_id": "fill-1", "order_id": "order-1", field: value}
    _mock_sdk_rows(monkeypatch, rows=[row], accounts=[{"acc_id": "123", "trd_env": "REAL"}])
    seen = []
    listener = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=seen.append)
    _ctx, handler = listener._build_default_context()
    handler.on_recv_rsp(None)
    assert len(seen) == 1
    assert {key: seen[0][key] for key in row} == row
    assert f"conflict:push_{field}" in seen[0]["_trade_intake_source_identity_errors"]
    assert seen[0]["_trade_intake_source_account_evidence"]["environment"] == "REAL"


@pytest.mark.parametrize("row,accounts,error", [
    ({"deal_id": "fill-1"}, [{"acc_id": "123", "trd_env": "REAL"}], "missing:push_physical_account"),
    ({"deal_id": "fill-1", "account": "lx"}, [{"acc_id": "123", "trd_env": "REAL"}], "missing:push_physical_account"),
    ({"deal_id": "fill-1", "acc_id": "123"}, None, "missing:source_account_environment"),
    ({"deal_id": "fill-1", "acc_id": "123", "futu_account_id": "456"}, [], "conflict:push_physical_account"),
    ({"deal_id": "fill-1", "acc_id": "123"}, [{"acc_id": "123", "trd_env": "REAL"}, {"acc_id": "123", "trd_env": "SIMULATE"}], "conflict:source_account_environment"),
])
def test_push_missing_or_ambiguous_physical_binding_is_not_inferred(monkeypatch, row, accounts, error):
    _mock_sdk_rows(monkeypatch, rows=[row], accounts=accounts)
    seen = []
    _ctx, handler = OpenDTradePushListener(host="127.0.0.1", port=11111, on_deal=seen.append)._build_default_context()
    handler.on_recv_rsp(None)
    assert len(seen) == 1
    assert {key: seen[0][key] for key in row} == row
    assert error in seen[0]["_trade_intake_source_identity_errors"]
    assert "environment" not in seen[0]
    assert "broker_account_id" not in seen[0]


@pytest.mark.parametrize("row", [
    {"deal_id": "fill-1", "code": "US.NVDA260918P100000"},
    {"deal_id": "fill-1", "acc_id": "123", "trd_env": "SIMULATE", "code": "US.NVDA260918P100000"},
])
def test_real_listener_source_loop_keeps_rejected_push_in_durable_review(monkeypatch, tmp_path, row):
    import json
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades import auto_intake

    stop = threading.Event()
    _mock_sdk_rows(monkeypatch, rows=[row], accounts=[{"acc_id": "123", "trd_env": "REAL"}], stop=stop)
    monkeypatch.setattr(auto_intake, "OpenDHistoryDealClient", lambda **_kwargs: SimpleNamespace(close=lambda: None))
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    authoritative = tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3"
    source = {"id": "lx", "account": "lx", "host": "127.0.0.1", "port": 11111,
              "state_path": tmp_path / "state.json", "audit_path": tmp_path / "audit.jsonl",
              "status_path": tmp_path / "status.json", "inbox_path": authoritative,
              "account_mapping": {"123": "lx"}, "futu_account_ids": ["123"],
              "backfill": {"enabled": False}, "settlement_observation": {"enabled": False}}
    result = auto_intake._run_listener_source_loop(
        source=source, repo=repo, cfg={}, cfg_path=tmp_path / "config.json", runtime_root=tmp_path,
        runtime_root_source="test", intake_cfg={"enabled": True, "mode": "apply", "backfill": {"enabled": False}},
        apply_changes=True, receipt_callback=lambda _context: pytest.fail("rejected push must not send"),
        process_lock=threading.RLock(), stop_event=stop,
    )
    assert result == 0
    assert repo.list_trade_events() == []
    with sqlite3.connect(authoritative) as conn:
        stored = conn.execute("SELECT payload_json, status, broker_deal_key FROM trade_inbox").fetchall()
    assert len(stored) == 1
    payload = json.loads(stored[0][0])
    assert {key: payload[key] for key in row} == row
    assert stored[0][1:] == ("identity_needs_review", None)
    assert "futu_account_id" not in payload
