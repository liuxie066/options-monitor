"""Parity against the original anchored-band algorithm, including stable ties."""
from __future__ import annotations

from copy import deepcopy
import math
import random
from typing import Any

import pytest

from domain.domain.engine import candidate_engine as engine


def _reference_bands(rows, *, period_return_fn, tie_key, near_return_threshold):
    remaining = list(enumerate(rows))
    ranked: list[dict[str, Any]] = []
    while remaining:
        usable = [
            value
            for _index, row in remaining
            if (value := period_return_fn(row)) is not None
        ]
        if not usable:
            ranked.extend(
                row
                for _index, row in sorted(
                    remaining,
                    key=lambda item: (tie_key(item[1]), item[0]),
                )
            )
            break
        band_max = max(usable)
        floor = band_max - near_return_threshold
        band = [
            (index, row)
            for index, row in remaining
            if (value := period_return_fn(row)) is not None
            and value >= floor
        ]
        ranked.extend(
            row
            for _index, row in sorted(
                band,
                key=lambda item: (tie_key(item[1]), item[0]),
            )
        )
        band_indices = {index for index, _row in band}
        remaining = [item for item in remaining if item[0] not in band_indices]
    return ranked


@pytest.mark.parametrize("mode,profile", [
    ("put", "current_tie_break"), ("put", "without_concentration"),
    ("put", "concentration_first"), ("put", "option_market_concentration"),
    ("call", "current_tie_break"),
])
@pytest.mark.parametrize("threshold", [0.0, 0.002, 0.01])
def test_public_ranking_matches_original_bands(monkeypatch, mode, profile, threshold):
    rng = random.Random(137)
    floor = 0.01 - threshold
    returns = [None, float("nan"), float("inf"), -0.01, 0.0, 0.01,
               floor, math.nextafter(floor, -math.inf), math.nextafter(floor, math.inf)]
    rows = [
        {"symbol": f"S{rng.randrange(8)}", "contract_symbol": f"C{i % 17}",
         "period_net_return": rng.choice(returns),
         "net_assignment_discount_pct": rng.choice([0.1, 0.2]),
         "strike_upside_margin_pct": rng.choice([0.1, 0.2]),
         "spread_ratio": rng.choice([None, 0.02]),
         "open_interest": rng.choice([None, 0, 100]),
         "symbol_concentration_after": rng.choice([None, 0.1, 0.2]),
         "option_market_concentration_after": rng.choice([None, 0.1, 0.2])}
        for i in range(150)
    ]
    # Duplicate identities and repeated objects must preserve input-position ties.
    rows.extend([rows[0], dict(rows[1])])
    before = deepcopy(rows)
    kwargs = dict(mode=mode, sell_put_ranking_profile=profile, near_return_threshold=threshold)
    actual = engine.rank_candidate_rows(rows, **kwargs)
    with monkeypatch.context() as context:
        context.setattr(engine, "_rank_return_bands", _reference_bands)
        expected = engine.rank_candidate_rows(rows, **kwargs)
    assert [id(row) for row in actual] == [id(row) for row in expected]
    assert repr(rows) == repr(before)


@pytest.mark.parametrize("returns", [[], [None] * 10, [0.1] * 10,
                                    [i * 0.003 for i in range(1000)]])
def test_return_extraction_is_once_per_row(returns):
    calls = []
    rows = [{"value": value} for value in returns]

    def period_return(row):
        calls.append(row)
        return row["value"]

    result = engine._rank_return_bands(rows, period_return_fn=period_return,
                                       tie_key=lambda row: (), near_return_threshold=0.002)
    assert len(calls) == len(rows)
    assert len(result) == len(rows)
