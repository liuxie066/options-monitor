from __future__ import annotations

from contextlib import nullcontext
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from src.infrastructure.futu_dataframe import futu_dataframe_records


@pytest.mark.parametrize("frame", [
    pd.DataFrame(),
    pd.DataFrame(index=[0, 1]),
    pd.DataFrame(columns=["code", "price"]),
    pd.DataFrame({"acc_id": [999000000000000001], "price": [1.125], "ready": [True]}),
    pd.DataFrame({"value": [np.int64(999000000000000001), np.float32(1.25), np.bool_(True), pd.NA]}, dtype=object),
    pd.DataFrame({"value": [np.datetime64("2026-10-11"), np.timedelta64(5, "s"), Decimal("1.125")]}, dtype=object),
    pd.DataFrame({"time": pd.to_datetime(["2026-10-11T01:00:00Z", None]), "price": [1.25, float("nan")]}),
    pd.DataFrame({"qty": pd.Series([1, None], dtype="Int64"), "code": pd.Series(["US.NVDA", None], dtype="string")}),
    pd.DataFrame([[1, 2]], columns=["qty", "qty"]),
])
def test_records_preserve_pandas_values_and_scalar_types(frame):
    with pytest.warns(UserWarning) if not frame.columns.is_unique else nullcontext():
        expected = frame.to_dict("records")
    with pytest.warns(UserWarning) if not frame.columns.is_unique else nullcontext():
        actual = futu_dataframe_records(frame)
    assert len(actual) == len(expected)
    for left, right in zip(actual, expected, strict=True):
        assert left.keys() == right.keys()
        for key, value in right.items():
            assert type(left[key]) is type(value)
            assert left[key] == value or left[key] is value or (pd.isna(left[key]) and pd.isna(value))


def test_records_keep_duplicate_index_rows():
    frame = pd.DataFrame([{"deal_id": "1"}, {"deal_id": "2"}], index=[0, 0])
    assert futu_dataframe_records(frame) == [{"deal_id": "1"}, {"deal_id": "2"}]


def test_records_keep_dataframe_compatible_transport():
    class Frame:
        def to_dict(self, *, orient):
            assert orient == "records"
            return [{"deal_id": "1"}]
    assert futu_dataframe_records(Frame()) == [{"deal_id": "1"}]
