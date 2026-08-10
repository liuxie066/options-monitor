from __future__ import annotations

from typing import Any

from domain.domain.decision_state_fingerprint import canonical_sha256


# Stable wire identifier retained so existing decision-state fingerprints stay
# comparable after the generic module rename.
PORTFOLIO_SCOPE_SCHEMA = "options-monitor.position-advice.scope.v2"


def normalize_account_label(value: Any) -> str:
    account = str(value or "").strip().lower()
    if not account:
        raise ValueError("normalized account label is required")
    return account


def portfolio_scope_id(account: Any) -> str:
    normalized = normalize_account_label(account)
    return canonical_sha256(
        {
            "schema": PORTFOLIO_SCOPE_SCHEMA,
            "normalized_account_label": normalized,
        }
    )


__all__ = [
    "PORTFOLIO_SCOPE_SCHEMA",
    "normalize_account_label",
    "portfolio_scope_id",
]
