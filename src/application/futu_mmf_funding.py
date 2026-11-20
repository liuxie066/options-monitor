"""Bind PM's verified money-fund adjustments to their source balances."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from math import isfinite


def apply_mmf_funding(context, funding, *, account, physical_account_id, environment):
    if funding.get("account") != account:
        raise ValueError("PM MMF funding account mismatch")
    if funding.get("status") == "none":
        return
    if funding.get("status") != "verified":
        raise ValueError("PM MMF funding is unverified")
    fingerprint = "sha256:" + sha256(str(physical_account_id).encode()).hexdigest()
    if (
        funding.get("account_fingerprint") != fingerprint
        or funding.get("environment") != environment
        or not funding.get("snapshot_id")
        or not context.get("cash_balance_reliable")
    ):
        raise ValueError("PM MMF funding scope mismatch")
    observed_at = datetime.fromisoformat(funding["observed_at"])
    if observed_at.tzinfo is None or not (
        timedelta(0) <= datetime.now(timezone.utc) - observed_at <= timedelta(minutes=15)
    ):
        raise ValueError("PM MMF funding balance is stale")

    def finite(value):
        if isinstance(value, bool):
            raise ValueError("Invalid PM MMF balance")
        result = float(value)
        if not isfinite(result):
            raise ValueError("Invalid PM MMF balance")
        return result

    native = funding["cash"]
    adjustments = funding["adjustments"]
    currencies = {"CNY", "HKD", "USD"}
    if set(native) != currencies or set(adjustments) - currencies:
        raise ValueError("Invalid PM MMF balance currencies")
    # Amount matching is a consistency check only. The actual calculation uses
    # PM's whole balance and adjustments together, never OM's separate balance.
    components = context["cash_components_by_currency"]
    sources = {"CNY": "cn_cash", "HKD": "hk_cash", "USD": "us_cash"}
    balances = {currency: finite(native[currency]) for currency in currencies}
    fund = finite(funding["fund_assets_cny"])
    if any(
        balances[currency] != finite(components[currency][source])
        for currency, source in sources.items()
    ) or abs(fund - finite(components["CNY"]["fund_assets"])) > 0.0051:
        raise ValueError("PM and current OpenD balances diverged")
    amounts = {currency: finite(adjustments.get(currency, 0)) for currency in currencies}
    if any(amount < 0 for amount in amounts.values()):
        raise ValueError("Invalid negative MMF adjustment")
    totals = {
        currency: balances[currency] + amounts[currency] + (fund if currency == "CNY" else 0)
        for currency in sorted(currencies)
    }
    if not all(isfinite(amount) for amount in totals.values()):
        raise ValueError("Invalid PM MMF totals")
    for currency, amount in totals.items():
        context["cash_components_by_currency"][currency]["mmf_in_transit"] = amounts[currency]
        context["cash_capacity_by_currency"][currency].update(
            amount=amount, source_observed_at=observed_at.isoformat(),
        )
    context["cash_by_currency"] = totals
    context["cash_source_observed_at"] = observed_at.isoformat()
    context["cash_source"] = "futu_cash_like_assets_with_pm_mmf"
