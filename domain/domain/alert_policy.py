from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
import json


@dataclass(frozen=True)
class SellPutAlertThresholds:
    """Sell Put 候选评级阈值。默认值与历史硬编码行为一致。"""

    high_annual: float = 0.20
    high_spread_max: float = 0.20
    medium_annual: float = 0.12

    def to_mapping(self) -> dict[str, float]:
        return {
            "high_annual": float(self.high_annual),
            "high_spread_max": float(self.high_spread_max),
            "medium_annual": float(self.medium_annual),
        }


@dataclass(frozen=True)
class SellCallAlertThresholds:
    """Sell Call 候选评级阈值。默认值与历史硬编码行为一致。"""

    high_annual: float = 0.10
    high_total: float = 0.15
    medium_annual: float = 0.06

    def to_mapping(self) -> dict[str, float]:
        return {
            "high_annual": float(self.high_annual),
            "high_total": float(self.high_total),
            "medium_annual": float(self.medium_annual),
        }


@dataclass(frozen=True)
class AlertPolicy:
    change_annual_threshold: float = 0.02
    sell_put: SellPutAlertThresholds = field(default_factory=SellPutAlertThresholds)
    sell_call: SellCallAlertThresholds = field(default_factory=SellCallAlertThresholds)

    def to_mapping(self) -> dict:
        return {
            "change_annual_threshold": float(self.change_annual_threshold),
            "sell_put": self.sell_put.to_mapping(),
            "sell_call": self.sell_call.to_mapping(),
        }


DEFAULT_ALERT_POLICY = AlertPolicy()


def _coerce_float(value, default: float) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _resolve_sell_put(raw: dict | None) -> SellPutAlertThresholds:
    src = raw or {}
    defaults = SellPutAlertThresholds()
    return SellPutAlertThresholds(
        high_annual=_coerce_float(src.get("high_annual"), defaults.high_annual),
        high_spread_max=_coerce_float(src.get("high_spread_max"), defaults.high_spread_max),
        medium_annual=_coerce_float(src.get("medium_annual"), defaults.medium_annual),
    )


def _resolve_sell_call(raw: dict | None) -> SellCallAlertThresholds:
    src = raw or {}
    defaults = SellCallAlertThresholds()
    return SellCallAlertThresholds(
        high_annual=_coerce_float(src.get("high_annual"), defaults.high_annual),
        high_total=_coerce_float(src.get("high_total"), defaults.high_total),
        medium_annual=_coerce_float(src.get("medium_annual"), defaults.medium_annual),
    )


def resolve_alert_policy(raw: dict | None = None) -> AlertPolicy:
    src = raw or {}
    defaults = DEFAULT_ALERT_POLICY
    change_annual_threshold = _coerce_float(
        src.get("change_annual_threshold"),
        defaults.change_annual_threshold,
    )
    sell_put = _resolve_sell_put(src.get("sell_put") if isinstance(src.get("sell_put"), dict) else None)
    sell_call = _resolve_sell_call(src.get("sell_call") if isinstance(src.get("sell_call"), dict) else None)
    return AlertPolicy(
        change_annual_threshold=change_annual_threshold,
        sell_put=sell_put,
        sell_call=sell_call,
    )


def load_alert_policy(policy_json: str | None, *, repo_base: Path, resolve_repo_path_fn) -> dict:
    if not policy_json:
        return DEFAULT_ALERT_POLICY.to_mapping()
    try:
        p = resolve_repo_path_fn(repo_base=repo_base, value=policy_json)
        if p.exists() and p.stat().st_size > 0:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return resolve_alert_policy(data).to_mapping()
    except Exception:
        pass
    return DEFAULT_ALERT_POLICY.to_mapping()
