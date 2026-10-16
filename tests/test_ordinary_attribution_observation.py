from copy import deepcopy
from types import SimpleNamespace

import pytest

from src.application.wheel import capacity
from src.infrastructure.futu_history_deals import OpenDHistoryDealClient, _query_history_deals
from src.infrastructure.futu_gateway import _FutuAPIClient, build_futu_gateway
from test_ordinary_trade_attribution import _scope, FILL, NOW, REF


def test_native_current_order_receipt_and_gateway_preserve_completeness(monkeypatch):
    client = _FutuAPIClient(SimpleNamespace(), is_option_chain_cache_enabled=False)
    calls = []
    def query(**kwargs):
        calls.append(kwargs)
        return 0, [{"order_id":"1"}]
    monkeypatch.setattr(client, "_trade", lambda: SimpleNamespace(order_list_query=query))
    receipt = client.get_order_list_with_receipt(acc_id="1001", refresh_cache=True)
    assert receipt["coverage_complete"] is receipt["pagination_complete"] is True
    assert receipt["rows"]==[{"order_id":"1"}]
    assert calls==[{"acc_id":1001, "refresh_cache":True}]
    class Backend:
        def __init__(self, **kwargs): pass
    class Client:
        def __init__(self, backend, **kwargs): pass
        def get_order_list_with_receipt(self, **kwargs): return receipt
    gateway = build_futu_gateway(backend_cls=Backend, client_cls=Client)
    assert gateway.get_order_list_with_receipt(acc_id="1001")==receipt


def test_history_normalization_does_not_certify_silently_dropped_rows():
    class Gateway:
        def get_history_deals(self, **kwargs):
            return {"retcode":0, "coverage_complete":True, "pagination_complete":True,
                    "rows":[{"deal_id":"1"}, "malformed"]}
    rows, diagnostics = _query_history_deals(gateway=Gateway(), futu_account_ids=["1001"], lookback_hours=24, now=None)
    assert rows==[]
    assert diagnostics["account_results"][0]["coverage_status"]=="unknown"
    assert diagnostics["account_results"][0]["error_type"]=="ValueError"


def test_attribution_history_client_preserves_receipts_and_order_semantics(monkeypatch):
    client = OpenDHistoryDealClient(host="127.0.0.1", port=11111)
    calls = []
    order = {"order_id":"1", "order_status":"FILLED_PART", "dealt_qty":"1", "create_time":"2026-07-31 23:00:00"}
    receipt = {"retcode":0, "coverage_complete":True, "pagination_complete":True, "rows":[order]}
    class Gateway:
        def get_order_list_with_receipt(self, **kwargs):
            calls.append(("current", kwargs)); return deepcopy(receipt)
        def get_history_orders(self, **kwargs):
            calls.append(("history", kwargs)); return deepcopy(receipt)
    monkeypatch.setattr(client, "_gateway_client", lambda: Gateway())
    def fetch(**kwargs):
        calls.append(("deals", kwargs))
        return [{"deal_id":"1"}], {"start_date":"2026-07-31 00:00:00", "end_date":"2026-08-01 00:00:00"}
    monkeypatch.setattr(client, "fetch", fetch)
    result = client.fetch_attribution_evidence(futu_account_id="1001", start_ms=FILL)
    assert result["deals"]==[{"deal_id":"1"}]
    assert result["current_orders"]["rows"][0]["status"]=="retryable"
    assert result["history_orders"]["coverage_complete"] is True
    assert calls[1][1]=={"trd_env":"REAL", "acc_id":1001, "refresh_cache":True}
    assert result["window_end_ms"] % 1000==0
    receipt["rows"][0]["acc_id"]="other"
    with pytest.raises(ValueError, match="account differs"):
        client.fetch_attribution_evidence(futu_account_id="1001", start_ms=FILL)


def test_application_observer_binds_configured_account_and_closes_client(tmp_path, monkeypatch):
    repo, context = _scope(tmp_path, monkeypatch)
    original = context["capacity_observation"]["ordinary_evidence"]
    result = deepcopy(original)
    result["deals"] = [{"execution_input":row} for row in result.pop("executions")]
    closed = []
    class Client:
        def __init__(self, **kwargs): pass
        def fetch_attribution_evidence(self, **kwargs):
            assert kwargs=={"futu_account_id":"1001", "start_ms":original["window_start_ms"]}
            return result
        def close(self): closed.append(1)
    monkeypatch.setattr("src.infrastructure.futu_history_deals.OpenDHistoryDealClient", Client)
    before = list(tmp_path.rglob("*"))
    observed = capacity._read_ordinary_attribution_broker_evidence(config=context["config"], account="lx",
        runtime_root=tmp_path, start_ms=original["window_start_ms"])
    assert observed["scope"]=={**REF,"account":"lx"}
    assert observed["executions"]==original["executions"] and closed==[1]
    assert list(tmp_path.rglob("*"))==before


@pytest.mark.parametrize("failed", [False, True])
def test_optional_history_failure_retains_portfolio_and_success_forces_fresh_snapshot(tmp_path, monkeypatch, failed):
    import src.application.wheel.capacity as capacity
    repo, context = _scope(tmp_path, monkeypatch)
    calls = []
    def history(**kwargs):
        calls.append("history")
        if failed: raise RuntimeError("provider partial")
        return context["capacity_observation"]["ordinary_evidence"]
    def portfolio(**kwargs):
        calls.append("portfolio")
        assert kwargs["write_cache"] is False
        assert ("load_json_fn" in kwargs) is (not failed)
        if not failed: assert kwargs["load_json_fn"](None) is None
        return context["capacity_observation"]["portfolio"]
    monkeypatch.setattr(capacity, "_read_ordinary_attribution_broker_evidence", history)
    monkeypatch.setattr(capacity, "load_account_portfolio_context", portfolio)
    monkeypatch.setattr(capacity, "current_exchange_rate_snapshot", lambda **kwargs: None)
    monkeypatch.setattr(capacity, "project_exchange_rate_snapshot", lambda *a,**kw: None)
    class Connection:
        def send(self, value): self.result=value
        def close(self): self.closed=True
    connection = Connection()
    capacity._attribution_capacity_worker(connection, context["config"], "lx", tmp_path, FILL)
    assert calls==["history", "portfolio"] and connection.closed
    assert connection.result["portfolio"]
    assert bool(connection.result["ordinary_evidence"].get("error")) is failed


@pytest.mark.parametrize("method", ["get_order_list_with_receipt", "get_history_deals"])
@pytest.mark.parametrize("rows", [None, {"error":"unavailable"}, [{"order_id":"1"}, "malformed"]])
def test_native_coverage_receipts_reject_unknown_or_dropped_rows(monkeypatch, method, rows):
    client = _FutuAPIClient(SimpleNamespace(), is_option_chain_cache_enabled=False)
    query = lambda **kwargs: (0, rows)
    monkeypatch.setattr(client, "_trade", lambda: SimpleNamespace(order_list_query=query, history_deal_list_query=query))
    with pytest.raises(ValueError, match="incomplete or malformed"):
        getattr(client, method)(acc_id="1001")


@pytest.mark.parametrize("response,paginated", [([], False), ((None, []), False), ((0, [], b"next"), False)])
def test_native_coverage_requires_explicit_success_and_complete_pagination(response, paginated):
    client = _FutuAPIClient(SimpleNamespace(), is_option_chain_cache_enabled=False)
    with pytest.raises((ValueError, RuntimeError)):
        client._query_with_coverage(lambda **kwargs: response, paginated=paginated, kwargs={})


def test_native_history_preserves_sdk_two_item_complete_response():
    client = _FutuAPIClient(SimpleNamespace(), is_option_chain_cache_enabled=False)
    receipt = client._query_with_coverage(lambda **kwargs: (0, []), paginated=True, kwargs={})
    assert receipt["coverage_complete"] is receipt["pagination_complete"] is True
    assert receipt["rows"]==[]
