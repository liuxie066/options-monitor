from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping

from domain.domain.combo_identity import (
    FUNDING_PUT_ROLES,
    PARTICIPATION_CALL_ROLES,
    validate_combo_identity,
)
from domain.domain.ledger.identity import ContractKey
from domain.domain.option_position_identity import normalize_broker
from domain.domain.symbol_identity import canonical_symbol, symbol_currency
from src.application.ai_decision_advice.evidence_store import (
    EvidenceIndex,
    content_fingerprint,
)
from src.application.ai_decision_advice.projection import project_all_candidates
from src.application.ledger.combo_membership import (
    validate_combo_group_membership,
)
from src.application.prepared_option_positions_context import (
    cny_per_currency_rates_from_option_context,
)
from src.application.prepared_portfolio_distribution import (
    PreparedPortfolioDistribution,
)


FACT_REGISTRY_SCHEMA = "ai_decision_advice_fact_registry.v1"
_REASON_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_:-]{0,95}$")
_SEMANTIC_HASH_OMITTED_KEYS = frozenset(
    {
        "evidence_as_of",
        "frozen_at",
        "last_checked_at",
        "last_success_at",
        "observed_at_utc",
        "source_observed_at",
    }
)


class FrozenInputError(ValueError):
    """A supposedly verified input cannot be frozen without guessing."""


@dataclass(frozen=True)
class FrozenInputs:
    """Model-visible frozen bundles and their independent content hashes."""

    # Keep the ``portfolio`` attribute until S4/S6 switch all consumers; its
    # value is the new PM distribution view, not the retired holdings shape.
    candidates: dict[str, Any]
    portfolio: dict[str, Any]
    option_positions: dict[str, Any]
    external_evidence: dict[str, Any]
    candidate_snapshot_hash: str
    portfolio_context_hash: str
    option_positions_hash: str
    external_evidence_hash: str
    external_evidence_run_id: str | None
    projections: dict[str, Any] = field(default_factory=dict)
    fact_registry: dict[str, Any] = field(default_factory=dict)
    portfolio_distribution_hash: str = ""
    projection_hash: str = ""
    fact_registry_hash: str = ""

    @property
    def portfolio_distribution(self) -> dict[str, Any]:
        return self.portfolio

    def input_bindings(self) -> dict[str, Any]:
        return {
            "candidate_snapshot_hash": self.candidate_snapshot_hash,
            "portfolio_distribution_hash": (
                self.portfolio_distribution_hash
                or self.portfolio_context_hash
            ),
            "option_positions_hash": self.option_positions_hash,
            "fact_registry_hash": self.fact_registry_hash,
            "external_evidence_hash": self.external_evidence_hash,
            "external_evidence_run_id": self.external_evidence_run_id,
        }


@dataclass(frozen=True)
class _PortfolioFreeze:
    model: dict[str, Any]
    total_value_cny: float | None
    shares_by_symbol: dict[str, float]
    searchable_symbols: tuple[str, ...]


@dataclass(frozen=True)
class _OptionFreeze:
    model: dict[str, Any]
    projection_rows: tuple[dict[str, Any], ...]
    underlying_symbols: tuple[str, ...]


def freeze_candidates(
    snapshot: Mapping[str, Any],
    *,
    market: str,
) -> dict[str, Any]:
    """Freeze the complete accepted SP/CC pool without re-ranking it."""

    market_norm = str(market or "").strip().upper()
    ranked = snapshot.get("ranked_candidates")
    if not isinstance(ranked, list):
        raise FrozenInputError("candidate snapshot ranked_candidates is invalid")
    families: dict[str, list[dict[str, Any]]] = {
        "sell_put": [],
        "covered_call": [],
    }
    seen_ids: set[str] = set()
    ranks_by_family: dict[str, list[int]] = {
        "sell_put": [],
        "covered_call": [],
    }
    for item in ranked:
        if not isinstance(item, Mapping):
            raise FrozenInputError("candidate snapshot row is invalid")
        facts = item.get("facts")
        if not isinstance(facts, Mapping):
            raise FrozenInputError("candidate snapshot facts are invalid")
        mode = str(item.get("strategy_mode") or "").strip().lower()
        family = {"put": "sell_put", "call": "covered_call"}.get(mode)
        if family is None:
            raise FrozenInputError("candidate snapshot strategy mode is unsupported")
        candidate_id = str(item.get("candidate_id") or "").strip()
        rank = _positive_integer(item.get("rank"))
        symbol = canonical_symbol(facts.get("symbol"))
        if not candidate_id or candidate_id in seen_ids:
            raise FrozenInputError("candidate identity is missing or duplicated")
        if rank is None or symbol is None:
            raise FrozenInputError("candidate rank or symbol is invalid")
        seen_ids.add(candidate_id)
        ranks_by_family[family].append(rank)

        option_type = str(facts.get("option_type") or mode).strip().lower()
        if option_type != mode:
            raise FrozenInputError("candidate option type and strategy mode mismatch")
        currency = str(
            facts.get("currency") or symbol_currency(symbol) or ""
        ).strip().upper()
        row = {
            "candidate_id": candidate_id,
            "rank": rank,
            "symbol": symbol,
            "option_type": option_type,
            "strike": _canonical_optional_positive_number(
                facts.get("strike")
            ),
            "expiry": _optional_date(
                _first_present(
                    facts,
                    "expiry",
                    "expiration_ymd",
                    "expiration",
                )
            ),
            "multiplier": _canonical_optional_positive_number(
                facts.get("multiplier")
            ),
            "currency": currency or None,
            "dte": _canonical_optional_nonnegative_number(facts.get("dte")),
            "delta": _canonical_optional_number(facts.get("delta")),
            "period_net_return": _canonical_optional_number(
                _first_present(
                    facts,
                    "period_net_return_on_cash_basis",
                    "period_net_return",
                )
            ),
            "annualized_gate": _canonical_optional_number(
                _first_present(
                    facts,
                    "annualized_net_return_on_cash_basis",
                    "annualized_net_premium_return",
                )
            ),
            "spread_ratio": _canonical_optional_nonnegative_number(
                facts.get("spread_ratio")
            ),
            "open_interest": _canonical_optional_nonnegative_number(
                facts.get("open_interest")
            ),
            "volume": _canonical_optional_nonnegative_number(
                facts.get("volume")
            ),
            "implied_volatility": _canonical_optional_nonnegative_number(
                facts.get("implied_volatility")
            ),
            "term_matched_rv": _canonical_optional_nonnegative_number(
                facts.get("term_matched_rv")
            ),
            "iv_rv_ratio": _canonical_optional_nonnegative_number(
                facts.get("iv_rv_ratio")
            ),
            "earnings_evidence_status": (
                str(facts.get("earnings_evidence_status"))
                if facts.get("earnings_evidence_status") is not None
                else None
            ),
            "earnings_has_event": (
                facts.get("earnings_has_event")
                if isinstance(facts.get("earnings_has_event"), bool)
                else None
            ),
        }
        families[family].append(row)

    for family, ranks in ranks_by_family.items():
        if ranks != list(range(1, len(ranks) + 1)):
            raise FrozenInputError(
                f"candidate family {family} rank sequence is invalid"
            )
    return {"market": market_norm, **families}


def freeze_portfolio_distribution(
    prepared: PreparedPortfolioDistribution | Mapping[str, Any] | None,
    *,
    expected_run_id: str | None = None,
    expected_account: str | None = None,
    expected_account_config_sha256: str | None = None,
    unavailable_reason: str = "portfolio_unavailable",
) -> dict[str, Any]:
    """Return only the PM distribution fields allowed in model input."""

    return _freeze_portfolio_distribution(
        prepared,
        expected_run_id=expected_run_id,
        expected_account=expected_account,
        expected_account_config_sha256=expected_account_config_sha256,
        unavailable_reason=unavailable_reason,
    ).model


def freeze_portfolio(
    prepared: PreparedPortfolioDistribution | Mapping[str, Any] | None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compatibility name for the PM-only serializer (no legacy adaptation)."""

    return freeze_portfolio_distribution(prepared, **kwargs)


def freeze_option_positions(
    context: Mapping[str, Any] | None,
    *,
    candidate_symbols: Iterable[str] = (),
    expected_run_id: str | None = None,
    expected_account: str | None = None,
    expected_account_config_sha256: str | None = None,
    unavailable_reason: str = "option_positions_unavailable",
) -> dict[str, Any]:
    """Aggregate a verified prepared option payload and redact private fields."""

    return _freeze_option_positions(
        context,
        candidate_symbols=candidate_symbols,
        expected_run_id=expected_run_id,
        expected_account=expected_account,
        expected_account_config_sha256=expected_account_config_sha256,
        unavailable_reason=unavailable_reason,
    ).model


def freeze_external_evidence(
    index: EvidenceIndex,
    *,
    symbols: Iterable[str],
) -> dict[str, Any]:
    """Freeze latest evidence for the exact account-relevant public symbols."""

    items: list[dict[str, Any]] = []
    for raw_symbol in sorted(set(symbols)):
        symbol = canonical_symbol(raw_symbol) or str(raw_symbol).strip().upper()
        coverage_ref = f"coverage:{symbol}"
        view = index.view_for(symbol)
        if view is None:
            items.append(
                {
                    "symbol": symbol,
                    "coverage_ref": coverage_ref,
                    "coverage": "no_evidence",
                    "unavailable_reason": "no_evidence",
                    "last_checked_at": None,
                    "last_success_at": None,
                    "evidence": [],
                }
            )
            continue
        evidence_rows: list[dict[str, Any]] = []
        for row in view.evidence:
            fingerprint = str(row.get("content_fingerprint") or "") or (
                content_fingerprint(row.get("url"), row.get("claim"))
            )
            source = row.get("source")
            source_row = source if isinstance(source, Mapping) else {}
            evidence_ref = (
                f"evidence:{_short_hash({'symbol': symbol, 'fingerprint': fingerprint})}"
            )
            evidence_rows.append(
                {
                    "ref": evidence_ref,
                    "topic": row.get("topic"),
                    "claim": row.get("claim"),
                    "event_status": row.get("event_status"),
                    "event_time": row.get("event_time"),
                    "source": {
                        "title": source_row.get("title"),
                        "publisher": source_row.get("publisher"),
                        "url": source_row.get("url") or row.get("url"),
                        "published_at": source_row.get("published_at"),
                    },
                }
            )
        evidence_rows.sort(key=lambda item: str(item["ref"]))
        items.append(
            {
                "symbol": symbol,
                "coverage_ref": coverage_ref,
                "coverage": view.coverage,
                "unavailable_reason": view.unavailable_reason,
                "last_checked_at": view.last_checked_at,
                "last_success_at": view.last_success_at,
                "evidence": evidence_rows,
            }
        )
    return {
        "evidence_as_of": index.evidence_as_of,
        "frozen_at": index.frozen_at,
        "index_hash": index.index_hash(),
        "symbols": items,
    }


def build_fact_registry(
    *,
    candidates: Mapping[str, Any],
    portfolio: Mapping[str, Any],
    option_positions: Mapping[str, Any],
    projections: Mapping[str, Any],
    external_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the only fact-ID namespace that model output may cite."""

    facts: dict[str, dict[str, Any]] = {}

    def add(
        fact_id: str,
        *,
        kind: str,
        scope: str,
        support_class: str,
        data: Mapping[str, Any],
    ) -> None:
        item = {
            "id": fact_id,
            "kind": kind,
            "scope": scope,
            "support_class": support_class,
            "data": dict(data),
        }
        previous = facts.get(fact_id)
        if previous is not None and previous != item:
            raise FrozenInputError("deterministic fact identity collision")
        facts[fact_id] = item

    for family in ("sell_put", "covered_call"):
        for row in candidates.get(family) or []:
            if not isinstance(row, Mapping):
                raise FrozenInputError("candidate fact row is invalid")
            candidate_id = str(row.get("candidate_id") or "")
            scope = _candidate_scope(family, row)
            add(
                f"candidate:{candidate_id}",
                kind="candidate",
                scope=scope,
                support_class="candidate",
                data=row,
            )

    add(
        "portfolio:distribution",
        kind="portfolio",
        scope="account",
        support_class="risk",
        data=portfolio,
    )
    for reason in portfolio.get("gaps") or []:
        _add_gap_fact(
            add,
            source="portfolio",
            scope="account",
            reason=str(reason),
        )

    summary = option_positions.get("summary")
    if isinstance(summary, Mapping):
        add(
            "position:summary",
            kind="position",
            scope="account",
            support_class="risk",
            data=summary,
        )
    for row in option_positions.get("candidate_contracts") or []:
        if not isinstance(row, Mapping):
            continue
        add(
            f"position:contract:{_short_hash(row)}",
            kind="position",
            scope="account",
            support_class="risk",
            data=row,
        )
    for row in option_positions.get("verified_structures") or []:
        if not isinstance(row, Mapping):
            continue
        add(
            f"position:structure:{_short_hash(row)}",
            kind="position",
            scope="account",
            support_class="risk",
            data=row,
        )
    for reason in option_positions.get("gaps") or []:
        _add_gap_fact(
            add,
            source="option_positions",
            scope="account",
            reason=str(reason),
        )

    for candidate_id, projection in projections.items():
        if not isinstance(projection, Mapping):
            raise FrozenInputError("projection fact row is invalid")
        family = (
            "sell_put"
            if projection.get("strategy_mode") == "put"
            else "covered_call"
        )
        scope = _candidate_scope(family, projection)
        add(
            f"projection:{candidate_id}",
            kind="projection",
            scope=scope,
            support_class="risk",
            data=projection,
        )
        for reason in projection.get("gaps") or []:
            _add_gap_fact(
                add,
                source="projection",
                scope=scope,
                reason=str(reason),
                candidate_id=str(candidate_id),
            )

    for symbol_row in external_evidence.get("symbols") or []:
        if not isinstance(symbol_row, Mapping):
            continue
        coverage_ref = str(symbol_row.get("coverage_ref") or "")
        coverage_data = {
            key: symbol_row.get(key)
            for key in (
                "symbol",
                "coverage",
                "unavailable_reason",
                "last_checked_at",
                "last_success_at",
            )
        }
        add(
            coverage_ref,
            kind="coverage",
            scope="account",
            support_class="coverage",
            data=coverage_data,
        )
        if symbol_row.get("coverage") != "completed":
            _add_gap_fact(
                add,
                source="coverage",
                scope="account",
                reason=str(
                    symbol_row.get("unavailable_reason")
                    or symbol_row.get("coverage")
                    or "coverage_unavailable"
                ),
                symbol=str(symbol_row.get("symbol") or ""),
            )
        for evidence in symbol_row.get("evidence") or []:
            if not isinstance(evidence, Mapping):
                continue
            add(
                str(evidence.get("ref") or ""),
                kind="evidence",
                scope="account",
                support_class="external_risk",
                data={
                    "symbol": symbol_row.get("symbol"),
                    **dict(evidence),
                },
            )

    return {
        "schema_version": FACT_REGISTRY_SCHEMA,
        "facts": [facts[key] for key in sorted(facts)],
    }


def build_frozen_inputs(
    *,
    snapshot: Mapping[str, Any],
    portfolio_distribution: (
        PreparedPortfolioDistribution | Mapping[str, Any] | None
    ),
    option_positions_context: Mapping[str, Any] | None,
    evidence_index: EvidenceIndex,
    market: str,
    evidence_run_id: str | None = None,
    portfolio_unavailable_reason: str = "portfolio_unavailable",
    option_positions_unavailable_reason: str = (
        "option_positions_unavailable"
    ),
) -> FrozenInputs:
    """Freeze verified authorities into one privacy-minimized Advice input."""

    run_id = _required_text(snapshot.get("run_id"), "candidate run_id")
    account = _required_text(snapshot.get("account"), "candidate account").lower()
    account_config_sha256 = _required_sha256(
        snapshot.get("account_config_sha256"),
        "candidate account_config_sha256",
    )
    candidates = freeze_candidates(snapshot, market=market)
    candidate_symbols = {
        str(row["symbol"])
        for row in (
            *candidates["sell_put"],
            *candidates["covered_call"],
        )
    }

    portfolio_frozen = _freeze_portfolio_distribution(
        portfolio_distribution,
        expected_run_id=run_id,
        expected_account=account,
        expected_account_config_sha256=account_config_sha256,
        unavailable_reason=portfolio_unavailable_reason,
    )
    option_frozen = _freeze_option_positions(
        option_positions_context,
        candidate_symbols=candidate_symbols,
        expected_run_id=run_id,
        expected_account=account,
        expected_account_config_sha256=account_config_sha256,
        unavailable_reason=option_positions_unavailable_reason,
    )
    cny_rates = (
        cny_per_currency_rates_from_option_context(option_positions_context)
        if option_positions_context is not None
        and option_frozen.model.get("status") == "ready"
        else {"CNY": 1.0}
    )
    projections = project_all_candidates(
        candidates=candidates,
        portfolio=portfolio_frozen.model,
        option_positions=option_frozen.model,
        position_rows=option_frozen.projection_rows,
        portfolio_total_cny=portfolio_frozen.total_value_cny,
        shares_by_symbol=portfolio_frozen.shares_by_symbol,
        cny_per_currency=cny_rates,
    )

    relevant_symbols = sorted(
        candidate_symbols
        | set(portfolio_frozen.searchable_symbols)
        | set(option_frozen.underlying_symbols)
    )
    external = freeze_external_evidence(
        evidence_index,
        symbols=relevant_symbols,
    )
    fact_registry = build_fact_registry(
        candidates=candidates,
        portfolio=portfolio_frozen.model,
        option_positions=option_frozen.model,
        projections=projections,
        external_evidence=external,
    )
    portfolio_hash = _semantic_hash(portfolio_frozen.model)
    return FrozenInputs(
        candidates=candidates,
        portfolio=portfolio_frozen.model,
        option_positions=option_frozen.model,
        external_evidence=external,
        candidate_snapshot_hash=_semantic_hash(candidates),
        portfolio_context_hash=portfolio_hash,
        option_positions_hash=_semantic_hash(option_frozen.model),
        external_evidence_hash=_semantic_hash(external),
        external_evidence_run_id=evidence_run_id,
        projections=projections,
        fact_registry=fact_registry,
        portfolio_distribution_hash=portfolio_hash,
        projection_hash=_semantic_hash(projections),
        fact_registry_hash=_semantic_hash(fact_registry),
    )


def _freeze_portfolio_distribution(
    prepared: PreparedPortfolioDistribution | Mapping[str, Any] | None,
    *,
    expected_run_id: str | None,
    expected_account: str | None,
    expected_account_config_sha256: str | None,
    unavailable_reason: str,
) -> _PortfolioFreeze:
    if prepared is None:
        return _unavailable_portfolio(_reason_code(unavailable_reason))
    if isinstance(prepared, PreparedPortfolioDistribution):
        envelope = prepared.envelope
    elif isinstance(prepared, Mapping):
        nested = prepared.get("envelope")
        envelope = nested if isinstance(nested, Mapping) else prepared
    else:
        return _unavailable_portfolio("portfolio_payload_invalid")
    authority = envelope.get("authority")
    payload = envelope.get("payload")
    if not isinstance(authority, Mapping) or not isinstance(payload, Mapping):
        return _unavailable_portfolio("portfolio_payload_invalid")
    if _authority_mismatch(
        authority,
        expected_run_id=expected_run_id,
        expected_account=expected_account,
        expected_account_config_sha256=expected_account_config_sha256,
    ):
        return _unavailable_portfolio("portfolio_authority_mismatch")

    status = str(authority.get("status") or "").strip().lower()
    reason = _reason_code(authority.get("reason"), "portfolio_unavailable")
    quality = _portfolio_quality(payload)
    if status == "unavailable":
        return _unavailable_portfolio(reason, quality=quality)
    if status not in {"ready", "degraded"}:
        return _unavailable_portfolio("portfolio_status_invalid")

    assets = payload.get("assets")
    derived = payload.get("derived")
    if not isinstance(assets, list) or not isinstance(derived, Mapping):
        return _unavailable_portfolio("portfolio_payload_invalid")
    total = _finite_float(derived.get("total_value"))
    if total is None or total < 0 or (assets and total <= 0):
        return _unavailable_portfolio("portfolio_total_invalid")

    asset_values: dict[str, float] = {}
    shares_by_symbol: dict[str, float] = {}
    searchable_symbols: set[str] = set()
    for row in assets:
        if not isinstance(row, Mapping):
            return _unavailable_portfolio("portfolio_asset_invalid")
        code = str(row.get("code") or "").strip()
        normalized_type = str(row.get("normalized_type") or "").strip()
        amount = _finite_float(row.get("value"))
        quantity = _finite_float(row.get("quantity"))
        if not code or amount is None or quantity is None:
            return _unavailable_portfolio("portfolio_asset_invalid")
        public_code = code
        if normalized_type == "stock":
            public_code = canonical_symbol(code) or code
            searchable_symbols.add(public_code)
            shares_by_symbol[public_code] = math.fsum(
                (shares_by_symbol.get(public_code, 0.0), quantity)
            )
        asset_values[public_code] = math.fsum(
            (asset_values.get(public_code, 0.0), amount)
        )

    asset_weights = (
        {
            code: round(value / total, 8)
            for code, value in sorted(asset_values.items())
        }
        if total > 0
        else {}
    )
    raw_currency_weights = derived.get("currency_weights")
    if not isinstance(raw_currency_weights, Mapping):
        return _unavailable_portfolio("portfolio_currency_weights_invalid")
    currency_weights: dict[str, float] = {}
    for currency, value in sorted(raw_currency_weights.items()):
        parsed = _finite_float(value)
        if parsed is None:
            return _unavailable_portfolio(
                "portfolio_currency_weights_invalid"
            )
        currency_weights[str(currency)] = round(parsed, 8)
    cash_weight = _finite_float(derived.get("cash_and_mmf_weight"))
    if cash_weight is None:
        return _unavailable_portfolio("portfolio_cash_weight_invalid")

    gaps: list[str] = []
    if status == "degraded":
        gaps.append(reason)
    model = {
        "status": status,
        "quality": quality,
        "asset_weights": asset_weights,
        "currency_weights": currency_weights,
        "cash_and_mmf_weight": round(cash_weight, 8),
        "gaps": sorted(set(gaps)),
    }
    return _PortfolioFreeze(
        model=model,
        total_value_cny=total,
        shares_by_symbol={
            key: value
            for key, value in sorted(shares_by_symbol.items())
            if math.isfinite(value) and value > 0
        },
        searchable_symbols=tuple(sorted(searchable_symbols)),
    )


def _unavailable_portfolio(
    reason: str,
    *,
    quality: Mapping[str, Any] | None = None,
) -> _PortfolioFreeze:
    return _PortfolioFreeze(
        model={
            "status": "unavailable",
            "quality": dict(quality or _empty_portfolio_quality()),
            "asset_weights": {},
            "currency_weights": {},
            "cash_and_mmf_weight": None,
            "gaps": [f"portfolio_unavailable:{reason}"],
        },
        total_value_cny=None,
        shares_by_symbol={},
        searchable_symbols=(),
    )


def _portfolio_quality(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "freshness_status": str(
            payload.get("freshness_status") or "unavailable"
        ),
        "trust_status": str(payload.get("trust_status") or "unavailable"),
        "observed_at_utc": payload.get("observed_at_utc"),
    }


def _empty_portfolio_quality() -> dict[str, Any]:
    return {
        "freshness_status": "unavailable",
        "trust_status": "unavailable",
        "observed_at_utc": None,
    }


def _freeze_option_positions(
    context: Mapping[str, Any] | None,
    *,
    candidate_symbols: Iterable[str],
    expected_run_id: str | None,
    expected_account: str | None,
    expected_account_config_sha256: str | None,
    unavailable_reason: str,
) -> _OptionFreeze:
    if context is None:
        return _unavailable_options(_reason_code(unavailable_reason))
    prepared = context.get("prepared_authority")
    filters = context.get("filters")
    if not isinstance(prepared, Mapping) or not isinstance(filters, Mapping):
        return _unavailable_options("option_authority_missing")
    if _authority_mismatch(
        prepared,
        expected_run_id=expected_run_id,
        expected_account=expected_account,
        expected_account_config_sha256=expected_account_config_sha256,
    ):
        return _unavailable_options("option_authority_mismatch")
    if expected_account is not None and str(
        filters.get("account") or ""
    ).strip().lower() != str(expected_account).strip().lower():
        return _unavailable_options("option_account_mismatch")
    context_account = str(filters.get("account") or "").strip().lower()
    context_broker = normalize_broker(str(filters.get("broker") or ""))
    if not context_account or not context_broker:
        return _unavailable_options("option_authority_missing")
    effective_account = (
        str(expected_account).strip().lower()
        if expected_account is not None
        else context_account
    )
    if (
        str(context.get("context_status") or "") != "available"
        or str(context.get("decision_snapshot_status") or "") != "trusted"
    ):
        return _unavailable_options("option_context_untrusted")
    raw_rows = context.get("open_positions_min")
    if not isinstance(raw_rows, list):
        return _unavailable_options("option_positions_invalid")

    candidate_set = {
        canonical_symbol(symbol) or str(symbol).strip().upper()
        for symbol in candidate_symbols
    }
    aggregates: dict[
        tuple[str, str, str, str, str, str], dict[str, Any]
    ] = {}
    records_by_id: dict[str, dict[str, Any]] = {}
    try:
        for raw in raw_rows:
            parsed = _parse_option_row(raw, expected_account=effective_account)
            key = (
                parsed["symbol"],
                parsed["option_type"],
                parsed["side"],
                parsed["strike_key"],
                parsed["expiry"],
                parsed["multiplier_key"],
            )
            current = aggregates.get(key)
            if current is None:
                current = {
                    "symbol": parsed["symbol"],
                    "option_type": parsed["option_type"],
                    "side": parsed["side"],
                    "strike": parsed["strike"],
                    "expiry": parsed["expiry"],
                    "multiplier": parsed["multiplier"],
                    "contracts": 0,
                }
                aggregates[key] = current
            current["contracts"] += parsed["contracts"]
            record_id = parsed.get("record_id")
            if record_id:
                if record_id in records_by_id:
                    raise FrozenInputError("option record identity is duplicated")
                records_by_id[record_id] = parsed
    except FrozenInputError:
        return _unavailable_options("option_positions_invalid")

    projection_rows = tuple(
        aggregates[key]
        for key in sorted(aggregates)
    )
    total_contracts = sum(int(row["contracts"]) for row in projection_rows)
    by_direction_and_type: dict[tuple[str, str], int] = {}
    by_expiry: dict[str, int] = {}
    for row in projection_rows:
        pair = (str(row["side"]), str(row["option_type"]))
        by_direction_and_type[pair] = (
            by_direction_and_type.get(pair, 0) + int(row["contracts"])
        )
        expiry = str(row["expiry"])
        by_expiry[expiry] = by_expiry.get(expiry, 0) + int(
            row["contracts"]
        )

    public_contracts: dict[
        tuple[str, str, str, str, str, str], dict[str, Any]
    ] = {}
    for row in projection_rows:
        if row["symbol"] not in candidate_set:
            continue
        public_key = (
            str(row["symbol"]),
            str(row["option_type"]),
            str(row["side"]),
            str(row["strike"]),
            str(row["expiry"]),
            str(row["multiplier"]),
        )
        current = public_contracts.get(public_key)
        if current is None:
            current = {
                "symbol": row["symbol"],
                "option_type": row["option_type"],
                "side": row["side"],
                "strike": row["strike"],
                "expiry": row["expiry"],
                "multiplier": row["multiplier"],
                "contracts": 0,
            }
            public_contracts[public_key] = current
        current["contracts"] += int(row["contracts"])

    structures = _verified_structures(
        context,
        records_by_id=records_by_id,
        expected_account=effective_account,
        expected_broker=context_broker,
        candidate_symbols=candidate_set,
    )
    observed_at = prepared.get("source_observed_at") or context.get("as_of_utc")
    model = {
        "status": "ready",
        "source_observed_at": observed_at,
        "summary": {
            "total_open_contracts": total_contracts,
            "by_direction_and_type": [
                {
                    "side": side,
                    "option_type": option_type,
                    "contracts": contracts,
                }
                for (side, option_type), contracts in sorted(
                    by_direction_and_type.items()
                )
            ],
            "by_expiry": [
                {"expiry": expiry, "contracts": contracts}
                for expiry, contracts in sorted(by_expiry.items())
            ],
        },
        "candidate_contracts": [
            public_contracts[key] for key in sorted(public_contracts)
        ],
        "verified_structures": structures,
        "gaps": [],
    }
    return _OptionFreeze(
        model=model,
        projection_rows=projection_rows,
        underlying_symbols=tuple(
            sorted({str(row["symbol"]) for row in projection_rows})
        ),
    )


def _unavailable_options(reason: str) -> _OptionFreeze:
    return _OptionFreeze(
        model={
            "status": "unavailable",
            "source_observed_at": None,
            "summary": {
                "total_open_contracts": None,
                "by_direction_and_type": [],
                "by_expiry": [],
            },
            "candidate_contracts": [],
            "verified_structures": [],
            "gaps": [f"option_positions_unavailable:{reason}"],
        },
        projection_rows=(),
        underlying_symbols=(),
    )


def _parse_option_row(
    value: Any,
    *,
    expected_account: str | None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FrozenInputError("option row must be an object")
    if str(value.get("status") or "").strip().lower() != "open":
        raise FrozenInputError("option row is not open")
    account = str(value.get("account") or "").strip().lower()
    if expected_account is not None and account != expected_account.lower():
        raise FrozenInputError("option row account mismatch")
    symbol = canonical_symbol(value.get("symbol"))
    option_type = str(value.get("option_type") or "").strip().lower()
    side = str(value.get("side") or "").strip().lower()
    strike = _positive_decimal(value.get("strike"))
    multiplier = _positive_decimal(value.get("multiplier"))
    contracts = _positive_integer(value.get("contracts_open"))
    expiry = _optional_date(
        _first_present(value, "expiration_ymd", "expiry")
    )
    if (
        symbol is None
        or option_type not in {"put", "call"}
        or side not in {"short", "long"}
        or strike is None
        or multiplier is None
        or contracts is None
        or expiry is None
    ):
        raise FrozenInputError("option row economic contract is invalid")
    return {
        "record_id": str(value.get("record_id") or "").strip() or None,
        "strategy_group_id": (
            str(value.get("strategy_group_id") or "").strip() or None
        ),
        "symbol": symbol,
        "option_type": option_type,
        "side": side,
        "strike": _decimal_number(strike),
        "strike_key": _decimal_key(strike),
        "expiry": expiry,
        "multiplier": _decimal_number(multiplier),
        "multiplier_key": _decimal_key(multiplier),
        "contracts": contracts,
    }


def _verified_structures(
    context: Mapping[str, Any],
    *,
    records_by_id: Mapping[str, Mapping[str, Any]],
    expected_account: str | None,
    expected_broker: str,
    candidate_symbols: set[str],
) -> list[dict[str, Any]]:
    snapshot = context.get("decision_state_snapshot")
    if not isinstance(snapshot, Mapping):
        return []
    identities = snapshot.get("account_combo_identities")
    memberships = snapshot.get("account_combo_group_memberships")
    if not isinstance(identities, list) or not isinstance(memberships, list):
        return []
    exact_memberships: dict[str, dict[str, Any]] = {}
    duplicate_membership_groups: set[str] = set()
    for raw_membership in memberships:
        if not isinstance(raw_membership, Mapping):
            continue
        membership = dict(raw_membership)
        membership_validation = validate_combo_group_membership(membership)
        group_id = str(membership.get("group_id") or "")
        if (
            not group_id
            or membership.get("status") != "exact"
            or membership_validation.status != "valid"
            or membership_validation.membership_hash
            != membership.get("membership_hash")
        ):
            continue
        if group_id in exact_memberships:
            duplicate_membership_groups.add(group_id)
            continue
        exact_memberships[group_id] = membership
    for group_id in duplicate_membership_groups:
        exact_memberships.pop(group_id, None)

    structures: list[dict[str, Any]] = []
    for raw in identities:
        if not isinstance(raw, Mapping):
            continue
        identity = dict(raw)
        validation = validate_combo_identity(identity)
        if (
            validation.status != "valid"
            or validation.identity_hash != identity.get("identity_hash")
        ):
            continue
        account = str(identity.get("account") or "").strip().lower()
        symbol = canonical_symbol(identity.get("symbol"))
        if (
            expected_account is not None
            and account != expected_account.lower()
        ) or (
            str(identity.get("strategy") or "").strip().lower()
            != "combo_yield"
        ) or symbol is None or symbol not in candidate_symbols:
            continue
        put_id = str(identity.get("funding_put_record_id") or "")
        call_id = str(identity.get("participation_call_record_id") or "")
        put_row = records_by_id.get(put_id)
        call_row = records_by_id.get(call_id)
        group_id = str(identity.get("group_id") or "")
        membership = exact_memberships.get(group_id)
        member_ids = (
            membership.get("current_account_member_record_ids")
            if membership is not None
            else None
        )
        binding_rows = (
            membership.get("member_bindings_for_current_account")
            if membership is not None
            else None
        )
        bindings_by_record = {
            str(binding.get("record_id") or ""): binding
            for binding in binding_rows or []
            if isinstance(binding, Mapping)
        }
        put_binding = bindings_by_record.get(put_id)
        call_binding = bindings_by_record.get(call_id)
        if (
            membership is None
            or member_ids != sorted({put_id, call_id})
            or put_row is None
            or call_row is None
            or put_row.get("strategy_group_id") != group_id
            or call_row.get("strategy_group_id") != group_id
            or (put_row.get("option_type"), put_row.get("side"))
            != ("put", "short")
            or (call_row.get("option_type"), call_row.get("side"))
            != ("call", "long")
            or put_row.get("symbol") != symbol
            or call_row.get("symbol") != symbol
            or not _membership_binding_matches_identity(
                put_binding,
                identity=identity,
                record_id=put_id,
                event_field="funding_put_open_event_id",
                roles=FUNDING_PUT_ROLES,
                account=account,
                symbol=symbol,
            )
            or not _membership_binding_matches_identity(
                call_binding,
                identity=identity,
                record_id=call_id,
                event_field="participation_call_open_event_id",
                roles=PARTICIPATION_CALL_ROLES,
                account=account,
                symbol=symbol,
            )
            or not _contract_key_matches_row(
                identity.get("funding_put_contract_key"),
                put_row,
                expected_account=account,
                expected_broker=expected_broker,
            )
            or not _contract_key_matches_row(
                identity.get("participation_call_contract_key"),
                call_row,
                expected_account=account,
                expected_broker=expected_broker,
            )
        ):
            continue
        funding = int(put_row["contracts"])
        expression = int(call_row["contracts"])
        if funding <= 0 or expression <= 0:
            continue
        structures.append(
            {
                "label": "SP+LC",
                "symbol": symbol,
                "funding_contracts": funding,
                "expression_contracts": expression,
                "expression_to_funding_ratio": round(
                    expression / funding,
                    8,
                ),
            }
        )
    return sorted(
        structures,
        key=lambda item: (
            str(item["symbol"]),
            str(item["label"]),
            int(item["funding_contracts"]),
            int(item["expression_contracts"]),
        ),
    )


def _membership_binding_matches_identity(
    binding: Any,
    *,
    identity: Mapping[str, Any],
    record_id: str,
    event_field: str,
    roles: frozenset[str],
    account: str,
    symbol: str,
) -> bool:
    if not isinstance(binding, Mapping):
        return False
    return (
        str(binding.get("record_id") or "") == record_id
        and str(binding.get("open_event_id") or "")
        == str(identity.get(event_field) or "")
        and str(binding.get("role") or "").strip().lower() in roles
        and str(binding.get("strategy") or "").strip().lower()
        == "combo_yield"
        and str(binding.get("account") or "").strip().lower() == account
        and canonical_symbol(binding.get("symbol")) == symbol
    )


def _contract_key_matches_row(
    value: Any,
    row: Mapping[str, Any],
    *,
    expected_account: str,
    expected_broker: str,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    try:
        contract_key = ContractKey.from_values(
            broker=value.get("broker"),
            account=value.get("account"),
            underlying_symbol=value.get("underlying_symbol"),
            option_type=value.get("option_type"),
            position_side=value.get("position_side"),
            strike=value.get("strike"),
            expiration_ymd=value.get("expiration_ymd"),
        )
    except (TypeError, ValueError):
        return False
    supplied_position_key = str(value.get("position_key") or "")
    if supplied_position_key and supplied_position_key != contract_key.position_key:
        return False
    row_strike = _finite_float(row.get("strike"))
    return (
        contract_key.broker == expected_broker
        and contract_key.account == expected_account
        and contract_key.underlying_symbol == row.get("symbol")
        and contract_key.option_type == row.get("option_type")
        and contract_key.position_side == row.get("side")
        and row_strike is not None
        and contract_key.strike == round(row_strike, 6)
        and contract_key.expiration_ymd == row.get("expiry")
    )


def _authority_mismatch(
    authority: Mapping[str, Any],
    *,
    expected_run_id: str | None,
    expected_account: str | None,
    expected_account_config_sha256: str | None,
) -> bool:
    expected = {
        "run_id": expected_run_id,
        "account": (
            str(expected_account).lower()
            if expected_account is not None
            else None
        ),
        "account_config_sha256": expected_account_config_sha256,
    }
    for field, value in expected.items():
        if value is None:
            continue
        actual = str(authority.get(field) or "")
        if field == "account":
            actual = actual.lower()
        if actual != str(value):
            return True
    return False


def _candidate_scope(family: str, row: Mapping[str, Any]) -> str:
    if family == "sell_put":
        return "sell_put"
    return f"covered_call:{str(row.get('symbol') or '')}"


def _add_gap_fact(
    add: Any,
    *,
    source: str,
    scope: str,
    reason: str,
    candidate_id: str | None = None,
    symbol: str | None = None,
) -> None:
    data = {
        "source": source,
        "reason": reason,
        "candidate_id": candidate_id,
        "symbol": symbol,
    }
    add(
        f"gap:{source}:{_short_hash(data)}",
        kind="gap",
        scope=scope,
        support_class="gap",
        data=data,
    )


def _reason_code(value: Any, fallback: str = "unavailable") -> str:
    text = str(value or "").strip().lower()
    return text if _REASON_CODE_RE.fullmatch(text) else fallback


def _first_present(values: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in values and values.get(key) not in (None, ""):
            return values.get(key)
    return None


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise FrozenInputError(f"{field_name} is required")
    return text


def _required_sha256(value: Any, field_name: str) -> str:
    text = _required_text(value, field_name).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise FrozenInputError(f"{field_name} is invalid")
    return text


def _positive_integer(value: Any) -> int | None:
    number = _positive_decimal(value)
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def _positive_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _canonical_optional_positive_number(value: Any) -> int | float | None:
    number = _positive_decimal(value)
    return _decimal_number(number) if number is not None else None


def _canonical_optional_nonnegative_number(
    value: Any,
) -> int | float | None:
    number = _decimal(value)
    if number is None or number < 0:
        return None
    return _decimal_number(number)


def _canonical_optional_number(value: Any) -> int | float | None:
    number = _decimal(value)
    return _decimal_number(number) if number is not None else None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _decimal_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _decimal_key(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _optional_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _short_hash(payload: Mapping[str, Any]) -> str:
    return _hash(payload)[:16]


def _hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _semantic_hash(payload: Mapping[str, Any]) -> str:
    semantic = _semantic_hash_value(payload)
    if not isinstance(semantic, Mapping):
        raise TypeError("semantic hash payload must be an object")
    return _hash(semantic)


def _semantic_hash_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _semantic_hash_value(item)
            for key, item in value.items()
            if key not in _SEMANTIC_HASH_OMITTED_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_semantic_hash_value(item) for item in value]
    return value


__all__ = [
    "FACT_REGISTRY_SCHEMA",
    "FrozenInputError",
    "FrozenInputs",
    "build_fact_registry",
    "build_frozen_inputs",
    "freeze_candidates",
    "freeze_external_evidence",
    "freeze_option_positions",
    "freeze_portfolio",
    "freeze_portfolio_distribution",
]
