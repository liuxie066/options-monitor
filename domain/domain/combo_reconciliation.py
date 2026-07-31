from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from domain.domain.decision_state_fingerprint import canonical_sha256


COMBO_PAIR_MATCH_SCHEMA = "combo_pair_match.v1"
COMBO_PAIR_INFERENCE_SCHEMA = "combo_pair_inference.v1"
COMBO_PAIR_ALGORITHM_VERSION = "combo_pair_matching.v1"
COMBO_PROPOSAL_TTL_MS = 24 * 60 * 60 * 1000

EXACT_DELIVERED_CANDIDATE = "exact_delivered_candidate"
EXACT_LIVE_CANDIDATE = "exact_live_candidate"
STRUCTURAL_ONLY = "structural_only"

PROPOSAL_READY = "proposal_ready"
AMBIGUOUS = "ambiguous"

_EVIDENCE_PRIORITY = {
    STRUCTURAL_ONLY: 0,
    EXACT_LIVE_CANDIDATE: 1,
    EXACT_DELIVERED_CANDIDATE: 2,
}


@dataclass(frozen=True)
class _Lot:
    record_id: str
    open_event_id: str
    account: str
    broker: str
    runtime_environment: str
    market: str
    market_date: str
    symbol: str
    option_type: str
    position_side: str
    contracts_original: int
    contracts_open: int
    currency: str
    multiplier: str
    strike: str
    expiration_ymd: str
    trade_time_ms: int
    strategy: str
    strategy_group_id: str
    leg_role: str

    @property
    def contract_key(self) -> dict[str, str]:
        return {
            "underlying_symbol": self.symbol,
            "option_type": self.option_type,
            "expiration_ymd": self.expiration_ymd,
            "strike": self.strike,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "open_event_id": self.open_event_id,
            "account": self.account,
            "broker": self.broker,
            "runtime_environment": self.runtime_environment,
            "market": self.market,
            "market_date": self.market_date,
            "symbol": self.symbol,
            "option_type": self.option_type,
            "position_side": self.position_side,
            "contracts_original": self.contracts_original,
            "contracts_open": self.contracts_open,
            "currency": self.currency,
            "multiplier": self.multiplier,
            "strike": self.strike,
            "expiration_ymd": self.expiration_ymd,
            "trade_time_ms": self.trade_time_ms,
            "strategy": self.strategy,
            "strategy_group_id": self.strategy_group_id,
            "leg_role": self.leg_role,
        }


@dataclass(frozen=True)
class _Exposure:
    candidate_exposure_id: str
    candidate_occurrence_id: str
    account: str
    market: str
    put_contract_key: tuple[str, str, str, str]
    call_contract_key: tuple[str, str, str, str]
    currency: str
    multiplier: str
    generated_at_ms: int
    valid_until_ms: int
    delivery_confirmed: bool


@dataclass(frozen=True)
class _Edge:
    inference_id: str
    put: _Lot
    call: _Lot
    structure_mode: str
    evidence_grade: str
    evidence: tuple[_Exposure, ...]


@dataclass
class _FlowArc:
    to: int
    reverse_index: int
    capacity: int
    cost: int
    inference_id: str | None = None


def match_post_trade_combo_pairs(
    *,
    lots: Iterable[Mapping[str, Any]],
    exposures: Iterable[Mapping[str, Any]] = (),
    forbidden_inference_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Return deterministic post-trade Combo pair proposals from immutable facts."""

    normalized_lots: list[_Lot] = []
    excluded_lots: list[dict[str, Any]] = []
    for raw in lots:
        normalized, reasons = _normalize_lot(raw)
        if normalized is None:
            excluded_lots.append(
                {
                    "record_id": _text(raw.get("record_id")),
                    "open_event_id": _text(raw.get("open_event_id")),
                    "reason_codes": sorted(reasons),
                }
            )
            continue
        normalized_lots.append(normalized)

    normalized_lots.sort(key=_lot_sort_key)
    normalized_exposures = sorted(
        (
            normalized
            for raw in exposures
            if (normalized := _normalize_exposure(raw)) is not None
        ),
        key=lambda item: item.candidate_exposure_id,
    )

    puts = [
        item
        for item in normalized_lots
        if item.option_type == "put" and item.position_side == "short"
    ]
    calls = [
        item
        for item in normalized_lots
        if item.option_type == "call" and item.position_side == "long"
    ]
    unsupported = [
        item
        for item in normalized_lots
        if item not in puts and item not in calls
    ]
    excluded_lots.extend(
        {
            "record_id": item.record_id,
            "open_event_id": item.open_event_id,
            "reason_codes": ["combo_leg_type_unsupported"],
        }
        for item in unsupported
    )

    forbidden = {
        str(item).strip()
        for item in forbidden_inference_ids
        if str(item).strip()
    }
    candidate_edges = sorted(
        (
            edge
            for put in puts
            for call in calls
            if (edge := _build_edge(put, call, normalized_exposures)) is not None
        ),
        key=lambda item: item.inference_id,
    )
    suppressed_edges = [
        edge.inference_id
        for edge in candidate_edges
        if edge.inference_id in forbidden
    ]
    edges = [
        edge
        for edge in candidate_edges
        if edge.inference_id not in forbidden
    ]
    edge_ids_by_record: dict[str, list[str]] = {}
    for edge in edges:
        edge_ids_by_record.setdefault(edge.put.record_id, []).append(edge.inference_id)
        edge_ids_by_record.setdefault(edge.call.record_id, []).append(edge.inference_id)

    optimum_score, optimum_edge_ids = _maximum_weight_matching(edges)
    forced_ids = {
        edge.inference_id
        for edge in edges
        if _maximum_weight_matching(edges, forbidden_ids={edge.inference_id})[0]
        < optimum_score
    }

    proposals: list[dict[str, Any]] = []
    for edge in edges:
        status = PROPOSAL_READY if edge.inference_id in forced_ids else AMBIGUOUS
        alternatives = sorted(
            (
                set(edge_ids_by_record.get(edge.put.record_id, ()))
                | set(edge_ids_by_record.get(edge.call.record_id, ()))
            )
            - {edge.inference_id}
        )
        proposals.append(
            _proposal_payload(
                edge,
                status=status,
                alternatives=alternatives,
                selected_in_one_optimum=edge.inference_id in optimum_edge_ids,
            )
        )

    connected_record_ids = {
        record_id for record_id, inference_ids in edge_ids_by_record.items() if inference_ids
    }
    waiting = [
        {
            "record_id": item.record_id,
            "open_event_id": item.open_event_id,
            "account": item.account,
            "market": item.market,
            "market_date": item.market_date,
            "symbol": item.symbol,
            "option_type": item.option_type,
            "position_side": item.position_side,
            "status": "waiting_for_counterpart",
        }
        for item in normalized_lots
        if item.record_id not in connected_record_ids
        and item in puts + calls
    ]
    waiting.sort(key=lambda item: (item["account"], item["record_id"]))
    excluded_lots.sort(
        key=lambda item: (
            str(item.get("record_id") or ""),
            str(item.get("open_event_id") or ""),
        )
    )
    return {
        "schema_version": COMBO_PAIR_MATCH_SCHEMA,
        "algorithm_version": COMBO_PAIR_ALGORITHM_VERSION,
        "eligible_lot_count": len(normalized_lots) - len(unsupported),
        "legal_edge_count": len(edges),
        "optimum_score": str(optimum_score),
        "proposal_ready_count": sum(
            1 for item in proposals if item["status"] == PROPOSAL_READY
        ),
        "ambiguous_count": sum(
            1 for item in proposals if item["status"] == AMBIGUOUS
        ),
        "waiting_for_counterpart": waiting,
        "excluded_lots": excluded_lots,
        "suppressed_terminal_inference_ids": sorted(suppressed_edges),
        "inferences": proposals,
    }


def _normalize_lot(raw: Mapping[str, Any]) -> tuple[_Lot | None, set[str]]:
    item = dict(raw or {})
    reasons: set[str] = set()
    record_id = _text(item.get("record_id"))
    open_event_id = _text(item.get("open_event_id"))
    account = _text(item.get("account"), lower=True)
    broker = _text(item.get("broker"), lower=True)
    runtime_environment = _text(item.get("runtime_environment"), lower=True)
    market = _text(item.get("market"), upper=True)
    market_date = _text(item.get("market_date"))
    symbol = _text(item.get("symbol"), upper=True)
    option_type = _text(item.get("option_type"), lower=True)
    position_side = _text(
        item.get("position_side") or item.get("side"), lower=True
    )
    currency = _text(item.get("currency"), upper=True)
    expiration_ymd = _text(
        item.get("expiration_ymd") or item.get("expiration")
    )
    strategy = _text(
        item.get("strategy") or item.get("strategy_type"), lower=True
    )
    strategy_group_id = _text(item.get("strategy_group_id"))
    leg_role = _text(item.get("leg_role"), lower=True)
    if not record_id:
        reasons.add("combo_lot_record_id_missing")
    if not open_event_id:
        reasons.add("combo_lot_open_event_id_missing")
    if not account:
        reasons.add("combo_lot_account_missing")
    if not broker:
        reasons.add("combo_lot_broker_missing")
    if not runtime_environment:
        reasons.add("combo_lot_runtime_environment_missing")
    if market not in {"US", "HK"}:
        reasons.add("combo_lot_market_invalid")
    if not _is_ymd(market_date):
        reasons.add("combo_lot_market_date_invalid")
    if not symbol:
        reasons.add("combo_lot_symbol_missing")
    if option_type not in {"put", "call"}:
        reasons.add("combo_lot_option_type_invalid")
    if position_side not in {"short", "long"}:
        reasons.add("combo_lot_position_side_invalid")
    if not currency:
        reasons.add("combo_lot_currency_missing")
    if not _is_ymd(expiration_ymd):
        reasons.add("combo_lot_expiration_invalid")
    contracts_original = _positive_int(
        item.get("contracts_original") or item.get("contracts")
    )
    contracts_open = _positive_int(item.get("contracts_open"))
    if contracts_original is None or contracts_open is None:
        reasons.add("combo_lot_contracts_invalid")
    elif contracts_original != contracts_open:
        reasons.add("combo_lot_not_fully_open")
    multiplier = _positive_decimal(item.get("multiplier"))
    strike = _positive_decimal(item.get("strike"))
    if multiplier is None:
        reasons.add("combo_lot_multiplier_invalid")
    if strike is None:
        reasons.add("combo_lot_strike_invalid")
    trade_time_ms = _positive_int(
        item.get("trade_time_ms") or item.get("opened_at_ms")
    )
    if trade_time_ms is None:
        reasons.add("combo_lot_trade_time_invalid")
    if strategy_group_id or leg_role or strategy == "combo_yield":
        reasons.add("combo_lot_already_grouped")
    if bool(item.get("effective_combo_identity")):
        reasons.add("combo_lot_effective_identity_present")
    if bool(item.get("confirmed_combo_claim")):
        reasons.add("combo_lot_confirmed_claim_present")
    if reasons:
        return None, reasons
    return (
        _Lot(
            record_id=record_id,
            open_event_id=open_event_id,
            account=account,
            broker=broker,
            runtime_environment=runtime_environment,
            market=market,
            market_date=market_date,
            symbol=symbol,
            option_type=option_type,
            position_side=position_side,
            contracts_original=int(contracts_original),
            contracts_open=int(contracts_open),
            currency=currency,
            multiplier=str(multiplier),
            strike=str(strike),
            expiration_ymd=expiration_ymd,
            trade_time_ms=int(trade_time_ms),
            strategy=strategy,
            strategy_group_id=strategy_group_id,
            leg_role=leg_role,
        ),
        set(),
    )


def _normalize_exposure(raw: Mapping[str, Any]) -> _Exposure | None:
    item = dict(raw or {})
    exposure_id = _text(item.get("candidate_exposure_id"))
    occurrence_id = _text(item.get("candidate_occurrence_id"))
    account = _text(item.get("account"), lower=True)
    market = _text(item.get("market"), upper=True)
    currency = _text(item.get("currency"), upper=True)
    multiplier = _positive_decimal(item.get("multiplier"))
    generated_at_ms = _positive_int(item.get("generated_at_ms"))
    valid_until_ms = _positive_int(item.get("valid_until_ms"))
    put_contract_key = _normalize_contract_key(
        item.get("put_contract_key"), expected_type="put"
    )
    call_contract_key = _normalize_contract_key(
        item.get("call_contract_key"), expected_type="call"
    )
    if (
        not exposure_id
        or not occurrence_id
        or not account
        or market not in {"US", "HK"}
        or not currency
        or multiplier is None
        or generated_at_ms is None
        or valid_until_ms is None
        or generated_at_ms > valid_until_ms
        or put_contract_key is None
        or call_contract_key is None
    ):
        return None
    return _Exposure(
        candidate_exposure_id=exposure_id,
        candidate_occurrence_id=occurrence_id,
        account=account,
        market=market,
        put_contract_key=put_contract_key,
        call_contract_key=call_contract_key,
        currency=currency,
        multiplier=str(multiplier),
        generated_at_ms=int(generated_at_ms),
        valid_until_ms=int(valid_until_ms),
        delivery_confirmed=bool(item.get("delivery_confirmed")),
    )


def _build_edge(
    put: _Lot,
    call: _Lot,
    exposures: Sequence[_Exposure],
) -> _Edge | None:
    if (
        put.account != call.account
        or put.broker != call.broker
        or put.runtime_environment != call.runtime_environment
        or put.market != call.market
        or put.market_date != call.market_date
        or put.symbol != call.symbol
        or put.contracts_original != call.contracts_original
        or put.currency != call.currency
        or put.multiplier != call.multiplier
        or Decimal(put.strike) >= Decimal(call.strike)
        or put.expiration_ymd > call.expiration_ymd
    ):
        return None
    structure_mode = (
        "same_expiry_pair"
        if put.expiration_ymd == call.expiration_ymd
        else "staggered_expiry_pair"
    )
    valid_evidence = tuple(
        item
        for item in exposures
        if _exposure_matches(item, put=put, call=call)
    )
    grade = STRUCTURAL_ONLY
    if valid_evidence:
        grade = (
            EXACT_DELIVERED_CANDIDATE
            if any(item.delivery_confirmed for item in valid_evidence)
            else EXACT_LIVE_CANDIDATE
        )
    inference_id = "combo-inference:v1:" + canonical_sha256(
        {
            "schema_version": COMBO_PAIR_INFERENCE_SCHEMA,
            "put_open_event_id": put.open_event_id,
            "call_open_event_id": call.open_event_id,
        }
    )
    return _Edge(
        inference_id=inference_id,
        put=put,
        call=call,
        structure_mode=structure_mode,
        evidence_grade=grade,
        evidence=valid_evidence,
    )


def _exposure_matches(exposure: _Exposure, *, put: _Lot, call: _Lot) -> bool:
    first_trade_ms = min(put.trade_time_ms, call.trade_time_ms)
    second_trade_ms = max(put.trade_time_ms, call.trade_time_ms)
    return (
        exposure.account == put.account
        and exposure.market == put.market
        and exposure.currency == put.currency
        and exposure.multiplier == put.multiplier
        and exposure.put_contract_key == _contract_key_tuple(put.contract_key)
        and exposure.call_contract_key == _contract_key_tuple(call.contract_key)
        and exposure.generated_at_ms <= first_trade_ms
        and second_trade_ms <= exposure.valid_until_ms
    )


def _proposal_payload(
    edge: _Edge,
    *,
    status: str,
    alternatives: list[str],
    selected_in_one_optimum: bool,
) -> dict[str, Any]:
    occurrence_ids = sorted(
        {item.candidate_occurrence_id for item in edge.evidence}
    )
    exposure_ids = sorted(
        {item.candidate_exposure_id for item in edge.evidence}
    )
    proposal_expires_at_ms = (
        max(edge.put.trade_time_ms, edge.call.trade_time_ms)
        + COMBO_PROPOSAL_TTL_MS
    )
    strategy_group_id = "combo-post-trade:v1:" + canonical_sha256(
        {
            "account": edge.put.account,
            "put_open_event_id": edge.put.open_event_id,
            "call_open_event_id": edge.call.open_event_id,
        }
    )
    snapshot = {
        "schema_version": COMBO_PAIR_INFERENCE_SCHEMA,
        "algorithm_version": COMBO_PAIR_ALGORITHM_VERSION,
        "inference_id": edge.inference_id,
        "put": edge.put.snapshot(),
        "call": edge.call.snapshot(),
        "evidence_grade": edge.evidence_grade,
        "candidate_occurrence_ids": occurrence_ids,
        "candidate_exposure_ids": exposure_ids,
        "status": status,
        "proposal_expires_at_ms": proposal_expires_at_ms,
        "alternative_inference_ids": alternatives,
    }
    return {
        "inference_id": edge.inference_id,
        "schema_version": COMBO_PAIR_INFERENCE_SCHEMA,
        "algorithm_version": COMBO_PAIR_ALGORITHM_VERSION,
        "account": edge.put.account,
        "symbol": edge.put.symbol,
        "market": edge.put.market,
        "market_date": edge.put.market_date,
        "broker": edge.put.broker,
        "runtime_environment": edge.put.runtime_environment,
        "structure_mode": edge.structure_mode,
        "put_record_id": edge.put.record_id,
        "put_open_event_id": edge.put.open_event_id,
        "call_record_id": edge.call.record_id,
        "call_open_event_id": edge.call.open_event_id,
        "contracts": edge.put.contracts_original,
        "evidence_grade": edge.evidence_grade,
        "candidate_occurrence_ids": occurrence_ids,
        "candidate_exposure_ids": exposure_ids,
        "input_snapshot_hash": canonical_sha256(snapshot),
        "status": status,
        "proposal_expires_at_ms": proposal_expires_at_ms,
        "strategy_group_id": strategy_group_id,
        "alternative_inference_ids": alternatives,
        "selected_in_one_optimum": bool(selected_in_one_optimum),
        "evidence": [
            {
                "candidate_exposure_id": item.candidate_exposure_id,
                "candidate_occurrence_id": item.candidate_occurrence_id,
                "delivery_confirmed": item.delivery_confirmed,
                "generated_at_ms": item.generated_at_ms,
                "valid_until_ms": item.valid_until_ms,
            }
            for item in edge.evidence
        ],
        "put_lot_snapshot": edge.put.snapshot(),
        "call_lot_snapshot": edge.call.snapshot(),
    }


def _maximum_weight_matching(
    edges: Sequence[_Edge],
    *,
    forbidden_ids: set[str] | None = None,
) -> tuple[int, set[str]]:
    forbidden = forbidden_ids or set()
    active = [edge for edge in edges if edge.inference_id not in forbidden]
    if not active:
        return 0, set()
    put_ids = sorted({edge.put.record_id for edge in active})
    call_ids = sorted({edge.call.record_id for edge in active})
    source = 0
    put_node = {record_id: index + 1 for index, record_id in enumerate(put_ids)}
    call_offset = 1 + len(put_ids)
    call_node = {
        record_id: call_offset + index for index, record_id in enumerate(call_ids)
    }
    sink = call_offset + len(call_ids)
    graph: list[list[_FlowArc]] = [[] for _ in range(sink + 1)]
    for record_id in put_ids:
        _add_flow_arc(graph, source, put_node[record_id], capacity=1, cost=0)
    for record_id in call_ids:
        _add_flow_arc(graph, call_node[record_id], sink, capacity=1, cost=0)
    base = min(len(put_ids), len(call_ids)) + 1
    edge_arcs: dict[str, _FlowArc] = {}
    for edge in active:
        priority = _EVIDENCE_PRIORITY[edge.evidence_grade]
        weight = (
            base * base + 1
            if priority == 2
            else base + 1
            if priority == 1
            else 1
        )
        arc = _add_flow_arc(
            graph,
            put_node[edge.put.record_id],
            call_node[edge.call.record_id],
            capacity=1,
            cost=-weight,
            inference_id=edge.inference_id,
        )
        edge_arcs[edge.inference_id] = arc

    total_cost = 0
    while True:
        distances: list[int | None] = [None] * len(graph)
        previous: list[tuple[int, int] | None] = [None] * len(graph)
        distances[source] = 0
        for _ in range(len(graph) - 1):
            changed = False
            for node, arcs in enumerate(graph):
                if distances[node] is None:
                    continue
                for arc_index, arc in enumerate(arcs):
                    if arc.capacity <= 0:
                        continue
                    candidate = int(distances[node]) + arc.cost
                    if distances[arc.to] is None or candidate < int(distances[arc.to]):
                        distances[arc.to] = candidate
                        previous[arc.to] = (node, arc_index)
                        changed = True
            if not changed:
                break
        if distances[sink] is None or int(distances[sink]) >= 0:
            break
        node = sink
        while node != source:
            prior = previous[node]
            if prior is None:
                raise RuntimeError("combo matching flow path is incomplete")
            from_node, arc_index = prior
            arc = graph[from_node][arc_index]
            arc.capacity -= 1
            graph[node][arc.reverse_index].capacity += 1
            node = from_node
        total_cost += int(distances[sink])
    selected = {
        inference_id
        for inference_id, arc in edge_arcs.items()
        if arc.capacity == 0
    }
    return -total_cost, selected


def _add_flow_arc(
    graph: list[list[_FlowArc]],
    source: int,
    target: int,
    *,
    capacity: int,
    cost: int,
    inference_id: str | None = None,
) -> _FlowArc:
    forward = _FlowArc(
        to=target,
        reverse_index=len(graph[target]),
        capacity=capacity,
        cost=cost,
        inference_id=inference_id,
    )
    reverse = _FlowArc(
        to=source,
        reverse_index=len(graph[source]),
        capacity=0,
        cost=-cost,
    )
    graph[source].append(forward)
    graph[target].append(reverse)
    return forward


def _normalize_contract_key(
    raw: Any,
    *,
    expected_type: str,
) -> tuple[str, str, str, str] | None:
    if not isinstance(raw, Mapping):
        return None
    item = dict(raw)
    symbol = _text(
        item.get("underlying_symbol") or item.get("symbol") or item.get("underlying"),
        upper=True,
    )
    option_type = _text(item.get("option_type"), lower=True) or expected_type
    expiration_ymd = _text(
        item.get("expiration_ymd") or item.get("expiration")
    )
    strike = _positive_decimal(item.get("strike"))
    if (
        not symbol
        or option_type != expected_type
        or not _is_ymd(expiration_ymd)
        or strike is None
    ):
        return None
    return symbol, option_type, expiration_ymd, str(strike)


def _contract_key_tuple(value: Mapping[str, Any]) -> tuple[str, str, str, str]:
    normalized = _normalize_contract_key(
        value,
        expected_type=_text(value.get("option_type"), lower=True),
    )
    if normalized is None:
        raise ValueError("normalized lot contract key became invalid")
    return normalized


def _lot_sort_key(item: _Lot) -> tuple[str, str, str, str, str]:
    return (
        item.account,
        item.market_date,
        item.symbol,
        item.record_id,
        item.open_event_id,
    )


def _text(value: Any, *, lower: bool = False, upper: bool = False) -> str:
    text = str(value or "").strip()
    if lower:
        return text.lower()
    if upper:
        return text.upper()
    return text


def _positive_int(value: Any) -> int | None:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    if not number.is_finite() or number <= 0 or number != number.to_integral():
        return None
    return int(number)


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number.normalize()


def _is_ymd(value: str) -> bool:
    if len(value) != 10 or value[4:5] != "-" or value[7:8] != "-":
        return False
    try:
        year, month, day = (int(part) for part in value.split("-"))
    except (TypeError, ValueError):
        return False
    return year >= 2000 and 1 <= month <= 12 and 1 <= day <= 31


__all__ = [
    "AMBIGUOUS",
    "COMBO_PAIR_ALGORITHM_VERSION",
    "COMBO_PAIR_INFERENCE_SCHEMA",
    "COMBO_PAIR_MATCH_SCHEMA",
    "COMBO_PROPOSAL_TTL_MS",
    "EXACT_DELIVERED_CANDIDATE",
    "EXACT_LIVE_CANDIDATE",
    "PROPOSAL_READY",
    "STRUCTURAL_ONLY",
    "match_post_trade_combo_pairs",
]
