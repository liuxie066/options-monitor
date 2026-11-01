"""A running listener obeys disk-published delivery controls without business reload."""
from copy import deepcopy
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from domain.domain.multi_tick import notification_route_identity_hash
from src.application import notification_fanout as fanout
from src.application.notification_fanout_delivery import send_notification_fanout
from src.application.notification_delivery_route import resolve_notification_delivery_routes
from src.application.config_authoring_transaction import publish_yaml_config_generation
from src.application.ledger import notification_outbox
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades import auto_intake, inbox, receipt


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def listener_lab(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_SECRET_BACKEND", "env")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "fixture-app")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "fixture-secret")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "unselected-env-recipient")
    runtime = tmp_path / "selected-runtime"
    runtime.mkdir()
    authoring = runtime / "config.yaml"
    doc = {
        "accounts": {"lx": {"type": "futu", "futu_account_id": "12345678"},
                     "sy": {"type": "futu", "futu_account_id": "87654321"}},
        "markets": {"us": {"accounts": ["lx"], "symbols": ["NVDA"]},
                    "hk": {"accounts": ["sy"], "symbols": ["0700.HK"]}},
        "bot": {"enabled": False},
        "channels": {"feishu": {"enabled": True, "receive_enabled": False, "notify_enabled": True}},
        "notifications": {"enabled": True, "routes": [
            {"id": target, "provider": "feishu_app", "target": target, "accounts": ["lx"]}
            for target in ("A", "B", "C")]},
    }
    authoring.write_text(yaml.safe_dump(doc))

    def publish(current):
        return publish_yaml_config_generation(repo_root=ROOT, config_yaml_path=authoring,
            config_doc=current, runtime_root=runtime, markets=["us"], apply=True)

    publish(doc)
    clock = [2_000_000_000.0]
    monkeypatch.setattr(fanout.time, "time", lambda: clock[0])
    repo = SQLiteOptionPositionsRepository(runtime / "ledger.sqlite3")
    calls = []

    def send(**kwargs):
        calls.append(deepcopy(kwargs))
        target = kwargs["target"]
        if target == "B" and sum(call["target"] == "B" for call in calls) == 1:
            return {"explicit_pre_acceptance_failure": True}
        if target == "C":
            return {"ambiguous_send": True}
        return {"delivery_confirmed": True, "message_id": "fixture-" + target,
                "attribution_route_key": notification_route_identity_hash(provider="feishu_app",
                    channel="feishu_app", sender="fixture-app", recipient=target)}

    normalize = lambda send_result: send_result
    monkeypatch.setattr(auto_intake, "send_trade_intake_receipt",
        partial(receipt.send_trade_intake_receipt, send_fn=send, normalize_fn=normalize))
    monkeypatch.setattr(auto_intake, "send_trade_lifecycle_outbox_payload",
        partial(receipt.send_trade_lifecycle_outbox_payload, send_fn=send, normalize_fn=normalize))
    source = {
        "id": "lx", "account": "lx", "enabled": True, "mode": "apply",
        "host": "fixture.invalid", "port": 11111,
        "state_path": Path("state.json"), "audit_path": Path("audit.jsonl"),
        "status_path": Path("status.json"), "inbox_path": Path("inbox.sqlite3"),
        "backfill_checkpoint_path": Path("backfill.json"), "receipt": {"enabled": True},
        "account_mapping": {"12345678": "lx"}, "futu_account_ids": ["12345678"],
    }
    intake = {"enabled": True, "mode": "apply", "receipt": {"enabled": True}, "sources": [source],
              **{key: source[key] for key in ("state_path", "audit_path", "status_path",
                                             "account_mapping", "futu_account_ids")}}
    monkeypatch.setattr(auto_intake, "resolve_trade_intake_config", lambda *_a, **_kw: intake)
    monkeypatch.setattr(auto_intake, "open_position_ledger_from_runtime_config", lambda **kw: (None, repo))
    # A separate application directory proves authorization follows --config, not base/default runtime.
    application = tmp_path / "application"
    application.mkdir()
    monkeypatch.setattr(auto_intake, "repo_base", application)
    dispatchers = []
    monkeypatch.setattr(auto_intake.LifecycleReceiptBatchDispatcher, "start", lambda self: dispatchers.append(self))
    return SimpleNamespace(doc=doc, publish=publish, clock=clock, repo=repo, calls=calls,
        runtime=runtime, application=application, dispatchers=dispatchers)


def _inbox_context(lab):
    path = lab.runtime / "inbox.sqlite3"
    key = inbox.enqueue_trade_payload(path, payload={"deal_id": "one", "futu_account_id": "12345678", "account": "lx"},
        source="push", broker_deal_key="futu:lx:12345678:one")
    with inbox._connect(path) as conn:
        conn.execute("UPDATE trade_inbox SET receipt_recovery_allowed=1 WHERE inbox_id=?", (key,))
    claim = inbox.claim_trade_payload(path, inbox_id=key)
    result = inbox.save_trade_payload_result(path, claim=claim, result={"account": "lx",
        "status": "failed", "reason": "fixture_failure", "diagnostics": {"retryable": False}})
    inbox.settle_trade_payload_result(path, inbox_id=key, claim=claim, result=result)
    return {"apply_changes": True, "state": {}, "deal": SimpleNamespace(deal_id="one", internal_account="lx"),
            "result": result, "effective_payload": {"account": "lx"}, "inbox_path": path, "inbox_id": key}


@pytest.mark.parametrize("family", ["callback", "dispatcher"])
@pytest.mark.parametrize("change", ["master", "notify", "emergency", "removed", "rotated", "account_removed"])
def test_live_listener_published_controls_pause_and_only_explicit_failure_resumes(listener_lab, monkeypatch, family, change):
    lab = listener_lab
    context = _inbox_context(lab)
    intent = notification_outbox.build_notification_intent(case_id="fixture-case", transition_type="needs_review",
        resolution_revision=1, transition_key="fixture-review", state_fingerprint="fixture",
        payload={"account": "lx", "symbol": "NVDA", "transition_type": "needs_review", "market": "US"})
    notification_outbox.enqueue_notification_intent(lab.repo, intent)
    lab.clock[0] += 11
    loaded = []
    load = auto_intake.load_config

    def load_once(**kwargs):
        result = load(**kwargs)
        # Persisted metadata must never redirect this listener away from its explicit --config.
        result["config_source_path"] = str(lab.application / "wrong-runtime.json")
        loaded.append(result)
        return result

    monkeypatch.setattr(auto_intake, "load_config", load_once)

    def run_source(**kwargs):
        startup = kwargs["cfg"]
        frozen_business = deepcopy(startup)
        assert len(lab.dispatchers) == 1

        def emit():
            if family == "callback":
                return kwargs["receipt_callback"](context)
            result = lab.dispatchers[0].run_once()
            assert result["status"] != "error", result
            return result["batch"]["provider_receipt"]

        initial = emit()
        assert [call["target"] for call in lab.calls] == ["A", "B", "C"], initial
        assert [child["status"] for child in initial["children"]] == ["confirmed", "explicit_failed", "unknown"]
        changed = deepcopy(lab.doc)
        changed["markets"]["us"]["symbols"] = ["AAPL"]
        if change == "master":
            changed["channels"]["feishu"]["enabled"] = False
        elif change == "notify":
            changed["channels"]["feishu"]["notify_enabled"] = False
        elif change == "emergency":
            changed["notifications"]["enabled"] = False
        elif change == "removed":
            changed["notifications"]["routes"] = [row for row in changed["notifications"]["routes"] if row["id"] != "B"]
        elif change == "rotated":
            changed["notifications"]["routes"][1]["target"] = "new-B"
        else:
            changed["notifications"]["routes"][1]["accounts"] = ["sy"]
        lab.publish(changed)
        lab.clock[0] += 601
        paused = emit()
        assert len(lab.calls) == 3
        assert [child["attempt_count"] for child in paused["children"]] == [1, 1, 1]
        assert [child["spec"] for child in paused["children"]] == [child["spec"] for child in initial["children"]]
        restored = deepcopy(lab.doc)
        restored["markets"]["us"]["symbols"] = ["AAPL"]
        restored["notifications"]["routes"].append(
            {"id": "D", "provider": "feishu_app", "target": "new-recipient", "accounts": ["lx"]})
        lab.publish(restored)
        lab.clock[0] += 601
        resumed = emit()
        assert [call["target"] for call in lab.calls] == ["A", "B", "C", "B"]
        assert lab.calls[1] == lab.calls[-1]
        assert [child["attempt_count"] for child in resumed["children"]] == [1, 2, 1]
        assert [child["status"] for child in resumed["children"]] == ["confirmed", "confirmed", "unknown"]
        assert startup == frozen_business and loaded == [startup]
        assert startup["config_source_path"] == str((lab.runtime / "config.us.json").resolve())
        assert lab.repo.list_trade_events() == lab.repo.list_position_lots() == []
        return 0

    monkeypatch.setattr(auto_intake, "_run_listener_source_loop", run_source)
    assert auto_intake.main(["--config", str(lab.runtime / "config.us.json"), "--runtime-root", str(lab.runtime),
                             "--mode", "apply", "--confirm"]) == 0


@pytest.mark.parametrize("broken", ["missing", "invalid_json", "not_an_object"])
def test_bound_authorization_failure_never_uses_cached_enabled_config(listener_lab, broken):
    lab = listener_lab
    path = lab.runtime / "config.us.json"
    cfg = auto_intake.load_config(base=lab.application, config_path=path, is_scheduled=False, log=lambda _: None)
    cfg["config_source_path"] = str(path)
    calls = []

    def send(**kwargs):
        calls.append(kwargs)
        return {"explicit_pre_acceptance_failure": True}

    args = dict(base=lab.application, config=cfg, account="lx", logical_key="unavailable-authority",
                message="frozen", send_fn=send, normalize_fn=lambda send_result: send_result)
    first = send_notification_fanout(**args)
    assert len(calls) == 3
    if broken == "missing":
        path.unlink()
    else:
        path.write_text("{" if broken == "invalid_json" else "[]")
    lab.clock[0] += 61
    paused = send_notification_fanout(**args)
    assert len(calls) == 3
    assert paused["children"] == first["children"]
    assert paused["attempted"] is False


def test_global_plan_children_authorize_frozen_actual_account_without_all_wildcard(listener_lab):
    lab = listener_lab
    cfg = deepcopy(lab.doc)
    cfg["notifications"]["routes"] = [
        {"id": "A", "provider": "feishu_app", "target": "A", "accounts": ["lx"]},
        {"id": "B", "provider": "feishu_app", "target": "B", "accounts": ["sy"]}]
    routes = [route for account in ("lx", "sy")
              for route in resolve_notification_delivery_routes(config=cfg, account=account, base=lab.application)]
    calls = []

    def send(**kwargs):
        calls.append(kwargs)
        return {"explicit_pre_acceptance_failure": True}

    args = dict(base=lab.application, config=cfg, account="all", logical_key="global",
                routes=routes, message="global event", send_fn=send, normalize_fn=lambda send_result: send_result)
    initial = send_notification_fanout(**args)
    assert [child["spec"]["metadata"]["account"] for child in initial["children"]] == ["lx", "sy"]
    assert [call["target"] for call in calls] == ["A", "B"]
    cfg["notifications"]["routes"][1]["accounts"] = ["lx"]
    lab.clock[0] += 61
    paused = send_notification_fanout(**args)
    assert [call["target"] for call in calls] == ["A", "B", "A"]
    assert [child["attempt_count"] for child in paused["children"]] == [2, 1]


@pytest.mark.parametrize("family", ["callback", "dispatcher"])
@pytest.mark.parametrize("change", ["master", "notify", "emergency", "removed", "rotated", "account_removed"])
@pytest.mark.parametrize("outcome", ["explicit_failed", "confirmed", "unknown"])
def test_legacy_live_listener_keeps_original_obligation_and_pauses_before_budget(
        listener_lab, monkeypatch, family, change, outcome):
    lab = listener_lab
    lab.doc.pop("channels")
    monkeypatch.setattr(receipt, "report_system_meta_signal", lambda **_kwargs: None)
    lab.doc["notifications"] = {"enabled": True, "provider": "feishu_app"}
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "B")
    lab.publish(lab.doc)
    context = _inbox_context(lab)
    intent = notification_outbox.build_notification_intent(case_id="legacy-case", transition_type="needs_review",
        resolution_revision=1, transition_key="legacy-review", state_fingerprint="fixture",
        payload={"account": "lx", "symbol": "NVDA", "transition_type": "needs_review", "market": "US"})
    notification_outbox.enqueue_notification_intent(lab.repo, intent)
    lab.clock[0] += 11

    def send(**kwargs):
        lab.calls.append(deepcopy(kwargs))
        if len(lab.calls) == 1:
            return {"explicit_pre_acceptance_failure": True} if outcome == "explicit_failed" else (
                {"ambiguous_send": True} if outcome == "unknown" else
                {"delivery_confirmed": True, "message_id": "confirmed"})
        return {"delivery_confirmed": True, "message_id": "resumed"}

    monkeypatch.setattr(auto_intake, "send_trade_intake_receipt", partial(
        receipt.send_trade_intake_receipt, send_fn=send, normalize_fn=lambda send_result: send_result))
    monkeypatch.setattr(auto_intake, "send_trade_lifecycle_outbox_payload", partial(
        receipt.send_trade_lifecycle_outbox_payload, send_fn=send, normalize_fn=lambda send_result: send_result))
    loaded = []
    load = auto_intake.load_config

    def load_once(**kwargs):
        cfg = load(**kwargs)
        cfg["config_source_path"] = str(lab.application / "wrong-runtime.json")
        loaded.append(cfg)
        return cfg

    monkeypatch.setattr(auto_intake, "load_config", load_once)

    def run_source(**kwargs):
        startup = kwargs["cfg"]
        frozen_cfg = deepcopy(startup)
        assert "channels" not in startup and "routes" not in startup["notifications"]
        dispatcher = lab.dispatchers[0]

        def emit():
            result = kwargs["receipt_callback"](context) if family == "callback" else dispatcher.run_once()
            assert result["status"] != "error", result
            return result

        def obligation():
            if family == "callback":
                return inbox.read_trade_payload(context["inbox_path"], inbox_id=context["inbox_id"])["receipt"]
            return lab.repo.list_trade_lifecycle_notification_batches()[0]

        emit()
        assert len(lab.calls) == 1
        original = deepcopy(obligation())
        attempts = original["attempt_count"]
        assert attempts == 1
        changed = deepcopy(lab.doc)
        changed["channels"] = {"feishu": {"enabled": True, "receive_enabled": False, "notify_enabled": True}}
        changed["notifications"]["routes"] = [{"id": "migrated-B", "provider": "feishu_app",
                                                "target": "B", "accounts": ["lx"]}]
        if change == "master":
            changed["channels"]["feishu"]["enabled"] = False
        elif change == "notify":
            changed["channels"]["feishu"]["notify_enabled"] = False
        elif change == "emergency":
            changed["notifications"]["enabled"] = False
        elif change == "removed":
            changed["notifications"]["routes"] = []
        elif change == "rotated":
            changed["notifications"]["routes"][0]["target"] = "new-B"
        else:
            changed["notifications"]["routes"][0]["accounts"] = ["sy"]
        changed["markets"]["us"]["symbols"] = ["AAPL"]
        lab.publish(changed)
        lab.clock[0] += 601
        emit()
        assert len(lab.calls) == 1
        assert obligation() == original
        restored = deepcopy(lab.doc)
        restored["channels"] = {"feishu": {"enabled": True, "receive_enabled": False, "notify_enabled": True}}
        restored["notifications"]["routes"] = [
            {"id": "migrated-B", "provider": "feishu_app", "target": "B", "accounts": ["lx"]},
            {"id": "new-D", "provider": "feishu_app", "target": "D", "accounts": ["lx"]}]
        lab.publish(restored)
        lab.clock[0] += 601
        emit()
        assert len(lab.calls) == (2 if outcome == "explicit_failed" else 1)
        if outcome == "explicit_failed":
            assert lab.calls[0] == lab.calls[1]
            assert obligation()["attempt_count"] == 2
        else:
            assert obligation() == original
        assert startup == frozen_cfg and loaded == [startup]
        assert startup["config_source_path"] == str((lab.runtime / "config.us.json").resolve())
        assert lab.repo.list_trade_events() == lab.repo.list_position_lots() == []
        assert not list(lab.application.glob("**/*fanout*.sqlite3"))
        return 0

    monkeypatch.setattr(auto_intake, "_run_listener_source_loop", run_source)
    assert auto_intake.main(["--config", str(lab.runtime / "config.us.json"), "--runtime-root", str(lab.runtime),
                             "--mode", "apply", "--confirm"]) == 0
