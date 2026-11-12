"""Point-of-use quote refresh keeps stale-scan put roll windows deliverable (#1098).

The sealed required-data receipts are observed at scan time; by the time the
account brief is assembled the 300s quote window can be nearly exhausted. The
refresh seam re-observes the same contract identities at the point of use so
``actionable_until_utc`` is recomputed from fresh evidence. When the refresh is
unavailable or itself stale, the candidate must stay out of the delivery set
without raising.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import MethodType

from src.application import short_put_roll
from src.application.close_advice_required_data import (
    PLAN_FILE_NAME,
    build_close_advice_required_data_plan,
    close_advice_market_date,
    enrich_close_advice_required_data_plan,
    publish_close_advice_required_data_plan,
)
from src.application.close_advice_runner import run_close_advice
from src.application.daily_decision_brief_service import _position_view
from src.application.ledger.api import build_position_risk_context
from src.application.multi_tick.required_data_prefetch import prefetch_required_data
from src.application.opening_quote_evidence import normalize_underlier_observation
from src.application.required_data_prefetch_planning import (
    merge_close_advice_requirements_into_prefetch_config,
)
from src.application.required_data_snapshot import seal_required_data_snapshot
from src.application.runtime_paths import runtime_root_scope
from domain.domain.daily_decision_brief import (
    daily_brief_position_roll_identity,
    daily_brief_roll_notification_identities,
)
from test_close_advice_required_data import _position
from test_daily_decision_brief_notification_flow import _brief
from test_position_advice_pipeline import pipeline  # noqa: F401
from test_required_data_prefetch_inprocess import _strict_success_rows_payload
from test_short_put_roll_service import quote


SEALED_AGE_SECONDS = 290
DELIVERY_DELAY_SECONDS = 60


def _run_close_advice(
    tmp_path,
    monkeypatch,
    pipeline,
    *,
    sealed_age_seconds=SEALED_AGE_SECONDS,
    refresh_fn=None,
    refresh_rows_age_seconds=0,
):
    """Run the real sealed pipeline with put rows observed ``sealed_age_seconds`` ago."""
    from test_position_advice_service import config

    _, provider, _ = pipeline
    cfg = config()
    cfg["market_data"] = {"provider": provider.name}
    # Exercise quote age without also crossing a New York session date.
    scan_at = datetime.now(timezone.utc).replace(hour=15, minute=0, second=0, microsecond=0)
    evaluation_now = scan_at + timedelta(seconds=sealed_age_seconds)
    now = scan_at
    old_expiry = provider.payload["expirations"][0]
    new_expiry = (now + timedelta(days=35)).date().isoformat()
    spot = 98
    provider.payload["expirations"] = [old_expiry, new_expiry]
    observation = normalize_underlier_observation(code="US.NVDA", market="US", snapshot_row={
        "code": "US.NVDA", "last_price": spot, "update_time": now.isoformat(),
        "sec_status": "NORMAL", "suspension": False,
    }, market_state_row={"code": "US.NVDA", "market_state": "MORNING"}, now_utc=now)
    provider.payload["meta"]["underlier_observation"] = observation.to_dict()
    cfg["symbols"][0]["sell_put"] = {"enabled": False, "max_dte": 60}
    record = _position(account="sample", lot_id="lot-1", expiration=old_expiry)
    record["fields"]["contract_key"]["option_type"] = "put"
    record["fields"]["premium_open"] = 2
    records = [record]
    forbidden_calls = []

    def forbidden(*args, **kwargs):
        forbidden_calls.append(True)
        raise AssertionError("scheduled evaluation attempted another broker/ledger fetch")

    monkeypatch.setattr(short_put_roll, "_read_holdings", forbidden)
    monkeypatch.setattr(short_put_roll, "load_account_portfolio_context", forbidden)
    monkeypatch.setattr(short_put_roll, "resolve_market_data_provider", forbidden)

    state = {"phase": "sealed"}

    class EvidenceClock(datetime):
        @classmethod
        def now(cls, tz=None):
            observed = scan_at if state["phase"] == "sealed" else evaluation_now
            return observed.astimezone(tz) if tz is not None else observed.replace(tzinfo=None)

    # Keep post-snapshot rights/state receipts on the same clock as the rows.
    monkeypatch.setattr("src.application.close_advice_required_data.datetime", EvidenceClock)
    monkeypatch.setattr("src.application.roll_quote_refresh.datetime", EvidenceClock)
    monkeypatch.setattr("src.application.required_data_snapshot.datetime", EvidenceClock)
    monkeypatch.setattr("src.application.opend_symbol_outputs.datetime", EvidenceClock)

    def fetch(self, request, **kwargs):
        self.calls.append(("fetch", request))
        if state["phase"] == "sealed":
            received = scan_at
        else:
            received = evaluation_now - timedelta(seconds=refresh_rows_age_seconds)
        rows = []
        for expiry in request.explicit_expirations:
            for strike in (100, 95):
                rows.append(quote(
                    strike, expiry,
                    bid=4.9 if strike == 100 else 6.5,
                    ask=5 if strike == 100 else 6.6,
                    option_type="put", spot=spot,
                    contract_symbol=(
                        f"US.NVDA{expiry.replace('-', '')[2:]}P{strike * 1000:08d}"
                    ),
                    dte=(
                        datetime.fromisoformat(expiry).date()
                        - datetime.fromisoformat(request.trading_date).date()
                    ).days,
                    spot_observed_at_utc=received.isoformat(),
                    snapshot_requested_at_utc=(received - timedelta(seconds=1)).isoformat(),
                    snapshot_received_at_utc=received.isoformat(),
                ))
        return _strict_success_rows_payload(
            request.symbol, rows, trading_date=request.trading_date,
            host=request.host, port=request.port, meta={"source": self.name,
                "source_observed_at": received.isoformat(),
                "completed_at_utc": received.isoformat(),
                "underlier_observation": request.underlier_observation},
        )

    provider.fetch_symbol_request = MethodType(fetch, provider)
    run_id = "put-window-refresh"
    run_dir = tmp_path / "output_runs" / run_id
    required_root = run_dir / "required_data"
    required_root.mkdir(parents=True)
    plan_path = run_dir / "state" / PLAN_FILE_NAME
    manifest_path = run_dir / "state" / "required_data_snapshot_manifest.json"
    monkeypatch.setattr(
        "src.application.opend_utils.get_trading_date",
        lambda _market: close_advice_market_date(now, "US"),
    )
    plan = build_close_advice_required_data_plan(
        run_id=run_id, run_started_at_utc=now,
        account_configs={"sample": cfg}, base_config=cfg, markets_to_run=["US"],
        position_records_by_account={"sample": records}, include_roll_candidates=True,
    )
    candidate = deepcopy(cfg)
    candidate["symbols"] = []
    prefetch_config, plan = merge_close_advice_requirements_into_prefetch_config(
        candidate_config=candidate, requirements_plan=plan)
    publish_close_advice_required_data_plan(path=plan_path, payload=plan)
    repo_root = Path(__file__).resolve().parents[1]
    with runtime_root_scope(tmp_path):
        summary = prefetch_required_data(
            vpy=Path(sys.executable), base=tmp_path, repo_root=repo_root,
            cfg=prefetch_config, shared_required=required_root, force_refresh=True,
            producer_run_id=run_id, scan_at_utc=now,
        )
        enrich_close_advice_required_data_plan(plan_path=plan_path, expected_run_id=run_id)
        seal_required_data_snapshot(
            manifest_path=manifest_path, required_data_root=required_root,
            run_id=run_id, prefetch_summary=summary,
            close_advice_required_data_plan_path=plan_path,
            sealed_at=scan_at,
        )
        context = build_position_risk_context(
            records, broker="富途", account="sample", observed_at=now)
        output_dir = run_dir / "accounts" / "sample"

        def refresh(**kwargs):
            state["phase"] = "refresh"
            return refresh_fn(**kwargs)

        refresh_kwargs = (
            {} if refresh_fn is None else {"roll_quote_refresh_fn": refresh}
        )
        result = run_close_advice(
            config=cfg,
            context_path=output_dir / "state" / "option_positions_context.json",
            context_override=context,
            required_data_root=required_root,
            output_dir=output_dir,
            base_dir=repo_root,
            required_data_snapshot_manifest=manifest_path,
            required_data_snapshot_run_id=run_id,
            close_advice_required_data_plan=plan_path,
            account="sample",
            markets_to_run=["US"],
            now_utc=evaluation_now,
            **refresh_kwargs,
        )
    assert forbidden_calls == []
    assert result.get("snapshot_authority") == "valid", {
        key: result.get(key)
        for key in ("enabled", "status", "rows", "notify_rows", "reason", "error")
    }
    from src.application.daily_decision_brief_service import _load_close_advice

    source_artifacts, data_gaps = [], []
    rows, loaded = _load_close_advice(
        path=output_dir / "close_advice.csv", run_account_dir=output_dir,
        market="US", account="sample", run_id=run_id,
        source_artifacts=source_artifacts, data_gaps=data_gaps,
    )
    assert loaded is True and data_gaps == []
    return evaluation_now, rows[0], rows[0]["roll_advice"]


def _view(row):
    return _position_view(
        row, notification_eligible=True, close_actionable=False,
    )


def _brief_with(tmp_path, row):
    brief = _brief(base=tmp_path, run_id="put-window-refresh", account="sample", candidate=False)
    brief["positions"] = [_view(row)]
    return brief


def test_sealed_scan_evidence_expires_before_delivery(tmp_path, monkeypatch, pipeline):  # noqa: F811
    """Reproduction: the sealed 300s window closes before the candidate is sent."""
    now, row, roll = _run_close_advice(tmp_path, monkeypatch, pipeline)
    assert (roll["status"], roll["outcome"]) == ("success", "found"), {
        key: roll.get(key) for key in ("status", "outcome", "reason_codes", "coverage")
    }
    assert roll["candidates"], roll
    window = datetime.fromisoformat(roll["actionable_until_utc"])
    send_at = now + timedelta(seconds=DELIVERY_DELAY_SECONDS)
    # The scan-time observation already consumed most of the 300s window.
    assert window < send_at
    assert (window - now).total_seconds() <= 15
    brief = _brief_with(tmp_path, row)
    assert daily_brief_position_roll_identity(_view(row), account="sample", market="US")
    assert daily_brief_roll_notification_identities(brief, now_utc=send_at) == set()


def test_point_of_use_refresh_keeps_put_roll_deliverable(tmp_path, monkeypatch, pipeline):  # noqa: F811
    """The point-of-use refresh re-anchors the window on fresh observations."""
    from src.application.roll_quote_refresh import refresh_roll_quote_evidence

    now, row, roll = _run_close_advice(
        tmp_path, monkeypatch, pipeline, refresh_fn=refresh_roll_quote_evidence,
    )
    assert (roll["status"], roll["outcome"]) == ("success", "found"), {
        key: roll.get(key)
        for key in ("status", "outcome", "reason_codes", "quote_evidence")
    }
    evidence = roll["quote_evidence"]
    assert evidence["source"] == "point_of_use_refresh"
    assert evidence["status"] == "refreshed"
    observed = datetime.fromisoformat(evidence["observed_at_utc"])
    assert observed >= now
    window = datetime.fromisoformat(roll["actionable_until_utc"])
    send_at = now + timedelta(seconds=DELIVERY_DELAY_SECONDS)
    assert window > send_at
    candidate = roll["candidates"][0]
    assert datetime.fromisoformat(
        candidate["evidence"]["snapshot_received_at_utc"]) >= now
    assert datetime.fromisoformat(
        candidate["evidence"]["spot_observed_at_utc"]) >= now
    brief = _brief_with(tmp_path, row)
    assert daily_brief_position_roll_identity(_view(row), account="sample", market="US") in (
        daily_brief_roll_notification_identities(brief, now_utc=send_at)
    )


def test_refresh_with_stale_quotes_keeps_candidate_dropped(tmp_path, monkeypatch, pipeline):  # noqa: F811
    """A refresh that itself observes late is still skipped, without raising."""
    from src.application.roll_quote_refresh import refresh_roll_quote_evidence

    now, row, roll = _run_close_advice(
        tmp_path, monkeypatch, pipeline,
        refresh_fn=refresh_roll_quote_evidence,
        refresh_rows_age_seconds=400,
    )
    assert roll["quote_evidence"]["source"] == "point_of_use_refresh"
    assert roll["quote_evidence"]["status"] == "stale"
    assert "quote_refresh_observed_stale" in roll["quote_evidence"]["reason_codes"]
    assert roll.get("candidates") in (None, [],)
    brief = _brief_with(tmp_path, row)
    assert daily_brief_roll_notification_identities(
        brief, now_utc=now + timedelta(seconds=DELIVERY_DELAY_SECONDS)
    ) == set()


def test_fresh_sealed_evidence_skips_the_refresh(tmp_path, monkeypatch, pipeline):  # noqa: F811
    """Inside the refresh threshold the sealed rows stay authoritative."""
    from src.application.roll_quote_refresh import refresh_roll_quote_evidence

    calls = []

    def refresh(**kwargs):
        calls.append(kwargs)
        return refresh_roll_quote_evidence(**kwargs)

    now, row, roll = _run_close_advice(
        tmp_path, monkeypatch, pipeline,
        sealed_age_seconds=60, refresh_fn=refresh,
    )
    assert calls == []
    assert roll["quote_evidence"]["source"] == "sealed_scan"
    assert roll["quote_evidence"]["status"] == "not_needed"
    assert roll["quote_evidence"]["sealed_age_seconds"] >= 60
    assert roll["quote_evidence"]["window_anchor"] == "quote_observation"
    assert datetime.fromisoformat(roll["actionable_until_utc"]) > (
        now + timedelta(seconds=DELIVERY_DELAY_SECONDS)
    )


def test_refresh_failure_stays_fail_closed(tmp_path, monkeypatch, pipeline):  # noqa: F811
    """An unavailable refresh neither crashes nor sends the stale window."""
    def refresh(**kwargs):
        raise TimeoutError("isolated refresh failure")

    now, row, roll = _run_close_advice(
        tmp_path, monkeypatch, pipeline, refresh_fn=refresh,
    )
    assert roll["quote_evidence"]["source"] == "point_of_use_refresh"
    assert roll["quote_evidence"]["status"] == "unavailable"
    assert "quote_refresh_failed" in roll["quote_evidence"]["reason_codes"]
    if roll.get("candidates"):
        assert datetime.fromisoformat(roll["actionable_until_utc"]) <= (
            now + timedelta(seconds=DELIVERY_DELAY_SECONDS)
        )
    brief = _brief_with(tmp_path, row)
    assert daily_brief_roll_notification_identities(
        brief, now_utc=now + timedelta(seconds=DELIVERY_DELAY_SECONDS)
    ) == set()
