from __future__ import annotations

from typing import Any


def broker_external_event_key(deal: Any) -> str:
    deal_id = str(getattr(deal, "deal_id", "") or "").strip()
    account = str(getattr(deal, "internal_account", "") or "").strip().lower()
    futu_account_id = str(getattr(deal, "futu_account_id", "") or "").strip()
    if deal_id and account and futu_account_id:
        return f"futu:{account}:{futu_account_id}:{deal_id}"
    return ""
