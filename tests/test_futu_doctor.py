from __future__ import annotations

from src.application import futu_doctor


def test_required_option_fields_uses_gateway(monkeypatch) -> None:
    calls: list[list[str]] = []

    class _Gateway:
        def get_option_chain(self, **_kwargs):  # noqa: ANN003, ANN201
            return [{"code": "HK.TCH260828P440000"}]

        def get_snapshot(self, codes):  # noqa: ANN001, ANN201
            batch = list(codes)
            calls.append(batch)
            if batch == ["HK.09992"]:
                return [{"code": "HK.09992", "last_price": 145.0}]
            return [
                {
                    "code": batch[0],
                    "last_price": 1.0,
                    "bid_price": 0.9,
                    "ask_price": 1.1,
                    "volume": 10,
                    "option_open_interest": 20,
                    "option_implied_volatility": 0.3,
                    "option_delta": -0.2,
                    "option_contract_multiplier": 100,
                }
            ]

        def close(self) -> None:
            calls.append([])

    monkeypatch.setattr(
        futu_doctor,
        "build_ready_futu_quote_gateway",
        lambda **_kwargs: _Gateway(),
    )

    result = futu_doctor.check_required_option_fields(
        symbols=["9992.HK"],
        host="127.0.0.1",
        port=11111,
    )

    assert result["results"][0]["ok"] is True
    assert result["results"][0]["spot"] == 145.0
    assert calls == [
        ["HK.TCH260828P440000"],
        ["HK.09992"],
        [],
    ]
