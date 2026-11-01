"""Chinese policy explanations, independent of report I/O."""
from __future__ import annotations

from collections.abc import Sequence


def format_close_advice_reason(*, recommendation: str, flags: Sequence[str]) -> str:
    if "invalid_economic_denominator" in flags:
        return "开仓净权利金、买回成本或资金分母无效，当前无法评估"
    if "invalid_economic_result" in flags:
        return "收益率计算结果无效，当前无法评估"
    if recommendation == "close":
        return "已净兑现至少 80% 权利金，剩余最高年化不超过 10%，可考虑买回平仓"
    if list(flags) == ["near_expiry_far_otm_hold"]:
        return "临近到期且 Delta 很低，倾向持有到期"
    if "expiry_hold_evidence_unavailable" in flags:
        return "临期交易日或 Delta 证据不足，当前无法排除持有到期条件"
    if recommendation == "not_evaluable":
        return "必要的持仓、日期、双边报价或手续费证据不完整，当前无法评估"
    return "价内状态、净兑现或剩余最高年化未达到提前止盈条件，继续持有"
