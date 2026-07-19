from __future__ import annotations

import importlib
import logging
import queue
import sys
import threading
from typing import Any, Callable

from src.infrastructure.opend_watchdog import classify_watchdog_result


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
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.on_deal = on_deal
        self._ctx: Any = None
        self._handler: Any = None

    def _build_default_context(self) -> tuple[Any, Any]:
        try:
            futu_mod = importlib.import_module("futu")
        except Exception as exc:
            raise RuntimeError("futu SDK not importable; install futu-api in runtime env") from exc
        OpenSecTradeContext: Any = getattr(futu_mod, "OpenSecTradeContext")
        TradeDealHandlerBase: Any = getattr(futu_mod, "TradeDealHandlerBase")

        class DealHandler(TradeDealHandlerBase):
            def __init__(self, callback: Callable[[dict[str, Any]], None]) -> None:
                super().__init__()
                self._callback = callback

            def on_recv_rsp(self, rsp_pb: Any) -> tuple[int, Any]:
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret == 0 and data is not None:
                    rows = data.to_dict("records") if hasattr(data, "to_dict") else []
                    if isinstance(rows, list):
                        for row in rows:
                            if isinstance(row, dict):
                                try:
                                    self._callback(row)
                                except Exception as exc:
                                    print(
                                        f"[WARN] trade push callback failed: {type(exc).__name__}: {exc}",
                                        file=sys.stderr,
                                        flush=True,
                                    )
                return ret, data

        ctx = None
        last_error: Exception | None = None
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
        return ctx, DealHandler(self.on_deal)

    def start(self, *, cancel_event: threading.Event | None = None) -> None:
        results: queue.Queue[tuple[str, Any, Any]] = queue.Queue(maxsize=1)
        auth_required = threading.Event()
        auth_evidence: dict[str, str] = {}

        class _TradeContextInitLogHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                detail = record.getMessage()
                if "init connect fail" not in detail or "OpenSecTradeContext" not in detail:
                    return
                error_code, message = classify_watchdog_result(None, detail)
                if error_code == "OPEND_NEEDS_PHONE_VERIFY":
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
                    continue
                if result == "error":
                    raise value
                self._ctx, self._handler = value, handler
                self._ctx.set_handler(self._handler)
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
        if error_code == "OPEND_NEEDS_PHONE_VERIFY":
            raise TradeIntakeAuthRequired(error_code=error_code, message=message, detail=detail)
        raise RuntimeError(f"{error_code} {message}: {detail}")

    def close(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            finally:
                self._ctx = None
                self._handler = None
