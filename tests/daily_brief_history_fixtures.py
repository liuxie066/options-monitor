"""Synthetic delivery history for offline integrity and timing comparisons."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from src.application import daily_decision_brief_repository as repository
from test_daily_decision_brief_repository_v2 import _action, _brief


def seed_delivery_history(base: Path, *, days: int = 35) -> Path:
    """Create valid historical sources and ~2.5 MB of frozen envelopes, without I/O providers."""
    collected: dict = {}
    delivery_path = base / "output_accounts/lx/state/daily_decision_brief_delivery.US.json"
    for offset in range(days):
        market_date = (date(2026, 7, 20) - timedelta(days=offset)).isoformat()
        actions = [_action(symbol=f"FIX{i:03}") for i in range(24)]
        brief = _brief(run_id=f"offline-{market_date}", market_date=market_date, actions=actions)
        persisted = repository.persist_daily_decision_brief_success(base=base, brief=brief)
        for hour in (10, 11, 12, 13):
            prepared = repository.prepare_daily_decision_brief_delivery(
                base=base, account="lx", market="US", market_trading_date=market_date,
                run_id=brief["run_id"], delivery_kind="fixed_report", source_kind="successful_brief",
                source_digest=persisted["current_brief_digest"], revision=persisted["current_revision"],
                scheduled_target_market=f"{market_date}T{hour:02}:00:00-04:00",
                candidate_identities=persisted["current_candidate_identities"],
                rendered_message=("Offline option candidate facts. " * 540),
                prepared_at_utc=f"{market_date}T14:00:02+00:00",
            )
            delivery_path = prepared["paths"]["delivery"]
        raw = json.loads(delivery_path.read_text())
        collected.update(raw["days"])
        # Keep construction linear; all prior days are restored before observation.
        delivery_path.unlink()
    raw["days"] = collected
    delivery_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    assert repository.read_daily_decision_brief_delivery_state(
        base=base, account="lx", market="US",
    )["available"]
    return delivery_path
