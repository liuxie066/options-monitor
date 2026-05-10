from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SellPutRiskBand:
    max_otm_pct: float | None
    band: str
    risk_label: str

    def matches(self, otm_pct: float) -> bool:
        if self.max_otm_pct is None:
            return True
        return otm_pct < self.max_otm_pct


UNKNOWN_SELL_PUT_RISK_BAND = SellPutRiskBand(max_otm_pct=None, band="unknown", risk_label="未知")

DEFAULT_SELL_PUT_RISK_BANDS: tuple[SellPutRiskBand, ...] = (
    SellPutRiskBand(max_otm_pct=0.03, band="<3%", risk_label="激进"),
    SellPutRiskBand(max_otm_pct=0.07, band="3%-7%", risk_label="中性"),
    SellPutRiskBand(max_otm_pct=None, band=">=7%", risk_label="保守"),
)


def classify_sell_put_risk(otm_pct: float | None) -> SellPutRiskBand:
    if otm_pct is None:
        return UNKNOWN_SELL_PUT_RISK_BAND
    for rule in DEFAULT_SELL_PUT_RISK_BANDS:
        if rule.matches(float(otm_pct)):
            return rule
    return DEFAULT_SELL_PUT_RISK_BANDS[-1]
