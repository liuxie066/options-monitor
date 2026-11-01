from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading

import pytest

from src.application import notification_fanout as mod


def spec(route="a", identity=None):
    return {"recipient": {"route_id": route, "identity_key": identity or "identity-" + route,
        "provider": "fixture", "channel": "fixture", "target": "target-" + route,
        "transport_options": {}}, "message": "frozen " + route,
        "transport_envelope": None, "metadata": {"revision": 1}}


def prepare(base, deliveries=None, key="fixture:message", account="lx"):
    return mod.prepare_notification_fanout(base=base, account=account, logical_key=key,
        deliveries=deliveries or [spec()], retry_policy=mod.FanoutRetryPolicy(3, (100, 200)), now_ms=10)


def test_mixed_outcomes_retry_only_rejected_child_and_keep_exact_spec(tmp_path):
    original = prepare(tmp_path, [spec("a"), spec("b"), spec("c")])
    calls = []
    def send(*, spec, idempotency_key):
        calls.append((deepcopy(spec), idempotency_key))
        route = spec["recipient"]["route_id"]
        return {"outcome": {"a": "confirmed", "b": "explicit_failed", "c": "unknown"}[route], "attribution_route_key": spec["recipient"]["identity_key"]}
    partial = mod.dispatch_notification_fanout(base=tmp_path, fanout_id=original["fanout_id"], send_fn=send, now_ms=10)
    assert partial["status"] == "partial" and not partial["delivery_confirmed"]
    assert not partial["explicit_pre_acceptance_failure"]
    assert partial["has_retryable_children"] and partial["has_unknown_children"]
    assert partial["next_attempt_at_ms"] == 110
    mod.dispatch_notification_fanout(base=tmp_path, fanout_id=original["fanout_id"], send_fn=send, now_ms=109)
    assert len(calls) == 3
    def retry(**kwargs):
        assert kwargs["spec"] == calls[1][0]
        assert kwargs["idempotency_key"] == calls[1][1]
        calls.append((kwargs["spec"], kwargs["idempotency_key"]))
        return {"delivery_confirmed": True, "attribution_route_key": kwargs["spec"]["recipient"]["identity_key"]}
    result = mod.dispatch_notification_fanout(base=tmp_path, fanout_id=original["fanout_id"], send_fn=retry, now_ms=110)
    assert [child["status"] for child in result["children"]] == ["confirmed", "confirmed", "unknown"]
    assert not result["has_retryable_children"] and result["status"] == "partial"
    assert len(calls) == 4 and len({key for _, key in calls[:3]}) == 3


def test_prepare_is_idempotent_but_never_retargets_or_changes_frozen_content(tmp_path):
    first = prepare(tmp_path, [spec("b"), spec("a")])
    assert prepare(tmp_path, [spec("a"), spec("b")]) == first
    for deliveries in ([spec("a")], [spec("a", "changed"), spec("b")],
                       [{**spec("a"), "message": "changed"}, spec("b")]):
        with pytest.raises(ValueError, match="frozen plan changed"):
            prepare(tmp_path, deliveries)
    assert mod.list_notification_fanouts(base=tmp_path, account="lx", logical_key_prefix="fixture:") == [first]
    assert mod.list_notification_fanouts(base=tmp_path, account="sy") == []
    assert mod.read_notification_fanout_recipient(base=tmp_path, fanout_id=first["fanout_id"],
        route_id="a", identity_key="changed") is None


def test_frozen_plan_is_private_and_validated_on_read(tmp_path):
    first = prepare(tmp_path)
    path = next(tmp_path.rglob("nf_*.json"))
    assert path.stat().st_mode & 0o777 == 0o600
    state = json.loads(path.read_text())
    state["children"][0]["spec"]["message"] = "tampered"
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="frozen spec mismatch"):
        mod.read_notification_fanout(base=tmp_path, fanout_id=first["fanout_id"])


def test_concurrent_dispatch_skips_claim_without_holding_lock_over_provider(tmp_path):
    first = prepare(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def send(**kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(5)
        return {"delivery_confirmed": True, "attribution_route_key": kwargs["spec"]["recipient"]["identity_key"]}
    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(mod.dispatch_notification_fanout, base=tmp_path,
            fanout_id=first["fanout_id"], send_fn=send, now_ms=10)
        try:
            assert entered.wait(5)
            concurrent = pool.submit(mod.dispatch_notification_fanout, base=tmp_path,
                fanout_id=first["fanout_id"], send_fn=send, now_ms=11).result(timeout=2)
            assert concurrent["status"] == "in_progress" and len(calls) == 1
        finally:
            release.set()
        assert future.result()["delivery_confirmed"]


def test_crash_preserves_unknown_and_other_children_continue(tmp_path):
    first = prepare(tmp_path, [spec("a"), spec("b")])
    def crash(**kwargs):
        raise SystemExit("provider process lost")
    with pytest.raises(SystemExit):
        mod.dispatch_notification_fanout(base=tmp_path, fanout_id=first["fanout_id"], send_fn=crash, now_ms=10)
    calls = []
    def send(**kwargs):
        calls.append(kwargs["spec"]["recipient"]["route_id"])
        return {"delivery_confirmed": True, "attribution_route_key": kwargs["spec"]["recipient"]["identity_key"]}
    recovered = mod.dispatch_notification_fanout(base=tmp_path, fanout_id=first["fanout_id"],
        send_fn=send, now_ms=10 + mod.CLAIM_LEASE_MS)
    assert calls == ["b"]
    assert [child["status"] for child in recovered["children"]] == ["unknown", "confirmed"]


def test_disabled_child_does_not_consume_attempt_or_drop_obligation(tmp_path):
    first = prepare(tmp_path)
    def forbidden(**kwargs):
        pytest.fail("disabled child attempted transport")
    disabled = mod.dispatch_notification_fanout(base=tmp_path, fanout_id=first["fanout_id"],
        send_fn=forbidden, can_send_fn=lambda _: False, now_ms=10)
    assert disabled["children"][0]["attempt_count"] == 0
    assert disabled["has_retryable_children"] and disabled["status"] == "pending"


@pytest.mark.parametrize("result,expected", [
    ({"command_ok": True}, "accepted"),
    ({"delivery_confirmed": True}, "unknown"),
    ({"outcome": "confirmed"}, "unknown"),
    ({"delivery_confirmed": True, "attribution_route_key": "identity-a"}, "confirmed"),
    ({"delivery_confirmed": True, "attribution_route_key": "identity-a", "ambiguous_send": True}, "unknown"),
    ({"http_status": 200, "provider_response_code": -2}, "explicit_failed"),
    ({"http_status": 400, "ambiguous_send": True}, "unknown"),
    ({"delivery_confirmed": True, "attribution_route_key": "different"}, "unknown"),
])
def test_outcome_evidence_is_conservative(tmp_path, result, expected):
    first = prepare(tmp_path)
    final = mod.dispatch_notification_fanout(base=tmp_path, fanout_id=first["fanout_id"],
        send_fn=lambda **_: result, now_ms=10)
    assert final["children"][0]["status"] == expected


def test_retry_budget_belongs_to_child_and_stops_after_three(tmp_path):
    first = prepare(tmp_path)
    calls = []
    def reject(**kwargs):
        calls.append(kwargs)
        return {"explicit_pre_acceptance_failure": True}
    for now in (10, 110, 310, 10000):
        final = mod.dispatch_notification_fanout(base=tmp_path, fanout_id=first["fanout_id"], send_fn=reject, now_ms=now)
    assert len(calls) == 3 and final["status"] == "explicit_failed"
    assert not final["has_retryable_children"] and final["next_attempt_at_ms"] is None


def test_duplicate_actual_recipient_is_rejected_before_creating_obligations(tmp_path):
    with pytest.raises(ValueError, match="duplicate"):
        prepare(tmp_path, [spec("a", "same"), spec("b", "same")])
    assert not list(tmp_path.rglob("*.json"))


def test_route_validation_error_retains_child_and_allows_other_children(tmp_path):
    first = prepare(tmp_path, [spec("a"), spec("b")])
    def allowed(spec):
        if spec["recipient"]["route_id"] == "a":
            raise ValueError("current binding unavailable")
        return True
    final = mod.dispatch_notification_fanout(base=tmp_path, fanout_id=first["fanout_id"],
        can_send_fn=allowed, now_ms=10, send_fn=lambda *, spec, **_: {
            "delivery_confirmed": True, "attribution_route_key": spec["recipient"]["identity_key"]})
    assert [child["attempt_count"] for child in final["children"]] == [0, 1]
    assert [child["status"] for child in final["children"]] == ["pending", "confirmed"]
    assert final["blocked_children"][0]["reason"] == "ValueError"
