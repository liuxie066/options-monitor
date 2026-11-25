"""Assignment inference and reconciled buyback pending cash (not redemption receipts)."""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from domain.domain.symbol_identity import OPTION_CODE_RE, canonical_symbol
from domain.domain.trade_contract_identity import contract_share_quantity


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = Decimal(str(value))
        return value if value.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def match_assignment_pending(*, balance, orders, terms, account_id, environment, today, order_fees=()):
    """Match complete, scoped filled orders against one denominated funds response."""
    currency = str(balance.get("currency") or "")
    pending = number(balance.get("pending_asset"))
    result = {"status": "unmatched", "reason": "assignment_amount_mismatch",
              "currency": currency, "pending_amount": float(pending) if pending is not None else None,
              "adjustment": 0.0, "order_ids": []}
    field = {"USD": "us_cash", "HKD": "hk_cash"}.get(currency)
    cash = number(balance.get(field)) if field else None
    if pending is None or pending < 0:
        return {**result, "reason": "pending_amount_unavailable"}
    if pending == 0:
        if any((number(balance.get(key)) or 0) < 0 for key in ("us_cash", "hk_cash", "cn_cash")):
            return {**result, "reason": "negative_cash_without_pending"}
        return {**result, "status": "none", "reason": "no_pending"}
    if cash is None or cash >= 0 or pending != -cash:
        return result
    # No default contract multiplier: terms come from the account's existing ledger.
    by_contract = {}
    for term in terms:
        if term.get("option_type", "put") != "put":
            continue
        identity = (canonical_symbol(term.get("symbol")), str(term.get("expiration_ymd")),
                    number(term.get("strike")))
        value = (canonical_symbol(term.get("symbol")), number(term.get("multiplier")))
        by_contract.setdefault(identity, set()).add(value)
    unique = {}
    for row in orders:
        if (str(row.get("acc_id", account_id)) != str(account_id)
                or str(row.get("trd_env", environment)) != environment):
            return {**result, "reason": "assignment_scope_mismatch"}
        key = str(row.get("order_id") or "")
        if not key or (key in unique and unique[key] != row):
            return {**result, "reason": "assignment_orders_invalid"}
        unique[key] = row
    filled = []
    for key, row in unique.items():
        try:
            at = datetime.fromisoformat(str(row.get("create_time")))
        except ValueError:
            continue
        if not today - timedelta(days=7) <= at.date() <= today:
            continue
        qty = number(row.get("qty"))
        if (row.get("order_status") != "FILLED_ALL" or row.get("currency") != currency
                or qty is None or qty <= 0 or number(row.get("dealt_qty")) != qty):
            continue
        filled.append((key, row, at.replace(microsecond=0)))
    pairs = []
    for key, row, at in filled:
        code = str(row.get("code") or "").upper()
        parsed = OPTION_CODE_RE.fullmatch(code)
        if (not parsed or parsed["cp"] != "P" or row.get("trd_side") != "BUY_BACK"
                or number(row.get("price")) != 0 or number(row.get("dealt_avg_price")) != 0):
            continue
        strike = Decimal(parsed["strike"]) / 1000
        expiration = f"20{parsed['yy']}-{parsed['mm']}-{parsed['dd']}"
        known = by_contract.get((canonical_symbol(code), expiration, strike), set())
        if len(known) != 1:
            continue
        symbol, multiplier = next(iter(known))
        try:
            shares = contract_share_quantity(row["dealt_qty"], multiplier)
        except (ValueError, TypeError):
            continue
        matches = [(stock_id, stock) for stock_id, stock, stock_at in filled
                   if stock_at == at and stock.get("trd_side") == "BUY"
                   and not OPTION_CODE_RE.fullmatch(str(stock.get("code") or ""))
                   and canonical_symbol(stock.get("code")) == symbol
                   and number(stock.get("dealt_qty")) == shares
                   and number(stock.get("price")) == strike
                   and number(stock.get("dealt_avg_price")) == strike]
        if len(matches) == 1:
            pairs.append((key, matches[0][0], strike * shares))
    stock_ids = [item[1] for item in pairs]
    if len(stock_ids) != len(set(stock_ids)):
        return {**result, "reason": "assignment_pair_ambiguous"}
    if not pairs or sum(item[2] for item in pairs) != pending:
        if any(str(fee.get("acc_id", account_id)) != str(account_id)
               or str(fee.get("trd_env", environment)) != environment for fee in order_fees):
            return {**result, "reason": "close_fee_scope_mismatch"}
        return _match_close_pending(balance, filled, terms, order_fees, result)
    return {**result, "status": "matched", "reason": "assignment_pending_inferred",
            "adjustment": float(pending),
            "order_ids": [key for pair in pairs for key in pair[:2]]}


def _match_close_pending(balance, filled, terms, fees, result):
    """Reconcile an independent pending asset before attributing it to buybacks."""
    values = [number(balance.get(key)) for key in
              ("total_assets", "market_val", "fund_assets", "cash", "pending_asset")]
    if any(value is None for value in values):
        return {**result, "reason": "close_fund_debit_unverified"}
    total, securities, fund, cash, pending = values
    # OpenD reports fund/cash to cents and securities to sub-cent precision.
    residual = total - securities - fund - cash
    if fund < 0 or residual.quantize(Decimal("0.01")) != pending:
        return {**result, "reason": "close_fund_debit_unverified"}
    closes = [(key, row) for key, row, _ in filled
              if row.get("trd_side") == "BUY_BACK"
              and OPTION_CODE_RE.fullmatch(str(row.get("code") or "").upper())
              and (number(row.get("dealt_avg_price")) or 0) > 0]
    if not closes:
        return result
    premium = Decimal(0)
    for _, row in closes:
        code = str(row["code"]).upper()
        parsed = OPTION_CODE_RE.fullmatch(code)
        expiry = f"20{parsed['yy']}-{parsed['mm']}-{parsed['dd']}"
        multipliers = {number(term.get("multiplier")) for term in terms
                       if canonical_symbol(term.get("symbol")) == canonical_symbol(code)
                       and str(term.get("expiration_ymd")) == expiry
                       and number(term.get("strike")) == Decimal(parsed["strike"]) / 1000
                       and term.get("option_type", "put") == ("put" if parsed["cp"] == "P" else "call")}
        if len(multipliers) != 1:
            return {**result, "reason": "close_contract_terms_unavailable"}
        try:
            shares = contract_share_quantity(row["dealt_qty"], next(iter(multipliers)))
        except (ValueError, TypeError):
            return {**result, "reason": "close_contract_terms_unavailable"}
        premium += number(row["dealt_avg_price"]) * shares
    if premium > pending:
        return result
    ids = [key for key, _ in closes]
    by_id = {}
    for fee in fees:
        key = str(fee.get("order_id") or "")
        amount = number(fee.get("fee_amount"))
        if key not in ids or key in by_id or amount is None or amount < 0:
            return {**result, "reason": "close_fees_invalid"}
        by_id[key] = amount
    if set(by_id) != set(ids):
        return {**result, "reason": "close_fees_unavailable", "order_ids": ids}
    if premium + sum(by_id.values()) != pending:
        return result
    return {**result, "status": "matched", "reason": "close_pending_reconciled",
            "adjustment": float(pending), "order_ids": ids}
