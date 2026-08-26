from __future__ import annotations

from .current_decision_common import (
    Any,
    CURRENT_ASSIGNED_STOCK_SCHEMA,
    CurrentDecisionProjectionError,
    Decimal,
    Iterable,
    Mapping,
    Sequence,
    _decimal_text,
    _hash_without,
    _integer,
    _nonnegative_decimal_text,
    _optional_integer,
    _optional_text,
    _position_lot_fields,
    _sha256,
    _text,
    assigned_stock_fee_fact,
    canonical_sha256,
    hashlib,
)

from .current_decision_lifecycle import (
    _ASSIGNED_ALLOCATION_KEYS,
    _ASSIGNED_LINKAGE_BASES,
    _ASSIGNED_LOT_KEYS,
    _ASSIGNED_REVIEW_KEYS,
)

def _sale_fact_chain(event_ids: Iterable[str]) -> tuple[int, str]:
    chain = bytes(32)
    count = 0
    for event_id in event_ids:
        value = str(event_id or "").strip()
        if not value:
            raise CurrentDecisionProjectionError("assigned sale event id is required")
        chain = hashlib.sha256(
            chain + len(value.encode("utf-8")).to_bytes(4, "big") + value.encode("utf-8")
        ).digest()
        count += 1
    return count, chain.hex()

def compact_assigned_stock_view(
    report: Mapping[str, Any],
    *,
    account: str,
    current_position_lots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    if not account_value:
        raise CurrentDecisionProjectionError("assigned stock account is required")
    active_open_event_ids = {
        str(
            (item.get("fields") or {}).get("source_event_id")
            or (item.get("fields") or {}).get("open_event_id")
            or item.get("source_event_id")
            or ""
        ).strip()
        for item in current_position_lots
        if isinstance(item, Mapping)
        and isinstance(item.get("fields"), Mapping)
        and int((item.get("fields") or {}).get("contracts_open") or 0) > 0
    }
    lot_rows: list[dict[str, Any]] = []
    raw_lots = report.get("_all_assigned_stock_lots")
    if not isinstance(raw_lots, list):
        raw_lots = report.get("assigned_stock_lots")
    if not isinstance(raw_lots, list):
        raise CurrentDecisionProjectionError("assigned stock report lots are invalid")
    for raw in raw_lots:
        if not isinstance(raw, Mapping):
            raise CurrentDecisionProjectionError("assigned stock report lot is invalid")
        row = dict(raw)
        if str(row.get("account") or "").strip().lower() != account_value:
            continue
        shares_remaining = int(row.get("shares_remaining") or 0)
        if shares_remaining <= 0:
            continue
        sale_ids = [str(value).strip() for value in row.get("sale_event_ids") or []]
        sale_count, sale_chain = _sale_fact_chain(sale_ids)
        lot_rows.append(
            {
                "stock_lot_id": str(row.get("stock_lot_id") or "").strip(),
                "source_assignment_event_id": str(
                    row.get("source_assignment_event_id") or ""
                ).strip(),
                "source_option_lot_id": str(
                    row.get("source_option_lot_id") or ""
                ).strip()
                or None,
                "account": account_value,
                "broker": str(row.get("broker") or "").strip().lower(),
                "symbol": str(row.get("symbol") or "").strip().upper(),
                "currency": str(row.get("currency") or "").strip().upper(),
                "assigned_at_ms": int(row.get("assigned_at_ms") or 0),
                "shares_opened": int(row.get("shares_opened") or 0),
                "shares_remaining": shares_remaining,
                "assignment_price": _decimal_text(
                    row.get("assignment_price"),
                    field="assignment_price",
                ),
                "remaining_cost_basis": _decimal_text(
                    row.get("remaining_stock_cost_basis"),
                    field="remaining_cost_basis",
                ),
                "basis_policy": str(row.get("basis_policy") or "").strip(),
                "strategy": str(row.get("strategy") or "").strip().lower()
                or None,
                "leg_role": str(row.get("leg_role") or "").strip().lower()
                or None,
                "strategy_group_id": str(
                    row.get("strategy_group_id") or ""
                ).strip()
                or None,
                "yield_enhancement_mode": str(
                    row.get("yield_enhancement_mode") or ""
                ).strip().lower()
                or None,
                "source_option_leg_role": str(
                    row.get("source_option_leg_role") or ""
                ).strip().lower()
                or None,
                "sale_fact_count": sale_count,
                "sale_fact_chain_sha256": sale_chain,
            }
        )
    lot_rows.sort(key=lambda item: item["stock_lot_id"])

    allocations: list[dict[str, Any]] = []
    raw_allocations = report.get("covered_call_allocations") or []
    if not isinstance(raw_allocations, list):
        raise CurrentDecisionProjectionError("assigned stock allocations are invalid")
    for raw in raw_allocations:
        if not isinstance(raw, Mapping):
            raise CurrentDecisionProjectionError("assigned stock allocation is invalid")
        row = dict(raw)
        open_event_id = str(row.get("open_event_id") or "").strip()
        if (
            str(row.get("account") or "").strip().lower() != account_value
            or open_event_id not in active_open_event_ids
        ):
            continue
        allocations.append(
            {
                "open_event_id": open_event_id,
                "stock_lot_id": str(row.get("stock_lot_id") or "").strip(),
                "account": account_value,
                "broker": str(row.get("broker") or "").strip().lower(),
                "symbol": str(row.get("symbol") or "").strip().upper(),
                "currency": str(row.get("currency") or "").strip().upper(),
                "shares": int(row.get("shares") or 0),
                "start_at_ms": int(row.get("start_at_ms") or 0),
                "end_at_ms": None,
                "allocation_status": str(
                    row.get("allocation_status") or ""
                ).strip().lower(),
                "linkage_basis": str(row.get("linkage_basis") or "")
                .strip()
                .lower(),
            }
        )
    allocations.sort(
        key=lambda item: (
            item["open_event_id"],
            item["stock_lot_id"],
            item["start_at_ms"],
        )
    )

    review_rows: list[dict[str, Any]] = []
    quote_only = {"missing_quote", "covered_call_unrealized_missing"}
    raw_reviews = report.get("assigned_stock_review_rows") or []
    if not isinstance(raw_reviews, list):
        raise CurrentDecisionProjectionError("assigned stock reviews are invalid")
    for raw in raw_reviews:
        if not isinstance(raw, Mapping):
            raise CurrentDecisionProjectionError("assigned stock review is invalid")
        row = dict(raw)
        status = str(row.get("status") or "").strip().lower()
        row_account = str(row.get("account") or "").strip().lower()
        if status in quote_only or (row_account and row_account != account_value):
            continue
        review_rows.append(
            {
                "status": status,
                "event_id": str(row.get("event_id") or "").strip() or None,
                "stock_lot_id": str(row.get("stock_lot_id") or "").strip()
                or None,
                "stock_event_id": str(row.get("stock_event_id") or "").strip()
                or None,
                "account": account_value,
                "broker": str(row.get("broker") or "").strip().lower() or None,
                "symbol": str(row.get("symbol") or "").strip().upper() or None,
                "details_sha256": canonical_sha256(dict(row.get("details") or {})),
            }
        )
    review_rows.sort(
        key=lambda item: (
            item["status"],
            item["stock_lot_id"] or "",
            item["stock_event_id"] or "",
            item["event_id"] or "",
        )
    )
    sale_count = sum(int(item["sale_fact_count"]) for item in lot_rows)
    sale_chain = canonical_sha256(
        [
            {
                "stock_lot_id": item["stock_lot_id"],
                "sale_fact_count": item["sale_fact_count"],
                "sale_fact_chain_sha256": item["sale_fact_chain_sha256"],
            }
            for item in lot_rows
        ]
    )
    result = {
        "schema_version": CURRENT_ASSIGNED_STOCK_SCHEMA,
        "account": account_value,
        "lots": lot_rows,
        "covered_call_allocations": allocations,
        "review_facts": review_rows,
        "applied_sale_fact_count": sale_count,
        "applied_sale_fact_chain_sha256": sale_chain,
    }
    result["current_view_hash"] = canonical_sha256(result)
    return validate_assigned_stock_fact(result)

def empty_assigned_stock_fact(account: str) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    count, chain = 0, canonical_sha256([])
    result = {
        "schema_version": CURRENT_ASSIGNED_STOCK_SCHEMA,
        "account": account_value,
        "lots": [],
        "covered_call_allocations": [],
        "review_facts": [],
        "applied_sale_fact_count": count,
        "applied_sale_fact_chain_sha256": chain,
    }
    result["current_view_hash"] = canonical_sha256(result)
    return validate_assigned_stock_fact(result)

def validate_assigned_stock_fact(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "account",
        "lots",
        "covered_call_allocations",
        "review_facts",
        "applied_sale_fact_count",
        "applied_sale_fact_chain_sha256",
        "current_view_hash",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise CurrentDecisionProjectionError("assigned stock fact shape is invalid")
    item = dict(payload)
    if item["schema_version"] != CURRENT_ASSIGNED_STOCK_SCHEMA:
        raise CurrentDecisionProjectionError("assigned stock schema is invalid")
    account = _text(item["account"], field="assigned account", lower=True)
    lots = item["lots"]
    if not isinstance(lots, list):
        raise CurrentDecisionProjectionError("assigned stock lots must be a list")
    lot_ids: list[str] = []
    sale_count = 0
    sale_summaries: list[dict[str, Any]] = []
    for raw in lots:
        if not isinstance(raw, Mapping) or set(raw) != _ASSIGNED_LOT_KEYS:
            raise CurrentDecisionProjectionError("assigned stock lot shape is invalid")
        lot = dict(raw)
        lot_ids.append(_text(lot["stock_lot_id"], field="stock_lot_id"))
        _text(lot["source_assignment_event_id"], field="source_assignment_event_id")
        _optional_text(lot["source_option_lot_id"], field="source_option_lot_id")
        if _text(lot["account"], field="lot account", lower=True) != account:
            raise CurrentDecisionProjectionError("assigned stock lot account mismatch")
        _text(lot["broker"], field="lot broker", lower=True)
        _text(lot["symbol"], field="lot symbol", upper=True)
        _text(lot["currency"], field="lot currency", upper=True)
        _integer(lot["assigned_at_ms"], field="assigned_at_ms", minimum=1)
        opened = _integer(lot["shares_opened"], field="shares_opened", minimum=1)
        remaining = _integer(
            lot["shares_remaining"], field="shares_remaining", minimum=1
        )
        if remaining > opened:
            raise CurrentDecisionProjectionError("assigned shares exceed opened shares")
        for field in ("assignment_price", "remaining_cost_basis"):
            if _nonnegative_decimal_text(lot[field], field=field) != lot[field]:
                raise CurrentDecisionProjectionError(f"{field} is not canonical")
        _text(lot["basis_policy"], field="basis_policy")
        for field in (
            "strategy",
            "leg_role",
            "yield_enhancement_mode",
            "source_option_leg_role",
        ):
            _optional_text(lot[field], field=field, lower=True)
        _optional_text(lot["strategy_group_id"], field="strategy_group_id")
        lot_sale_count = _integer(lot["sale_fact_count"], field="sale_fact_count")
        lot_sale_chain = _sha256(
            lot["sale_fact_chain_sha256"], field="sale_fact_chain_sha256"
        )
        sale_count += lot_sale_count
        sale_summaries.append(
            {
                "stock_lot_id": lot["stock_lot_id"],
                "sale_fact_count": lot_sale_count,
                "sale_fact_chain_sha256": lot_sale_chain,
            }
        )
    if lot_ids != sorted(set(lot_ids)):
        raise CurrentDecisionProjectionError("assigned stock lots are not canonical")

    lot_id_set = set(lot_ids)
    allocations = item["covered_call_allocations"]
    if not isinstance(allocations, list):
        raise CurrentDecisionProjectionError("covered call allocations must be a list")
    allocation_keys: list[tuple[str, str, int]] = []
    for raw in allocations:
        if not isinstance(raw, Mapping) or set(raw) != _ASSIGNED_ALLOCATION_KEYS:
            raise CurrentDecisionProjectionError("covered call allocation shape is invalid")
        allocation = dict(raw)
        open_event_id = _text(allocation["open_event_id"], field="open_event_id")
        stock_lot_id = _text(allocation["stock_lot_id"], field="stock_lot_id")
        if stock_lot_id not in lot_id_set:
            raise CurrentDecisionProjectionError("covered call allocation lot is missing")
        if _text(allocation["account"], field="allocation account", lower=True) != account:
            raise CurrentDecisionProjectionError("covered call allocation account mismatch")
        _text(allocation["broker"], field="allocation broker", lower=True)
        _text(allocation["symbol"], field="allocation symbol", upper=True)
        _text(allocation["currency"], field="allocation currency", upper=True)
        _integer(allocation["shares"], field="allocation shares", minimum=1)
        start = _integer(allocation["start_at_ms"], field="allocation start", minimum=1)
        end = _optional_integer(allocation["end_at_ms"], field="allocation end", minimum=1)
        if end is not None and end < start:
            raise CurrentDecisionProjectionError("covered call allocation time is invalid")
        _text(allocation["allocation_status"], field="allocation_status", lower=True)
        linkage_basis = _text(
            allocation["linkage_basis"], field="linkage_basis", lower=True
        )
        if linkage_basis not in _ASSIGNED_LINKAGE_BASES:
            raise CurrentDecisionProjectionError(
                "covered call linkage basis is invalid"
            )
        allocation_keys.append((open_event_id, stock_lot_id, start))
    if allocation_keys != sorted(set(allocation_keys)):
        raise CurrentDecisionProjectionError("covered call allocations are not canonical")

    reviews = item["review_facts"]
    if not isinstance(reviews, list):
        raise CurrentDecisionProjectionError("assigned review facts must be a list")
    review_keys: list[tuple[str, str, str, str]] = []
    for raw in reviews:
        if not isinstance(raw, Mapping) or set(raw) != _ASSIGNED_REVIEW_KEYS:
            raise CurrentDecisionProjectionError("assigned review fact shape is invalid")
        review = dict(raw)
        status = _text(review["status"], field="review status", lower=True)
        event_id = _optional_text(review["event_id"], field="review event_id")
        lot_id = _optional_text(review["stock_lot_id"], field="review stock_lot_id")
        stock_event_id = _optional_text(
            review["stock_event_id"], field="review stock_event_id"
        )
        if _text(review["account"], field="review account", lower=True) != account:
            raise CurrentDecisionProjectionError("assigned review account mismatch")
        _optional_text(review["broker"], field="review broker", lower=True)
        _optional_text(review["symbol"], field="review symbol", upper=True)
        _sha256(review["details_sha256"], field="review details_sha256")
        review_keys.append((status, lot_id or "", stock_event_id or "", event_id or ""))
    if review_keys != sorted(set(review_keys)):
        raise CurrentDecisionProjectionError("assigned review facts are not canonical")

    if _integer(item["applied_sale_fact_count"], field="applied_sale_fact_count") != sale_count:
        raise CurrentDecisionProjectionError("assigned sale count mismatch")
    if (
        _sha256(
            item["applied_sale_fact_chain_sha256"],
            field="applied_sale_fact_chain_sha256",
        )
        != canonical_sha256(sale_summaries)
    ):
        raise CurrentDecisionProjectionError("assigned sale chain mismatch")
    supplied_hash = _sha256(item["current_view_hash"], field="current_view_hash")
    if supplied_hash != _hash_without(item, "current_view_hash"):
        raise CurrentDecisionProjectionError("assigned stock view hash mismatch")
    return item

def _assigned_fact_with(
    prior: Mapping[str, Any],
    *,
    lots: Sequence[Mapping[str, Any]] | None = None,
    allocations: Sequence[Mapping[str, Any]] | None = None,
    reviews: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    item = validate_assigned_stock_fact(prior)
    next_lots = sorted(
        (dict(row) for row in (lots if lots is not None else item["lots"])),
        key=lambda row: str(row["stock_lot_id"]),
    )
    next_allocations = sorted(
        (
            dict(row)
            for row in (
                allocations
                if allocations is not None
                else item["covered_call_allocations"]
            )
        ),
        key=lambda row: (
            str(row["open_event_id"]),
            str(row["stock_lot_id"]),
            int(row["start_at_ms"]),
        ),
    )
    next_reviews = sorted(
        (dict(row) for row in (reviews if reviews is not None else item["review_facts"])),
        key=lambda row: (
            str(row["status"]),
            str(row.get("stock_lot_id") or ""),
            str(row.get("stock_event_id") or ""),
            str(row.get("event_id") or ""),
        ),
    )
    sale_summaries = [
        {
            "stock_lot_id": row["stock_lot_id"],
            "sale_fact_count": row["sale_fact_count"],
            "sale_fact_chain_sha256": row["sale_fact_chain_sha256"],
        }
        for row in next_lots
    ]
    result = {
        **item,
        "lots": next_lots,
        "covered_call_allocations": next_allocations,
        "review_facts": next_reviews,
        "applied_sale_fact_count": sum(
            int(row["sale_fact_count"]) for row in next_lots
        ),
        "applied_sale_fact_chain_sha256": canonical_sha256(sale_summaries),
    }
    result["current_view_hash"] = _hash_without(result, "current_view_hash")
    return validate_assigned_stock_fact(result)

def _require_final_option_lot(
    current_position_lots: Sequence[Mapping[str, Any]],
    *,
    target_lot_id: str,
    expected_contracts_open: int,
    settlement: Mapping[str, Any],
) -> None:
    lots = _position_lot_fields(current_position_lots)
    fields = lots.get(target_lot_id)
    if fields is None:
        raise CurrentDecisionProjectionError(
            "assigned-stock transition final option lot is missing"
        )
    observed = _integer(
        fields.get("contracts_open"),
        field="final option contracts_open",
    )
    if observed != expected_contracts_open:
        raise CurrentDecisionProjectionError(
            "assigned-stock transition final option lot mismatch"
        )
    if (
        str(fields.get("account") or "").strip().lower() != settlement["account"]
        or str(fields.get("broker") or "").strip().lower() != settlement["broker"]
        or str(fields.get("symbol") or "").strip().upper() != settlement["symbol"]
        or str(fields.get("currency") or "").strip().upper() != settlement["currency"]
        or str(fields.get("option_type") or "").strip().lower()
        != settlement["option_type"]
        or str(fields.get("side") or "").strip().lower()
        != settlement["position_side"]
    ):
        raise CurrentDecisionProjectionError(
            "assigned-stock transition final option identity mismatch"
        )

def _settlement_transition(
    transition: Mapping[str, Any],
    *,
    expected_side: str,
) -> dict[str, Any]:
    required = {
        "kind",
        "terminal_event_id",
        "terminal_type",
        "option_type",
        "position_side",
        "strike",
        "target_option_lot_id",
        "expected_contracts_open_after",
        "contracts",
        "multiplier",
        "account",
        "broker",
        "symbol",
        "currency",
        "stock_settlement",
    }
    if not isinstance(transition, Mapping) or set(transition) != required:
        raise CurrentDecisionProjectionError(
            "assigned-stock settlement transition shape is invalid"
        )
    item = dict(transition)
    terminal_type = _text(item["terminal_type"], field="terminal_type", lower=True)
    option_type = _text(item["option_type"], field="option_type", lower=True)
    position_side = _text(
        item["position_side"], field="position_side", lower=True
    )
    side = {
        ("assignment", "put", "short"): "buy",
        ("assignment", "call", "short"): "sell",
        ("exercise", "call", "long"): "buy",
        ("exercise", "put", "long"): "sell",
    }.get((terminal_type, option_type, position_side))
    if side != expected_side:
        raise CurrentDecisionProjectionError(
            "assigned-stock settlement option binding is invalid"
        )
    stock = item["stock_settlement"]
    stock_keys = {"side", "shares", "price", "event_time_ms", "fees"}
    if not isinstance(stock, Mapping) or set(stock) != stock_keys:
        raise CurrentDecisionProjectionError(
            "assigned-stock settlement facts are invalid"
        )
    stock_row = dict(stock)
    if _text(stock_row["side"], field="stock side", lower=True) != expected_side:
        raise CurrentDecisionProjectionError("assigned-stock settlement side mismatch")
    contracts = _integer(item["contracts"], field="contracts", minimum=1)
    multiplier = _integer(item["multiplier"], field="multiplier", minimum=1)
    shares = _integer(stock_row["shares"], field="shares", minimum=1)
    if shares != contracts * multiplier:
        raise CurrentDecisionProjectionError(
            "assigned-stock settlement quantity mismatch"
        )
    stock_row["price"] = _nonnegative_decimal_text(
        stock_row["price"], field="stock price"
    )
    stock_row["fees"] = _nonnegative_decimal_text(stock_row["fees"], field="stock fees")
    stock_row["event_time_ms"] = _integer(
        stock_row["event_time_ms"], field="stock event_time_ms", minimum=1
    )
    item["terminal_event_id"] = _text(
        item["terminal_event_id"], field="terminal_event_id"
    )
    item["target_option_lot_id"] = _text(
        item["target_option_lot_id"], field="target_option_lot_id"
    )
    item["strike"] = _nonnegative_decimal_text(item["strike"], field="strike")
    if item["strike"] == "0":
        raise CurrentDecisionProjectionError("strike must be positive")
    item["expected_contracts_open_after"] = _integer(
        item["expected_contracts_open_after"],
        field="expected_contracts_open_after",
    )
    item["account"] = _text(item["account"], field="account", lower=True)
    item["broker"] = _text(item["broker"], field="broker", lower=True)
    item["symbol"] = _text(item["symbol"], field="symbol", upper=True)
    item["currency"] = _text(item["currency"], field="currency", upper=True)
    item["stock_settlement"] = stock_row
    return item

def update_assigned_stock_fact(
    prior: Mapping[str, Any],
    *,
    transition: Mapping[str, Any],
    current_position_lots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply one supported compact transition without reading lifetime history."""

    item = validate_assigned_stock_fact(prior)
    if not isinstance(transition, Mapping):
        raise CurrentDecisionProjectionError(
            "assigned-stock transition must be an object"
        )
    kind = str(transition.get("kind") or "").strip().lower()
    if kind == "exact_duplicate":
        if set(transition) != {"kind", "current_view_hash"} or (
            transition.get("current_view_hash") != item["current_view_hash"]
        ):
            raise CurrentDecisionProjectionError(
                "assigned-stock duplicate fact mismatch"
            )
        return item

    lots_by_id = {row["stock_lot_id"]: dict(row) for row in item["lots"]}
    if kind == "buy_settlement":
        if set(transition) != {
            "kind",
            "terminal_event_id",
            "terminal_type",
            "option_type",
            "position_side",
            "strike",
            "target_option_lot_id",
            "expected_contracts_open_after",
            "contracts",
            "multiplier",
            "account",
            "broker",
            "symbol",
            "currency",
            "stock_settlement",
            "strategy_fields",
        }:
            raise CurrentDecisionProjectionError(
                "assigned-stock buy transition shape is invalid"
            )
        settlement_input = dict(transition)
        strategy = settlement_input.pop("strategy_fields")
        settled = _settlement_transition(settlement_input, expected_side="buy")
        strategy_keys = {
            "strategy",
            "leg_role",
            "strategy_group_id",
            "yield_enhancement_mode",
            "source_option_leg_role",
        }
        if not isinstance(strategy, Mapping) or set(strategy) != strategy_keys:
            raise CurrentDecisionProjectionError(
                "assigned-stock strategy binding is invalid"
            )
        strategy_fields = {
            field: _optional_text(
                strategy[field],
                field=field,
                lower=field != "strategy_group_id",
            )
            for field in strategy_keys
        }
        if settled["account"] != item["account"]:
            raise CurrentDecisionProjectionError(
                "assigned-stock settlement account mismatch"
            )
        _require_final_option_lot(
            current_position_lots,
            target_lot_id=settled["target_option_lot_id"],
            expected_contracts_open=settled["expected_contracts_open_after"],
            settlement=settled,
        )
        stock = settled["stock_settlement"]
        stock_lot_id = f"assigned-stock-{settled['terminal_event_id']}"
        price = Decimal(str(stock["price"]))
        shares = int(stock["shares"])
        fees = Decimal(str(stock["fees"]))
        _count, empty_chain = _sale_fact_chain(())
        next_lot = {
            "stock_lot_id": stock_lot_id,
            "source_assignment_event_id": settled["terminal_event_id"],
            "source_option_lot_id": settled["target_option_lot_id"],
            "account": settled["account"],
            "broker": settled["broker"],
            "symbol": settled["symbol"],
            "currency": settled["currency"],
            "assigned_at_ms": stock["event_time_ms"],
            "shares_opened": shares,
            "shares_remaining": shares,
            "assignment_price": _decimal_text(price, field="assignment_price"),
            "remaining_cost_basis": _decimal_text(
                price * shares + fees,
                field="remaining_cost_basis",
            ),
            "basis_policy": "assignment_stock_cost_basis",
            **strategy_fields,
            "sale_fact_count": 0,
            "sale_fact_chain_sha256": empty_chain,
        }
        existing = lots_by_id.get(stock_lot_id)
        if existing is not None:
            if existing == next_lot:
                return item
            raise CurrentDecisionProjectionError(
                "assigned-stock deterministic lot conflict"
            )
        lots_by_id[stock_lot_id] = next_lot
        return _assigned_fact_with(item, lots=lots_by_id.values())

    if kind == "sell_settlement":
        settlement_input = dict(transition)
        stock_lot_id_raw = settlement_input.pop("stock_lot_id", None)
        settled = _settlement_transition(settlement_input, expected_side="sell")
        if stock_lot_id_raw is None:
            raise CurrentDecisionProjectionError(
                "assigned-stock sell transition shape is invalid"
            )
        stock_lot_id = _text(
            stock_lot_id_raw, field="stock_lot_id"
        )
        prior_lot = lots_by_id.get(stock_lot_id)
        if prior_lot is None or prior_lot["account"] != settled["account"]:
            raise CurrentDecisionProjectionError(
                "assigned-stock sell lot binding is missing"
            )
        _require_final_option_lot(
            current_position_lots,
            target_lot_id=settled["target_option_lot_id"],
            expected_contracts_open=settled["expected_contracts_open_after"],
            settlement=settled,
        )
        stock = settled["stock_settlement"]
        if (
            prior_lot["broker"] != settled["broker"]
            or prior_lot["symbol"] != settled["symbol"]
            or prior_lot["currency"] != settled["currency"]
            or int(stock["event_time_ms"]) < int(prior_lot["assigned_at_ms"])
        ):
            raise CurrentDecisionProjectionError(
                "assigned-stock sell identity or time mismatch"
            )
        remaining = int(prior_lot["shares_remaining"]) - int(stock["shares"])
        if remaining < 0:
            raise CurrentDecisionProjectionError(
                "assigned-stock sell exceeds remaining shares"
            )
        if remaining == 0:
            lots_by_id.pop(stock_lot_id)
        else:
            prior_basis = Decimal(str(prior_lot["remaining_cost_basis"]))
            prior_remaining = int(prior_lot["shares_remaining"])
            prior_lot["shares_remaining"] = remaining
            prior_lot["remaining_cost_basis"] = _decimal_text(
                round(float(prior_basis * remaining / prior_remaining), 6),
                field="remaining_cost_basis",
            )
            lots_by_id[stock_lot_id] = prior_lot
        active_open_event_ids = {
            str(fields.get("source_event_id") or fields.get("open_event_id") or "")
            for fields in _position_lot_fields(current_position_lots).values()
            if str(fields.get("status") or "").strip().lower() == "open"
            and int(fields.get("contracts_open") or 0) > 0
        }
        allocations = [
            row
            for row in item["covered_call_allocations"]
            if row["stock_lot_id"] in lots_by_id
            and row["open_event_id"] in active_open_event_ids
        ]
        return _assigned_fact_with(
            item,
            lots=lots_by_id.values(),
            allocations=allocations,
        )

    if kind == "assigned_stock_sale":
        required = {
            "kind",
            "stock_event_id",
            "stock_lot_id",
            "shares",
            "trade_time_ms",
            "lot_after",
        }
        if set(transition) != required:
            raise CurrentDecisionProjectionError(
                "assigned-stock sale transition shape is invalid"
            )
        stock_event_id = _text(
            transition["stock_event_id"], field="stock_event_id"
        )
        stock_lot_id = _text(
            transition["stock_lot_id"], field="stock_lot_id"
        )
        shares = _integer(transition["shares"], field="sale shares", minimum=1)
        trade_time_ms = _integer(
            transition["trade_time_ms"], field="sale trade_time_ms", minimum=1
        )
        prior_lot = lots_by_id.get(stock_lot_id)
        if prior_lot is None or trade_time_ms < int(prior_lot["assigned_at_ms"]):
            raise CurrentDecisionProjectionError(
                "assigned-stock sale lot is missing or backdated"
            )
        remaining = int(prior_lot["shares_remaining"]) - shares
        if remaining < 0:
            raise CurrentDecisionProjectionError(
                "assigned-stock sale exceeds remaining shares"
            )
        supplied_after = transition["lot_after"]
        if remaining == 0:
            if supplied_after is not None:
                raise CurrentDecisionProjectionError(
                    "closed assigned-stock sale after-view mismatch"
                )
            lots_by_id.pop(stock_lot_id)
        else:
            if not isinstance(supplied_after, Mapping):
                raise CurrentDecisionProjectionError(
                    "assigned-stock sale after-view is missing"
                )
            next_lot = dict(prior_lot)
            prior_basis = Decimal(str(prior_lot["remaining_cost_basis"]))
            prior_remaining = int(prior_lot["shares_remaining"])
            next_lot["shares_remaining"] = remaining
            next_lot["remaining_cost_basis"] = _decimal_text(
                round(float(prior_basis * remaining / prior_remaining), 6),
                field="remaining_cost_basis",
            )
            event_bytes = stock_event_id.encode("utf-8")
            next_lot["sale_fact_count"] = int(prior_lot["sale_fact_count"]) + 1
            next_lot["sale_fact_chain_sha256"] = hashlib.sha256(
                bytes.fromhex(str(prior_lot["sale_fact_chain_sha256"]))
                + len(event_bytes).to_bytes(4, "big")
                + event_bytes
            ).hexdigest()
            if dict(supplied_after) != next_lot:
                raise CurrentDecisionProjectionError(
                    "assigned-stock sale after-view mismatch"
                )
            lots_by_id[stock_lot_id] = next_lot
        if any(
            row["stock_lot_id"] == stock_lot_id
            and int(row["shares"]) > remaining
            for row in item["covered_call_allocations"]
        ):
            raise CurrentDecisionProjectionError(
                "assigned-stock sale conflicts with covered-call allocation"
            )
        return _assigned_fact_with(item, lots=lots_by_id.values())

    if kind == "covered_call_linkage":
        if set(transition) != {"kind", "allocations"}:
            raise CurrentDecisionProjectionError(
                "covered-call linkage transition shape is invalid"
            )
        allocations = transition["allocations"]
        if not isinstance(allocations, list):
            raise CurrentDecisionProjectionError(
                "covered-call linkage allocations must be a list"
            )
        active_open_events = {
            str(fields.get("source_event_id") or fields.get("open_event_id") or ""): fields
            for fields in _position_lot_fields(current_position_lots).values()
            if str(fields.get("status") or "").strip().lower() == "open"
            and int(fields.get("contracts_open") or 0) > 0
            and str(
                fields.get("source_event_id") or fields.get("open_event_id") or ""
            ).strip()
        }
        shares_by_stock_lot: dict[str, int] = {}
        shares_by_open_event: dict[str, int] = {}
        for raw in allocations:
            if not isinstance(raw, Mapping):
                raise CurrentDecisionProjectionError(
                    "covered-call linkage option identity is missing"
                )
            row = dict(raw)
            option = active_open_events.get(str(row.get("open_event_id") or ""))
            stock = lots_by_id.get(str(row.get("stock_lot_id") or ""))
            linkage_basis = _text(
                row.get("linkage_basis"), field="linkage_basis", lower=True
            )
            if option is None or stock is None or any(
                str(row.get(field) or "").strip().lower()
                != str(source.get(field) or "").strip().lower()
                for source in (stock, option)
                for field in ("account", "broker", "symbol", "currency")
            ) or (
                str(option.get("option_type") or "").strip().lower() != "call"
                or str(option.get("side") or "").strip().lower() != "short"
            ):
                raise CurrentDecisionProjectionError(
                    "covered-call linkage identity mismatch"
                )
            if linkage_basis not in _ASSIGNED_LINKAGE_BASES or (
                linkage_basis == "strategy_group"
                and (
                    not str(option.get("strategy_group_id") or "").strip()
                    or str(option.get("strategy_group_id") or "").strip()
                    != str(stock.get("strategy_group_id") or "").strip()
                )
            ):
                raise CurrentDecisionProjectionError(
                    "covered-call linkage basis mismatch"
                )
            shares = _integer(row.get("shares"), field="allocation shares", minimum=1)
            open_event_id = str(row["open_event_id"])
            shares_by_open_event[open_event_id] = (
                shares_by_open_event.get(open_event_id, 0) + shares
            )
            stock_lot_id = str(row["stock_lot_id"])
            shares_by_stock_lot[stock_lot_id] = (
                shares_by_stock_lot.get(stock_lot_id, 0) + shares
            )
        if any(
            shares > int(lots_by_id[stock_lot_id]["shares_remaining"])
            for stock_lot_id, shares in shares_by_stock_lot.items()
        ):
            raise CurrentDecisionProjectionError(
                "covered-call linkage stock quantity mismatch"
            )
        if any(
            shares
            > int(active_open_events[open_event_id].get("contracts_open") or 0)
            * int(active_open_events[open_event_id].get("multiplier") or 0)
            for open_event_id, shares in shares_by_open_event.items()
        ):
            raise CurrentDecisionProjectionError(
                "covered-call linkage option quantity mismatch"
            )
        return _assigned_fact_with(item, allocations=allocations)

    raise CurrentDecisionProjectionError(
        f"unsupported assigned-stock transition: {kind or 'missing'}"
    )

def _trade_event_field(event: Any, field: str) -> Any:
    return event.get(field) if isinstance(event, Mapping) else getattr(event, field, None)

def _trade_event_payload(event: Any) -> dict[str, Any]:
    value = _trade_event_field(event, "raw_payload")
    return dict(value) if isinstance(value, Mapping) else {}

def _trade_event_contract(event: Any) -> dict[str, Any]:
    value = _trade_event_field(event, "contract_key")
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    return dict(to_dict()) if callable(to_dict) else {}

def _positive_integral_number(value: Any, *, field: str) -> int:
    rendered = _decimal_text(value, field=field)
    assert rendered is not None
    number = Decimal(rendered)
    if number <= 0 or number != number.to_integral_value():
        raise CurrentDecisionProjectionError(f"{field} must be a positive integer")
    return int(number)

def _settlement_transition_from_event(
    event: Any,
    *,
    prior: Mapping[str, Any],
    current_position_lots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    event_type = str(_trade_event_field(event, "event_type") or "").strip().lower()
    if event_type not in {"assignment", "exercise"}:
        raise CurrentDecisionProjectionError("trade event is not a stock settlement")
    payload = _trade_event_payload(event)
    stock_raw = payload.get("stock_settlement")
    if not isinstance(stock_raw, Mapping) or not stock_raw:
        raise CurrentDecisionProjectionError(
            "assigned-stock settlement facts are missing"
        )
    stock = dict(stock_raw)
    contract = _trade_event_contract(event)
    target_lot_id = str(
        _trade_event_field(event, "target_lot_id")
        or payload.get("target_lot_id")
        or payload.get("record_id")
        or ""
    ).strip()
    final_fields = _position_lot_fields(current_position_lots).get(target_lot_id)
    if final_fields is None:
        raise CurrentDecisionProjectionError(
            "assigned-stock transition final option lot is missing"
        )
    event_time_ms = _integer(
        stock["event_time_ms"]
        if stock.get("event_time_ms") is not None
        else stock["trade_time_ms"]
        if stock.get("trade_time_ms") is not None
        else _trade_event_field(event, "event_time_ms"),
        field="stock event_time_ms",
        minimum=1,
    )
    opened_at_ms = _integer(
        final_fields.get("opened_at"),
        field="final option opened_at",
        minimum=1,
    )
    if event_time_ms < opened_at_ms:
        raise CurrentDecisionProjectionError(
            "assigned-stock settlement is backdated"
        )
    multiplier = _positive_integral_number(
        _trade_event_field(event, "multiplier"),
        field="multiplier",
    )
    shares = _integer(
        stock.get("shares") if stock.get("shares") is not None else stock.get("stock_qty"),
        field="stock shares",
        minimum=1,
    )
    expected_side = {
        ("assignment", "put", "short"): "buy",
        ("assignment", "call", "short"): "sell",
        ("exercise", "call", "long"): "buy",
        ("exercise", "put", "long"): "sell",
    }.get(
        (
            event_type,
            str(contract.get("option_type") or "").strip().lower(),
            str(contract.get("position_side") or contract.get("side") or "")
            .strip()
            .lower(),
        )
    )
    if expected_side is None:
        raise CurrentDecisionProjectionError(
            "assigned-stock settlement option binding is invalid"
        )
    stock_price = (
        stock.get("price")
        if stock.get("price") is not None
        else stock.get("stock_price")
    )
    raw_stock_fee = (
        stock.get("fees") if stock.get("fees") is not None else stock.get("fee")
    )
    if raw_stock_fee is not None:
        _nonnegative_decimal_text(raw_stock_fee, field="stock fees")
    fee_fact = assigned_stock_fee_fact(
        {
            **stock,
            "account": contract.get("account"),
            "broker": contract.get("broker"),
            "symbol": contract.get("underlying_symbol") or contract.get("symbol"),
            "currency": stock.get("currency") or _trade_event_field(event, "currency"),
            "shares": shares,
            "price": stock_price,
        },
        component=f"{event_type}_stock_fee",
        transaction_kind="assignment" if expected_side == "buy" else "sale",
    )
    common = {
        "terminal_event_id": _text(
            _trade_event_field(event, "event_id"),
            field="terminal_event_id",
        ),
        "terminal_type": event_type,
        "option_type": _text(
            contract.get("option_type"), field="option_type", lower=True
        ),
        "position_side": _text(
            contract.get("position_side") or contract.get("side"),
            field="position_side",
            lower=True,
        ),
        "strike": contract.get("strike"),
        "target_option_lot_id": target_lot_id,
        "expected_contracts_open_after": _integer(
            final_fields.get("contracts_open"),
            field="final option contracts_open",
        ),
        "contracts": _integer(
            _trade_event_field(event, "contracts"),
            field="contracts",
            minimum=1,
        ),
        "multiplier": multiplier,
        "account": _text(contract.get("account"), field="account", lower=True),
        "broker": _text(contract.get("broker"), field="broker", lower=True),
        "symbol": _text(
            contract.get("underlying_symbol") or contract.get("symbol"),
            field="symbol",
            upper=True,
        ),
        "currency": _text(
            stock.get("currency") or _trade_event_field(event, "currency"),
            field="currency",
            upper=True,
        ),
        "stock_settlement": {
            "side": _text(
                stock.get("side") or stock.get("stock_side"),
                field="stock side",
                lower=True,
            ),
            "shares": shares,
            "price": stock_price,
            "event_time_ms": event_time_ms,
            "fees": fee_fact["amount"],
        },
    }
    if expected_side == "buy":
        group_id = str(
            payload.get("strategy_group_id")
            or final_fields.get("strategy_group_id")
            or ""
        ).strip() or None
        source_role = str(
            payload.get("leg_role") or final_fields.get("leg_role") or ""
        ).strip().lower() or None
        return {
            "kind": "buy_settlement",
            **common,
            "strategy_fields": {
                "strategy": str(
                    payload.get("strategy") or final_fields.get("strategy") or ""
                ).strip().lower() or None,
                "leg_role": "assigned_stock" if group_id else None,
                "strategy_group_id": group_id,
                "yield_enhancement_mode": str(
                    payload.get("yield_enhancement_mode")
                    or final_fields.get("yield_enhancement_mode")
                    or ""
                ).strip().lower() or None,
                "source_option_leg_role": source_role,
            },
        }
    if expected_side != "sell":
        raise CurrentDecisionProjectionError(
            "assigned-stock settlement option binding is invalid"
        )

    explicit_stock_lot_id = next(
        (
            str(source.get(key) or "").strip()
            for source in (stock, payload)
            for key in ("stock_lot_id", "target_stock_lot_id", "source_stock_lot_id")
            if str(source.get(key) or "").strip()
        ),
        None,
    )
    group_id = str(
        payload.get("strategy_group_id")
        or final_fields.get("strategy_group_id")
        or ""
    ).strip()
    candidates = [
        row
        for row in validate_assigned_stock_fact(prior)["lots"]
        if row["account"] == common["account"]
        and row["broker"] == common["broker"]
        and row["symbol"] == common["symbol"]
        and row["currency"] == common["currency"]
        and int(row["shares_remaining"]) >= shares
        and int(row["assigned_at_ms"]) <= event_time_ms
        and (not group_id or row["strategy_group_id"] == group_id)
    ]
    if explicit_stock_lot_id is not None:
        candidates = [
            row for row in candidates if row["stock_lot_id"] == explicit_stock_lot_id
        ]
    if len(candidates) != 1:
        raise CurrentDecisionProjectionError(
            "assigned-stock sell lot binding is not unique"
        )
    return {
        "kind": "sell_settlement",
        **common,
        "stock_lot_id": candidates[0]["stock_lot_id"],
    }

def _sync_covered_call_allocations(
    prior: Mapping[str, Any],
    *,
    event_mutations: Sequence[tuple[Any, bool]],
    current_position_lots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    item = validate_assigned_stock_fact(prior)
    lots_by_id = {row["stock_lot_id"]: dict(row) for row in item["lots"]}
    explicit_by_open_event: dict[str, str] = {}
    for event, created in event_mutations:
        if not created or str(_trade_event_field(event, "event_type") or "").strip().lower() != "open":
            continue
        payload = _trade_event_payload(event)
        explicit = next(
            (
                str(payload.get(key) or "").strip()
                for key in ("stock_lot_id", "target_stock_lot_id", "source_stock_lot_id")
                if str(payload.get(key) or "").strip()
            ),
            None,
        )
        if explicit:
            explicit_by_open_event[
                _text(_trade_event_field(event, "event_id"), field="open_event_id")
            ] = explicit

    active_calls: list[tuple[str, str, dict[str, Any]]] = []
    for record_id, fields in _position_lot_fields(current_position_lots).items():
        if (
            str(fields.get("status") or "").strip().lower() == "open"
            and int(fields.get("contracts_open") or 0) > 0
            and str(fields.get("option_type") or "").strip().lower() == "call"
            and str(fields.get("side") or "").strip().lower() == "short"
        ):
            open_event_id = str(
                fields.get("source_event_id") or fields.get("open_event_id") or ""
            ).strip()
            if open_event_id:
                active_calls.append((open_event_id, record_id, fields))
    active_calls.sort(
        key=lambda row: (
            int(row[2].get("opened_at") or 0),
            row[0],
            row[1],
        )
    )

    remaining_by_stock = {
        lot_id: int(row["shares_remaining"]) for lot_id, row in lots_by_id.items()
    }
    identity_candidates: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    group_candidates: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for row in lots_by_id.values():
        identity = (
            row["account"],
            row["broker"],
            row["symbol"],
            row["currency"],
        )
        for index, key in (
            (identity_candidates, identity),
            (group_candidates, (*identity, str(row.get("strategy_group_id") or ""))),
        ):
            candidates = index.setdefault(key, [])
            candidates.append(row)
            candidates.sort(
                key=lambda item: (
                    int(item["assigned_at_ms"]),
                    str(item["stock_lot_id"]),
                )
            )
            del candidates[2:]
    remaining_by_call: dict[str, int] = {}
    for open_event_id, _record_id, fields in active_calls:
        remaining_by_call[open_event_id] = (
            _integer(fields.get("contracts_open"), field="covered call contracts")
            * _positive_integral_number(
                fields.get("multiplier"), field="covered call multiplier"
            )
        )
    prior_linkages: dict[str, list[dict[str, Any]]] = {}
    for row in item["covered_call_allocations"]:
        prior_linkages.setdefault(str(row["open_event_id"]), []).append(row)

    allocations: list[dict[str, Any]] = []
    for open_event_id, _record_id, fields in active_calls:
        required = remaining_by_call[open_event_id]
        explicit = explicit_by_open_event.get(open_event_id)
        group_id = str(fields.get("strategy_group_id") or "").strip()
        opened_at = _integer(
            fields.get("opened_at"),
            field="covered call opened_at",
            minimum=1,
        )
        identity = (
            str(fields.get("account") or "").strip().lower(),
            str(fields.get("broker") or "").strip().lower(),
            str(fields.get("symbol") or "").strip().upper(),
            str(fields.get("currency") or "").strip().upper(),
        )
        base_candidates = [
            row
            for row in identity_candidates.get(identity, ())
            if int(row["assigned_at_ms"]) <= opened_at
        ]
        prior_links = prior_linkages.get(open_event_id, ())
        linkage_basis = "stock_lot_id" if explicit is not None else "strategy_group"
        if explicit is None and prior_links:
            prior_bases = {str(row["linkage_basis"]) for row in prior_links}
            prior_stock_ids = {str(row["stock_lot_id"]) for row in prior_links}
            if prior_bases == {"stock_lot_id"}:
                if len(prior_stock_ids) != 1:
                    raise CurrentDecisionProjectionError(
                        "covered-call linkage identity is not unique"
                    )
                explicit = next(iter(prior_stock_ids))
                linkage_basis = "stock_lot_id"
            elif prior_bases == {"strategy_group"}:
                if len(prior_stock_ids) != 1:
                    raise CurrentDecisionProjectionError(
                        "covered-call linkage identity is not unique"
                    )
                prior_group_id = str(
                    lots_by_id[next(iter(prior_stock_ids))].get(
                        "strategy_group_id"
                    )
                    or ""
                ).strip()
                if not prior_group_id or prior_group_id != group_id:
                    raise CurrentDecisionProjectionError(
                        "covered-call linkage identity mismatch"
                    )
            else:
                raise CurrentDecisionProjectionError(
                    "covered-call linkage basis is ambiguous"
                )
        if explicit is not None:
            candidate = lots_by_id.get(explicit)
            candidates = (
                [candidate]
                if candidate is not None
                and tuple(
                    candidate[field]
                    for field in ("account", "broker", "symbol", "currency")
                )
                == identity
                and int(candidate["assigned_at_ms"]) <= opened_at
                else []
            )
        elif not group_id:
            if base_candidates:
                raise CurrentDecisionProjectionError(
                    "covered-call linkage identity is missing"
                )
            continue
        else:
            candidates = [
                row
                for row in group_candidates.get((*identity, group_id), ())
                if int(row["assigned_at_ms"]) <= opened_at
            ]
            if base_candidates and not candidates:
                raise CurrentDecisionProjectionError(
                    "covered-call linkage identity mismatch"
                )
            if not base_candidates:
                continue
        if len(candidates) != 1:
            raise CurrentDecisionProjectionError(
                "covered-call linkage identity is not unique"
            )
        stock_lot_id = str(candidates[0]["stock_lot_id"])
        if remaining_by_stock[stock_lot_id] < required:
            raise CurrentDecisionProjectionError(
                "covered-call linkage stock quantity mismatch"
            )
        allocations.append(
            {
                "open_event_id": open_event_id,
                "stock_lot_id": stock_lot_id,
                "account": str(fields.get("account") or "").strip().lower(),
                "broker": str(fields.get("broker") or "").strip().lower(),
                "symbol": str(fields.get("symbol") or "").strip().upper(),
                "currency": str(fields.get("currency") or "").strip().upper(),
                "shares": required,
                "start_at_ms": opened_at,
                "end_at_ms": None,
                "allocation_status": "explicit",
                "linkage_basis": linkage_basis,
            }
        )
        remaining_by_stock[stock_lot_id] -= required
        remaining_by_call[open_event_id] = 0
    updated = update_assigned_stock_fact(
        item,
        transition={"kind": "covered_call_linkage", "allocations": allocations},
        current_position_lots=current_position_lots,
    )
    return _assigned_fact_with(
        updated,
        reviews=[
            row
            for row in updated["review_facts"]
            if row["status"] != "covered_call_unallocated"
        ],
    )

def advance_assigned_stock_fact_for_trade_events(
    prior: Mapping[str, Any],
    *,
    event_mutations: Sequence[tuple[Any, bool]],
    current_position_lots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply created settlement events, then refresh bounded current CC links."""

    item = validate_assigned_stock_fact(prior)
    for event, created in event_mutations:
        if not created:
            continue
        event_type = str(_trade_event_field(event, "event_type") or "").strip().lower()
        if event_type not in {"assignment", "exercise"}:
            continue
        item = update_assigned_stock_fact(
            item,
            transition=_settlement_transition_from_event(
                event,
                prior=item,
                current_position_lots=current_position_lots,
            ),
            current_position_lots=current_position_lots,
        )
    return _sync_covered_call_allocations(
        item,
        event_mutations=event_mutations,
        current_position_lots=current_position_lots,
    )

_COMBO_GROUP_KEYS = frozenset(
    {
        "schema_version",
        "group_id",
        "identity_hash",
        "account",
        "symbol",
        "strategy",
        "original_contracts",
        "expected_roles",
        "active_member_bindings",
        "assigned_stock_lot_ids",
        "status",
        "reason_codes",
        "fact_sha256",
    }
)

_COMBO_MEMBER_KEYS = frozenset(
    {
        "record_id",
        "role",
        "open_event_id",
        "account",
        "symbol",
        "contracts_open",
    }
)
