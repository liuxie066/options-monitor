"""Provider-free, nonempty Combo history and ledger for intake A/B checks."""
from datetime import date, datetime, timedelta, timezone
import json

from domain.domain.combo_candidate_evidence import (
    build_combo_candidate_occurrence, combo_exposure_render_context, derive_combo_candidate_exposures,
)
from src.application import daily_decision_brief_repository as history
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object
from src.application.notification_delivery_adapter import build_notification_transport_key
from test_daily_decision_brief_repository_v2 import _legacy_combo_brief
from test_trades_combo_reconciliation import _event


def seed_intake_combo_history(base, *, days=3, requested_dates=3):
    """Seed only isolated local files; no SDK, provider, or notification transport.

    Adapt the existing Combo fixture to the current writer's action identity.
    Each historical day has a real confirmed envelope and a nonempty exposure.
    Each requested date has real put/call ledger events, so the reconciler (not
    a mock) determines its scopes. Fixture construction is excluded from timing.
    """
    assert 1 <= requested_dates <= days
    collected, dates = {}, []
    for offset in range(days):
        day = (date(2026, 7, 21) - timedelta(days=offset)).isoformat()
        dates.append(day)
        run_id = f"isolated-combo-{day}"
        brief = _legacy_combo_brief(run_id=run_id)
        brief.update(market_trading_date=day, generated_at_utc=f"{day}T14:00:00+00:00",
                     data_as_of_utc=f"{day}T13:59:00+00:00", valid_until_utc=f"{day}T20:00:00+00:00")
        brief["actions"][0].pop("action_id")
        brief["actions"][0]["candidate_pair_id"] = "pair-nvda-100-110"
        row = brief["candidates"]["combo_yield"][0]
        occurrence = build_combo_candidate_occurrence(
            row, account="lx", market="US", run_id=run_id,
            generated_at_utc=brief["generated_at_utc"], data_as_of_utc=brief["data_as_of_utc"])
        row.update(occurrence)
        brief["candidate_index"][0]["representative"].update(occurrence)
        persisted = history.persist_daily_decision_brief_success(base=base, brief=brief)
        exposures = derive_combo_candidate_exposures(persisted["brief"])
        assert len(exposures) == 1
        envelope_result = history.prepare_daily_decision_brief_delivery(
            base=base, account="lx", market="US", market_trading_date=day, run_id=run_id,
            delivery_kind="fixed_report", source_kind="successful_brief",
            source_digest=persisted["current_brief_digest"], revision=persisted["current_revision"],
            scheduled_target_market=f"{day}T10:00:00-04:00",
            candidate_identities=persisted["current_candidate_identities"],
            rendered_message="Synthetic Combo facts. " * 540,
            render_context={**combo_exposure_render_context(exposures),
                            "rendered_combo_candidate_identities": sorted(
                                {item["candidate_identity"] for item in exposures})},
            prepared_at_utc=f"{day}T14:00:02+00:00")
        envelope = envelope_result["envelope"]
        history.confirm_daily_decision_brief_delivery_v2(
            base=base, account="lx", market="US", market_trading_date=day,
            delivery_key=envelope["delivery_key"], source_digest=envelope["source_digest"],
            message_sha256=envelope["message_sha256"],
            transport_idempotency_key=build_notification_transport_key(envelope["delivery_key"]),
            confirmed_at_utc=f"{day}T14:00:03+00:00")
        delivery_path = envelope_result["paths"]["delivery"]
        raw = json.loads(delivery_path.read_text())
        collected.update(raw["days"])
        # Keep setup linear, as in daily_brief_history_fixtures.
        delivery_path.unlink()
    raw["days"] = collected
    delivery_path.write_text(json.dumps(raw, sort_keys=True) + "\n")
    ledger = SQLiteOptionPositionsRepository(base / "output_shared/state/option_positions.sqlite3")
    for day in dates[:requested_dates]:
        stamp = int(datetime.fromisoformat(f"{day}T14:00:00+00:00").timestamp() * 1000)
        for leg, kind, side, strike in [("call", "call", "long", 110), ("put", "put", "short", 100)]:
            persist_trade_event_object(ledger, _event(
                f"{day}-{leg}", f"{day}-{leg}-lot", option_type=kind, side=side,
                strike=strike, event_time_ms=stamp + (1000 if leg == "call" else 2000)))
    now_ms = int(datetime(2026, 7, 21, 14, 0, 5, tzinfo=timezone.utc).timestamp() * 1000)
    return ledger, sorted(dates[:requested_dates]), now_ms
