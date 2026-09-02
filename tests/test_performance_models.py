from __future__ import annotations

from decimal import Decimal

import pytest

from domain.domain.performance.models import (
    FeeBasis,
    FeeComponent,
    FeeFact,
    quantize_money,
)


def test_actual_zero_fee_is_complete_but_legacy_missing_is_not_zero() -> None:
    actual = FeeFact(
        amount=0,
        basis=FeeBasis.ACTUAL,
        component=FeeComponent.OPTION_OPEN,
        source="broker",
        source_event_id="evt-1",
    )
    missing = FeeFact(
        amount=None,
        basis=FeeBasis.MISSING,
        component=FeeComponent.OPTION_OPEN,
        reason="legacy fee has no provenance",
        source_event_id="evt-2",
    )

    assert actual.amount == Decimal("0.000000")
    assert actual.is_complete is True
    assert missing.amount is None
    assert missing.is_complete is False


def test_estimated_fee_is_explicit_and_quantized() -> None:
    fee = FeeFact(
        amount="1.2345678",
        basis="estimated",
        component="option_close",
        source_event_id="evt-1",
    )

    assert fee.amount == Decimal("1.234568")
    assert fee.to_dict()["basis"] == "estimated"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"amount": 1, "basis": "missing", "component": "option_open", "source_event_id": "evt"},
        {"amount": None, "basis": "actual", "component": "option_open", "source_event_id": "evt"},
        {"amount": -1, "basis": "actual", "component": "option_open", "source_event_id": "evt"},
        {"amount": 1, "basis": "actual", "component": "option_open", "source_event_id": ""},
    ],
)
def test_invalid_fee_facts_are_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        FeeFact(**kwargs)


def test_quantize_money_uses_six_decimal_places() -> None:
    assert quantize_money("1.0000005") == Decimal("1.000001")
