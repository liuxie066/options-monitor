from __future__ import annotations

from scripts.futu_trade_detail_lookup import enrich_trade_push_payload_with_account_id


def test_enrich_trade_push_payload_keeps_existing_account_id() -> None:
    payload = {"deal_id": "d1", "futu_account_id": "123"}
    out = enrich_trade_push_payload_with_account_id(payload, host="127.0.0.1", port=11111, futu_account_ids=["456"])
    assert out.payload == payload
    assert out.diagnostics["matched_via"] == "payload"


def test_enrich_trade_push_payload_normalizes_existing_nonstandard_account_id() -> None:
    payload = {"deal_id": "d1", "trade_acc_id": "123"}
    out = enrich_trade_push_payload_with_account_id(payload, host="127.0.0.1", port=11111, futu_account_ids=["456"])
    assert out.payload["trade_acc_id"] == "123"
    assert out.payload["futu_account_id"] == "123"


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

    monkeypatch.setattr("scripts.futu_trade_detail_lookup.build_futu_gateway", lambda **kwargs: FakeGateway())
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

    monkeypatch.setattr("scripts.futu_trade_detail_lookup.build_futu_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {"deal_id": "deal-2"},
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["333"],
    )
    assert out.payload["futu_account_id"] == "333"
    assert out.diagnostics["matched_via"] == "deal_lookup_by_acc_id"


def test_enrich_trade_push_payload_falls_back_to_lookup_without_acc_id(monkeypatch) -> None:
    class FakeGateway:
        def get_order_list(self, **kwargs):
            if "acc_id" in kwargs:
                return []
            return [{"order_id": "order-3", "acc_id": "777", "stock_name": "泡泡玛特", "code": "HK.POP260528P150000"}]

        def get_deal_list(self, **kwargs):
            return []

        def close(self):
            return None

    monkeypatch.setattr("scripts.futu_trade_detail_lookup.build_futu_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {"order_id": "order-3", "deal_id": "deal-3"},
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["111"],
    )

    assert out.payload["futu_account_id"] == "777"
    assert out.payload["stock_name"] == "泡泡玛特"
    assert out.payload["code"] == "HK.POP260528P150000"
    assert out.diagnostics["matched_via"] == "order_lookup_without_acc_id"


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

    monkeypatch.setattr("scripts.futu_trade_detail_lookup.build_futu_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {"order_id": "order-5", "deal_id": "deal-5", "code": "HK.POP260528P150000"},
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["777"],
    )

    assert out.payload["futu_account_id"] == "777"
    assert out.payload["symbol"] == "9992.HK"
    assert out.diagnostics["matched_via"] == "order_lookup_by_acc_id"


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

    monkeypatch.setattr("scripts.futu_trade_detail_lookup.build_futu_gateway", lambda **kwargs: FakeGateway())
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

    monkeypatch.setattr("scripts.futu_trade_detail_lookup.build_futu_gateway", lambda **kwargs: FakeGateway())
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

    monkeypatch.setattr("scripts.futu_trade_detail_lookup.build_futu_gateway", lambda **kwargs: FakeGateway())
    out = enrich_trade_push_payload_with_account_id(
        {"order_id": "order-7", "deal_id": "deal-7"},
        host="127.0.0.1",
        port=11111,
        futu_account_ids=["888"],
    )

    assert out.payload["futu_account_id"] == "888"
    assert out.payload["symbol"] == "NVDA"
    assert out.diagnostics["matched_via"] == "order_lookup_by_acc_id"
