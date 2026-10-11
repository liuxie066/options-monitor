"""Offline Futu replay; no OpenD connections, notifications or runtime writes.

Run this same script with --source-root pointing at the baseline and candidate
checkouts. Timings include SDK decoding and durable inbox admission, but exclude
network latency, downstream ledger application and notification delivery.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from unittest.mock import patch


def measure(call, *, iterations: int = 100) -> dict:
    call()
    samples = []
    for _ in range(11):
        start = time.perf_counter()
        for _ in range(iterations):
            call()
        samples.append((time.perf_counter() - start) * 1000 / iterations)
    return {"median_ms": statistics.median(samples), "max_sample_ms": max(samples),
            "samples_ms": samples, "iterations_per_sample": iterations}


def run(root: Path) -> dict:
    sys.path.insert(0, str(root))
    import pandas as pd
    from src.application import opend_market_snapshot_fetching as snapshots
    from src.application.opend_fetch_config import OpenDEndpointRateLimit
    from src.application.ledger.api import open_position_ledger
    from src.application.trades.auto_intake import _bind_push_payload_to_source
    from src.application.trades.deal_identity import broker_deal_key_from_payload
    from src.application.trades.inbox import enqueue_trade_payload, TRADE_INTAKE_ADAPTER_VERSIONS
    from src.application.trades.inbox_authority import resolve_execution_inbox_path
    from src.infrastructure import futu_trade_push

    result = {"source_root": str(root), "python": platform.python_version(),
              "pandas": pd.__version__, "machine": platform.machine(), "scenarios": {}}
    for size in (200, 400):
        codes = [f"US.NVDA261016P{100000 + i:06d}" for i in range(size)]
        frame = pd.DataFrame({key: codes if key == "code" else [1.25] * size
                              for key in snapshots.SNAPSHOT_KEEP_COLUMNS[:27]})
        class Quote:
            def get_snapshot(self, requested):
                assert requested == codes
                return frame

        def query():
            value = snapshots.fetch_option_snapshots(
                option_codes=codes, gateway=Quote(), snapshot_limit=OpenDEndpointRateLimit(30, 60, 30),
                base_dir=root, snapshot_batch_size=size, snapshot_fallback_max_codes=0,
                no_retry=True, rate_limited_call=lambda **kw: kw["call"]())
            assert value.complete and not value.errors and value.opend_call_count == 1
            assert set(value.snap_map) == set(codes)
            return value.snap_map

        digest = hashlib.sha256(json.dumps(query(), sort_keys=True).encode()).hexdigest()
        result["scenarios"][f"snapshot_{size}"] = {**measure(query), "output_digest": digest}

    with tempfile.TemporaryDirectory(prefix="om-futu-benchmark-") as temporary:
        storage = Path(temporary)
        join = os.path.join
        with patch("os.path.join", side_effect=lambda *p: str(storage / "sdk-log")
                   if p[-1:] == (".com.futunn.FutuOpenD/Log",) else join(*p)):
            import futu
        from futu.common.pb import Trd_UpdateOrderFill_pb2, Trd_UpdateOrder_pb2
        result["futu"] = futu.__version__

        class Context:
            def __init__(self, **_kwargs):
                pass

            def set_sync_query_connect_timeout(self, _timeout):
                pass

            def get_acc_list(self):
                return 0, pd.DataFrame([{"acc_id": 123, "trd_env": "REAL"}])

            def close(self):
                pass

        response = Trd_UpdateOrderFill_pb2.Response()
        response.retType = 0
        for key, value in {"accID": 123, "trdEnv": 1, "trdMarket": 2}.items():
            setattr(response.s2c.header, key, value)
        for key, value in {"fillID": 321, "fillIDEx": "321", "orderIDEx": "order-1", "trdSide": 1,
                           "code": "NVDA", "name": "NVIDIA", "qty": 1, "price": 100,
                           "createTime": "2026-10-09 10:00:00", "secMarket": 2}.items():
            setattr(response.s2c.orderFill, key, value)
        assert response.IsInitialized(), response.FindInitializationErrors()
        seen, hints = [], []
        listener = futu_trade_push.OpenDTradePushListener(
            host="offline", port=0, on_deal=seen.append, on_order_hint=hints.append)
        with patch.object(futu, "OpenSecTradeContext", Context), patch.object(futu_trade_push, "port_open", return_value=True):
            _context, handler = listener._build_default_context()
        def deal():
            ret, _frame = handler.on_recv_rsp(response)
            assert ret == 0
        deal()
        assert seen[-1]["broker_account_id"] == "futu:REAL:123"
        assert seen[-1]["acc_id"] == "123"
        digest = hashlib.sha256(json.dumps(seen[-1], sort_keys=True).encode()).hexdigest()
        result["scenarios"]["deal_sdk"] = {**measure(deal), "output_digest": digest}

        order = Trd_UpdateOrder_pb2.Response()
        order.retType = 0
        order.s2c.header.CopyFrom(response.s2c.header)
        for key, value in {"orderID": 1, "orderIDEx": "order-1", "trdSide": 1, "orderType": 1,
                           "orderStatus": 5, "code": "NVDA", "name": "NVIDIA", "qty": 1,
                           "price": 100, "createTime": "2026-10-09 10:00:00",
                           "updateTime": "2026-10-09 10:01:00", "trdMarket": 2}.items():
            setattr(order.s2c.order, key, value)
        assert order.IsInitialized(), order.FindInitializationErrors()
        def order_hint():
            ret, _frame = listener._order_handler.on_recv_rsp(order)
            assert ret == 0
        order_hint()
        assert hints[-1] == {"futu_account_id": "123", "environment": "REAL", "market": "US", "order_id": "order-1"}
        digest = hashlib.sha256(json.dumps(hints[-1], sort_keys=True).encode()).hexdigest()
        result["scenarios"]["order_sdk"] = {**measure(order_hint), "output_digest": digest}

        repo = open_position_ledger(storage / "data.json", runtime_root=storage)
        inbox = resolve_execution_inbox_path(repo, storage / "inbox.sqlite3")
        source = {"id": "offline", "account": "lx", "host": "offline", "port": 0,
                  "account_mapping": {"123": "lx"}, "futu_account_ids": ["123"]}
        def durable(row):
            payload = _bind_push_payload_to_source(row, source=source, received_at_utc="2026-10-09T14:00:00Z")
            key = broker_deal_key_from_payload(payload, account_mapping={"123": "lx"})
            assert key
            enqueue_trade_payload(inbox, payload=payload, source="push", broker_deal_key=key,
                                  repo=repo, adapter_version=TRADE_INTAKE_ADAPTER_VERSIONS["push"])
        listener.on_deal = durable
        result["scenarios"]["deal_durable_replay"] = measure(deal, iterations=20)
        with sqlite3.connect(f"{inbox.as_uri()}?mode=ro", uri=True) as conn:
            assert conn.execute("SELECT count(*) FROM trade_inbox").fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM trade_inbox_evidence").fetchone()[0] == 1
            rows = conn.execute("""SELECT payload_json, economic_payload_hash, status,
                result_reason, receipt_recovery_allowed FROM trade_inbox ORDER BY inbox_id""").fetchall()
            result["scenarios"]["deal_durable_replay"]["output_digest"] = hashlib.sha256(
                json.dumps(rows).encode()).hexdigest()
        next_id = 321
        def new_deal():
            nonlocal next_id
            next_id += 1
            response.s2c.orderFill.fillID = next_id
            response.s2c.orderFill.fillIDEx = str(next_id)
            deal()
        result["scenarios"]["deal_durable_new"] = measure(new_deal, iterations=20)
        with sqlite3.connect(f"{inbox.as_uri()}?mode=ro", uri=True) as conn:
            assert conn.execute("SELECT count(*) FROM trade_inbox").fetchone()[0] == 222
            assert conn.execute("SELECT count(*) FROM trade_inbox_evidence").fetchone()[0] == 222
            rows = conn.execute("""SELECT payload_json, economic_payload_hash, status,
                result_reason, receipt_recovery_allowed FROM trade_inbox ORDER BY inbox_id""").fetchall()
            result["scenarios"]["deal_durable_new"]["output_digest"] = hashlib.sha256(
                json.dumps(rows).encode()).hexdigest()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    data = run(args.source_root.resolve())
    text = json.dumps(data, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
