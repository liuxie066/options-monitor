from __future__ import annotations

from datetime import date
from pathlib import Path


def test_fetch_symbol_explicit_expirations_override_limit_and_cache(monkeypatch, tmp_path: Path) -> None:
    import scripts.fetch_market_data_opend as mod

    requested_chain_dates: list[str] = []

    class _Gateway:
        def get_snapshot(self, codes):  # noqa: ANN001
            import pandas as pd

            rows = []
            for code in codes:
                if str(code).startswith("US."):
                    rows.append({"code": code, "last_price": 100.0})
                else:
                    rows.append(
                        {
                            "code": code,
                            "last_price": 1.0,
                            "bid_price": 0.9,
                            "ask_price": 1.1,
                            "option_contract_multiplier": 100,
                        }
                    )
            return pd.DataFrame(rows)

        def get_option_expiration_dates(self, code):  # noqa: ANN001
            import pandas as pd

            return pd.DataFrame([{"strike_time": "2026-04-29"}, {"strike_time": "2026-06-29"}])

        def get_option_chain(self, *, code, start=None, end=None, is_force_refresh=False):  # noqa: ANN001
            import pandas as pd

            requested_chain_dates.append(str(start))
            return pd.DataFrame(
                [
                    {
                        "code": f"{code}.{start}.P135",
                        "strike_time": str(start),
                        "strike_price": 135.0,
                        "option_type": "PUT",
                        "lot_size": 100,
                    },
                    {
                        "code": f"{code}.{start}.C200",
                        "strike_time": str(start),
                        "strike_price": 200.0,
                        "option_type": "CALL",
                        "lot_size": 100,
                    },
                ]
            )

    monkeypatch.setattr(mod, "build_ready_futu_gateway", lambda **kwargs: _Gateway())
    monkeypatch.setattr(mod, "retry_futu_gateway_call", lambda _name, fn, **kwargs: fn())
    monkeypatch.setattr(mod, "get_trading_date", lambda market: date(2026, 4, 28))
    monkeypatch.setattr(mod, "get_spot_opend", lambda gateway, code: 100.0)
    monkeypatch.setattr(
        mod,
        "_load_chain_cache",
        lambda path: {
            "asof_date": "2026-04-28",
            "underlier_code": "US.NVDA",
            "expirations_all": ["2026-05-28"],
            "rows": [{"strike_time": "2026-05-28", "code": "US.NVDA.2026-05-28.P135"}],
        },
    )

    payload = mod.fetch_symbol(
        "NVDA",
        limit_expirations=1,
        base_dir=tmp_path,
        explicit_expirations=["2026-04-29", "2026-06-29"],
        option_types="put,call",
        chain_cache=True,
    )

    expirations = sorted({str(row.get("expiration")) for row in (payload.get("rows") or [])})
    assert requested_chain_dates == ["2026-04-29", "2026-06-29"]
    assert expirations == ["2026-04-29", "2026-06-29"]
