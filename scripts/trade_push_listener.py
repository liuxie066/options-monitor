from __future__ import annotations

from typing import Any, Callable


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
            from futu import OpenSecTradeContext, TradeDealHandlerBase
        except Exception as exc:
            raise RuntimeError("futu SDK not importable; install futu-api in runtime env") from exc

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
                                self._callback(row)
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

    def close(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            finally:
                self._ctx = None
                self._handler = None
