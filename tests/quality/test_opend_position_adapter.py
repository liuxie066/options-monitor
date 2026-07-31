from __future__ import annotations

import src.infrastructure.quality.opend_position_adapter as adapter_module
from src.infrastructure.quality.opend_position_adapter import (
    OpenDOptionPositionAdapter,
)


def _config() -> dict:
    return {
        "accounts": ["lx"],
        "account_settings": {
            "lx": {
                "type": "futu",
                "futu": {
                    "host": "127.0.0.1",
                    "port": 11111,
                    "account_id": "123456",
                    "trd_env": "REAL",
                },
            }
        },
    }


class _Gateway:
    def __init__(
        self,
        *,
        positions: list[dict],
        snapshots: dict[str, dict],
    ) -> None:
        self.positions = positions
        self.snapshots = snapshots
        self.snapshot_calls: list[list[str]] = []
        self.closed = False

    def get_positions(self, **_kwargs):  # noqa: ANN003, ANN201
        return list(self.positions)

    def get_trading_days(self, **_kwargs):  # noqa: ANN003, ANN201
        return [{"time": "2026-07-31", "trade_date_type": "WHOLE"}]

    def get_snapshot(self, codes):  # noqa: ANN001, ANN201
        batch = list(codes)
        self.snapshot_calls.append(batch)
        return [self.snapshots[code] for code in batch if code in self.snapshots]

    def close(self) -> None:
        self.closed = True


def test_adapter_enriches_missing_multipliers_from_batched_market_snapshot(
    monkeypatch,
) -> None:
    gateway = _Gateway(
        positions=[
            {
                "code": "US.NVDA260821P100000",
                "qty": -1,
                "position_side": "SHORT",
                "sec_type": "DRVT",
            },
            {
                "code": "HK.POP260828P145000",
                "qty": -2,
                "position_side": "SHORT",
                "sec_type": "DRVT",
            },
            {
                "code": "HK.TCH260731P440000",
                "qty": 0,
                "position_side": "SHORT",
                "sec_type": "DRVT",
            },
        ],
        snapshots={
            "US.NVDA260821P100000": {
                "code": "US.NVDA260821P100000",
                "option_contract_multiplier": 100,
            },
            "HK.POP260828P145000": {
                "code": "HK.POP260828P145000",
                "lot_size": 200,
            },
        },
    )
    monkeypatch.setattr(
        adapter_module,
        "build_ready_futu_gateway",
        lambda **_kwargs: gateway,
    )
    monkeypatch.setattr(
        adapter_module,
        "market_to_futu_trade_date_market",
        lambda market: market.upper(),
    )
    monkeypatch.setattr(adapter_module, "_MARKET_SNAPSHOT_BATCH_SIZE", 1)

    snapshot = OpenDOptionPositionAdapter().fetch(
        cfg=_config(),
        account="lx",
        market="us",
    )

    assert snapshot.complete is True
    assert [row.get("options_per_contract") for row in snapshot.rows] == [
        100.0,
        200.0,
        None,
    ]
    assert gateway.snapshot_calls == [
        ["HK.POP260828P145000"],
        ["US.NVDA260821P100000"],
    ]
    assert gateway.closed is True


def test_adapter_fails_closed_when_nonzero_position_multiplier_is_missing(
    monkeypatch,
) -> None:
    gateway = _Gateway(
        positions=[
            {
                "code": "HK.POP260828P145000",
                "qty": -1,
                "position_side": "SHORT",
                "sec_type": "DRVT",
            }
        ],
        snapshots={},
    )
    monkeypatch.setattr(
        adapter_module,
        "build_ready_futu_gateway",
        lambda **_kwargs: gateway,
    )
    monkeypatch.setattr(
        adapter_module,
        "market_to_futu_trade_date_market",
        lambda market: market.upper(),
    )

    snapshot = OpenDOptionPositionAdapter().fetch(
        cfg=_config(),
        account="lx",
        market="hk",
    )

    assert snapshot.complete is False
    assert snapshot.rows == []
    assert snapshot.error_code == "OPEND_OPTION_MULTIPLIER_EVIDENCE_INCOMPLETE"
    assert "1 non-zero option position" in str(snapshot.error_message)
    assert gateway.closed is True
