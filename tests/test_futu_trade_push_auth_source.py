"""InitConnect provenance through real listener/gateway and an isolated SDK Timer."""
from __future__ import annotations

from copy import deepcopy
import logging
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from src.infrastructure import futu_gateway as gateway
from src.infrastructure import futu_trade_push as push


class _Attempt:
    def __init__(self, message="", ret=0):
        self.result = (ret, message)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.logged = threading.Event()
        self.ready = threading.Event()
        self.done = threading.Event()
        self.stop = threading.Event()
        self.context = None
        self.listener = None
        self.outcome = None
        self.handshakes = 0
        self.constructor_thread = None
        self.handshake_thread = None


class _SdkLab:
    def __init__(self, monkeypatch):
        self.attempts = []
        self.threads = []
        self.errors = []
        self.calls = []
        self.initial_threads = {thread.ident for thread in threading.enumerate()}
        self.handlers = list(logging.getLogger("FTConsoleLog").handlers)
        self.next_attempt = 0
        lab = self

        class Context:
            def __init__(self, *, host, port, is_encrypt=None):
                attempt = lab.attempts[lab.next_attempt]
                lab.next_attempt += 1
                self.attempt = attempt
                attempt.context = self
                attempt.constructor_thread = threading.get_ident()
                self.closed = 0
                self.scheduled = []
                lab.calls.append((host, port, is_encrypt))
                assert self._init_connect_sync() == 0

            def _wait_reconnect(self, delay):
                self.scheduled.append(delay)
                assert delay == 0
                timer = threading.Timer(delay, self._reconnect)
                lab.threads.append(timer)
                timer.start()

            def _reconnect(self):
                try:
                    self._init_connect_sync()
                except BaseException as exc:
                    lab.errors.append(exc)

            def _init_connect_sync(self):
                attempt = self.attempt
                attempt.handshakes += 1
                attempt.handshake_thread = threading.get_ident()
                ret, message = self._send_init_connect_sync()
                if ret != 0:
                    logging.getLogger("FTConsoleLog").warning(
                        "init connect fail: msg=%s context=%r", message, self,
                    )
                attempt.logged.set()
                return ret

            def _send_init_connect_sync(self):
                self.attempt.entered.set()
                assert self.attempt.release.wait(3), "test did not release SDK result"
                return self.attempt.result

            def get_sync_conn_id(self):
                # Controlled transport readiness also lets abandoned contexts finish late.
                return 1 if self.attempt.ready.is_set() else 0

            def set_sync_query_connect_timeout(self, timeout):
                assert timeout in (8.0, 2)

            def get_acc_list(self):
                return 0, []

            def set_handler(self, _handler):
                return 0

            def start(self):
                return None

            def close(self):
                self.closed += 1

        def worker(*args, **kwargs):
            thread = threading.Thread(*args, **kwargs)
            self.threads.append(thread)
            return thread

        monkeypatch.setitem(sys.modules, "futu", SimpleNamespace(
            RET_OK=0, OpenSecTradeContext=Context,
            TradeDealHandlerBase=type("DealBase", (), {}),
            TradeOrderHandlerBase=type("OrderBase", (), {}),
        ))
        monkeypatch.setattr(gateway, "port_open", lambda host, port: host.endswith(".invalid") and port == 1)
        monkeypatch.setattr(push, "threading", SimpleNamespace(Thread=worker, Event=threading.Event, Lock=threading.Lock))

    def add(self, message="", ret=0):
        attempt = _Attempt(message, ret)
        self.attempts.append(attempt)
        return attempt

    def spawn(self, fn):
        def run():
            try:
                fn()
            except BaseException as exc:
                self.errors.append(exc)
        thread = threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()
        return thread

    def start(self, attempt, *, broker=False, host="same-endpoint.invalid", listener=None):
        def run():
            try:
                if broker:
                    context = gateway.build_futu_trade_context(host=host, port=1)
                    context.close()
                else:
                    attempt.listener = listener or push.OpenDTradePushListener(host=host, port=1, on_deal=lambda _row: None)
                    attempt.listener.start(cancel_event=attempt.stop)
                attempt.outcome = "ok"
            except Exception as exc:
                attempt.outcome = exc
            finally:
                attempt.done.set()
        self.spawn(run)
        assert attempt.entered.wait(2)

    def terminal(self, attempt):
        assert attempt.done.wait(2), "listener did not reach its expected terminal result"
        assert not self.errors, self.errors
        return attempt.outcome

    def close(self):
        for attempt in self.attempts:
            attempt.stop.set()
            attempt.release.set()
            attempt.ready.set()
        deadline = time.monotonic() + 3
        for thread in self.threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        assert all(not thread.is_alive() for thread in self.threads)
        assert not [thread for thread in threading.enumerate() if thread.ident not in self.initial_threads]
        for attempt in self.attempts:
            if attempt.listener is not None:
                attempt.listener.close()
        assert not self.errors, self.errors
        for attempt in self.attempts:
            assert attempt.context.closed == 1
            assert attempt.context.scheduled == [0] and attempt.handshakes == 1
        assert logging.getLogger("FTConsoleLog").handlers == self.handlers


@pytest.fixture
def sdk_lab(monkeypatch):
    lab = _SdkLab(monkeypatch)
    try:
        yield lab
    finally:
        lab.close()


@pytest.mark.parametrize("message,code,reverse", [
    ("需要手机验证码", "OPEND_NEEDS_PHONE_VERIFY", False),
    ("需要手机验证码", "OPEND_NEEDS_PHONE_VERIFY", True),
    ("需要图形验证码", "OPEND_NEEDS_PIC_VERIFY", False),
    ("登录密码被修改，已退出登录", "OPEND_LOGIN_INVALID", False),
])
def test_concurrent_listener_auth_belongs_only_to_its_context(sdk_lab, message, code, reverse):
    # Start order fixes constructor assignment; both sources share the same endpoint.
    first = sdk_lab.add("" if reverse else message, 0 if reverse else -1)
    second = sdk_lab.add(message if reverse else "", -1 if reverse else 0)
    sdk_lab.start(first)
    sdk_lab.start(second)
    auth, healthy = (second, first) if reverse else (first, second)
    auth.release.set()
    healthy.release.set()
    assert auth.logged.wait(2)
    result = sdk_lab.terminal(auth)
    assert isinstance(result, push.TradeIntakeAuthRequired)
    assert result.error_code == code
    assert not healthy.done.wait(1), "healthy listener must await its own readiness"
    healthy.ready.set()
    assert sdk_lab.terminal(healthy) == "ok"
    assert result.detail == message
    assert auth.handshake_thread != auth.constructor_thread
    assert healthy.handshake_thread != healthy.constructor_thread


@pytest.mark.parametrize("foreign_kind", ["broker", "raw_log"])
def test_foreign_auth_evidence_does_not_block_initializing_listener(sdk_lab, foreign_kind):
    target = sdk_lab.add()
    sdk_lab.start(target)
    if foreign_kind == "broker":
        foreign = sdk_lab.add("需要手机验证码", -1)
        sdk_lab.start(foreign, broker=True, host="broker.invalid")
        foreign.release.set()
        assert foreign.logged.wait(2)
        foreign.ready.set()
        assert sdk_lab.terminal(foreign) == "ok"
    else:
        logging.getLogger("FTConsoleLog").warning("init connect fail: 需要手机验证码 context=OpenSecTradeContext")
    assert not target.done.wait(1), "foreign evidence must not terminate the listener"
    target.release.set()
    target.ready.set()
    assert sdk_lab.terminal(target) == "ok"


def test_both_auth_sources_keep_distinct_upstream_messages(sdk_lab):
    a = sdk_lab.add("A 需要手机验证码", -1)
    b = sdk_lab.add("B 需要图形验证码", -1)
    sdk_lab.start(a)
    sdk_lab.start(b)
    a.release.set(); b.release.set()
    assert a.logged.wait(2) and b.logged.wait(2)
    for attempt, code in ((a, "OPEND_NEEDS_PHONE_VERIFY"), (b, "OPEND_NEEDS_PIC_VERIFY")):
        result = sdk_lab.terminal(attempt)
        assert isinstance(result, push.TradeIntakeAuthRequired)
        assert result.error_code == code and result.detail == attempt.result[1]


@pytest.mark.parametrize("message", ["quote not logged in", "trade not logged in", "not logged in", "rate limit", "connection refused"])
def test_non_auth_init_result_keeps_cancellation_semantics(sdk_lab, message):
    attempt = sdk_lab.add(message, -1)
    sdk_lab.start(attempt)
    attempt.release.set()
    assert attempt.logged.wait(2)
    attempt.stop.set()
    assert isinstance(sdk_lab.terminal(attempt), push.TradeIntakeStartCancelled)


def test_auth_uses_failed_result_even_if_sdk_logger_is_disabled(sdk_lab, monkeypatch):
    monkeypatch.setattr(logging.getLogger("FTConsoleLog"), "disabled", True)
    attempt = sdk_lab.add("需要手机验证码", -1)
    sdk_lab.start(attempt)
    attempt.release.set()
    assert attempt.logged.wait(2)
    attempt.stop.set()
    result = sdk_lab.terminal(attempt)
    assert isinstance(result, push.TradeIntakeAuthRequired)
    assert result.detail == attempt.result[1]


def test_success_message_is_not_auth_failure(sdk_lab):
    attempt = sdk_lab.add("需要手机验证码", 0)
    sdk_lab.start(attempt)
    attempt.release.set(); attempt.ready.set()
    assert sdk_lab.terminal(attempt) == "ok"


def test_old_cancelled_attempt_does_not_contaminate_new_start(sdk_lab):
    old = sdk_lab.add("old 需要手机验证码", -1)
    sdk_lab.start(old)
    old.stop.set()
    assert isinstance(sdk_lab.terminal(old), push.TradeIntakeStartCancelled)
    new = sdk_lab.add()
    sdk_lab.start(new, listener=old.listener)
    old.release.set()
    assert old.logged.wait(2)
    assert not new.done.wait(1), "old attempt must not terminate new initialization"
    new.release.set(); new.ready.set()
    assert sdk_lab.terminal(new) == "ok"
    old.ready.set()


@pytest.mark.parametrize("observer", [False, True])
@pytest.mark.parametrize("init_ret", [0, -1])
def test_gateway_preserves_result_and_retires_observer_after_initialization(monkeypatch, observer, init_ret):
    messages, contexts, returned = [], [], []
    result = (init_ret, "需要手机验证码")

    class Context:
        def __init__(self, **_kwargs):
            contexts.append(self)
            self.closed = 0
            self.pending = False
            assert self._init_connect_sync() == 0
        def _wait_reconnect(self, delay):
            assert delay == 0
            self.pending = True
        def _init_connect_sync(self):
            value = self._send_init_connect_sync()
            returned.append(value)
            return value[0]
        def _send_init_connect_sync(self):
            return result
        def get_sync_conn_id(self):
            if self.pending:
                self.pending = False
                self._init_connect_sync()
            return 1
        def set_sync_query_connect_timeout(self, value):
            assert value == 8
        def close(self):
            self.closed += 1

    monkeypatch.setitem(sys.modules, "futu", SimpleNamespace(RET_OK=0, OpenSecTradeContext=Context))
    monkeypatch.setattr(gateway, "port_open", lambda *_: True)
    handlers = list(logging.getLogger("FTConsoleLog").handlers)
    kwargs = {"on_init_connect_error": lambda message: messages.append(message) or object()} if observer else {}
    context = gateway.build_futu_trade_context(host="synthetic.invalid", port=1, **kwargs)
    expected_messages = [result[1]] if observer and init_ret != 0 else []
    assert returned == [result] and returned[0] is result and messages == expected_messages
    result = (-1, "late 需要手机验证码")
    assert context._send_init_connect_sync() is result
    assert messages == expected_messages
    context.close()
    assert context.closed == 1 and logging.getLogger("FTConsoleLog").handlers == handlers


@pytest.mark.parametrize("where", ["sdk", "observer"])
def test_gateway_preserves_initialization_exception_identity_and_closes(monkeypatch, where):
    expected = RuntimeError("synthetic " + where + " failure")
    contexts = []

    class Context:
        def __init__(self, **_kwargs):
            self.closed = 0
            contexts.append(self)
            assert self._init_connect_sync() == 0
        def _wait_reconnect(self, delay):
            assert delay == 0
        def _init_connect_sync(self):
            return self._send_init_connect_sync()[0]
        def _send_init_connect_sync(self):
            if where == "sdk":
                raise expected
            return -1, "需要手机验证码"
        def get_sync_conn_id(self):
            self._init_connect_sync()
            return 0
        def set_sync_query_connect_timeout(self, value):
            assert value == 8
        def close(self):
            self.closed += 1

    def observer(_message):
        raise expected

    monkeypatch.setitem(sys.modules, "futu", SimpleNamespace(RET_OK=0, OpenSecTradeContext=Context))
    monkeypatch.setattr(gateway, "port_open", lambda *_: True)
    with pytest.raises(RuntimeError) as caught:
        gateway.build_futu_trade_context(host="synthetic.invalid", port=1, on_init_connect_error=observer)
    assert caught.value is expected and len(contexts) == 1 and contexts[0].closed == 1


def test_actual_coordinator_preserves_auth_source_and_sibling_cancel(sdk_lab, monkeypatch, tmp_path):
    from src.application.trades import auto_intake

    a = sdk_lab.add("A 需要手机验证码", -1)
    b = sdk_lab.add()
    statuses, history_closed, result = {}, [], []

    class History:
        def __init__(self, **_kwargs):
            pass
        def close(self):
            history_closed.append(self)

    def capture(path, payload):
        path = Path(path)
        assert path.parent in {tmp_path / "a", tmp_path / "b"} and path.name == "status.json"
        statuses.setdefault(path.parent.name, []).append(deepcopy(payload))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unexpected real provider")

    monkeypatch.setattr(auto_intake, "OpenDHistoryDealClient", History)
    monkeypatch.setattr(auto_intake, "atomic_write_json", capture)
    monkeypatch.setattr(auto_intake, "build_futu_gateway", forbidden)
    sources = [{"id": name, "account": name, "host": name + ".invalid", "port": 1,
                "state_path": tmp_path / name / "state.json", "audit_path": tmp_path / name / "audit.jsonl",
                "status_path": tmp_path / name / "status.json", "account_mapping": {}, "futu_account_ids": [],
                "backfill": {"enabled": False}, "settlement_observation": {"enabled": False}}
               for name in ("a", "b")]

    def run_source(source, stop):
        # Deterministic constructor assignment without serializing active initialization.
        if source["id"] == "b":
            assert a.entered.wait(2)
        return auto_intake._run_listener_source_loop(
            source=source, repo=object(), cfg={}, cfg_path=tmp_path / "config.json",
            runtime_root=tmp_path, runtime_root_source="synthetic test", intake_cfg={"mode": "dry-run", "enabled": True,
                "account_mapping": {}, "backfill": {"enabled": False}}, apply_changes=False,
            receipt_callback=lambda _row: {}, process_lock=threading.RLock(), stop_event=stop,
        )

    thread = sdk_lab.spawn(lambda: result.append(auto_intake._coordinate_listener_sources(
        sources, run_source=run_source, shutdown_timeout_sec=3,
    )))
    assert a.entered.wait(2) and b.entered.wait(2)
    a.release.set(); b.release.set()
    thread.join(3)
    assert not thread.is_alive() and not sdk_lab.errors
    sdk_lab.status_evidence = {"statuses": statuses, "exit_codes": result, "history_closed": len(history_closed)}
    assert result == [78] and len(history_closed) == 2
    assert statuses["a"][-1]["status"] == "blocked" and statuses["a"][-1]["error_code"] == "OPEND_NEEDS_PHONE_VERIFY"
    assert statuses["b"][-1]["status"] == "stopped" and statuses["b"][-1]["stage"] == "start_cancelled"
    assert not any(row.get("error_code") == "OPEND_NEEDS_PHONE_VERIFY" or row.get("stage") == "auth_required" for row in statuses["b"])
    assert not (tmp_path / "a/status.json").exists() and not (tmp_path / "b/status.json").exists()


@pytest.mark.parametrize("failure", ["constructor_fallback", "readiness"])
def test_failed_builder_retires_observer_and_keeps_fallback_kwargs(monkeypatch, failure):
    contexts, calls, messages = [], [], []
    expected = RuntimeError("synthetic readiness error")

    class Context:
        def __init__(self, **kwargs):
            contexts.append(self)
            calls.append(kwargs)
            self.closed = 0
            if failure == "constructor_fallback" and len(contexts) == 1:
                raise RuntimeError("first constructor failed")
        def _send_init_connect_sync(self):
            return -1, "old 需要手机验证码"
        def get_sync_conn_id(self):
            if failure == "readiness":
                raise expected
            return 1
        def set_sync_query_connect_timeout(self, _timeout):
            pass
        def get_acc_list(self):
            return 0, []
        def close(self):
            self.closed += 1

    monkeypatch.setitem(sys.modules, "futu", SimpleNamespace(
        RET_OK=0, OpenSecTradeContext=Context, TradeDealHandlerBase=type("DealBase", (), {}),
    ))
    monkeypatch.setattr(gateway, "port_open", lambda *_: True)
    if failure == "constructor_fallback":
        listener = push.OpenDTradePushListener(host="synthetic.invalid", port=1, on_deal=lambda _row: None)
        context, _handler = listener._build_default_context(on_init_connect_error=messages.append)
        assert calls == [{"host": "synthetic.invalid", "port": 1},
                         {"host": "synthetic.invalid", "port": 1, "is_encrypt": False}]
        assert contexts[0]._send_init_connect_sync() == (-1, "old 需要手机验证码")
        assert messages == []
        context.close()
        assert contexts[0].closed == 0 and contexts[1].closed == 1
    else:
        with pytest.raises(RuntimeError) as caught:
            gateway.build_futu_trade_context(host="synthetic.invalid", port=1, on_init_connect_error=messages.append)
        assert caught.value is expected and contexts[0].closed == 1
        assert contexts[0]._send_init_connect_sync() == (-1, "old 需要手机验证码")
        assert messages == []
