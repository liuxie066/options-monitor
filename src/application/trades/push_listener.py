from __future__ import annotations

import importlib
import sys
from typing import Any, Callable

from src.infrastructure.opend_watchdog import classify_watchdog_result


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

    def start(self) -> None:
        self._ctx, self._handler = self._build_default_context()
        self._ctx.set_handler(self._handler)
        self._ctx.start()

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
