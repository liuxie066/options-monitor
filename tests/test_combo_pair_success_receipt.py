from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from functools import partial
import socket
from threading import Barrier

import pytest

from src.application.ledger.api import read_trade_attribution_snapshot
from src.application.ledger import trade_attribution as ledger_attribution
from src.application.ledger.combo_reconciliation import supersede_post_trade_combo_pair
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades import attribution, auto_intake, inbox, receipt
from src.application.trades.deal_identity import broker_deal_key_from_payload
from src.application.trades.lifecycle_outbox import (
    BATCH_RETRY_BACKOFF_MS, CLAIM_LEASE_MS, QUIET_WINDOW_MS,
    claim_next_notification, claim_next_notification_batch, dispatch_notification_batch_once,
    mark_notification_batch_send_started, plan_notification_batch,
)
from test_trade_attribution_combo_boundary import _scope
from test_combo_reconciliation_application import BASE_TIME_MS, _call_open, _put_open


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Combo receipt tests must not access network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(receipt, "notification_reminder_route_key",
                        lambda **kwargs: "synthetic-sender-recipient-v1")


def _config(config):
    config["notifications"] = {"enabled": True, "provider": "wechat_clawbot",
                               "target": "wechat:offline-combo-test"}
    config["trade_intake"]["sources"] = [{"enabled": True, "account": "lx",
                                        "receipt": {"enabled": True}}]
    return config


def _case(tmp_path, monkeypatch):
    repo, config, pair, observation, evidence, now = _scope(tmp_path, monkeypatch)
    _config(config)
    route = receipt.resolve_trade_lifecycle_notification_batch_route(config=config, base=tmp_path)
    return repo, config, pair, observation, evidence, now, route


def _args(case):
    repo, config, _, observation, evidence, now, route = case
    view = attribution.build_trade_attribution_view(
        read_trade_attribution_snapshot(repo, account="lx", market="us"),
        config=config, account="lx", market="us", now_ms=now,
        combo_evidence=evidence, capacity_observation=observation, combo_mode="confirm",
    )
    fact = next(row for row in view["rows"] if row["contract_key"]["option_type"] == "put")
    return dict(account="lx", market="us", config=config, execution_key=fact["execution_key"],
                candidate_id=fact["candidate_ids"][0], expected_input_hash=fact["input_hash"],
                request_id="fixture:pair", actor="fixture:operator", manual=True,
                combo_evidence=evidence, capacity_observation=observation, combo_mode="confirm",
                confirmation_receipt_route=route)


def _confirm(case, **overrides):
    args = {**_args(case), **overrides}
    return attribution.apply_trade_attribution(case[0], **args)


def _row(repo):
    row, = repo.list_trade_lifecycle_notifications()
    return row


def _ready(repo):
    return _row(repo)["created_at_ms"] + QUIET_WINDOW_MS


def _dispatch(case, calls, *, now=None, sender=None, repo=None, route=None, accounts=("lx",)):
    return dispatch_notification_batch_once(
        repo or case[0], route=route or case[-1], now_ms=now or _ready(case[0]),
        allowed_accounts=accounts,
        send_fn=sender or (lambda payload: calls.append(payload) or {
            "status": "confirmed", "delivery_confirmed": True, "message_id": "offline-success"}),
    )


def _supersede(case):
    repo, _, pair, _, _, now, _ = case
    return supersede_post_trade_combo_pair(
        repo=repo, inference_id=pair["inference_id"],
        expected_input_hash=pair["input_snapshot_hash"], reason="fixture:user-reassigned",
        actor="fixture:operator", apply_changes=True, effective_now_ms=now + 1)


def test_confirmation_preview_rolls_back_and_commit_freezes_one_success_intent(tmp_path, monkeypatch):
    case = _case(tmp_path, monkeypatch)
    repo, _, pair, _, _, _, route = case
    original_hash = _args(case)["expected_input_hash"]
    before = repo.list_trade_events()
    assert not _confirm(case, apply_changes=False)["write_applied"]
    assert repo.list_trade_events() == before
    assert repo.list_trade_lifecycle_notifications() == []
    assert _confirm(case)["write_applied"]
    row = _row(repo)
    assert row["status"] == "pending" and row["transition_type"] == "combo_pair_confirmed"
    assert row["payload"]["receipt_route"]["attribution_route_key"] == route["attribution_route_key"]
    assert row["payload"]["identity"]["group_id"] == pair["strategy_group_id"]
    assert "target" not in row["payload"]["receipt_route"]
    assert not _confirm(case, expected_input_hash=original_hash)["write_applied"]
    assert len(repo.list_trade_events()) == len(before) + 2
    assert _row(repo) == row


def test_concurrent_confirmations_and_restarted_dispatch_make_one_receipt(tmp_path, monkeypatch):
    case = _case(tmp_path, monkeypatch)
    args = _args(case)
    barrier = Barrier(2)
    def confirm(index):
        repo = SQLiteOptionPositionsRepository(case[0].db_path)
        barrier.wait()
        return attribution.apply_trade_attribution(repo, **{**args, "request_id": f"fixture:concurrent-{index}"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, (1, 2)))
    assert sum(result["write_applied"] for result in results) == 1
    assert len(case[0].list_trade_events()) == 4
    row = _row(case[0])
    restarted = SQLiteOptionPositionsRepository(case[0].db_path)
    calls = []
    assert _dispatch(case, calls, repo=restarted)["status"] == "confirmed"
    assert _dispatch(case, calls, repo=restarted)["status"] == "idle"
    assert len(calls) == 1
    assert _row(restarted)["outbox_id"] == row["outbox_id"]


@pytest.mark.parametrize("failure", ["after_intent", "capacity", "cancel"])
def test_failure_after_publication_rolls_back_confirmation_and_intent(tmp_path, monkeypatch, failure):
    case = _case(tmp_path, monkeypatch)
    repo = case[0]
    before = repo.list_trade_events()
    if failure == "after_intent":
        from src.application.ledger import notification_outbox
        original = notification_outbox.enqueue_combo_pair_success_in_transaction
        def fail(*a, **kw):
            original(*a, **kw)
            raise RuntimeError("fixture:crash-after-intent")
        monkeypatch.setattr(notification_outbox, "enqueue_combo_pair_success_in_transaction", fail)
        expected = RuntimeError
    elif failure == "capacity":
        from src.application.wheel.capacity import trade_attribution_capacity_check
        monkeypatch.setattr(ledger_attribution, "trade_attribution_capacity_check", trade_attribution_capacity_check)
        original = ledger_attribution.write_trade_attribution_decision
        def expire_after_intent(*a, **kw):
            result = original(*a, **kw)
            monkeypatch.setattr(ledger_attribution.time, "time", lambda: (case[5] + 61_000) / 1000)
            return result
        monkeypatch.setattr(ledger_attribution, "write_trade_attribution_decision", expire_after_intent)
        expected = ValueError
    else:
        from threading import Event
        stop = Event()
        original = ledger_attribution.write_trade_attribution_decision
        def cancelled(*a, **kw):
            result = original(*a, **kw)
            stop.set()
            return result
        monkeypatch.setattr(ledger_attribution, "write_trade_attribution_decision", cancelled)
        expected = ValueError
    with pytest.raises(expected):
        _confirm(case, **({"stop_event": stop} if failure == "cancel" else {}))
    assert repo.list_trade_events() == before
    assert repo.list_trade_lifecycle_notifications() == []
    assert repo.get_combo_pair_inference(case[2]["inference_id"])["status"] != "user_confirmed"


@pytest.mark.parametrize("disabled", ["notifications", "source", "account", "missing_sender"])
def test_disabled_confirmation_never_becomes_historical_send_after_enable(tmp_path, monkeypatch, disabled):
    case = _case(tmp_path, monkeypatch)
    repo, config = case[:2]
    if disabled == "notifications":
        config["notifications"]["enabled"] = False
    elif disabled == "source":
        config["trade_intake"]["sources"][0]["receipt"]["enabled"] = False
    elif disabled == "account":
        config["trade_intake"]["sources"][0]["account"] = "sy"
    else:
        monkeypatch.setattr(receipt, "notification_reminder_route_key", lambda **kwargs: None)
    route = attribution.observe_combo_confirmation_receipt_route(config=config, account="lx", runtime_root=tmp_path)
    _confirm(case, confirmation_receipt_route=route)
    assert _row(repo)["status"] == "suppressed"
    _config(config)
    calls = []
    assert _dispatch(case, calls)["status"] == "idle"
    assert calls == []


def test_historically_confirmed_groups_are_not_backfilled_on_recovery(tmp_path, monkeypatch):
    case = _case(tmp_path, monkeypatch)
    original_hash = _args(case)["expected_input_hash"]
    from src.application.ledger import notification_outbox
    # Model the old version's confirming transaction: no new receipt producer.
    with monkeypatch.context() as historical:
        historical.setattr(notification_outbox, "enqueue_combo_pair_success_in_transaction", lambda *a, **kw: None)
        _confirm(case)
    assert case[0].list_trade_lifecycle_notifications() == []
    assert not _confirm(case, expected_input_hash=original_hash)["write_applied"]
    result = attribution.reconcile_trade_attribution_account(
        case[0], config=case[1], account="lx", market="us", runtime_root=tmp_path,
        inbox_path=tmp_path / "missing-inbox.sqlite3", combo_mode="confirm")
    assert result["errors"] == [] and result["linked"] == 0
    assert case[0].list_trade_lifecycle_notifications() == []


@pytest.mark.parametrize("change", ["account", "target", "sender"])
def test_account_and_frozen_target_sender_bindings_reject_wrong_dispatch(tmp_path, monkeypatch, change):
    case = _case(tmp_path, monkeypatch)
    _confirm(case)
    route = dict(case[-1])
    accounts = ("lx",)
    if change == "account":
        accounts = ("sy",)
    elif change == "target":
        from src.application.trades.lifecycle_outbox import build_notification_batch_route
        route = {**build_notification_batch_route(provider=route["provider"], channel=route["channel"],
                                                  target="wechat:different-test-recipient"),
                 "attribution_route_key": route["attribution_route_key"]}
    else:
        route["attribution_route_key"] = "synthetic-different-sender"
    calls = []
    assert _dispatch(case, calls, route=route, accounts=accounts)["status"] == "idle"
    assert calls == [] and _row(case[0])["status"] == "pending"
    assert claim_next_notification(case[0], now_ms=_ready(case[0]), account="lx") is None


def test_claim_cannot_take_another_accounts_already_planned_batch(tmp_path, monkeypatch):
    case = _case(tmp_path, monkeypatch)
    _confirm(case)
    planned = plan_notification_batch(case[0], route=case[-1], now_ms=_ready(case[0]), allowed_accounts=("lx",))
    assert planned["status"] == "created"
    assert claim_next_notification_batch(case[0], now_ms=_ready(case[0]),
        route_fingerprint=case[-1]["route_fingerprint"], allowed_accounts=("sy",), route=case[-1]) is None
    wrong_sender = {**case[-1], "attribution_route_key": "different-synthetic-sender"}
    assert claim_next_notification_batch(case[0], now_ms=_ready(case[0]),
        route_fingerprint=case[-1]["route_fingerprint"], allowed_accounts=("lx",), route=wrong_sender) is None
    assert _row(case[0])["status"] == "batched"
    calls = []
    assert _dispatch(case, calls)["status"] == "confirmed" and len(calls) == 1


@pytest.mark.parametrize("already_planned", [False, True])
def test_revoked_ownership_suppresses_unsent_receipt(tmp_path, monkeypatch, already_planned):
    case = _case(tmp_path, monkeypatch)
    _confirm(case)
    if already_planned:
        plan_notification_batch(case[0], route=case[-1], now_ms=_ready(case[0]), allowed_accounts=("lx",))
    _supersede(case)
    before = deepcopy(_row(case[0]))
    plan_notification_batch(case[0], route=case[-1], now_ms=_ready(case[0]),
                            allowed_accounts=("lx",), apply_changes=False)
    assert _row(case[0]) == before
    calls = []
    assert _dispatch(case, calls)["status"] == ("suppressed" if already_planned else "idle")
    assert calls == [] and _row(case[0])["status"] == "suppressed"


def test_explicit_failure_retries_the_same_frozen_batch_with_backoff(tmp_path, monkeypatch):
    case = _case(tmp_path, monkeypatch)
    _confirm(case)
    calls = []
    now = _ready(case[0])
    first = _dispatch(case, calls, now=now, sender=lambda payload: calls.append(payload) or {
        "status": "explicit_failed", "explicit_pre_acceptance_failure": True})
    assert first["status"] == "explicit_failed"
    assert _dispatch(case, calls, now=now + 1)["status"] == "idle"
    second = _dispatch(case, calls, now=now + BATCH_RETRY_BACKOFF_MS[0])
    assert second["status"] == "confirmed" and len(calls) == 2
    assert calls[0]["batch_id"] == calls[1]["batch_id"]
    assert calls[0]["_frozen_message"] == calls[1]["_frozen_message"]


@pytest.mark.parametrize("outcome", ["unknown", "accepted", "exception", "crash"])
def test_uncertain_or_accepted_send_is_never_automatically_replayed(tmp_path, monkeypatch, outcome):
    case = _case(tmp_path, monkeypatch)
    _confirm(case)
    calls = []
    now = _ready(case[0])
    if outcome == "crash":
        planned = plan_notification_batch(case[0], route=case[-1], now_ms=now, allowed_accounts=("lx",))
        batch = claim_next_notification_batch(case[0], now_ms=now,
            route_fingerprint=case[-1]["route_fingerprint"], allowed_accounts=("lx",), route=case[-1])
        mark_notification_batch_send_started(case[0], batch_id=planned["batch"]["batch_id"],
            claim_id=batch["claim_id"], now_ms=now, route=case[-1])
    else:
        def send(payload):
            calls.append(payload)
            if outcome == "exception":
                raise TimeoutError("synthetic ambiguous send")
            return {"status": outcome, "delivery_confirmed": False}
        _dispatch(case, calls, now=now, sender=send)
    count = len(calls)
    restarted = SQLiteOptionPositionsRepository(case[0].db_path)
    assert _dispatch(case, calls, repo=restarted, now=now + CLAIM_LEASE_MS + 1)["status"] == "idle"
    assert len(calls) == count
    assert _row(restarted)["status"] in {"unknown", "accepted"}


def test_success_transport_uses_stable_identity_and_proves_actual_route(tmp_path, monkeypatch):
    case = _case(tmp_path, monkeypatch)
    _confirm(case)
    sends = []
    def sender(**kwargs):
        sends.append(kwargs)
        return {"command_ok": True, "delivery_confirmed": True, "message_id": "offline-id",
                "attribution_route_key": case[-1]["attribution_route_key"]}
    def deliver(payload):
        return receipt.send_trade_lifecycle_outbox_payload(
            base=tmp_path, config=case[1], receipt_config={"enabled": True}, payload=payload,
            send_fn=sender, normalize_fn=lambda **kwargs: kwargs)
    calls = []
    assert _dispatch(case, calls, sender=deliver)["status"] == "confirmed"
    assert len(sends) == 1
    message = sends[0]["message"]
    assert "配对成功" in message and "Combo 配对" in message and "lx" in message and "NVDA" in message
    assert "卖出 2026-08-21 100" in message and "Put · 1 张" in message
    assert "买入 2026-08-21 110" in message and "Call · 1 张" in message
    batch, = case[0].list_trade_lifecycle_notification_batches()
    assert sends[0]["idempotency_key"] == batch["batch_id"]


def test_restart_recovers_claim_before_send_without_creating_another_intent(tmp_path, monkeypatch):
    case = _case(tmp_path, monkeypatch)
    _confirm(case)
    now = _ready(case[0])
    plan_notification_batch(case[0], route=case[-1], now_ms=now, allowed_accounts=("lx",))
    claimed = claim_next_notification_batch(case[0], now_ms=now,
        route_fingerprint=case[-1]["route_fingerprint"], allowed_accounts=("lx",), route=case[-1])
    assert claimed["status"] == "claimed" and claimed["attempt_count"] == 0
    calls = []
    result = _dispatch(case, calls, repo=SQLiteOptionPositionsRepository(case[0].db_path),
                       now=now + CLAIM_LEASE_MS + 1)
    assert result["status"] == "confirmed" and len(calls) == 1
    assert result["batch"]["batch_id"] == claimed["batch_id"]


def test_other_sender_can_plan_its_own_new_pair_without_consuming_existing_batch(tmp_path, monkeypatch):
    from src.application.ledger.writer import persist_trade_event_object
    from test_combo_reconciliation_domain import _exposure
    case = _case(tmp_path, monkeypatch)
    repo, config = case[:2]
    _confirm(case)
    original = plan_notification_batch(repo, route=case[-1], now_ms=_ready(repo), allowed_accounts=("lx",))
    persist_trade_event_object(repo, _put_open("second-put", "second-put-lot", strike=90))
    persist_trade_event_object(repo, _call_open("second-call", "second-call-lot", strike=100))
    evidence = {"complete": True, "exposures": [
        _exposure(delivery_confirmed=True),
        _exposure(put_strike=90, call_strike=100, delivery_confirmed=True,
                  occurrence_id="occ-second", exposure_id="exp-second"),
    ]}
    route = {**case[-1], "attribution_route_key": "synthetic-second-sender"}
    # Expand only the synthetic observed positions, matching the new executed legs.
    observed = deepcopy(case[3])
    positions = observed["portfolio"]["position_snapshot_input"]["rows"]
    for row in deepcopy(positions[:2]):
        instrument = row["instrument_ref"]
        is_put = instrument["option_type"] == "put"
        instrument["strike"] = "90" if is_put else "100"
        positions.append(row)
    view = attribution.build_trade_attribution_view(
        read_trade_attribution_snapshot(repo, account="lx", market="us"),
        config=config, account="lx", market="us", now_ms=case[5],
        combo_evidence=evidence, capacity_observation=observed, combo_mode="confirm")
    fact = next(row for row in view["rows"] if row["open_event_id"] == "second-put")
    choice, = [item for item in fact["candidates"] if item["inference"]["put_record_id"] == "second-put-lot"
               and item["inference"]["call_record_id"] == "second-call-lot"]
    assert not set(choice["reason_codes"]) - {"combo_confirmation_required", "combo_not_unique_delivered_pair"}
    attribution.apply_trade_attribution(repo, **{
        **_args(case), "execution_key": fact["execution_key"], "candidate_id": choice["candidate_id"],
        "expected_input_hash": fact["input_hash"], "request_id": "fixture:second-pair",
        "combo_evidence": evidence, "capacity_observation": observed, "confirmation_receipt_route": route})
    now = max(row["created_at_ms"] for row in repo.list_trade_lifecycle_notifications()) + QUIET_WINDOW_MS
    planned = plan_notification_batch(repo, route=route, now_ms=now, allowed_accounts=("lx",))
    assert planned["status"] == "created"
    assert planned["batch"]["batch_id"] != original["batch"]["batch_id"]
    calls = []
    result = _dispatch(case, calls, now=now, route=route)
    assert result["status"] == "confirmed" and len(calls) == 1
    assert repo.get_trade_lifecycle_notification_batch(original["batch"]["batch_id"])["status"] == "pending"


@pytest.mark.parametrize("race", ["preflight_changed", "actual_sender_changed", "actual_route_missing"])
def test_sender_route_changes_do_not_claim_bound_delivery_success(tmp_path, monkeypatch, race):
    case = _case(tmp_path, monkeypatch)
    _confirm(case)
    sends = []
    def sender(**kwargs):
        sends.append(kwargs)
        return {"command_ok": True, "delivery_confirmed": True, "message_id": "offline-id",
                **({"attribution_route_key": "synthetic-different-sender"}
                   if race == "actual_sender_changed" else {})}
    if race == "preflight_changed":
        monkeypatch.setattr(receipt, "notification_reminder_route_key",
                            lambda **kwargs: "synthetic-different-sender")
    def deliver(payload):
        return receipt.send_trade_lifecycle_outbox_payload(
            base=tmp_path, config=case[1], receipt_config={"enabled": True}, payload=payload,
            send_fn=sender, normalize_fn=lambda **kwargs: kwargs)
    result = _dispatch(case, [], sender=deliver)
    assert result["status"] == ("explicit_failed" if race == "preflight_changed" else "unknown")
    assert len(sends) == (0 if race == "preflight_changed" else 1)

def _execution_payload(event):
    key = event.contract_key.to_dict()
    return {
        "schema_version": "trade_execution.v1",
        "broker_account_ref": {"broker_id": "futu", "external_account_id": "1001",
                               "environment": "REAL", "broker_account_id": "futu:REAL:1001", "account_label": "lx"},
        "instrument_ref": {"asset_type": "option", "market": "US", "symbol": key["underlying_symbol"],
                           "currency": "USD", "option_type": key["option_type"], "strike": str(key["strike"]),
                           "expiration_ymd": key["expiration_ymd"], "multiplier": "100"},
        "external_id_namespace": "futu.deal", "external_execution_id": event.event_id,
        "external_order_namespace": "futu.order", "external_order_id": "order-" + event.event_id,
        "side": event.raw_payload["side"], "position_effect": "open", "quantity": "1",
        "price": "1", "currency": "USD", "status": "OK",
        "occurred_at_utc": datetime.fromtimestamp(event.event_time_ms / 1000, timezone.utc).isoformat(),
    }


def _enable_rules(repo):
    with repo._writer_connection(begin_immediate=True) as conn:
        conn.execute("INSERT INTO trade_attribution_policy_enablings "
            "(broker, physical_account_id, environment, account, market, policy_version, effective_from_ms, "
            "created_at_ms, actor, request_id, request_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("futu", "1001", "REAL", "lx", "us", "trade_attribution.v2", BASE_TIME_MS - 1000,
             BASE_TIME_MS - 1000, "fixture", "fixture", "a" * 64))


@pytest.mark.parametrize("put_first", [True, False])
def test_live_intake_pending_receipts_then_auto_confirmation_sends_independent_success(tmp_path, monkeypatch, put_first):
    support = _case(tmp_path / "support", monkeypatch)
    config = support[1]
    repo = SQLiteOptionPositionsRepository(tmp_path / "live-synthetic" / "ledger.sqlite3")
    root = repo.db_path.parent
    _enable_rules(repo)
    original_sends = []
    def original_sender(**kwargs):
        original_sends.append(kwargs)
        return {"ok": True, "command_ok": True, "delivery_confirmed": True, "message_id": "offline-original"}
    monkeypatch.setattr(auto_intake, "send_trade_intake_receipt", partial(
        receipt.send_trade_intake_receipt, send_fn=original_sender, normalize_fn=lambda **kwargs: kwargs))
    callback = auto_intake._build_receipt_callback(base=root, cfg=config, receipt_config={"enabled": True}, repo=repo)
    payloads = [_execution_payload(_put_open()), _execution_payload(_call_open())]
    if not put_first:
        payloads.reverse()
    for index, payload in enumerate(payloads):
        applied = auto_intake._process_payload(
            payload, repo=repo, state_path=root / "state.json", audit_path=root / "audit.jsonl",
            account_mapping={"1001": "lx"}, futu_account_ids=["1001"], apply_changes=True,
            host="127.0.0.1", port=11111, allow_external_lookup=False, config=config, runtime_root=root,
            on_result_fn=callback)
        assert applied["status"] == "applied"
        if index == 0:
            assert repo.list_trade_lifecycle_notifications() == []
            partial_result = attribution.reconcile_trade_attribution_account(
                repo, config=config, account="lx", market="us", runtime_root=root,
                inbox_path=root / "ledger.sqlite3.trade_intake_inbox.sqlite3", combo_mode="confirm")
            assert partial_result["linked"] == 0 and repo.list_trade_lifecycle_notifications() == []
    inbox_path = root / "ledger.sqlite3.trade_intake_inbox.sqlite3"
    keys = [broker_deal_key_from_payload(p, account_mapping={"1001": "lx"}) for p in payloads]
    original_rows = inbox.read_trade_payloads_for_reconciliation(inbox_path, deal_ids=keys)
    frozen = [deepcopy(row["receipt"]) for row in original_rows]
    assert len(original_sends) == len(frozen) == 2
    assert all(row["status"] == "sent" and row["business_result"]["attribution_result"]["status"] == "pending"
               for row in frozen)
    assert all("配对成功" not in sent["message"] for sent in original_sends)
    result = attribution.reconcile_trade_attribution_account(
        repo, config=config, account="lx", market="us", runtime_root=root,
        inbox_path=inbox_path, combo_mode="confirm")
    assert result["errors"] == [] and result["linked"] == 1
    assert len(repo.list_trade_events()) == 4
    repeated = attribution.reconcile_trade_attribution_account(
        repo, config=config, account="lx", market="us", runtime_root=root,
        inbox_path=inbox_path, combo_mode="confirm")
    assert repeated["errors"] == [] and repeated["linked"] == 0
    reread = inbox.read_trade_payloads_for_reconciliation(inbox_path, deal_ids=keys)
    assert [row["receipt"] for row in reread] == frozen
    route = receipt.resolve_trade_lifecycle_notification_batch_route(config=config, base=root)
    sends = []
    def success_sender(**kwargs):
        sends.append(kwargs)
        return {"command_ok": True, "delivery_confirmed": True, "message_id": "offline-paired",
                "attribution_route_key": route["attribution_route_key"]}
    deliver = lambda payload: receipt.send_trade_lifecycle_outbox_payload(
        base=root, config=config, receipt_config={"enabled": True}, payload=payload,
        send_fn=success_sender, normalize_fn=lambda **kwargs: kwargs)
    restarted = SQLiteOptionPositionsRepository(repo.db_path)
    now = _ready(repo)
    assert dispatch_notification_batch_once(restarted, route=route, now_ms=now,
        allowed_accounts=("lx",), send_fn=deliver)["status"] == "confirmed"
    assert dispatch_notification_batch_once(restarted, route=route, now_ms=now + 60_000,
        allowed_accounts=("lx",), send_fn=deliver)["status"] == "idle"
    assert len(sends) == 1 and "配对成功" in sends[0]["message"]
    assert len(original_sends) == 2


def test_bot_manual_preview_confirmation_and_replay_share_the_durable_receipt_owner(tmp_path, monkeypatch):
    from src.application.bot.control import attribution_operations as operations
    from src.application.bot.control.contracts import BotInboundRequest, ControlCommand
    from src.application.bot.control.operation_store import InboundOperationStore
    from src.application.ledger.api import ledger_resource_identity
    case = _case(tmp_path, monkeypatch)
    repo, config = case[:2]
    for key, value in {"OM_INBOUND_OPERATIONS_ENABLED": "1", "OM_INBOUND_TRADE_WRITE_ENABLED": "1",
                       "OM_INBOUND_ADMIN_OPEN_IDS": "wechat:offline-user",
                       "OM_INBOUND_OPERATION_HMAC_KEY": "isolated-combo-test-key"}.items():
        monkeypatch.setenv(key, value)
    authority = {"config_path": str(tmp_path / "config.us.json"), "runtime_root": str(tmp_path),
                 "ledger": ledger_resource_identity(repo), "account_mapping_hash": "fixture"}
    monkeypatch.setattr(operations, "attribution_runtime", lambda **kwargs: (
        repo, config, authority.copy(), {"physical_account_ids": ["1001"], "environment": "REAL"}))
    monkeypatch.setattr(operations, "observe_trade_attribution_capacity", lambda **kwargs: case[3])
    monkeypatch.setattr(operations, "read_attribution_combo_evidence", lambda *a, **kw: case[4])
    request = BotInboundRequest(text="fixture-confirm", sender_id="offline-user", channel="wechat",
                               conversation_id="offline-room", config_key="us")
    args = _args(case)
    preview = ControlCommand("attribution_preview", {"account": "lx", "execution_key": args["execution_key"],
        "action": "combo", "target_id": case[2]["strategy_group_id"]})
    store = InboundOperationStore(tmp_path / "audit.sqlite3")
    result = operations.handle_attribution_operation(preview, request, command_id="offline-preview", store=store)
    assert result["ok"] and repo.list_trade_lifecycle_notifications() == []
    confirm = ControlCommand("attribution_confirm", {"operation_id": "offline-preview"})
    applied = operations.handle_attribution_operation(confirm, request, command_id="offline-confirm", store=store)
    assert applied["data"]["status"] == "applied" and _row(repo)["status"] == "pending"
    replay = operations.handle_attribution_operation(confirm, request, command_id="offline-replay", store=store)
    assert replay["data"]["status"] == "applied" and len(repo.list_trade_lifecycle_notifications()) == 1
