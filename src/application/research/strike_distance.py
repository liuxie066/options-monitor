"""Explicit offline inputs only: no provider, runtime, ledger or delivery access."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any

from domain.domain.engine import inspect_opening_candidate_distances
from domain.domain.symbol_identity import canonical_symbol

_MODES = {"csp": "put", "cc": "call", "wheel_put": "put", "wheel_call": "call"}


def parse_distance_thresholds(values: list[str]) -> dict[str, float]:
    thresholds = {}
    for value in values:
        try:
            strategy, text = value.split("=", 1)
            number = Decimal(text)
            valid = (number.is_finite() and 0 <= number <= 1
                     and number == number.quantize(Decimal("0.0000000001")))
        except (ValueError, InvalidOperation):
            valid = False
        if not valid or strategy not in _MODES or strategy in thresholds:
            raise ValueError("threshold must be unique strategy=fraction, 0..1 with at most 10 decimal places")
        thresholds[strategy] = float(number)
    return thresholds


def _pct(value: float | None) -> str:
    if value is None:
        return "不可用"
    return f"{Decimal(str(value)) * 100:.8f}".rstrip("0").rstrip(".") + "%"


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def analyze_strike_distances(bundle: dict[str, Any], thresholds: dict[str, float]) -> dict[str, Any]:
    """Return a sidecar in original order; thresholds never select or rank rows."""
    if (not isinstance(bundle, dict) or type(bundle.get("schema_version")) is not int
            or bundle["schema_version"] != 1 or bundle.get("source_kind") not in ("synthetic", "offline_redacted")
            or not isinstance(bundle.get("rows"), list)):
        raise ValueError("expected schema_version=1, source_kind=synthetic|offline_redacted and rows array")
    from datetime import datetime, timezone
    try:
        clock = datetime.fromisoformat(bundle["as_of"].replace("Z", "+00:00"))
        if clock.tzinfo is None:
            raise ValueError()
        clock = clock.astimezone(timezone.utc)
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        raise ValueError("as_of must be an explicit timezone-aware replay clock") from None
    thresholds = parse_distance_thresholds([f"{key}={value}" for key, value in thresholds.items()])
    seen: set[str] = set()
    observations = []
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    lines = ["## 行权价距离 · 离线实验", "", "仅描述所给样本；不改候选、排序、张数或通知，不代表指派概率。",
             "正值为 OTM、零为 ATM、负值为 ITM。Call 净距离仅指短期权腿，不是 CC 持股保本缓冲。",
             "波动标准化缓冲不可用：RV 质量依赖 #629/#630/#631；未计算 IV/RV 波动倍数。", "",
             "| 策略 / 合约 | 裸距离 | 净盈亏平衡距离 | 实验阈值 | 假设结果 | 放弃净权利金 |",
             "| --- | ---: | ---: | ---: | --- | ---: |"]
    for row in bundle["rows"]:
        if not isinstance(row, dict):
            raise ValueError("each row must be an object")
        identity, strategy = row.get("candidate_id"), row.get("strategy")
        if (not isinstance(identity, str) or not identity.strip() or identity in seen
                or not isinstance(strategy, str) or strategy not in _MODES
                or row.get("option_type") != _MODES[strategy]
                or not isinstance(row.get("symbol"), str) or canonical_symbol(row["symbol"]) != row["symbol"]
                or not isinstance(row.get("contract_symbol"), str) or not row["contract_symbol"]
                or type(row.get("rank")) is not int or row["rank"] < 1):
            raise ValueError("rows need unique candidate_id, explicit strategy/option_type, canonical symbol, contract_symbol and rank")
        seen.add(identity)
        metrics = inspect_opening_candidate_distances(row, mode=_MODES[strategy], now_utc=clock)
        threshold = thresholds.get(strategy)
        distance = metrics["strike_distance_pct"]
        decision = ("not_requested" if threshold is None else "unavailable" if distance is None
                    else "would_exclude" if distance < threshold else "retain")
        count = row.get("contracts")
        quantity_ok = type(count) is int and count >= 0
        amount = None
        if metrics["net_status"] == "ok" and quantity_ok:
            amount = Decimal(str(metrics["net_income"])) * count
        forgone = amount if decision == "would_exclude" else None
        observation = {"candidate_id": identity, "strategy": strategy, "rank": row["rank"],
                       "contracts": count, "metrics": metrics, "threshold": threshold,
                       "decision": decision, "forgone_net_premium": str(forgone) if forgone is not None else None,
                       "currency": row.get("currency")}
        observations.append(observation)
        currency = row.get("currency") if row.get("currency") in ("USD", "HKD") else "unknown"
        summary = totals.setdefault((strategy, currency), {
            "strategy": strategy, "currency": currency, "excluded_candidate_ids": [],
            "unassessed_candidate_ids": [], "missing_premium_candidate_ids": [],
            "known_forgone_net_premium": Decimal(0), "threshold": threshold,
        })
        if decision == "unavailable":
            summary["unassessed_candidate_ids"].append(identity)
        elif decision == "would_exclude":
            summary["excluded_candidate_ids"].append(identity)
            if forgone is None:
                summary["missing_premium_candidate_ids"].append(identity)
            else:
                summary["known_forgone_net_premium"] += forgone
        label = {"not_requested": "未设阈值", "unavailable": "不可判定", "would_exclude": "假设排除", "retain": "保留"}[decision]
        premium = f"{forgone} {currency}" if forgone is not None else "不可用" if decision == "would_exclude" else "—"
        lines.append(f"| {_cell(strategy)} / {_cell(row['contract_symbol'])} | {_pct(distance)} | "
                     f"{_pct(metrics['net_breakeven_distance_pct'])} | {_pct(threshold) if threshold is not None else '未设置'} | {label} | {premium} |")
    for summary in totals.values():
        summary["status"] = ("not_requested" if summary["threshold"] is None else "partial"
                             if summary["unassessed_candidate_ids"] or summary["missing_premium_candidate_ids"] else "complete")
        summary["known_forgone_net_premium"] = str(summary["known_forgone_net_premium"])
    lines.extend(["", "金额按策略及币种分别汇总；候选通常是替代方案，张数不能相加作为可执行组合。",
                  "负净权利金保持负值；未判定或缺费用/张数证据时，仅有已知小计，不能视为完整机会损失。",
                  "费用口径、现价时点、缺失原因及明细见 observations.metrics 与 baseline_rows。"])
    return {"schema_version": "strike_distance_shadow.v1", "source_kind": bundle["source_kind"],
            "as_of": bundle["as_of"], "thresholds": thresholds, "baseline_rows": deepcopy(bundle["rows"]),
            "observations": observations, "summaries": list(totals.values()), "markdown": "\n".join(lines)}
