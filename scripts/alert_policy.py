from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json


@dataclass(frozen=True)
class AlertPolicy:
    change_annual_threshold: float = 0.02

    def to_mapping(self) -> dict[str, float]:
        return {
            "change_annual_threshold": float(self.change_annual_threshold),
        }


DEFAULT_ALERT_POLICY = AlertPolicy()


def resolve_alert_policy(raw: dict | None = None) -> AlertPolicy:
    src = raw or {}
    defaults = DEFAULT_ALERT_POLICY
    try:
        change_annual_threshold = float(src.get("change_annual_threshold", defaults.change_annual_threshold))
    except Exception:
        change_annual_threshold = defaults.change_annual_threshold
    return AlertPolicy(
        change_annual_threshold=change_annual_threshold,
    )


def load_alert_policy(policy_json: str | None, *, repo_base: Path, resolve_repo_path_fn) -> dict[str, float]:
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
