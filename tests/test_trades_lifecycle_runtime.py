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
