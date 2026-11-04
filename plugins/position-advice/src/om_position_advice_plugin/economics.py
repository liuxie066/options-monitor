"""Small arithmetic shared by the pure position-advice policies."""
from __future__ import annotations


def remaining_max_annualized_return(
    *, remaining_cash: float, capital_basis: float, dte: int,
) -> float:
    """Simple annualized expiry-to-zero cash upper bound on validated inputs.

    The caller owns cash direction, the capital proxy, positive DTE and
    eligibility. Keep the operation order used by the original close policy.
    """
    return remaining_cash / capital_basis * 365 / dte
