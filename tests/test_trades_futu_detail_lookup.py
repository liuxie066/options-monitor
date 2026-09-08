from __future__ import annotations

from src.application.trades.futu_detail_lookup import enrich_trade_push_payload_with_account_id


def test_enrich_trade_push_payload_keeps_existing_account_id() -> None:
    payload = {"futu_account_id": "123"}
    out = enrich_trade_push_payload_with_account_id(payload, host="127.0.0.1", port=11111, futu_account_ids=["456"])
    assert out.payload == payload
    assert out.diagnostics["matched_via"] == "payload"


def test_enrich_trade_push_payload_normalizes_existing_nonstandard_account_id() -> None:
    payload = {"trade_acc_id": "123"}
    out = enrich_trade_push_payload_with_account_id(payload, host="127.0.0.1", port=11111, futu_account_ids=["456"])
    assert out.payload["trade_acc_id"] == "123"
    assert out.payload["futu_account_id"] == "123"


def test_enrichment_readiness_failure_returns_fallback_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("broker unavailable")),
    )
    payload = {
        "futu_account_id": "123",
        "order_id": "order-1",
        "deal_id": "deal-1",
        "code": "US.NVDA260821P100000",
    }

    out = enrich_trade_push_payload_with_account_id(
        payload,
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["123"],
    )

    assert out.payload == payload
    assert out.diagnostics["matched_via"] == "broker_readiness_unavailable"
    assert out.diagnostics["query_errors"][0]["method"] == "broker_readiness"


def test_enrich_trade_push_payload_uses_existing_account_id_for_symbol_lookup(monkeypatch) -> None:
    class FakeGateway:
        def get_order_list(self, **kwargs):
            assert kwargs["acc_id"] == 777
            assert kwargs["order_id"] == "order-existing"
            return [{"order_id": "order-existing", "acc_id": "777", "owner_stock_code": "HK.09992"}]

        def get_deal_list(self, **kwargs):
            return []

        def close(self):
            return None

    monkeypatch.setattr("src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {
            "futu_account_id": "777",
            "order_id": "order-existing",
            "deal_id": "deal-existing",
            "code": "HK.XYZ260528P150000",
        },
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["777", "888"],
    )

    assert out.payload["futu_account_id"] == "777"
    assert out.payload["symbol"] == "9992.HK"
    assert out.diagnostics["matched_via"] == "order_lookup_by_acc_id"
    assert out.diagnostics["symbol_resolution"]["selected"]["source"] == "futu_lookup_row"


def test_enrich_trade_push_payload_resolves_account_id_via_order_lookup(monkeypatch) -> None:
    class FakeGateway:
        def get_order_list(self, **kwargs):
            assert kwargs["acc_id"] == 222
            assert kwargs["order_id"] == "order-1"
            return [{"order_id": "order-1", "acc_id": "222"}]

        def get_deal_list(self, **kwargs):
            return []

        def close(self):
            return None

    monkeypatch.setattr("src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {"order_id": "order-1", "deal_id": "deal-1"},
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["111", "222"],
    )
    assert out.payload["futu_account_id"] == "222"
    assert out.diagnostics["matched_via"] == "order_lookup_by_acc_id"


def test_enrich_trade_push_payload_resolves_account_id_via_deal_lookup(monkeypatch) -> None:
    class FakeGateway:
        def get_order_list(self, **kwargs):
            return []

        def get_deal_list(self, **kwargs):
            assert kwargs["acc_id"] == 333
            return [{"deal_id": "deal-2", "trd_acc_id": "333"}]

        def close(self):
            return None

    monkeypatch.setattr("src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {"deal_id": "deal-2"},
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["333"],
    )
    assert out.payload["futu_account_id"] == "333"
    assert out.diagnostics["matched_via"] == "deal_lookup_by_acc_id"


def test_enrich_trade_push_payload_filters_deals_locally_for_sdk_without_deal_id_kwarg(monkeypatch) -> None:
    calls: list[dict] = []

    class FakeGateway:
        def get_order_list(self, **kwargs):
            return []

        def get_deal_list(self, **kwargs):
            calls.append(dict(kwargs))
            if "deal_id" in kwargs or "order_id" in kwargs:
                raise TypeError("deal_list_query() got an unexpected keyword argument 'deal_id'")
            return [{"deal_id": "deal-2", "trd_acc_id": "333"}]

        def close(self):
            return None

    monkeypatch.setattr("src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {"deal_id": "deal-2"},
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["333"],
    )

    assert calls == [{"acc_id": 333}]
    assert out.payload["futu_account_id"] == "333"
    assert out.diagnostics["matched_via"] == "deal_lookup_by_acc_id"
    assert out.diagnostics["tried_queries"][-1]["filter_deal_id"] == "deal-2"


def test_enrich_trade_push_payload_does_not_query_outside_configured_accounts(monkeypatch) -> None:
    calls: list[dict] = []

    class FakeGateway:
        def get_order_list(self, **kwargs):
            calls.append(dict(kwargs))
            return []

        def get_deal_list(self, **kwargs):
            calls.append(dict(kwargs))
            return []

        def close(self):
            return None

    monkeypatch.setattr("src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {"order_id": "order-3", "deal_id": "deal-3"},
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["111"],
    )

    assert "futu_account_id" not in out.payload
    assert calls == [{"acc_id": 111}, {"acc_id": 111, "order_id": "order-3"}]
    assert out.diagnostics["matched_via"] == "not_found"


def test_order_lookup_only_adds_identity_and_other_deal_on_order_does_not_match(monkeypatch) -> None:
    class FakeGateway:
        def get_deal_list(self, **kwargs):
            return [{
                "deal_id": "other-deal",
                "order_id": "shared-order",
                "acc_id": "123",
                "qty": "9",
                "price": "99",
                "create_time": "2026-09-07 11:22:33",
            }]

        def get_order_list(self, **kwargs):
            return [{
                "order_id": "shared-order",
                "acc_id": "123",
                "owner_stock_code": "US.NVDA",
                "qty": "10",
                "quantity": "10",
                "contracts": "10",
                "price": "88",
                "execution_price": "88",
                "dealt_price": "88",
                "dealt_qty": "8",
                "dealt_avg_price": "87",
                "create_time": "2026-09-07 09:00:00",
                "updated_time": "2026-09-07 12:00:00",
            }]

        def close(self):
            return None

    monkeypatch.setattr("src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {"futu_account_id": "123", "order_id": "shared-order", "deal_id": "target-deal"},
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["123"],
    )

    assert out.payload["symbol"] == "NVDA"
    assert out.payload["futu_account_id"] == "123"
    assert out.diagnostics["matched_via"] == "order_lookup_by_acc_id"
    for key in (
        "qty", "quantity", "contracts", "price", "execution_price", "dealt_price",
        "dealt_qty", "dealt_avg_price", "create_time", "updated_time",
    ):
        assert key not in out.payload


def test_deal_lookup_requires_payload_account_match_and_preserves_complete_raw_fill(monkeypatch) -> None:
    class FakeGateway:
        def get_deal_list(self, **kwargs):
            return [{
                "deal_id": "deal-1",
                "order_id": "order-1",
                "acc_id": "456",
                "qty": "9",
                "price": "99",
                "create_time": "2026-09-07 11:22:33",
            }]

        def get_order_list(self, **kwargs):
            return []

        def close(self):
            return None

    monkeypatch.setattr("src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway", lambda **kwargs: FakeGateway())
    payload = {
        "futu_account_id": "123",
        "deal_id": "deal-1",
        "order_id": "order-1",
        "qty": "2",
        "price": "2.50",
        "create_time": "2026-09-07 10:30:00",
    }
    out = enrich_trade_push_payload_with_account_id(
        payload,
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["123", "456"],
    )

    assert out.payload == payload
    assert out.diagnostics["matched_via"] == "payload"


def test_deal_lookup_without_payload_account_requires_unique_row_account_evidence(monkeypatch) -> None:
    class FakeGateway:
        def get_deal_list(self, **kwargs):
            return [{"deal_id": "deal-1", "qty": "1", "price": "2.50"}]

        def get_order_list(self, **kwargs):
            return []

        def close(self):
            return None

    monkeypatch.setattr("src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {"deal_id": "deal-1"},
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["123"],
    )

    assert out.payload == {"deal_id": "deal-1"}
    assert out.diagnostics["matched_via"] == "not_found"


def test_payload_account_outside_configured_admission_is_not_queried(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unconfigured account must not reach OpenD")),
    )
    payload = {"futu_account_id": "999", "deal_id": "deal-1"}

    out = enrich_trade_push_payload_with_account_id(
        payload,
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["123"],
    )

    assert out.payload == payload
    assert out.diagnostics["matched_via"] == "unconfigured_payload_account"


def test_deal_lookup_without_payload_account_rejects_multiple_configured_matches(monkeypatch) -> None:
    class FakeGateway:
        def get_deal_list(self, **kwargs):
            acc_id = str(kwargs["acc_id"])
            return [{"deal_id": "same-deal", "acc_id": acc_id, "qty": "1", "price": "2.50"}]

        def get_order_list(self, **kwargs):
            return []

        def close(self):
            return None

    monkeypatch.setattr("src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {"deal_id": "same-deal"},
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["123", "456"],
    )

    assert out.payload == {"deal_id": "same-deal"}
    assert out.diagnostics["matched_via"] == "ambiguous_deal_lookup"


def test_enrich_trade_push_payload_unifies_symbol_from_futu_underlying_code(monkeypatch) -> None:
    class FakeGateway:
        def get_order_list(self, **kwargs):
            if "acc_id" in kwargs:
                return [{"order_id": "order-5", "acc_id": "777", "owner_stock_code": "HK.09992"}]
            return []

        def get_deal_list(self, **kwargs):
            return []

        def close(self):
            return None

    monkeypatch.setattr("src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {"order_id": "order-5", "deal_id": "deal-5", "code": "HK.POP260528P150000"},
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["777"],
    )

    assert out.payload["futu_account_id"] == "777"
    assert out.payload["symbol"] == "9992.HK"
    assert out.diagnostics["matched_via"] == "order_lookup_by_acc_id"


def test_enrich_trade_push_payload_prefers_futu_underlying_over_option_code_root(monkeypatch) -> None:
    class FakeGateway:
        def get_order_list(self, **kwargs):
            if "acc_id" in kwargs:
                return [{"order_id": "order-8", "acc_id": "777", "owner_stock_code": "HK.09992"}]
            return []

        def get_deal_list(self, **kwargs):
            return []

        def close(self):
            return None

    monkeypatch.setattr("src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {"order_id": "order-8", "deal_id": "deal-8", "code": "HK.XYZ260528P150000"},
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["777"],
    )

    assert out.payload["symbol"] == "9992.HK"
    assert out.diagnostics["symbol_resolution"]["selected"]["source"] == "futu_lookup_row"
    assert out.diagnostics["symbol_resolution"]["selected"]["key"] == "owner_stock_code"


def test_enrich_trade_push_payload_canonicalizes_alias_symbol_from_lookup_row(monkeypatch) -> None:
    class FakeGateway:
        def get_order_list(self, **kwargs):
            if "acc_id" in kwargs:
                return [{"order_id": "order-6", "acc_id": "777", "symbol": "POP"}]
            return []

        def get_deal_list(self, **kwargs):
            return []

        def close(self):
            return None

    monkeypatch.setattr("src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {"order_id": "order-6", "deal_id": "deal-6"},
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["777"],
    )

    assert out.payload["futu_account_id"] == "777"
    assert out.payload["symbol"] == "9992.HK"
    assert out.diagnostics["matched_via"] == "order_lookup_by_acc_id"


def test_enrich_trade_push_payload_records_lookup_errors(monkeypatch) -> None:
    class FakeGateway:
        def get_order_list(self, **kwargs):
            raise RuntimeError("lookup failed")

        def get_deal_list(self, **kwargs):
            return []

        def close(self):
            return None

    monkeypatch.setattr("src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {"order_id": "order-4", "deal_id": "deal-4"},
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["111"],
    )

    assert "futu_account_id" not in out.payload
    assert out.diagnostics["matched_via"] == "not_found"
    assert out.diagnostics["query_errors"]


def test_enrich_trade_push_payload_canonicalizes_us_prefixed_symbol_from_lookup_row(monkeypatch) -> None:
    class FakeGateway:
        def get_order_list(self, **kwargs):
            if "acc_id" in kwargs:
                return [{"order_id": "order-7", "acc_id": "888", "symbol": "US.NVDA"}]
            return []

        def get_deal_list(self, **kwargs):
            return []

        def close(self):
            return None

    monkeypatch.setattr("src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {"order_id": "order-7", "deal_id": "deal-7"},
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["888"],
    )

    assert out.payload["futu_account_id"] == "888"
    assert out.payload["symbol"] == "NVDA"
    assert out.diagnostics["matched_via"] == "order_lookup_by_acc_id"
