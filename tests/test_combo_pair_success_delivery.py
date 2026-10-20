from copy import deepcopy
from datetime import datetime, timezone
from functools import partial
import socket

import pytest

from src.application.futu_portfolio_context import build_futu_position_snapshot
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades import attribution, auto_intake, receipt
from src.application.trades.lifecycle_outbox import dispatch_notification_batch_once, QUIET_WINDOW_MS
from test_combo_pair_success_receipt import _case, _confirm, _ready, _execution_payload
from test_combo_reconciliation_application import BASE_TIME_MS, _put_open, _call_open
from test_combo_reconciliation_domain import _exposure


@pytest.fixture(autouse=True)
def no_io(monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("independent probes cannot access network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(receipt, "notification_reminder_route_key", lambda **kw: "synthetic-sender-sy")


@pytest.mark.parametrize("put_first", [True, False])
@pytest.mark.parametrize("market", ["us", "hk"])
def test_sy_live_intake_end_to_end(tmp_path, monkeypatch, put_first, market):
    now = BASE_TIME_MS + 3000
    monkeypatch.setattr(attribution.time, "time", lambda: now / 1000)
    config = {
        "accounts": ["sy"],
        "market": market,
        "account_settings": {"sy": {"futu": {"account_id": "2002", "trd_env": "REAL"}}},
        "trade_intake": {
            "combo_reconciliation": {"accounts": {"sy": "auto"}},
            "receipt": {"enabled": True},
        },
        "notifications": {"enabled": True, "provider": "wechat_clawbot", "target": "wechat:offline-sy"},
    }
    expo = _exposure(delivery_confirmed=True)
    expo["account"] = "sy"
    evidence = {"complete": True, "exposures": [expo]}
    snapshot = build_futu_position_snapshot(
        rows=[
            {
                "code": "US.NVDA260821P100000",
                "stock_owner": "US.NVDA",
                "sec_type": "OPTION",
                "qty": -1,
                "option_type": "PUT",
                "option_strike_price": 100,
                "strike_time": "2026-08-21",
                "multiplier": 100,
            },
            {
                "code": "US.NVDA260821C110000",
                "stock_owner": "US.NVDA",
                "sec_type": "OPTION",
                "qty": 1,
                "option_type": "CALL",
                "option_strike_price": 110,
                "strike_time": "2026-08-21",
                "multiplier": 100,
            },
        ],
        broker_account_ref={
            "broker_id": "futu",
            "external_account_id": "2002",
            "environment": "REAL",
            "account_label": "sy",
            "broker_account_id": "futu:REAL:2002",
        },
        markets=["US", "HK"],
        asset_types=["stock", "option"],
        completeness="complete",
        observed_at_utc=datetime.fromtimestamp(now / 1000, timezone.utc).isoformat(),
    )
    observation = {
        "portfolio": {
            "capacity_authority": {
                "status": "available",
                "logical_account": "sy",
                "futu_account_id": "2002",
                "trd_env": "REAL",
                "market": "us",
            },
            "position_snapshot_input": snapshot,
            "cash_by_currency": {"USD": 0},
            "exchange_rate_status": "unavailable",
        }
    }
    if market == "hk":
        expo.update(market="HK", currency="HKD", multiplier=200)
        for leg in ("put_contract_key", "call_contract_key"):
            expo[leg]["underlying_symbol"] = "9992.HK"
        hkrows = [
            {
                "code": f"HK.9992260821{kind}{strike}000",
                "stock_owner": "HK.9992",
                "sec_type": "OPTION",
                "qty": qty,
                "option_type": option,
                "option_strike_price": strike,
                "strike_time": "2026-08-21",
                "multiplier": 200,
            }
            for kind, strike, qty, option in [("P", 100, -1, "PUT"), ("C", 110, 1, "CALL")]
        ]
        observation["portfolio"]["position_snapshot_input"] = build_futu_position_snapshot(
            rows=hkrows,
            broker_account_ref={
                "broker_id": "futu",
                "external_account_id": "2002",
                "environment": "REAL",
                "account_label": "sy",
                "broker_account_id": "futu:REAL:2002",
            },
            markets=["US", "HK"],
            asset_types=["stock", "option"],
            completeness="complete",
            observed_at_utc=datetime.fromtimestamp(now / 1000, timezone.utc).isoformat(),
        )
        observation["portfolio"]["capacity_authority"]["market"] = "hk"
    monkeypatch.setattr(attribution, "read_attribution_combo_evidence", lambda *a, **kw: evidence)
    monkeypatch.setattr(
        "src.application.wheel.capacity.observe_trade_attribution_capacity", lambda **kw: deepcopy(observation)
    )
    repo = SQLiteOptionPositionsRepository(tmp_path / "sy-ledger.sqlite3")
    with repo._writer_connection(begin_immediate=True) as c:
        c.execute(
            "INSERT INTO trade_attribution_policy_enablings (broker,physical_account_id,environment,account,market,policy_version,effective_from_ms,created_at_ms,actor,request_id,request_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "futu",
                "2002",
                "REAL",
                "sy",
                market,
                "trade_attribution.v2",
                BASE_TIME_MS - 1000,
                BASE_TIME_MS - 1000,
                "fixture",
                "fixture",
                "b" * 64,
            ),
        )
    original = []

    def original_send(**kw):
        original.append(kw)
        return {"ok": True, "command_ok": True, "delivery_confirmed": True, "message_id": "sy-offline-original"}

    monkeypatch.setattr(
        auto_intake,
        "send_trade_intake_receipt",
        partial(receipt.send_trade_intake_receipt, send_fn=original_send, normalize_fn=lambda **kw: kw),
    )
    callback = auto_intake._build_receipt_callback(
        base=tmp_path, cfg=config, receipt_config={"enabled": True}, repo=repo
    )
    payloads = [_execution_payload(_put_open()), _execution_payload(_call_open())]
    for p in payloads:
        p["broker_account_ref"].update(
            account_label="sy", external_account_id="2002", broker_account_id="futu:REAL:2002"
        )
        if market == "hk":
            p["instrument_ref"].update(market="HK", symbol="9992.HK", currency="HKD", multiplier="200")
            p["currency"] = "HKD"
    if not put_first:
        payloads.reverse()
    for p in payloads:
        result = auto_intake._process_payload(
            p,
            repo=repo,
            state_path=tmp_path / "state.json",
            audit_path=tmp_path / "audit.jsonl",
            account_mapping={"2002": "sy"},
            futu_account_ids=["2002"],
            apply_changes=True,
            host="127.0.0.1",
            port=11112,
            allow_external_lookup=False,
            config=config,
            runtime_root=tmp_path,
            on_result_fn=callback,
        )
        assert result["status"] == "applied"
    assert len(original) == 2 and all("配对成功" not in s["message"] for s in original)
    result = attribution.reconcile_trade_attribution_account(
        repo,
        config=config,
        account="sy",
        market=market,
        runtime_root=tmp_path,
        inbox_path=tmp_path / "sy-ledger.sqlite3.trade_intake_inbox.sqlite3",
        combo_mode="auto",
    )
    assert result["errors"] == [] and result["linked"] == 1, result
    (row,) = repo.list_trade_lifecycle_notifications()
    assert row["payload"]["account"] == "sy" and row["status"] == "pending"
    route = receipt.resolve_trade_lifecycle_notification_batch_route(config=config, base=tmp_path)
    delivered = []

    def delivery(payload):
        def send(**kw):
            delivered.append(kw)
            return {
                "command_ok": True,
                "delivery_confirmed": True,
                "message_id": "sy-offline-paired",
                "attribution_route_key": route["attribution_route_key"],
            }

        return receipt.send_trade_lifecycle_outbox_payload(
            base=tmp_path,
            config=config,
            receipt_config={"enabled": True},
            payload=payload,
            send_fn=send,
            normalize_fn=lambda **kw: kw,
        )

    outcome = dispatch_notification_batch_once(
        SQLiteOptionPositionsRepository(repo.db_path),
        route=route,
        now_ms=row["created_at_ms"] + QUIET_WINDOW_MS,
        allowed_accounts=("sy",),
        send_fn=delivery,
    )
    assert outcome["status"] == "confirmed" and len(delivered) == 1, outcome
    assert "sy" in delivered[0]["message"] and "配对成功" in delivered[0]["message"]
    assert "lx" not in delivered[0]["message"]


def test_concurrent_dispatch_connections_send_once(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock

    case = _case(tmp_path, monkeypatch)
    _confirm(case)
    barrier = Barrier(2)
    lock = Lock()
    calls = []

    def run(_):
        repo = SQLiteOptionPositionsRepository(case[0].db_path)
        barrier.wait()

        def send(payload):
            with lock:
                calls.append(payload)
            return {"status": "confirmed", "delivery_confirmed": True}

        return dispatch_notification_batch_once(
            repo, route=case[-1], now_ms=_ready(case[0]), allowed_accounts=("lx",), send_fn=send
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sorted(r["status"] for r in results) == ["confirmed", "idle"]
    assert len(calls) == 1


def test_feishu_offline_adapter_success_has_stable_identity_and_actual_route(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from src.application import notification_delivery_adapter as adapter
    from domain.domain.multi_tick import notification_route_identity_hash

    case = _case(tmp_path, monkeypatch)
    config = case[1]
    config["notifications"] = {"enabled": True, "provider": "feishu_app"}
    fake = SimpleNamespace(
        app_id="offline-app",
        app_secret="offline-fake-no-credential",
        user_open_id="offline-recipient",
        credential_missing_fields=[],
    )
    monkeypatch.setattr(adapter, "resolve_feishu_bot_config", lambda *a, **kw: fake)
    routekey = notification_route_identity_hash(
        provider="feishu_app", channel="feishu_app", sender=fake.app_id, recipient=fake.user_open_id
    )
    monkeypatch.setattr(receipt, "notification_reminder_route_key", lambda **kw: routekey)
    route = receipt.resolve_trade_lifecycle_notification_batch_route(config=config, base=tmp_path)
    _confirm(case, confirmation_receipt_route=route)
    http = []

    def post(**kw):
        http.append(kw)
        return {"code": 0, "data": {"message_id": "offline-feishu-combo"}}

    monkeypatch.setattr(adapter, "send_post_message", post)

    def deliver(payload):
        return receipt.send_trade_lifecycle_outbox_payload(
            base=tmp_path, config=config, receipt_config={"enabled": True}, payload=payload
        )

    outcome = dispatch_notification_batch_once(
        case[0], route=route, now_ms=_ready(case[0]), allowed_accounts=("lx",), send_fn=deliver
    )
    assert outcome["status"] == "confirmed" and len(http) == 1, outcome
    (batch,) = case[0].list_trade_lifecycle_notification_batches()
    assert http[0]["uuid"] == batch["batch_id"]
    assert "配对成功" in http[0]["markdown"]
