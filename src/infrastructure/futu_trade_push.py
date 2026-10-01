"""Futu trade-push transport."""

from __future__ import annotations

import importlib
import logging
import queue
import sys
import threading
from typing import Any, Callable
from domain.domain.trade_account_identity import extract_visible_account_fields

from src.infrastructure.futu_gateway import FutuGatewayUnreachableError
from src.infrastructure.opend_watchdog import classify_watchdog_result, port_open


class TradeIntakeStartCancelled(RuntimeError):
    pass


class TradeIntakeAuthRequired(RuntimeError):
    def __init__(self, *, error_code: str, message: str, detail: str = "") -> None:
        self.error_code = str(error_code)
        self.message = str(message)
        self.detail = str(detail)
        suffix = f": {self.detail}" if self.detail else ""
        super().__init__(f"{self.error_code} {self.message}{suffix}")


class OpenDTradePushListener:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        on_deal: Callable[[dict[str, Any]], None],
        on_order_hint: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.on_deal = on_deal
        self.on_order_hint = on_order_hint
        self._ctx: Any = None
        self._handler: Any = None
        self._order_handler: Any = None

    def _build_default_context(self) -> tuple[Any, Any]:
        try:
            futu_mod = importlib.import_module("futu")
        except Exception as exc:
            raise RuntimeError("futu SDK not importable; install futu-api in runtime env") from exc
        OpenSecTradeContext: Any = getattr(futu_mod, "OpenSecTradeContext")
        TradeDealHandlerBase: Any = getattr(futu_mod, "TradeDealHandlerBase")
        TradeOrderHandlerBase: Any = (
            getattr(futu_mod, "TradeOrderHandlerBase") if self.on_order_hint is not None else None
        )

        class DealHandler(TradeDealHandlerBase):
            def __init__(self, callback: Callable[[dict[str, Any], dict[str, Any] | None], None]) -> None:
                super().__init__()
                self._callback = callback

            def on_recv_rsp(self, rsp_pb: Any) -> tuple[int, Any]:
                # The SDK DataFrame drops accID; retain protobuf presence before conversion.
                header = getattr(getattr(rsp_pb, "s2c", None), "header", None)
                header_fields = {
                    key: getattr(header, key)
                    for key in ("accID", "trdEnv")
                    if header is not None and header.HasField(key)
                }
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret == 0 and data is not None:
                    rows = data.to_dict("records") if hasattr(data, "to_dict") else []
                    if isinstance(rows, list):
                        raw_fill = getattr(getattr(rsp_pb, "s2c", None), "orderFill", None)
                        revision = (
                            raw_fill.updateTimestamp
                            if raw_fill is not None and raw_fill.HasField("updateTimestamp")
                            else None
                        )
                        for row in rows:
                            if isinstance(row, dict):
                                row = dict(row)
                                if revision is not None:
                                    row["update_timestamp"] = revision
                                try:
                                    self._callback(row, header_fields if header is not None else None)
                                except Exception as exc:
                                    print(
                                        f"[WARN] trade push callback failed: {type(exc).__name__}: {exc}",
                                        file=sys.stderr,
                                        flush=True,
                                    )
                return ret, data

        ctx = None
        last_error: Exception | None = None
        if not port_open(self.host, self.port):
            raise FutuGatewayUnreachableError(
                f"OpenD unreachable: {self.host}:{self.port}; start FutuOpenD before enabling the trade push listener"
            )
        for kwargs in (
            {"host": self.host, "port": self.port},
            {"host": self.host, "port": self.port, "is_encrypt": False},
        ):
            try:
                ctx = OpenSecTradeContext(**kwargs)
                break
            except Exception as exc:
                last_error = exc
        if ctx is None:
            raise RuntimeError(f"failed to initialize OpenSecTradeContext: {last_error}")
        ctx.set_sync_query_connect_timeout(2)
        environments: dict[str, str] = {}
        ambiguous_accounts: set[str] = set()
        try:
            ret, accounts = ctx.get_acc_list()
            if ret == 0 and hasattr(accounts, "to_dict"):
                for account in accounts.to_dict("records"):
                    physical = str(account.get("acc_id") or "").strip()
                    environment = str(account.get("trd_env") or "").rsplit(".", 1)[-1].upper()
                    if physical and environment in {"REAL", "SIMULATE"}:
                        if physical in environments and environments[physical] != environment:
                            ambiguous_accounts.add(physical)
                        environments[physical] = environment
        except Exception:
            # Missing account evidence remains unbound; never infer REAL from an account ID.
            pass

        def receive(row: dict[str, Any], header: dict[str, Any] | None) -> None:
            payload = dict(row)
            errors: list[str] = []
            visible = extract_visible_account_fields(row)
            physical_ids = {value for key, value in visible.items() if key != "account"}
            if any(not value.isascii() or not value.isdecimal() or int(value) <= 0 for value in physical_ids):
                errors.append("invalid:push_physical_account")
            if header is not None:
                payload["_trade_intake_push_header"] = header
                header_id = header.get("accID")
                header_env = header.get("trdEnv")
                if header_id is None:
                    errors.append("missing:push_header_physical_account")
                elif type(header_id) is not int or header_id <= 0:
                    errors.append("invalid:push_header_physical_account")
                else:
                    physical_ids.add(str(header_id))
                    if not visible.get("acc_id"):
                        payload["acc_id"] = str(header_id)
                if header_env is None:
                    errors.append("missing:push_header_environment")
                else:
                    environment_name = (
                        futu_mod.TrdEnv.to_string2(header_env) if type(header_env) is int else ""
                    )
                    if environment_name not in {"REAL", "SIMULATE"}:
                        errors.append("invalid:push_header_environment")
                    else:
                        for key in ("environment", "trd_env"):
                            supplied = str(row.get(key) or "").strip().rsplit(".", 1)[-1].upper()
                            if supplied and supplied != environment_name:
                                errors.append(f"conflict:push_{key}")
                        if not row.get("trd_env"):
                            payload["trd_env"] = environment_name
            physical = next(iter(physical_ids)) if len(physical_ids) == 1 else ""
            environment = environments.get(physical)
            if not physical_ids:
                errors.append("missing:push_physical_account")
            elif len(physical_ids) != 1:
                errors.append("conflict:push_physical_account")
            if physical in ambiguous_accounts:
                errors.append("conflict:source_account_environment")
            elif physical and not environment:
                errors.append("missing:source_account_environment")
            expected = {
                "broker_account_id": f"futu:{environment}:{physical}",
                "external_id_namespace": "futu.deal",
                "execution_id_namespace": "futu.deal",
                "external_order_namespace": "futu.order",
                "order_id_namespace": "futu.order",
            }
            for key, value in expected.items():
                supplied = str(row.get(key) or "").strip()
                if supplied and supplied != value:
                    errors.append(f"conflict:push_{key}")
            for key in ("environment", "trd_env"):
                supplied = str(payload.get(key) or "").strip().rsplit(".", 1)[-1].upper()
                if environment and supplied and supplied != environment:
                    errors.append(f"conflict:push_{key}")
            if errors:
                payload["_trade_intake_source_identity_errors"] = sorted(set(errors))
                payload["_trade_intake_source_account_evidence"] = {
                    "source": "get_acc_list", "host": self.host, "port": self.port,
                    "visible_account_fields": visible,
                    "physical_account_id": physical or None,
                    "environment": None if physical in ambiguous_accounts else environment,
                }
            else:
                payload.update(environment=environment,
                               broker_account_id=expected["broker_account_id"],
                               external_id_namespace="futu.deal")
                if any(row.get(key) not in (None, "") for key in ("order_id", "orderID", "orderId")):
                    payload["external_order_namespace"] = "futu.order"
            self.on_deal(payload)
        if TradeOrderHandlerBase is not None:
            class OrderHandler(TradeOrderHandlerBase):
                def on_recv_rsp(self, rsp_pb: Any) -> tuple[int, Any]:
                    header = getattr(getattr(rsp_pb, "s2c", None), "header", None)
                    ret, data = super().on_recv_rsp(rsp_pb)
                    if ret != 0 or header is None or data is None:
                        return ret, data
                    if not all(header.HasField(key) for key in ("accID", "trdEnv", "trdMarket")):
                        return ret, data
                    physical = header.accID
                    if type(physical) is not int or physical <= 0:
                        return ret, data
                    account_id = str(physical)
                    if account_id in ambiguous_accounts or environments.get(account_id) != "REAL":
                        return ret, data
                    environment = futu_mod.TrdEnv.to_string2(header.trdEnv)
                    market = futu_mod.TrdMarket.to_string2(header.trdMarket)
                    if environment != "REAL" or market in {"NONE", "N/A", ""}:
                        return ret, data
                    rows = data.to_dict("records") if hasattr(data, "to_dict") else []
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        order_id = str(row.get("order_id") or "").strip()
                        row_market = str(row.get("trd_market") or "").rsplit(".", 1)[-1].upper()
                        row_env = str(row.get("trd_env") or "").rsplit(".", 1)[-1].upper()
                        row_accounts = {
                            value for key, value in extract_visible_account_fields(row).items()
                            if key != "account"
                        }
                        if not order_id or (row_market not in {"", "N/A"} and row_market != market) or (
                            row_env not in {"", "N/A"} and row_env != environment
                        ) or (row_accounts and row_accounts != {account_id}):
                            continue
                        try:
                            self_outer.on_order_hint({
                                "futu_account_id": account_id,
                                "environment": environment,
                                "market": market,
                                "order_id": order_id,
                            })
                        except Exception as exc:
                            print(
                                f"[WARN] order push callback failed: {type(exc).__name__}: {exc}",
                                file=sys.stderr, flush=True,
                            )
                    return ret, data

            self_outer = self
            self._order_handler = OrderHandler()
        return ctx, DealHandler(receive)

    def start(
        self, *, cancel_event: threading.Event | None = None,
        on_wait: Callable[[], None] | None = None,
    ) -> None:
        results: queue.Queue[tuple[str, Any, Any]] = queue.Queue(maxsize=1)
        auth_required = threading.Event()
        auth_evidence: dict[str, str] = {}

        class _TradeContextInitLogHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                detail = record.getMessage()
                if "init connect fail" not in detail or "OpenSecTradeContext" not in detail:
                    return
                error_code, message = classify_watchdog_result(None, detail)
                if error_code in {"OPEND_NEEDS_PHONE_VERIFY", "OPEND_NEEDS_PIC_VERIFY", "OPEND_LOGIN_INVALID"}:
                    auth_evidence.update(error_code=error_code, message=message, detail=detail)
                    auth_required.set()

        def _construct() -> None:
            try:
                ctx, handler = self._build_default_context()
            except Exception as exc:
                results.put(("error", exc, None))
            else:
                results.put(("ok", ctx, handler))

        sdk_logger = logging.getLogger("FTConsoleLog")
        log_handler = _TradeContextInitLogHandler()
        sdk_logger.addHandler(log_handler)
        worker = threading.Thread(
            target=_construct,
            name=f"trade-context-init-{self.host}-{self.port}",
            daemon=True,
        )
        try:
            worker.start()
            while True:
                if auth_required.is_set():
                    raise TradeIntakeAuthRequired(
                        error_code=auth_evidence["error_code"],
                        message=auth_evidence["message"],
                        detail=auth_evidence["detail"],
                    )
                if cancel_event is not None and cancel_event.is_set():
                    raise TradeIntakeStartCancelled("trade context initialization cancelled")
                try:
                    result, value, handler = results.get(timeout=0.1)
                except queue.Empty:
                    if on_wait is not None and not (cancel_event is not None and cancel_event.is_set()):
                        on_wait()
                    continue
                if result == "error":
                    raise value
                self._ctx, self._handler = value, handler
                if self._ctx.set_handler(self._handler) not in (None, 0):
                    raise RuntimeError("trade deal push handler registration failed")
                if self._order_handler is not None:
                    if self._ctx.set_handler(self._order_handler) not in (None, 0):
                        raise RuntimeError("trade order push handler registration failed")
                self._ctx.start()
                return
        finally:
            sdk_logger.removeHandler(log_handler)

    def check_health(self) -> None:
        if self._ctx is None:
            raise RuntimeError("trade context is not started")
        try:
            ret, data = self._ctx.get_global_state()
        except Exception as exc:
            detail = f"get_global_state failed: {type(exc).__name__}: {exc}"
            error_code, message = classify_watchdog_result(None, detail)
        else:
            if ret == 0 and isinstance(data, dict):
                ready = data.get("program_status_type") in (None, "", "READY")
                trade_logined = bool(data.get("trd_logined", True))
                if ready and trade_logined:
                    return
                detail = f"OpenD trade context not ready: {data}"
                error_code, message = classify_watchdog_result(data, detail)
            else:
                detail = f"get_global_state ret={ret} data={data}"
                error_code, message = classify_watchdog_result(None, detail)
        if error_code in {"OPEND_NEEDS_PHONE_VERIFY", "OPEND_NEEDS_PIC_VERIFY", "OPEND_LOGIN_INVALID"}:
            raise TradeIntakeAuthRequired(error_code=error_code, message=message, detail=detail)
        raise RuntimeError(f"{error_code} {message}: {detail}")

    def close(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            finally:
                self._ctx = None
                self._handler = None
                self._order_handler = None
