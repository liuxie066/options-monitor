from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SellCallRiskBand:
    max_strike_above_spot_pct: float | None
    band: str
    risk_label: str

    def matches(self, strike_above_spot_pct: float) -> bool:
        if self.max_strike_above_spot_pct is None:
            return True
        return strike_above_spot_pct < self.max_strike_above_spot_pct


DEFAULT_SELL_CALL_RISK_BANDS: tuple[SellCallRiskBand, ...] = (
    SellCallRiskBand(max_strike_above_spot_pct=0.03, band="<3%", risk_label="激进"),
    SellCallRiskBand(max_strike_above_spot_pct=0.08, band="3%-8%", risk_label="中性"),
    SellCallRiskBand(max_strike_above_spot_pct=None, band=">=8%", risk_label="保守"),
)


def classify_sell_call_risk(strike_above_spot_pct: float) -> SellCallRiskBand:
    for rule in DEFAULT_SELL_CALL_RISK_BANDS:
        if rule.matches(strike_above_spot_pct):
            return rule
    return DEFAULT_SELL_CALL_RISK_BANDS[-1]
