from __future__ import annotations

import pytest

from src.application.trades.lifecycle_runtime import (
    _registry_contract_metadata,
)


@pytest.mark.parametrize(
    ("code", "symbol", "market"),
    [
        ("HK.TCH260731P440000", "0700.HK", "HK"),
        ("HK.POP260828P145000", "9992.HK", "HK"),
        ("US.NVDA260821P100000", "NVDA", "US"),
    ],
)
def test_registry_contract_metadata_compares_canonical_underlier_identity(
    code: str,
    symbol: str,
    market: str,
) -> None:
    metadata = _registry_contract_metadata(
        {"code": code},
        lifecycle_case={"symbol": symbol},
    )

    assert metadata["market"] == market
    assert metadata["contract_class"] == "standard_equity_option"


def test_registry_contract_metadata_rejects_real_underlier_conflict() -> None:
    with pytest.raises(
        ValueError,
        match="broker option code conflicts with lifecycle contract",
    ):
        _registry_contract_metadata(
            {"code": "HK.TCH260731P440000"},
            lifecycle_case={"symbol": "9992.HK"},
        )


def test_due_reconciliation_keeps_complete_source_account_id_set(monkeypatch) -> None:
    import src.application.trades.lifecycle_runtime as mod

    captured: dict = {}

    def build_collector(**kwargs):
        captured.update(kwargs)
        return object()

    def reconcile(_repo, **kwargs):
        captured["reconcile"] = kwargs
        return {"status": "ok"}

    monkeypatch.setattr(mod, "build_settlement_observation_collector", build_collector)
    monkeypatch.setattr(mod, "reconcile_due_lifecycle_cases", reconcile)

    result = mod.reconcile_due_lifecycle_cases_for_source(
        object(),
        source={"account": "lx", "futu_account_ids": ["1001", "1002"]},
        broker_gateway=object(),
        quote_gateway=object(),
        now_ms=123,
        apply_changes=False,
    )

    assert result == {"status": "ok"}
    assert captured["futu_account_ids"] == ["1001", "1002"]
    assert captured["reconcile"]["account"] == "lx"
