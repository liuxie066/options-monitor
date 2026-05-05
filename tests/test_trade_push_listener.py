from __future__ import annotations

import sys
from types import SimpleNamespace

from scripts.trade_push_listener import OpenDTradePushListener


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
