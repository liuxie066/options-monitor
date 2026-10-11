"""Convert Futu SDK frames without per-column pandas row iteration."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from pandas.api.types import is_extension_array_dtype


def futu_dataframe_records(frame: Any) -> list[dict[str, Any]]:
    if not isinstance(frame, pd.DataFrame) or not len(frame.columns) or not frame.columns.is_unique:
        return frame.to_dict(orient="records")
    dtypes = frame.dtypes
    columns = list(frame.columns)
    object_columns = [i for i, dtype in enumerate(dtypes) if dtype == object or is_extension_array_dtype(dtype)]
    rows = frame.to_numpy(dtype=object).tolist()
    for row in rows:
        for i in object_columns:
            value = row[i]
            if value is pd.NA:
                row[i] = None
            elif isinstance(value, np.datetime64):
                row[i] = pd.Timestamp(value)
            elif isinstance(value, np.timedelta64):
                row[i] = pd.Timedelta(value)
            elif isinstance(value, np.integer):
                row[i] = int(value)
            elif isinstance(value, np.floating):
                row[i] = float(value)
            elif isinstance(value, np.bool_):
                row[i] = bool(value)
    return [dict(zip(columns, row)) for row in rows]
