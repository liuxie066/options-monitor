from __future__ import annotations

def _compute_cover(shares_total: int, shares_locked: int, multiplier: int):
    shares_avail = max(0, int(shares_total) - int(shares_locked))
    contracts_avail = shares_avail // int(multiplier)
    return shares_avail, contracts_avail


def test_sell_call_cover_capacity_basic_hk() -> None:
    # Tencent HK: multiplier 100
    shares_avail, contracts_avail = _compute_cover(shares_total=800, shares_locked=300, multiplier=100)
    assert shares_avail == 500
    assert contracts_avail == 5


def test_sell_call_cover_capacity_never_negative() -> None:
    shares_avail, contracts_avail = _compute_cover(shares_total=100, shares_locked=999, multiplier=100)
    assert shares_avail == 0
    assert contracts_avail == 0
