from __future__ import annotations

from src.application.webui_presenters import list_rows, to_row


def test_to_row_uses_broker_only_for_market_field() -> None:
    row = to_row(
        "us",
        {
            "symbol": "NVDA",
            "market": "legacy-market",
            "broker": "富途",
        },
        {},
    )

    assert row.market == "富途"


def test_list_rows_does_not_surface_legacy_market_only_items() -> None:
    def _try_load_config(_key: str):
        return (
            {
                "symbols": [
                    {"symbol": "AAPL", "market": "legacy-market"},
                    {"symbol": "NVDA", "broker": "富途"},
                ]
            },
            None,
        )

    rows = list_rows(config_keys=("us",), try_load_config=_try_load_config, to_row_fn=to_row)

    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["market"] is None
    assert rows[1]["symbol"] == "NVDA"
    assert rows[1]["market"] == "富途"
