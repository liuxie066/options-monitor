from types import SimpleNamespace

import pytest

from src.application.trades import lifecycle
from src.application.trades.normalizer import NormalizedTradeDeal


@pytest.mark.parametrize("writer", ["v2", "legacy"])
@pytest.mark.parametrize(
    ("currency", "financial_fields"),
    [
        ("HKD", {"fees": "12.50", "fee_provenance": {"basis": "actual", "source": "broker"}}),
        ("HKD", {"fee": "3.25", "fee_provenance": {"basis": "actual", "source": "broker"}}),
        ("HKD", {"fees": 0, "fee_provenance": {"basis": "actual", "source": "broker"}}),
        ("HKD", {"fees": "12.50"}),
        ("HKD", {}),
        (None, {}),
    ],
)
def test_assignment_pair_preserves_financial_evidence_at_writer_boundary(
    monkeypatch, writer, currency, financial_fields,
):
    """Exercise both assignment adapters without replacing their evidence builders."""
    deal = NormalizedTradeDeal(
        broker="富途", futu_account_id="test-hk", internal_account="lx",
        deal_id="stock-assignment", order_id="stock-order", symbol="0700.HK",
        option_type=None, side="buy", position_effect=None, contracts=100,
        price=440.0, strike=None, multiplier=None, multiplier_source=None,
        expiration_ymd=None, currency=currency, trade_time_ms=1_000,
        raw_payload={"deal_id": "stock-assignment", **financial_fields},
        asset_type="stock",
    )
    stock = lifecycle._evidence_from_deal(
        deal, evidence_type="stock_settlement_leg", case_id="assignment-case",
    )
    captured = {}

    class WriterBoundaryReached(Exception):
        pass

    def reconcile(_repo, *, evidence, **kwargs):
        if writer == "legacy":
            return SimpleNamespace(
                status="needs_review", reason_codes=("lifecycle_case_not_found",),
            )
        captured.update(evidence["stock_settlement"])
        raise WriterBoundaryReached

    def record(_repo, *, stock_settlement, **kwargs):
        assert writer == "legacy"
        captured.update(stock_settlement)
        raise WriterBoundaryReached

    monkeypatch.setattr(lifecycle, "reconcile_lifecycle_evidence", reconcile)
    monkeypatch.setattr(lifecycle, "record_lifecycle_assignment", record)
    with pytest.raises(WriterBoundaryReached):
        lifecycle._write_lifecycle_close_from_case(
            object(),
            case={
                "case_id": "assignment-case", "account": "lx", "symbol": "0700.HK",
                "option_type": "put", "position_side": "short", "strike": 440,
                "expiration_ymd": "2026-09-11", "contracts": 1, "multiplier": 100,
                "event_time_ms": 1_000,
            },
            decision_type="assignment",
            # A different option currency must not overwrite the stock's evidence.
            option_evidence={"source_event_id": "option-assignment", "raw": {"currency": "USD"}},
            stock_evidence=stock,
            apply_changes=True,
        )

    assert captured["futu_account_id"] == "test-hk"
    assert captured["order_id"] == "stock-order"
    assert captured["symbol"] == "0700.HK"
    assert captured["shares"] == 100
    assert captured["price"] == 440.0
    assert captured["side"] == "buy"
    assert {
        key: captured[key]
        for key in ("currency", "fees", "fee", "fee_provenance")
        if key in captured
    } == {**({"currency": currency} if currency else {}), **financial_fields}
    assert deal.raw_payload == {"deal_id": "stock-assignment", **financial_fields}
