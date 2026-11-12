"""Read-only OpenD market snapshots; execute on the quote host via Python stdin."""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import json
import math
import sys


def fetch_prices(symbols: list[str]) -> dict:
    from domain.domain.symbol_identity import canonical_symbol, futu_underlier_code
    from futu import RET_OK, OpenQuoteContext

    if not isinstance(symbols, list) or not symbols:
        raise ValueError("a nonempty symbol list is required")
    code_to_symbol = {}
    for raw in symbols:
        symbol = canonical_symbol(raw)
        code = futu_underlier_code(raw)
        if symbol is None or code is None:
            raise ValueError("invalid configured symbol")
        code_to_symbol[code] = symbol
    context = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, data = context.get_market_snapshot(list(code_to_symbol))
        if ret != RET_OK:
            raise RuntimeError("OpenD market snapshot request failed")
        prices = {}
        for _, row in data.iterrows():
            symbol = code_to_symbol.get(str(row["code"]))
            price = float(row["last_price"])
            if symbol is not None and math.isfinite(price) and price > 0:
                prices[symbol] = {"price": price}
        if not prices:
            raise ValueError("OpenD returned no usable prices")
    finally:
        context.close()
    return {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": "opend_market_snapshot", "prices": prices}


def main() -> None:
    with contextlib.redirect_stdout(sys.stderr):
        payload = fetch_prices(json.loads(sys.argv[1]))
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
