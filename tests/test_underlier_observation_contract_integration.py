"""Offline regression for the extended OpenD underlier-observation contract.

Provider responses, wall-clock waiting and chain/snapshot retry + rate-limit
adapters are isolated. Everything on the contract path is real:

    get_underlier_observations_opend
      -> build_required_data_fetch_plan
      -> build_fetch_request_from_spec / execute_required_data_opend
      -> fetch_symbol_request (including OpeningUnderlierObservation.from_mapping)
      -> merge_required_data_payloads / bind_merged_payload_evidence
      -> validate_required_data_payload_candidate
      -> finalize_required_data_quote_candidate (receipt seal)
      -> seal_required_data_snapshot -> two account consumers

The regression is that ``OpenDUnderlierObservation.to_dict`` used to inherit the
base ``asdict(self)`` and leak ``fetch_diagnostics`` into the expected fetch
contract. The business observation must stay a pure
``opening_underlier_observation.v1`` payload while the diagnostics survive only
in the ``spot_observation_diagnostics`` sidecar.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pandas as pd
import pytest

from src.application import opend_market_snapshot_fetching as snapshot_fetching
from src.application import opend_symbol_fetching as symbol_fetching
from src.application import required_data_fetching
from src.application import required_data_planning as planning
from src.application.opend_fetch_config import OpenDEndpointRateLimit
from src.application.opend_symbol_outputs import (
    finalize_required_data_quote_candidate,
    validate_required_data_payload_candidate,
    validate_required_data_quote_candidate,
)
from src.application.opening_quote_evidence import OpeningUnderlierObservation
from src.application.required_data_plan_identity import (
    build_required_data_expected_fetch_contract,
)
from src.application.source_receipts import SourceReceiptError
from src.application.required_data_snapshot import seal_required_data_snapshot
from src.application.required_data_steps import ensure_required_data
from src.infrastructure import futu_gateway
from tests.test_required_data_snapshot import _summary


NOW = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)
TRADING_DATE = "2026-08-06"
HOST = "127.0.0.1"
PORT = 11111
RUN_ID = "run-underlier-observation-contract"


@dataclass(frozen=True)
class _MarketCase:
    market: str
    symbol: str
    provider_code: str
    spot: float
    strike: float
    expiration: str
    update_time: str


_US_CASE = _MarketCase(
    market="US",
    symbol="NVDA",
    provider_code="US.NVDA",
    spot=100.0,
    strike=95.0,
    expiration="2026-08-21",
    update_time="2026-08-06 10:59:00",
)

_HK_CASE = _MarketCase(
    market="HK",
    symbol="0700.HK",
    provider_code="HK.00700",
    spot=400.0,
    strike=380.0,
    expiration="2026-08-21",
    update_time="2026-08-06 22:59:00",
)


class _FakeOpenDGateway:
    """Provider transport only: no logic owned by OM is reimplemented."""

    def __init__(
        self,
        *,
        case: _MarketCase,
        chain_specs: dict[tuple[str, str], float],
        transient_underlier_failures: int = 0,
        calls: list | None = None,
    ) -> None:
        self.case = case
        self.calls = calls if calls is not None else []
        self.chain_specs = dict(chain_specs)
        self._transient_failures_remaining = int(transient_underlier_failures)
        self._option_specs = {
            self._option_code(expiration, side, strike): (
                expiration,
                side,
                strike,
            )
            for (expiration, side), strike in self.chain_specs.items()
        }
        self.closed = False
        self.option_chain_calls: list[dict[str, object]] = []

    def _option_code(self, expiration: str, side: str, strike: float) -> str:
        suffix = "P" if side == "put" else "C"
        return f"{self.case.provider_code}.{expiration.replace('-', '')}.{suffix}{strike:g}"

    def _chain_row(self, expiration: str, side: str, strike: float) -> dict[str, object]:
        return {
            "code": self._option_code(expiration, side, strike),
            "strike_time": expiration,
            "strike_price": strike,
            "option_type": side.upper(),
            "option_standard_type": "STANDARD",
            "stock_owner": self.case.provider_code,
            "stock_type": "DRVT",
            "suspension": False,
            "lot_size": 100,
        }

    def _option_snapshot_row(
        self,
        code: str,
        option_spec: tuple[str, str, float],
    ) -> dict[str, object]:
        _expiration, side, _strike = option_spec
        return {
            "code": code,
            "last_price": 1.0,
            "bid_price": 0.9,
            "ask_price": 1.1,
            "bid_vol": 10,
            "ask_vol": 12,
            "price_spread": 0.01,
            "volume": 5,
            "open_interest": 50,
            "sec_status": "NORMAL",
            "suspension": False,
            "option_contract_multiplier": 100,
            "option_contract_size": 100,
            "option_delta": -0.3 if side == "put" else 0.3,
            "option_gamma": 0.01,
            "option_theta": -0.02,
            "option_vega": 0.05,
            "option_rho": 0.01,
            "update_time": self.case.update_time,
        }

    def get_snapshot(self, codes: list[str]) -> pd.DataFrame:
        self.calls.append(("snapshot", list(codes)))
        rows: list[dict[str, object]] = []
        for raw_code in codes:
            code = str(raw_code)
            if code == self.case.provider_code:
                if self._transient_failures_remaining:
                    self._transient_failures_remaining -= 1
                    raise ConnectionError("connection reset")
                rows.append(
                    {
                        "code": code,
                        "last_price": self.case.spot,
                        "update_time": self.case.update_time,
                        "sec_status": "NORMAL",
                        "suspension": False,
                    }
                )
                continue
            option_spec = self._option_specs.get(code)
            if option_spec is not None:
                rows.append(self._option_snapshot_row(code, option_spec))
        return pd.DataFrame(rows)

    def get_market_state(self, codes: list[str]) -> pd.DataFrame:
        self.calls.append(("market_state", list(codes)))
        return pd.DataFrame([{"code": str(code), "market_state": "MORNING"} for code in codes])

    def get_option_chain(
        self,
        *,
        code: str,
        start: object = None,
        end: object = None,
        is_force_refresh: bool = False,
        option_type: object = None,
    ) -> pd.DataFrame:
        self.calls.append(("chain", code, start, option_type))
        expiration = str(start or "")
        self.option_chain_calls.append(
            {
                "code": code,
                "start": expiration,
                "option_type": option_type,
                "is_force_refresh": is_force_refresh,
            }
        )
        if option_type:
            sides = [str(option_type).strip().lower()]
        else:
            sides = sorted({side for exp, side in self.chain_specs if exp == expiration})
        rows = [
            self._chain_row(
                expiration,
                side,
                self.chain_specs[(expiration, side)],
            )
            for side in sides
            if (expiration, side) in self.chain_specs
        ]
        return pd.DataFrame(rows)

    def close(self) -> None:
        self.closed = True


@dataclass
class _PreparedRun:
    case: _MarketCase
    observation: snapshot_fetching.OpenDUnderlierObservation
    bundle: planning.RequiredDataFetchPlanBundle
    debug: dict[str, object]
    contract: dict[str, object]
    payloads: list[dict[str, object]]
    calls: list


def _fetch_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    case: _MarketCase,
    gateway: _FakeOpenDGateway,
) -> snapshot_fetching.OpenDUnderlierObservation:
    # Keep the real retry loop but remove wall-clock backoff.
    monkeypatch.setattr(futu_gateway.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        futu_gateway.random,
        "uniform",
        lambda *_args, **_kwargs: 0.0,
    )
    observations = snapshot_fetching.get_underlier_observations_opend(
        gateway,
        [case.provider_code],
        market=case.market,
        base_dir=tmp_path,
        snapshot_limit=OpenDEndpointRateLimit(
            window_sec=30,
            max_calls=60,
            max_wait_sec=30,
        ),
        snapshot_batch_size=2,
        now_utc=lambda: NOW,
        rate_limited_call=lambda **kwargs: kwargs["call"](),
    )
    return observations[case.provider_code]


def _prepare_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    case: _MarketCase,
    chain_specs: dict[tuple[str, str], float],
    position_requirements: list[dict[str, object]] | None = None,
    sell_put_cfg: dict[str, object] | None = None,
    want_put: bool = False,
    transient_underlier_failure: bool = False,
) -> _PreparedRun:
    calls = []
    observation = _fetch_observation(
        tmp_path,
        monkeypatch,
        case=case,
        gateway=_FakeOpenDGateway(
            case=case,
            chain_specs=chain_specs,
            calls=calls,
            transient_underlier_failures=(1 if transient_underlier_failure else 0),
        ),
    )

    def _build_gateway(**_kwargs: object) -> _FakeOpenDGateway:
        return _FakeOpenDGateway(case=case, chain_specs=chain_specs, calls=calls)

    monkeypatch.setattr(
        symbol_fetching,
        "build_ready_futu_quote_gateway",
        _build_gateway,
    )
    monkeypatch.setattr(
        symbol_fetching,
        "retry_futu_gateway_call",
        lambda _name, call, **_kwargs: call(),
    )
    monkeypatch.setattr(
        symbol_fetching,
        "rate_limited_opend_call",
        lambda **kwargs: kwargs["call"](),
    )

    expirations = sorted({expiration for expiration, _side in chain_specs})
    monkeypatch.setattr(
        planning,
        "list_option_expirations",
        lambda *_args, **_kwargs: list(expirations),
    )

    identity = planning.RequiredDataPlanningIdentity(
        symbol=case.symbol,
        source="opend",
        host=HOST,
        port=PORT,
        trading_date=TRADING_DATE,
        provider_code=case.provider_code,
        market=case.market,
    )
    bundle = planning.build_required_data_fetch_plan(
        base=tmp_path,
        required_data_dir=tmp_path,
        symbol=case.symbol,
        limit_expirations=0,
        want_put=want_put,
        want_call=False,
        sell_put_cfg=sell_put_cfg,
        position_requirements=position_requirements,
        fetch_host=HOST,
        fetch_port=PORT,
        fetch_source="futu",
        spot_observation_cache={identity.cache_key: observation},
        planning_identity=identity,
    )
    debug = bundle.to_debug_dict()
    assert bundle.projection_outcome == "success_rows", bundle.spot_observation_error
    assert bundle.merged_specs
    contract = build_required_data_expected_fetch_contract(
        symbol=case.symbol,
        fetch_plan=debug,
        source="opend",
        host=HOST,
        port=PORT,
    )
    payloads = [
        required_data_fetching.execute_required_data_opend(
            base=tmp_path,
            request=required_data_fetching.build_fetch_request_from_spec(
                spec=spec,
                chain_cache=False,
                spot_override=bundle.spot_reference,
                underlier_observation=bundle.underlier_observation.to_dict(),
            ),
        )
        for spec in bundle.merged_specs
    ]
    assert calls.count(("snapshot", [case.provider_code])) == (2 if transient_underlier_failure else 1)
    assert calls.count(("market_state", [case.provider_code])) == 1
    return _PreparedRun(
        case=case,
        observation=observation,
        bundle=bundle,
        debug=debug,
        contract=contract,
        payloads=payloads,
        calls=calls,
    )


def _assert_extended_observation(
    run: _PreparedRun,
    *,
    transient: bool,
) -> None:
    observation = run.observation
    assert isinstance(observation, snapshot_fetching.OpenDUnderlierObservation)
    assert observation.status == "ready"
    assert run.bundle.underlier_observation is observation
    assert run.bundle.spot_observation_complete is True

    diagnostics = observation.fetch_diagnostics
    assert diagnostics
    snapshot_evidence = next(item for item in diagnostics if item["endpoint"] == "get_snapshot")
    assert snapshot_evidence["attempts"] == (2 if transient else 1)
    assert snapshot_evidence["stop_reason"] == "success"
    if transient:
        assert snapshot_evidence["errors"][0]["error_class"] == "ConnectionError"
    else:
        assert snapshot_evidence["errors"] == []

    business_observation = run.debug["underlier_observation"]
    assert isinstance(business_observation, dict)
    assert "fetch_diagnostics" not in business_observation
    assert business_observation == (OpeningUnderlierObservation.from_mapping(business_observation).to_dict())
    # The diagnostics survive, but only in the dedicated sidecar.
    assert run.debug["spot_observation_diagnostics"] == diagnostics


def _assert_payload_observation(payload: dict[str, object], run: _PreparedRun) -> None:
    meta = payload["meta"]
    assert isinstance(meta, dict)
    observation = meta["underlier_observation"]
    assert observation == run.debug["underlier_observation"]
    assert isinstance(observation, dict)
    assert "fetch_diagnostics" not in observation


def _finalize_and_assert(
    run: _PreparedRun,
    payload: dict[str, object],
    tmp_path: Path,
    *,
    rows_expected: int,
) -> tuple[dict[str, object], dict[str, object]]:
    # Explicit strict-validator pass before the finalizer repeats it.
    validate_required_data_payload_candidate(
        payload=payload,
        expected_fetch_contract=run.contract,
    )
    now = datetime.now(timezone.utc)
    root = tmp_path / "output_runs" / RUN_ID / "required_data"
    result = finalize_required_data_quote_candidate(
        base=tmp_path,
        producer_root=root,
        producer_run_id=RUN_ID,
        symbol=run.case.symbol,
        expected_fetch_contract=run.contract,
        fetch_policy={"source": "opend", "host": HOST, "port": PORT},
        mode="fresh",
        payload=payload,
        now=now,
    )
    receipt_path = result["quote_receipt_path"]
    assert receipt_path is not None
    assert Path(receipt_path).is_file()
    assert result["evidence"] is not None
    validate_required_data_quote_candidate(
        producer_root=root,
        raw_path=result["raw_path"],
        csv_path=result["csv_path"],
        expected_fetch_contract=run.contract,
        now=now,
        require_fresh=True,
    )

    raw = json.loads(Path(result["raw_path"]).read_text(encoding="utf-8"))
    raw_observation = raw["meta"]["underlier_observation"]
    assert raw_observation == run.debug["underlier_observation"]
    assert "fetch_diagnostics" not in raw_observation
    frame = pd.read_csv(result["csv_path"])
    assert len(frame) == rows_expected

    manifest_path = root.parent / "state" / "required_data_snapshot_manifest.json"
    manifest = seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=root,
        run_id=RUN_ID,
        prefetch_summary=_summary(run.case.symbol, fetch_plans={run.case.symbol: run.debug}),
        sealed_at=now,
    )
    assert manifest["status"] == "complete"
    calls_before = list(run.calls)
    account_bytes = {}
    account_evidence = {}
    for account in ("lx", "sy"):
        received = []
        account_evidence[account] = ensure_required_data(
            py=sys.executable,
            base=tmp_path,
            symbol=run.case.symbol,
            required_data_dir=root,
            limit_expirations=0,
            want_put=True,
            want_call=False,
            timeout_sec=10,
            is_scheduled=True,
            state_dir=root.parent / "accounts" / account / "state",
            required_data_snapshot_manifest=manifest_path,
            required_data_snapshot_run_id=RUN_ID,
            required_data_csv_bytes_sink_fn=received.append,
        )
        assert len(received) == 1
        account_bytes[account] = received[0]
    assert account_bytes["lx"] == account_bytes["sy"] == Path(result["csv_path"]).read_bytes()
    assert account_evidence["lx"]["snapshot_id"] == account_evidence["sy"]["snapshot_id"]
    assert run.calls == calls_before
    return result, raw


@pytest.mark.parametrize(
    "case",
    [_US_CASE, _HK_CASE],
    ids=["us", "hk"],
)
@pytest.mark.parametrize(
    "transient",
    [False, True],
    ids=["success", "retry-success"],
)
def test_real_chain_finalizes_extended_underlier_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: _MarketCase,
    transient: bool,
) -> None:
    chain_specs = {(case.expiration, "put"): case.strike}
    run = _prepare_run(
        tmp_path,
        monkeypatch,
        case=case,
        chain_specs=chain_specs,
        position_requirements=[
            {
                "option_type": "put",
                "expiration": case.expiration,
                "strike": case.strike,
                "planning_status": "ready",
            }
        ],
        transient_underlier_failure=transient,
    )

    assert run.bundle.projection_outcome == "success_rows"
    assert len(run.bundle.merged_specs) == 1

    payload = run.payloads[0]
    meta = payload["meta"]
    assert isinstance(meta, dict)
    assert meta["status"] == "ok", meta.get("error")
    assert meta["source_outcome"] == "success_rows"
    assert meta["reason_code"] is None
    assert len(payload["rows"]) == 1
    result, raw = _finalize_and_assert(
        run,
        payload,
        tmp_path,
        rows_expected=1,
    )
    _assert_extended_observation(run, transient=transient)
    _assert_payload_observation(payload, run)
    assert len(raw["rows"]) == 1
    assert str(raw["rows"][0]["contract_symbol"]).startswith(case.provider_code)


@pytest.mark.parametrize("case", [_US_CASE, _HK_CASE], ids=["us", "hk"])
def test_real_merge_and_child_binding_seal_shared_extended_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: _MarketCase,
) -> None:
    put_expiration = "2026-08-21"
    call_expiration = "2026-09-18"
    chain_specs = {
        (put_expiration, "put"): case.strike,
        (call_expiration, "call"): case.spot * 1.05,
    }
    run = _prepare_run(
        tmp_path,
        monkeypatch,
        case=case,
        chain_specs=chain_specs,
        position_requirements=[
            {
                "option_type": "put",
                "expiration": put_expiration,
                "strike": case.strike,
                "planning_status": "ready",
            },
            {
                "option_type": "call",
                "expiration": call_expiration,
                "strike": case.spot * 1.05,
                "planning_status": "ready",
            },
        ],
    )

    _assert_extended_observation(run, transient=False)
    assert run.bundle.projection_outcome == "success_rows"
    assert len(run.bundle.merged_specs) == 2
    assert len(run.payloads) == 2

    bound_payloads: list[dict[str, object]] = []
    for index, spec in enumerate(run.bundle.merged_specs):
        bound = required_data_fetching.bind_required_data_child_request_evidence(
            payload=run.payloads[index],
            planned_request=spec.to_debug_dict(),
            request_index=index,
        )
        _assert_payload_observation(bound, run)
        bound_payloads.append(bound)

    merged = required_data_fetching.merge_required_data_payloads(
        symbol=case.symbol,
        payloads=bound_payloads,
    )
    required_data_fetching.bind_merged_payload_evidence(
        merged_payload=merged,
        payloads=bound_payloads,
    )

    merged_meta = merged["meta"]
    assert isinstance(merged_meta, dict)
    assert merged_meta["status"] == "ok", merged_meta.get("error")
    assert merged_meta["source_outcome"] == "success_rows"
    assert merged_meta["underlier_observation"] == run.debug["underlier_observation"]
    assert "fetch_diagnostics" not in merged_meta["underlier_observation"]
    requests = merged_meta["requests"]
    assert isinstance(requests, list) and len(requests) == 2
    assert len(merged["rows"]) == 2

    _result, raw = _finalize_and_assert(
        run,
        merged,
        tmp_path,
        rows_expected=2,
    )
    raw_requests = raw["meta"]["requests"]
    assert [child["request_index"] for child in raw_requests] == [0, 1]
    assert len(raw["rows"]) == 2


@pytest.mark.parametrize("case", [_US_CASE, _HK_CASE], ids=["us", "hk"])
def test_real_filtered_empty_chain_seals_extended_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: _MarketCase,
) -> None:
    # The chain proves the requested expiration but every row is outside the
    # spot-derived put window, so the provider result is a proven empty scope.
    chain_specs = {(case.expiration, "put"): 500.0}
    run = _prepare_run(
        tmp_path,
        monkeypatch,
        case=case,
        chain_specs=chain_specs,
        sell_put_cfg={
            "enabled": True,
            "strategy": "insurance_underwriting",
            "min_dte": 7,
            "max_dte": 60,
        },
        want_put=True,
    )

    _assert_extended_observation(run, transient=False)
    assert run.bundle.projection_outcome == "success_rows"
    assert len(run.bundle.merged_specs) == 1

    payload = run.payloads[0]
    meta = payload["meta"]
    assert isinstance(meta, dict)
    assert payload["rows"] == []
    assert meta["status"] == "ok", meta.get("error")
    assert meta["source_outcome"] == "success_empty"
    assert meta["reason_code"] == "no_contract_rows"
    assert meta["realized_volatility"]["status"] == ("not_applicable_no_contracts")
    _assert_payload_observation(payload, run)

    result, raw = _finalize_and_assert(
        run,
        payload,
        tmp_path,
        rows_expected=0,
    )
    assert raw["rows"] == []
    assert pd.read_csv(result["csv_path"]).empty


@pytest.mark.parametrize(
    "field",
    ["code", "last_price", "observed_at_utc"],
    ids=["code", "price", "time"],
)
def test_strict_validator_rejects_tampered_underlier_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    case = _US_CASE
    run = _prepare_run(
        tmp_path,
        monkeypatch,
        case=case,
        chain_specs={(case.expiration, "put"): case.strike},
        position_requirements=[
            {
                "option_type": "put",
                "expiration": case.expiration,
                "strike": case.strike,
                "planning_status": "ready",
            }
        ],
    )
    _assert_extended_observation(run, transient=False)

    payload = run.payloads[0]
    # The untampered payload is accepted only when the expected contract no
    # longer carries fetch_diagnostics.
    validate_required_data_payload_candidate(
        payload=payload,
        expected_fetch_contract=run.contract,
    )

    tampered = deepcopy(payload)
    observation = tampered["meta"]["underlier_observation"]
    assert isinstance(observation, dict)
    if field == "code":
        observation["code"] = "US.AAPL"
    elif field == "last_price":
        observation["last_price"] = float(observation["last_price"]) + 10.0
    else:
        observation["observed_at_utc"] = "2026-08-06T13:00:00+00:00"

    with pytest.raises(
        SourceReceiptError,
        match="underlier observation mismatch",
    ):
        validate_required_data_payload_candidate(
            payload=tampered,
            expected_fetch_contract=run.contract,
        )
