"""Account-free business orchestration for the five group-query methods.

Every entry re-checks the verified principal's capability and market even
though the router also authorizes the route, so no future caller can bypass
the fixed route mapping.  External I/O is injected; offline tests supply fake
transports and never reach OpenD, the model or Feishu.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import math
import shutil
import uuid
from typing import Any, Callable

from src.application.close_advice_required_data import (
    ScenarioEvidenceError,
    ScenarioInputError,
    build_user_provided_scenario_plan,
    scenario_requirement_index,
    validate_user_provided_scenario_plan,
)
from domain.domain.symbol_identity import canonical_symbol
from src.application.option_candidates import (
    evaluate_public_candidates,
    prepare_public_query,
)
from src.application.required_data_fetching import (
    RequiredDataFetchRequest,
    build_fetch_request_from_spec,
    fetch_required_data_opend,
    merge_required_data_payloads,
)
from src.application.required_data_planning import (
    RequiredDataFetchSpec,
    build_required_data_fetch_plan,
)
from src.application.required_data_plan_identity import (
    build_required_data_expected_fetch_contract,
)
from src.infrastructure.exchange_rates import current_exchange_rate_snapshot

from domain.domain.fee_calc import (
    estimate_futu_option_buy_fee,
    estimate_futu_option_sell_fee,
)

from .assessment import (
    AssessmentError,
    build_binding,
    canonical_json_bytes,
    conversation_scope_sha256,
    input_sha256,
    new_assessment_id,
    parse_utc,
    purge_expired_assessments,
    sha256_hex,
    write_assessment,
)
from .budget import BudgetExhausted, RunBudget
from .config import APISettings
from .contracts import (
    ERROR_STATUS,
    ContractError,
    NormalizedRequest,
    VerifiedPrincipal,
    error_envelope,
    success_envelope,
)
from .store import InputIdentityConflict


QUOTE_FIELDS = (
    "contract_symbol", "option_type", "expiration", "strike", "multiplier",
    "currency", "last_price", "bid", "ask", "mid", "spread_ratio",
    "implied_volatility", "delta", "open_interest", "volume",
    "snapshot_received_at_utc", "spot", "spot_observed_at_utc",
    "last_price_update_time", "market_state", "opening_contract_status",
    "underlier_observation_status", "option_sec_status",
    "underlier_sec_status", "quote_rights",
)
CURRENT_QUOTE_SECONDS = 300

CLOSE_METRIC_FIELDS = (
    "opening_gross_credit", "opening_net_credit", "all_in_close_cost",
    "net_capture_ratio", "close_cost_ratio", "remaining_term_ratio",
    "capital_basis", "remaining_max_annualized_return", "estimated_pnl_if_close_net",
)


class BusinessUnavailable(RuntimeError):
    """A dependency or evidence owner cannot answer right now."""


def _authorize(principal: VerifiedPrincipal, capability: str, market: str) -> None:
    if capability not in principal.capabilities:
        raise ContractError("PERMISSION_DENIED", "capability is not granted")
    if market not in principal.markets:
        raise ContractError("PERMISSION_DENIED", "market is outside the granted scope")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("business clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _public_quote(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in QUOTE_FIELDS}


def _payload_rows(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise BusinessUnavailable("required-data payload rows are unavailable")
    return rows


def _payload_digest(payload: Any) -> str:
    return sha256_hex(canonical_json_bytes(payload))


def _quote_status(quote: dict[str, Any], now: datetime, missing: list[str]) -> str:
    try:
        received = parse_utc(quote.get("snapshot_received_at_utc"), "snapshot_received_at_utc")
    except AssessmentError:
        missing.append("quote_snapshot_time")
        return "unavailable"
    age = (now - received).total_seconds()
    if age < 0:
        missing.append("quote_snapshot_future")
        return "unavailable"
    for field in ("opening_contract_status", "underlier_observation_status"):
        status = str(quote.get(field) or "").strip().lower()
        if status and status != "ready":
            missing.append(f"{field}_not_ready")
            return "unavailable"
    return "current" if age <= CURRENT_QUOTE_SECONDS else "last_known"


def _underlier_quote(row: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    """Build the underlier Quote from the spot evidence on an option row."""
    spot = _finite_or_none(row.get("spot"))
    if spot is None or spot <= 0:
        return None
    observed = (row.get("spot_observed_at_utc")
                or row.get("snapshot_received_at_utc"))
    result = {field: None for field in QUOTE_FIELDS}
    result.update({
        "contract_symbol": symbol,
        "last_price": spot,
        "spot": spot,
        "spot_observed_at_utc": row.get("spot_observed_at_utc"),
        "snapshot_received_at_utc": observed,
        "last_price_update_time": row.get("last_price_update_time"),
        "market_state": row.get("market_state"),
        "underlier_observation_status": row.get("underlier_observation_status"),
        "underlier_sec_status": row.get("underlier_sec_status"),
        "quote_rights": row.get("underlier_quote_rights") or row.get("quote_rights"),
    })
    return result


def _effective(request: NormalizedRequest, *, contract_requested: bool) -> dict[str, Any]:
    params = dict(request.parameters)
    if request.method == "query_quotes":
        params["contract_requested"] = contract_requested
        params["contrast"] = ("option not requested" if not contract_requested
                              else "option requested explicitly")
    return params


def _row_matches(row: dict[str, Any], contract: dict[str, Any]) -> bool:
    return (
        str(row.get("option_type") or "").lower() == contract["option_type"]
        and str(row.get("expiration") or "") == contract["expiration"]
        and _finite_or_none(row.get("strike")) == float(contract["strike"])
    )


def _spot_payloads(settings: APISettings, fetch_symbol: str,
                   observation: Any) -> list[dict[str, Any]]:
    """Wrap a ready underlier observation as the account-free spot quote row."""
    if observation is None or getattr(observation, "status", None) != "ready":
        return []
    price = _finite_or_none(getattr(observation, "last_price", None))
    if price is None or price <= 0:
        return []
    observed = str(getattr(observation, "observed_at_utc", "") or "")
    market = str(getattr(observation, "market", "") or "").upper()
    currency = "HKD" if market == "HK" else "USD" if market == "US" else None
    row = {
        "currency": currency,
        "last_price": price, "spot": price,
        "spot_observed_at_utc": observed, "snapshot_received_at_utc": observed,
        "last_price_update_time": getattr(observation, "update_time", None),
        "market_state": getattr(observation, "market_state", None),
        "underlier_sec_status": getattr(observation, "sec_status", None),
    }
    return [{
        "symbol": fetch_symbol,
        "underlier_code": str(getattr(observation, "code", "") or ""),
        "rows": [row],
        "meta": {"observed_at_utc": observed, "source": settings.quote_source},
    }]


def _payload_evidence(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence = []
    for item in payloads:
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        evidence.append({
            "payload_sha256": _payload_digest(item),
            "symbol": str(item.get("symbol") or ""),
            "underlier_code": str(item.get("underlier_code") or ""),
            "observed_at_utc": str(meta.get("observed_at_utc") or meta.get("fetched_at_utc") or ""),
            "source": str(meta.get("source") or ""),
        })
    return evidence


@dataclass(frozen=True)
class FetchPlan:
    symbol: str
    fetch_plan: dict[str, Any]
    expected_contract: dict[str, Any]
    global_plan: dict[str, Any]

    @property
    def plan_id(self) -> str:
        return self.global_plan["plan_id"]


@dataclass(frozen=True)
class Dependencies:
    """Injected transports; the defaults use the existing production owners."""

    fetch_symbol: Callable[[Path, RequiredDataFetchRequest], dict[str, Any]]
    exchange_rates: Callable[[APISettings, datetime, RunBudget], dict[str, Any]]

    @classmethod
    def defaults(cls) -> "Dependencies":
        def fetch_symbol(base: Path, request: RequiredDataFetchRequest) -> dict[str, Any]:
            payload_path, _meta_path = fetch_required_data_opend(base=base, request=request)
            try:
                return json.loads(Path(payload_path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise BusinessUnavailable("required-data payload is unreadable") from exc

        def exchange_rates(settings: APISettings, _now: datetime,
                           budget: RunBudget) -> dict[str, Any]:
            # The snapshot samples its evaluation clock after its own fetch.
            # Passing the request's earlier clock would drop provider quotes
            # stamped during the fetch as "future" and report FX unavailable.
            return current_exchange_rate_snapshot(
                cache_path=Path(settings.data_dir) / "output_shared/state/rate_cache.json",
                write_cache=False,
                deadline=budget.deadline_monotonic, cancelled=budget.expired,
            )

        return cls(fetch_symbol=fetch_symbol, exchange_rates=exchange_rates)


class BusinessService:
    def __init__(self, *, settings: APISettings, store: Any,
                 dependencies: Dependencies | None = None,
                 now: Callable[[], datetime] | None = None):
        self.settings = settings
        self.store = store
        self.dependencies = dependencies or Dependencies.defaults()
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ---- public entry -------------------------------------------------
    def execute(self, request: NormalizedRequest, principal: VerifiedPrincipal,
                budget: RunBudget) -> dict[str, Any]:
        from .contracts import ROUTES
        capability = ROUTES[request.method][1]
        _authorize(principal, capability, request.market)
        if request.method == "query_quotes":
            data = self._quotes(request, budget)
        elif request.method == "query_candidates":
            data = self._candidates(request, budget)
        else:
            data = self._scenario(request, principal, budget)
        return success_envelope(request.request_id, data)

    def execute_error(self, request_id: str | None,
                      exc: Exception) -> tuple[dict[str, Any], int]:
        from src.application.agent_tool_contracts import AgentToolError

        if isinstance(exc, ContractError):
            return (error_envelope(request_id, exc.code, exc.message,
                                   retry_after_seconds=exc.retry_after_seconds),
                    exc.status)
        if isinstance(exc, AgentToolError):
            if exc.code in ERROR_STATUS:
                return (error_envelope(request_id, exc.code, exc.message),
                        ERROR_STATUS[exc.code])
            return (error_envelope(request_id, "INTERNAL_ERROR",
                                   "internal group-query failure"), 500)
        if isinstance(exc, (TimeoutError,)):
            return (error_envelope(request_id, "UPSTREAM_TIMEOUT",
                                   "quote or evidence service timed out"), 504)
        if isinstance(exc, BudgetExhausted):
            return (error_envelope(request_id, "UPSTREAM_TIMEOUT",
                                   "request budget was exhausted"), 504)
        if isinstance(exc, BusinessUnavailable):
            return (error_envelope(request_id, "DEPENDENCY_UNAVAILABLE",
                                   "quote or evidence service is unavailable"), 503)
        return (error_envelope(request_id, "INTERNAL_ERROR",
                               "internal group-query failure"), 500)

    # ---- quotes -------------------------------------------------------
    def _quotes(self, request: NormalizedRequest, budget: RunBudget) -> dict[str, Any]:
        contract = request.parameters.get("contract")
        requested_at = _utc(self._now())
        payloads, plan = self._fetch(
            request=request, budget=budget,
            option_type=contract["option_type"] if contract else None,
            original_contract=contract,
        )
        # Freshness must be judged at the post-fetch clock: a snapshot received
        # while the fetch was in flight is newer than the request's clock but
        # is not a future quote.
        now = _utc(self._now())
        rows = [row for payload in payloads for row in _payload_rows(payload)]
        underlier = None
        option = None
        option_row = None
        for row in rows:
            if contract is not None and _row_matches(row, contract) and option is None:
                option = _public_quote(row)
                option_row = row
            elif contract is None and underlier is None:
                underlier = _public_quote(row)
                if underlier is not None:
                    underlier["contract_symbol"] = plan.symbol
        if option_row is not None:
            underlier = _underlier_quote(option_row, plan.symbol)
        missing: list[str] = []
        if underlier is None:
            missing.append("underlier_quote")
        if contract is not None and option is None:
            raise ContractError("INPUT_ERROR",
                                "option contract identity is unavailable")
        status = _quote_status(underlier, now, missing) if underlier is not None else "unavailable"
        if option is not None:
            option_status = _quote_status(option, now, missing)
            if status == "current" and option_status != "current":
                status = option_status
        currency = (option or {}).get("currency") or (underlier or {}).get("currency")
        return {
            "symbol": plan.symbol,
            "market": request.market,
            "currency": currency,
            "evaluated_at_utc": now.isoformat(),
            "public_policy_sha256": self.settings.public_policy.canonical_sha256(),
            "reason_codes": [],
            "underlier": underlier,
            "option": option,
            "quote_status": status,
            "missing_requirements": sorted(set(missing)),
            "effective_parameters": _effective(request, contract_requested=contract is not None),
            "requested_at_utc": requested_at.isoformat(),
            "evidence": {"plan_id": plan.plan_id, "payloads": _payload_evidence(payloads)},
        }

    # ---- candidates ---------------------------------------------------
    def _prepare_candidates(self, request: NormalizedRequest,
                            now: datetime) -> Any:
        return prepare_public_query(
            {**request.parameters, "market": request.market},
            self.settings.public_policy.policy,
            {"market_data_provider": "opend", "host": self.settings.quote_host,
             "port": self.settings.quote_port},
            now,
        )

    def _candidates(self, request: NormalizedRequest, budget: RunBudget) -> dict[str, Any]:
        requested_at = _utc(self._now())
        prepared = self._prepare_candidates(request, requested_at)
        payloads, plan = self._fetch(
            request=request, budget=budget,
            option_type=prepared.query["strategy"].removeprefix("short_"),
            candidate_config=prepared.config,
        )
        fetched_at = _utc(self._now())
        budget.begin_api_call()
        fx = self.dependencies.exchange_rates(self.settings, fetched_at, budget)
        if budget.expired():
            raise BudgetExhausted("deadline_exceeded")
        # Evaluate at the post-fetch clock so quotes and rates received during
        # the two fetches are not rejected as future evidence.  The prepared
        # query carries its scan date, so it must be rebuilt for that clock:
        # otherwise a fetch crossing the market-local midnight reports
        # date_changed and drops every candidate.
        now = _utc(self._now())
        prepared = self._prepare_candidates(request, now)
        merged = merge_required_data_payloads(
            symbol=plan.symbol, payloads=[dict(item) for item in payloads])
        result = evaluate_public_candidates(prepared, merged, dict(fx or {}), 0, now)
        filters = prepared.filters
        requested_range = filters.get("abs_delta_range")
        result["requested_abs_delta_range"] = requested_range
        result["effective_abs_delta_range"] = (
            [filters.get("min_abs_delta"), filters.get("max_abs_delta")]
            if requested_range is not None else None)
        result["evaluated_at_utc"] = now.isoformat()
        result["public_policy_sha256"] = self.settings.public_policy.canonical_sha256()
        result["effective_parameters"] = _effective(request, contract_requested=False)
        result["evidence"] = {"plan_id": plan.plan_id, "payloads": _payload_evidence(payloads)}
        return result

    # ---- scenarios ----------------------------------------------------
    def _scenario(self, request: NormalizedRequest, principal: VerifiedPrincipal,
                  budget: RunBudget) -> dict[str, Any]:
        requested_at = _utc(self._now())
        scenario = _scenario_input(request, requested_at)
        scope = conversation_scope_sha256(self.settings.instance_id, request.context)
        purge_expired_assessments(Path(self.settings.data_dir), requested_at)
        assessment_id = new_assessment_id()
        runtime_root = Path(self.settings.data_dir) / "output_runs" / assessment_id
        required_dir = runtime_root / "required_data"
        policy = self.settings.public_policy
        budget.begin_api_call()
        try:
            plan = build_user_provided_scenario_plan(
                scenario=scenario,
                policy_sha256=policy.canonical_sha256(),
                strategy_version=policy.strategy_version,
                fee_model_id=policy.fee_model_id,
                fee_model_version=policy.fee_model_version,
                quote_route={"source": self.settings.quote_source,
                             "host": self.settings.quote_host,
                             "port": self.settings.quote_port},
                runtime_root=runtime_root, required_data_dir=required_dir,
                now_utc=requested_at,
            )
            plan = validate_user_provided_scenario_plan(plan)
        except ScenarioInputError as exc:
            raise ContractError("INPUT_ERROR", str(exc)) from exc
        except ScenarioEvidenceError as exc:
            raise BusinessUnavailable(str(exc)) from exc
        try:
            payload, effective_scope = self._fetch_scenario_payload(
                plan, required_dir, budget)
            # Judge quote freshness after the fetch: rows received in flight
            # are newer than the request clock but are not future evidence.
            now = _utc(self._now())
            rows = _payload_rows(payload)
            original = _original_row(rows, plan)
            contract_evidence = _contract_evidence(plan, original)
            digest = input_sha256(operation=plan["operation"],
                                  parameters=_digest_parameters(request),
                                  contract_evidence=contract_evidence)
            try:
                self.store.bind_input(scope, request.input_id, digest, now)
            except InputIdentityConflict as exc:
                raise ContractError("INPUT_ERROR", str(exc)) from exc
            result = _evaluate_scenario(plan=plan, rows=rows, original=original,
                                        contract_evidence=contract_evidence,
                                        now=now, limit=int(plan["limit"]),
                                        effective_scope=effective_scope)
            binding = build_binding(
                principal_id=principal.principal_id, scope_sha256=scope,
                input_id=request.input_id, input_sha256_value=digest,
                input_as_of_utc=request.input_as_of_utc.isoformat(),
                assessment_id=assessment_id, assessed_at_utc=now, plan=plan,
                snapshot_manifest_sha256=None,
                receipts=[], payloads=_payload_evidence([payload]),
                policy_sha256=policy.canonical_sha256(),
                strategy_version=policy.strategy_version,
                fee_model_id=policy.fee_model_id,
                fee_model_version=policy.fee_model_version,
            )
            write_assessment(data_dir=Path(self.settings.data_dir),
                             assessment_id=assessment_id, scope_sha256=scope,
                             created_at_utc=now, plan=plan, binding=binding)
            shutil.rmtree(runtime_root, ignore_errors=True)
        except BaseException:
            shutil.rmtree(runtime_root, ignore_errors=True)
            raise
        result.update({
            "market": plan["market"],
            "evaluated_at_utc": now.isoformat(),
            "public_policy_sha256": policy.canonical_sha256(),
            "assessment_id": assessment_id,
            "assessment_mode": (
                "current"
                if result.get("evaluation_status") == "priced"
                or result.get("status") == "success" else None),
            "input_id": request.input_id,
            "input_source": "user_provided",
            "input_as_of_utc": request.input_as_of_utc.isoformat(),
            "input": scenario,
            "input_sha256": digest,
            "binding_sha256": binding["binding_sha256"],
            "assessed_at_utc": now.isoformat(),
            "effective_parameters": _effective(request, contract_requested=True),
            "evidence": {"plan_id": plan["global_required_data_plan"]["plan_id"],
                         "payloads": _payload_evidence([payload])},
        })
        return result

    def _fetch_scenario_payload(self, plan: dict[str, Any], required_dir: Path,
                                budget: RunBudget) -> tuple[dict[str, Any], dict[str, Any]]:
        index = scenario_requirement_index(plan)
        merged = (index["fetch_plan"].get("merged_requests") or [])
        if not merged:
            raise BusinessUnavailable("scenario fetch plan has no executable request")
        spec_payload = merged[0]
        spec = RequiredDataFetchSpec(
            symbol=spec_payload["symbol"],
            limit_expirations=int(spec_payload["limit_expirations"]),
            host=str(spec_payload["host"]), port=int(spec_payload["port"]),
            option_types=tuple(spec_payload["option_types"]),
            explicit_expirations=list(spec_payload["explicit_expirations"]),
            min_dte=spec_payload.get("min_dte"), max_dte=spec_payload.get("max_dte"),
            side_strike_windows={key: dict(value) for key, value in
                                 (spec_payload.get("side_strike_windows") or {}).items()},
            trading_date=spec_payload.get("trading_date"),
        )
        request = build_fetch_request_from_spec(spec=spec)
        required_dir.mkdir(parents=True, exist_ok=True)
        required_dir.chmod(0o700)
        budget.begin_api_call()
        payload = self.dependencies.fetch_symbol(required_dir, request)
        if budget.expired():
            raise BudgetExhausted("deadline_exceeded")
        option_type = plan["original_contract"]["option_type"]
        window = (spec.side_strike_windows or {}).get(option_type) or {}
        expirations = list(spec.explicit_expirations or [])
        effective_scope = {
            "option_type": option_type,
            "min_strike": window.get("min_strike"),
            "max_strike": window.get("max_strike"),
            "max_expiration": max(expirations) if expirations else None,
        }
        return payload, effective_scope

    # ---- shared fetch -------------------------------------------------
    def _fetch(self, *, request: NormalizedRequest, budget: RunBudget,
               option_type: str | None, original_contract: dict[str, Any] | None = None,
               candidate_config: Any = None,
               ) -> tuple[list[dict[str, Any]], FetchPlan]:
        settings = self.settings
        runtime_root = Path(settings.data_dir) / "output_runs" / f"gq-{uuid.uuid4().hex}"
        required_dir = runtime_root / "required_data"
        required_dir.mkdir(parents=True, exist_ok=True)
        required_dir.chmod(0o700)
        try:
            requirement = None
            sell_put_cfg = sell_call_cfg = None
            spot_only = original_contract is None and candidate_config is None
            if original_contract is not None:
                requirement = {"option_type": original_contract["option_type"],
                               "expiration": original_contract["expiration"],
                               "strike": float(original_contract["strike"]),
                               "planning_status": "ready"}
                want_put = want_call = False
            elif candidate_config is not None:
                mode = str(option_type)
                cfg = {
                    "enabled": True,
                    "min_dte": getattr(candidate_config, "min_dte", None),
                    "max_dte": getattr(candidate_config, "max_dte", None),
                    "min_strike": getattr(candidate_config, "min_strike", None),
                    "max_strike": getattr(candidate_config, "max_strike", None),
                }
                sell_put_cfg, sell_call_cfg = ((cfg, None) if mode == "put" else (None, cfg))
                want_put, want_call = mode == "put", mode == "call"
            else:
                want_put = want_call = False
            fetch_symbol = canonical_symbol(request.parameters["symbol"])
            if not fetch_symbol:
                raise ContractError("INPUT_ERROR", "symbol identity is unavailable")
            budget.begin_api_call()
            try:
                bundle = build_required_data_fetch_plan(
                    base=runtime_root, required_data_dir=required_dir,
                    symbol=fetch_symbol, limit_expirations=0,
                    want_put=want_put, want_call=want_call,
                    sell_put_cfg=sell_put_cfg, sell_call_cfg=sell_call_cfg,
                    position_requirements=[requirement] if requirement else None,
                    fetch_host=settings.quote_host, fetch_port=settings.quote_port,
                    fetch_source=settings.quote_source, market_data_provider="opend",
                )
                fetch_plan = bundle.to_debug_dict()
                if spot_only and not bundle.merged_specs:
                    # A spot-only plan has no fetch requests by design: its
                    # evidence is the underlier observation.  The shared
                    # expected-contract validator requires requests for a
                    # success_rows outcome, so it does not apply here.
                    expected = {}
                    global_plan = {"plan_id": _spot_plan_id(fetch_symbol, fetch_plan),
                                   "symbols": [{"symbol": fetch_symbol,
                                                "fetch_plan": fetch_plan}]}
                else:
                    expected = build_required_data_expected_fetch_contract(
                        symbol=fetch_symbol, fetch_plan=fetch_plan,
                        source=_fetch_source(settings), host=settings.quote_host,
                        port=settings.quote_port,
                    )
                    global_plan = {
                        "plan_id": _plan_id(fetch_symbol, expected, fetch_plan),
                        "symbols": [{"symbol": fetch_symbol,
                                     "expected_fetch_contract": expected,
                                     "fetch_plan": fetch_plan,
                                     "fetch_binding": expected["fetch_binding"]}]}
            except (RuntimeError, ValueError, OSError) as exc:
                raise BusinessUnavailable(
                    "quote discovery or fetch planning is unavailable") from exc
            payloads: list[dict[str, Any]] = []
            if bundle.merged_specs:
                for spec in bundle.merged_specs:
                    budget.begin_api_call()
                    payloads.append(self.dependencies.fetch_symbol(
                        required_dir, build_fetch_request_from_spec(spec=spec)))
                    if budget.expired():
                        raise BudgetExhausted("deadline_exceeded")
            elif spot_only:
                payloads = _spot_payloads(settings, fetch_symbol,
                                          bundle.underlier_observation)
            else:
                raise BusinessUnavailable("no bounded quote scope was produced")
            return payloads, FetchPlan(symbol=fetch_symbol,
                                       fetch_plan=fetch_plan, expected_contract=expected,
                                       global_plan=global_plan)
        finally:
            shutil.rmtree(runtime_root, ignore_errors=True)


def _fetch_source(settings: APISettings) -> str:
    from src.application.market_data_provider import market_data_source
    from domain.domain.fetch_source import normalize_fetch_source
    return normalize_fetch_source(market_data_source("opend", settings.quote_source))


def _plan_id(symbol: str, expected: dict[str, Any], fetch_plan: dict[str, Any]) -> str:
    from src.application.required_data_plan_identity import required_data_plan_id
    return required_data_plan_id([{"symbol": symbol,
                                   "expected_fetch_contract": expected,
                                   "fetch_plan": fetch_plan,
                                   "fetch_binding": expected["fetch_binding"]}])


def _spot_plan_id(symbol: str, fetch_plan: dict[str, Any]) -> str:
    """Stable evidence identity for a spot-only plan without fetch requests."""
    from src.application.required_data_plan_identity import required_data_plan_id
    return required_data_plan_id([{"symbol": symbol, "fetch_plan": fetch_plan}])


def _scenario_input(request: NormalizedRequest, now: datetime) -> dict[str, Any]:
    if request.input_id is None or request.input_as_of_utc is None:
        raise ContractError("INPUT_ERROR", "scenario identity is missing")
    if request.input_as_of_utc > now:
        raise ContractError("INPUT_ERROR", "input_as_of_utc is later than the evaluation time")
    operation = request.method.removeprefix("evaluate_").replace("_", "-")
    return {
        "operation": operation,
        "input_id": request.input_id,
        "input_as_of_utc": request.input_as_of_utc.isoformat(),
        **dict(request.parameters),
    }


def _digest_parameters(request: NormalizedRequest) -> dict[str, Any]:
    params = dict(request.parameters)
    params["operation"] = request.method.removeprefix("evaluate_").replace("_", "-")
    return params


def _original_row(rows: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    contract = plan["original_contract"]
    matches = [row for row in rows if _row_matches(row, contract)]
    if len(matches) != 1:
        raise BusinessUnavailable("original contract evidence is unavailable")
    row = matches[0]
    currency = str(row.get("currency") or "").strip().upper()
    if currency not in {"USD", "HKD"}:
        raise BusinessUnavailable("contract currency evidence is unavailable")
    multiplier = _finite_or_none(row.get("multiplier"))
    if multiplier is None or multiplier <= 0 or not float(multiplier).is_integer():
        raise BusinessUnavailable("contract multiplier evidence is unavailable")
    return row


def _contract_evidence(plan: dict[str, Any], original: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": plan["symbol"],
        "market": plan["market"],
        "contract": dict(plan["original_contract"]),
        "currency": str(original.get("currency") or "").strip().upper(),
        "multiplier": int(_finite_or_none(original.get("multiplier")) or 0),
        "delivery": "standard",
    }


def _public_candidate(row: dict[str, Any]) -> dict[str, Any]:
    """Nest the owner metrics while keeping the leg identity fields on top."""
    metrics = row.get("_metrics") or {}
    result = {key: value for key, value in row.items()
              if key != "_metrics"
              and (key not in metrics or key == "contracts")}
    result["metrics"] = dict(metrics)
    return result


def _scenario_evidence_reason(row: dict[str, Any], *, option_type: str,
                              quote_side: str, now: datetime) -> str | None:
    """Reuse the candidate owner's freshness and contract-evidence gates."""
    from domain.domain.engine.candidate_engine import (
        CandidateCalculationError,
        validate_opening_quote_freshness,
        validate_option_contract_evidence,
    )

    try:
        validate_opening_quote_freshness(row, now_utc=now)
        validate_option_contract_evidence(
            row, mode=option_type, quote_side=quote_side, now_utc=now)
    except CandidateCalculationError as exc:
        return exc.reason
    return None


def _scenario_fee(currency: str, price: float, *, contracts: int, multiplier: int,
                  sell: bool) -> tuple[float, str]:
    estimate = (estimate_futu_option_sell_fee if sell else estimate_futu_option_buy_fee)(
        currency, price, contracts=contracts, multiplier=multiplier)
    return float(estimate.amount), str(estimate.fee_schedule_version)


def _normalized_premium(plan: dict[str, Any], multiplier: int) -> tuple[float, str]:
    premium = plan.get("opening_premium") or {}
    amount = _finite_or_none(premium.get("amount"))
    if amount is None or amount <= 0:
        raise ContractError("INPUT_ERROR", "opening premium is invalid")
    unit = premium.get("unit")
    contracts = int(plan["contracts"])
    if unit == "per_share":
        per_share = amount
    elif unit == "per_contract":
        per_share = amount / multiplier
    elif unit == "total":
        per_share = amount / (multiplier * contracts)
    else:
        raise ContractError("INPUT_ERROR", "opening premium unit is invalid")
    return per_share, str(unit)


def _evaluate_scenario(*, plan: dict[str, Any], rows: list[dict[str, Any]],
                       original: dict[str, Any], contract_evidence: dict[str, Any],
                       now: datetime, limit: int,
                       effective_scope: dict[str, Any]) -> dict[str, Any]:
    from domain.domain.close_advice import (
        evaluate_conditional_covered_call_roll,
        evaluate_user_provided_close_advice,
        evaluate_user_provided_short_put_roll,
        UserProvidedCloseAdviceInput,
    )
    from om_position_advice_plugin import (
        rank_covered_call_roll_candidates,
        rank_short_put_roll_candidates,
    )

    currency = str(contract_evidence["currency"])
    multiplier = int(contract_evidence["multiplier"])
    contracts = int(plan["contracts"])
    spot = _finite_or_none(original.get("spot"))
    if spot is None or spot <= 0:
        raise BusinessUnavailable("underlier spot evidence is unavailable")
    old_expiration = plan["original_contract"]["expiration"]
    as_of = date_from_iso(plan["as_of_market_date"])
    old_dte = (date_from_iso(old_expiration) - as_of).days
    operation = plan["operation"]
    common = {
        "analysis_kind": "close" if operation == "close" else
        ("roll_down" if operation == "roll-down" else "conditional"),
        "position": {
            "symbol": plan["symbol"],
            "contract": plan["original_contract"],
            "side": "short", "contracts": contracts,
            "currency": currency, "multiplier": multiplier,
        },
        "market_evidence": {
            "spot": spot, "spot_observed_at_utc": original.get("spot_observed_at_utc"),
            "option_snapshot_received_at_utc": original.get("snapshot_received_at_utc"),
            "option_sec_status": original.get("option_sec_status"),
            "underlier_sec_status": original.get("underlier_sec_status"),
            "market_state": original.get("market_state"),
            "quote_rights": original.get("quote_rights"),
        },
        "symbol": plan["symbol"],
        "currency": currency,
        "fee_model_id": plan["fee_model_id"],
        "fee_model_version": plan["fee_model_version"],
        "strategy_version": plan["strategy_version"],
    }
    evidence_reason = _scenario_evidence_reason(
        original, option_type=plan["original_contract"]["option_type"],
        quote_side="buy", now=now)
    if evidence_reason is not None:
        common["old_quote"] = {"ask": _finite_or_none(original.get("ask")),
                               "bid": _finite_or_none(original.get("bid"))}
        common["quote"] = _public_quote(original)
        common["reason_codes"] = [evidence_reason]
        common["fee_model"] = {"id": plan["fee_model_id"],
                               "model_id": plan["fee_model_id"],
                               "version": plan["fee_model_version"],
                               "fee_basis": None, "currency": currency,
                               "legs": [], "is_actual": False}
        if operation == "close":
            common["recommendation_state"] = "not_evaluable"
            common["evaluation_status"] = "not_evaluable"
            common["metrics"] = {field: None for field in CLOSE_METRIC_FIELDS}
            common["policy_version"] = None
            common["reason"] = evidence_reason
            common["coverage"] = None
            return common
        common["status"] = "unavailable"
        common["outcome"] = "unable"
        common["candidates"] = []
        common["displayed"] = 0
        common["omitted"] = 0
        common["scope"] = {
            "max_new_expiration": plan["search_bounds"].get("max_new_expiration"),
            "strike_boundary": plan["search_bounds"].get(
                "min_new_strike" if operation == "roll-down" else "max_new_strike"),
        }
        common["coverage"] = {
            "requested_max_expiration": common["scope"]["max_new_expiration"],
            "requested_strike_boundary": common["scope"]["strike_boundary"],
            "candidate_count": 0, "matched": 0, "excluded": 0,
            "exclusion_reasons": {evidence_reason: 1},
            "limit": limit, "complete": False,
        }
        return common
    if operation == "close":
        premium, unit = _normalized_premium(plan, multiplier)
        user_currency = str((plan.get("opening_premium") or {}).get("currency") or "")
        if user_currency != currency:
            raise ContractError("INPUT_ERROR",
                                "opening premium currency does not match the contract")
        ask = _finite_or_none(original.get("ask"))
        bid = _finite_or_none(original.get("bid"))
        if ask is None or ask <= 0 or bid is None or bid < 0:
            raise BusinessUnavailable("original contract quote is unavailable")
        open_fee, open_basis = _scenario_fee(currency, premium, contracts=contracts,
                                             multiplier=multiplier, sell=True)
        close_fee, close_basis = _scenario_fee(currency, ask, contracts=contracts,
                                               multiplier=multiplier, sell=False)
        advice = evaluate_user_provided_close_advice(UserProvidedCloseAdviceInput(
            symbol=plan["symbol"], option_type=plan["original_contract"]["option_type"],
            side="short", expiration=old_expiration,
            strike=float(plan["original_contract"]["strike"]),
            contracts_open=contracts, premium=premium, bid=bid, ask=ask,
            dte=old_dte, multiplier=float(multiplier), spot=spot, currency=currency,
            estimated_open_fee=open_fee, estimated_close_fee=close_fee,
            fee_calc_status="conservative_estimate",
            fee_calc_basis=f"{open_basis}+{close_basis}/{unit}",
            delta=_finite_or_none(original.get("delta")),
            input_source="user_provided", input_id=plan["input_id"],
            input_as_of_utc=plan["input_as_of_utc"],
        ))
        evidence_flags = str(advice.get("data_quality_flags") or "").strip()
        common["fee_model_id"] = plan["fee_model_id"]
        common["fee_model"] = {
            "id": plan["fee_model_id"],
            "model_id": plan["fee_model_id"],
            "version": plan["fee_model_version"],
            "fee_basis": advice.get("fee_calc_basis"),
            "currency": currency,
            "is_actual": False,
            "legs": [
                {"leg": "open_sell", "estimated_fee": advice.get("estimated_open_fee")},
                {"leg": "close_buy", "estimated_fee": advice.get("estimated_close_fee")},
            ],
            "estimated_open_fee": advice.get("estimated_open_fee"),
            "estimated_close_fee": advice.get("estimated_close_fee"),
            "fee_calc_status": advice.get("fee_calc_status"),
            "fee_calc_basis": advice.get("fee_calc_basis"),
        }
        common["recommendation_state"] = advice.get("recommendation_state")
        common["evaluation_status"] = advice.get("evaluation_status")
        common["quote"] = _public_quote(original)
        common["metrics"] = {field: advice.get(field) for field in CLOSE_METRIC_FIELDS}
        common["policy_version"] = advice.get("policy_version")
        common["reason"] = advice.get("reason")
        common["reason_codes"] = [flag for flag in evidence_flags.split(";") if flag]
        common["coverage"] = {"original_contract": "evaluated", "candidates_requested": 0}
        return common
    old_ask = _finite_or_none(original.get("ask"))
    if old_ask is None or old_ask <= 0:
        raise BusinessUnavailable("original contract quote is unavailable")
    old_buy_fee, _old_fee_basis = _scenario_fee(
        currency, old_ask, contracts=contracts, multiplier=multiplier, sell=False)
    max_expiration = plan["search_bounds"]["max_new_expiration"]
    boundary = plan["search_bounds"].get("min_new_strike" if operation == "roll-down"
                                         else "max_new_strike")
    position = {
        "symbol": plan["symbol"], "option_type": plan["original_contract"]["option_type"],
        "side": "short", "expiration": old_expiration,
        "strike": float(plan["original_contract"]["strike"]),
        "contracts_open": contracts, "multiplier": multiplier, "currency": currency,
    }
    old_quote = {"ask": old_ask, "bid": _finite_or_none(original.get("bid"))}
    if old_quote["bid"] is None:
        old_quote.pop("bid")
    candidates: list[dict[str, Any]] = []
    excluded = 0
    reasons: dict[str, int] = {}
    seen: set[str] = set()
    for row in rows:
        identity = _roll_candidate_identity(row, plan, operation, old_expiration,
                                            max_expiration, boundary, spot, currency, multiplier)
        if identity is None:
            continue
        key = identity["contract_symbol"]
        if key in seen:
            continue
        seen.add(key)
        candidate_reason = _scenario_evidence_reason(
            row, option_type=identity["option_type"], quote_side="sell", now=now)
        if candidate_reason is not None:
            excluded += 1
            reasons[candidate_reason] = reasons.get(candidate_reason, 0) + 1
            continue
        identity["contracts"] = contracts
        new_dte = identity.pop("dte")
        try:
            sell_fee, sell_basis = _scenario_fee(
                currency, identity["bid"], contracts=contracts,
                multiplier=multiplier, sell=True)
            if operation == "roll-down":
                evaluation = evaluate_user_provided_short_put_roll(
                    position, identity, old_quote=old_quote, spot=spot,
                    old_dte=old_dte, new_dte=new_dte, old_buy_fee=old_buy_fee,
                    new_sell_fee=sell_fee)
                accepted = bool(evaluation.get("accepted"))
            else:
                evaluation = evaluate_conditional_covered_call_roll(
                    position, identity, old_quote=old_quote, spot=spot,
                    old_dte=old_dte, new_dte=new_dte, old_buy_fee=old_buy_fee,
                    new_sell_fee=sell_fee)
                accepted = evaluation.get("economic_result") == "positive_net_credit"
        except ValueError:
            excluded += 1
            reasons["candidate_evidence_invalid"] = reasons.get("candidate_evidence_invalid", 0) + 1
            continue
        if not accepted:
            excluded += 1
            for reason in evaluation.get("reason_codes") or []:
                reasons[str(reason)] = reasons.get(str(reason), 0) + 1
            continue
        metrics = dict(evaluation.get("metrics") or {})
        candidate = {
            "contract_symbol": identity["contract_symbol"],
            "option_contract": {
                "option_type": identity["option_type"],
                "expiration": identity["expiration"],
                "strike": identity["strike"],
            },
            "expiration": identity["expiration"],
            "strike": identity["strike"],
            "contracts": identity["contracts"],
            "bid": identity["bid"], "ask": identity["ask"],
            "dte": new_dte,
            "fee_model": plan["fee_model_id"],
            "fee_model_version": plan["fee_model_version"],
            "fee_basis": sell_basis,
            "estimated_sell_fee": sell_fee,
            "accepted": accepted,
            **metrics,
            "_metrics": metrics,
            "reason_codes": list(evaluation.get("reason_codes") or []),
        }
        if operation == "roll-up":
            candidate.update({key_name: evaluation.get(key_name)
                              for key_name in ("required_shares", "economic_result",
                                               "unverified_conditions")})
        candidates.append(candidate)
    ranked = (rank_short_put_roll_candidates(candidates) if operation == "roll-down"
              else rank_covered_call_roll_candidates(candidates))
    common["candidates"] = [_public_candidate(row) for row in ranked[:limit]]
    common["displayed"] = len(common["candidates"])
    common["omitted"] = max(0, len(ranked) - len(common["candidates"]))
    scope_complete = _scenario_scope_complete(
        operation=operation, requested_boundary=boundary,
        max_expiration=max_expiration, effective_scope=effective_scope)
    complete = bool(seen) and scope_complete
    if not seen:
        common["status"] = "unavailable"
    elif not complete:
        common["status"] = "partial"
    else:
        common["status"] = "success"
    common["outcome"] = "found" if candidates else ("none" if complete else "unable")
    common["reason_codes"] = ([] if candidates else
                              ["no_qualified_candidate" if complete
                               else "search_scope_not_proven_complete"])
    common["fee_model_id"] = plan["fee_model_id"]
    common["fee_model"] = {"id": plan["fee_model_id"],
                           "model_id": plan["fee_model_id"],
                           "version": plan["fee_model_version"],
                           "fee_basis": "futu_option_estimate",
                           "currency": currency,
                           "is_actual": False,
                           "legs": [{"leg": "old_buy",
                                     "estimated_fee": old_buy_fee}]}
    common["old_quote"] = dict(old_quote)
    common["scope"] = {"max_new_expiration": max_expiration,
                       "strike_boundary": boundary}
    common["coverage"] = {
        "requested_max_expiration": max_expiration,
        "requested_strike_boundary": boundary,
        "effective_max_expiration": effective_scope.get("max_expiration"),
        "effective_strike_boundary": (
            effective_scope.get("min_strike") if operation == "roll-down"
            else effective_scope.get("max_strike")),
        "candidate_count": len(seen),
        "matched": len(candidates),
        "excluded": excluded,
        "exclusion_reasons": dict(sorted(reasons.items())),
        "limit": limit,
        "complete": complete,
    }
    if operation == "roll-up":
        common["schema_version"] = "group_query_roll_up_result.v1"
        common["account_eligibility"] = "not_verified"
        required_shares = (contracts * multiplier
                           if contracts > 0 and multiplier > 0 else None)
        common["stock_coverage"] = {
            "verification_status": "not_verified",
            "required_shares": required_shares,
        }
        common["unverified_conditions"] = [
            "actual_share_count_not_verified",
            "all_unclosed_short_call_coverage_not_verified",
            "duplicate_obligation_not_verified",
        ]
        common["calculation_kind"] = "leg_cashflow_estimate"
    return common


def _scenario_scope_complete(*, operation: str, requested_boundary: Any,
                             max_expiration: str,
                             effective_scope: dict[str, Any]) -> bool:
    """A requested range counts as complete only when the fetch covered it."""
    effective_max = effective_scope.get("max_expiration")
    if not effective_max or str(effective_max) < str(max_expiration):
        return False
    if requested_boundary is None:
        return True
    boundary = float(requested_boundary)
    if operation == "roll-down":
        effective = effective_scope.get("min_strike")
        return effective is None or float(effective) <= boundary
    effective = effective_scope.get("max_strike")
    return effective is None or float(effective) >= boundary


def _roll_candidate_identity(row: dict[str, Any], plan: dict[str, Any], operation: str,
                             old_expiration: str, max_expiration: str, boundary: Any,
                             spot: float, currency: str, multiplier: int,
                             ) -> dict[str, Any] | None:
    if str(row.get("option_type") or "").lower() != plan["original_contract"]["option_type"]:
        return None
    if str(row.get("currency") or "").strip().upper() != currency:
        return None
    if _finite_or_none(row.get("multiplier")) != float(multiplier):
        return None
    expiration = str(row.get("expiration") or "")
    if not expiration or expiration > max_expiration or expiration < old_expiration:
        return None
    strike = _finite_or_none(row.get("strike"))
    bid = _finite_or_none(row.get("bid"))
    ask = _finite_or_none(row.get("ask"))
    if strike is None or strike <= 0 or bid is None or bid <= 0 or ask is None or ask <= 0:
        return None
    if operation == "roll-down":
        if strike >= float(plan["original_contract"]["strike"]) or strike >= spot:
            return None
        if boundary is not None and strike < float(boundary):
            return None
    else:
        if strike <= float(plan["original_contract"]["strike"]):
            return None
        if boundary is not None and strike > float(boundary):
            return None
    contract_symbol = str(row.get("contract_symbol") or "")
    if not contract_symbol:
        return None
    dte = (date_from_iso(expiration) - date_from_iso(plan["as_of_market_date"])).days
    return {"contract_symbol": contract_symbol, "symbol": plan["symbol"],
            "option_type": plan["original_contract"]["option_type"],
            "expiration": expiration, "strike": strike, "bid": bid, "ask": ask,
            "currency": currency, "multiplier": multiplier,
            "volume": _finite_or_none(row.get("volume")),
            "open_interest": _finite_or_none(row.get("open_interest")),
            "dte": dte}


def date_from_iso(value: Any):
    from datetime import date
    return date.fromisoformat(str(value))


__all__ = [
    "BusinessService",
    "BusinessUnavailable",
    "Dependencies",
    "FetchPlan",
]
