from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from domain.domain.combo_candidate_evidence import (
    build_combo_candidate_occurrence,
    combo_candidate_identities_for_rendered_rows,
    combo_exposure_render_context,
    derive_combo_candidate_exposures,
)
from domain.domain.daily_decision_brief import (
    build_daily_brief_candidate_identity,
)
from src.application.combo_yield_steps import attach_combo_candidate_occurrences
from src.application.daily_decision_brief_renderer import (
    render_fixed_report,
    render_fixed_report_card_markdown,
    select_rendered_combo_candidate_rows,
)


GENERATED_AT = datetime(2026, 7, 17, 13, 40, tzinfo=timezone.utc)


def _candidate_row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "symbol": "NVDA",
        "candidate_pair_id": "pair-nvda-100-110",
        "structure_mode": "same_expiry_pair",
        "put_expiration": "2026-08-21",
        "put_strike": 100,
        "put_contract_symbol": "NVDA260821P00100000",
        "call_expiration": "2026-08-21",
        "call_strike": 110,
        "call_contract_symbol": "NVDA260821C00110000",
        "currency": "USD",
        "multiplier": 100,
        "quote": float("nan"),
    }
    row.update(updates)
    return row


def _brief(
    row: dict[str, object],
    *,
    actionability: str = "live_actionable",
    run_id: str = "run-1",
) -> dict:
    symbol = str(row["symbol"])
    identity = build_daily_brief_candidate_identity(
        account="lx",
        market="US",
        symbol=symbol,
        strategy_family="combo_yield",
    )
    representative = {
        key: row[key]
        for key in (
            "symbol",
            "candidate_pair_id",
            "structure_mode",
            "put_contract_symbol",
            "put_expiration",
            "put_strike",
            "call_contract_symbol",
            "call_expiration",
            "call_strike",
            "currency",
            "multiplier",
        )
    }
    representative["strategy_group_id"] = row["candidate_pair_id"]
    representative["capacity"] = {"contracts_available": 1}
    return {
        "account": "lx",
        "market": "US",
        "market_trading_date": "2026-07-17",
        "run_id": run_id,
        "revision": 0,
        "generated_at_utc": GENERATED_AT.isoformat(),
        "data_as_of_utc": "2026-07-17T13:39:00+00:00",
        "valid_until_utc": "2026-07-17T20:00:00+00:00",
        "status": "ready",
        "actionability": actionability,
        "strategy_summary": "test",
        "actions": [],
        "positions": [],
        "capacity": {},
        "funds": {},
        "candidates": {"combo_yield": [row]},
        "candidate_index": [
            {
                "identity": identity,
                "symbol": symbol,
                "strategy_family": "combo_yield",
                "representative": representative,
                "contract_count": 1,
            }
        ],
        "rejections": {},
        "events": [],
        "data_gaps": [],
        "source_artifacts": [],
    }


def test_occurrence_hash_is_stable_for_column_order_and_non_finite_values() -> None:
    first_row = _candidate_row(quote=float("nan"))
    second_row = dict(reversed(list(_candidate_row(quote=float("inf")).items())))

    first = build_combo_candidate_occurrence(
        first_row,
        account="lx",
        market="US",
        run_id="run-1",
        generated_at_utc=GENERATED_AT,
    )
    second = build_combo_candidate_occurrence(
        second_row,
        account="lx",
        market="US",
        run_id="run-1",
        generated_at_utc=GENERATED_AT,
    )

    assert first["candidate_occurrence_id"] == second["candidate_occurrence_id"]
    assert first["candidate_row_content_hash"] == second["candidate_row_content_hash"]


def test_exposure_requires_one_rendered_live_occurrence() -> None:
    row = _candidate_row()
    row.update(
        build_combo_candidate_occurrence(
            row,
            account="lx",
            market="US",
            run_id="run-1",
            generated_at_utc=GENERATED_AT,
        )
    )

    exposures = derive_combo_candidate_exposures(_brief(row))

    assert len(exposures) == 1
    assert exposures[0]["candidate_occurrence_id"] == row["candidate_occurrence_id"]
    assert exposures[0]["delivery_confirmed"] is False
    assert combo_exposure_render_context(exposures) == {
        "candidate_occurrence_ids": [row["candidate_occurrence_id"]],
        "candidate_exposure_ids": [exposures[0]["candidate_exposure_id"]],
    }
    assert derive_combo_candidate_exposures(
        _brief(row, actionability="blocked")
    ) == []

    duplicate = _brief(row)
    duplicate["candidates"]["combo_yield"].append(dict(row))
    assert derive_combo_candidate_exposures(duplicate) == []


def test_publication_keeps_incomplete_rows_but_only_complete_rows_get_occurrences() -> None:
    published = attach_combo_candidate_occurrences(
        pd.DataFrame([_candidate_row(), _candidate_row(multiplier=None)]),
        account="lx",
        market="US",
        run_id="run-1",
        generated_at_utc=GENERATED_AT,
    )

    assert len(published) == 2
    assert isinstance(published.iloc[0]["candidate_occurrence_id"], str)
    assert pd.isna(published.iloc[1]["candidate_occurrence_id"])


def test_confirmed_delivery_upgrades_only_digest_bound_rendered_exposure(
    tmp_path,
) -> None:
    from src.application.daily_decision_brief_repository import (
        confirm_daily_decision_brief_delivery_v2,
        prepare_daily_decision_brief,
        prepare_daily_decision_brief_delivery,
        read_combo_candidate_exposures,
    )
    from src.application.notification_delivery_adapter import (
        build_notification_transport_key,
    )

    row = _candidate_row()
    row.update(
        build_combo_candidate_occurrence(
            row,
            account="lx",
            market="US",
            run_id="run-1",
            generated_at_utc=GENERATED_AT,
        )
    )
    persisted = prepare_daily_decision_brief(base=tmp_path, brief=_brief(row))
    brief = persisted["brief"]
    identity = brief["candidate_index"][0]["identity"]
    rendered_rows = select_rendered_combo_candidate_rows(
        brief,
        delivery_kind="fixed_report",
        limits={"max_candidates_per_strategy": 3},
    )
    rendered_identities = combo_candidate_identities_for_rendered_rows(
        brief,
        rendered_rows,
    )
    exposures = derive_combo_candidate_exposures(
        brief,
        candidate_identities=rendered_identities,
    )
    render_context = {
        **combo_exposure_render_context(exposures),
        "rendered_combo_candidate_identities": rendered_identities,
    }
    prepared = prepare_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date="2026-07-17",
        run_id="run-1",
        delivery_kind="fixed_report",
        source_kind="successful_brief",
        source_digest=persisted["current_brief_digest"],
        rendered_message="Combo evidence test",
        revision=persisted["current_revision"],
        scheduled_target_market="2026-07-17T10:00:00-04:00",
        candidate_identities=[identity],
        render_context=render_context,
        prepared_at_utc="2026-07-17T13:40:00+00:00",
    )
    envelope = prepared["envelope"]
    confirm_daily_decision_brief_delivery_v2(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date="2026-07-17",
        delivery_key=envelope["delivery_key"],
        source_digest=envelope["source_digest"],
        message_sha256=envelope["message_sha256"],
        transport_idempotency_key=build_notification_transport_key(
            envelope["delivery_key"]
        ),
        confirmed_at_utc="2026-07-17T13:41:00+00:00",
    )

    replayed = read_combo_candidate_exposures(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date="2026-07-17",
    )["exposures"]
    assert len(replayed) == 1
    assert replayed[0]["delivery_confirmed"] is True
    assert replayed[0]["candidate_exposure_id"] == exposures[0][
        "candidate_exposure_id"
    ]


def test_candidate_alert_selector_uses_the_same_fixed_limit_as_the_renderer() -> None:
    from src.application.daily_decision_brief_renderer import render_candidate_alert

    first = _candidate_row()
    second = _candidate_row(
        symbol="AMD",
        candidate_pair_id="pair-amd-100-110",
        put_contract_symbol="AMD260821P00100000",
        call_contract_symbol="AMD260821C00110000",
    )
    for row in (first, second):
        row.update(
            build_combo_candidate_occurrence(
                row,
                account="lx",
                market="US",
                run_id="run-1",
                generated_at_utc=GENERATED_AT,
            )
        )
    brief = _brief(first)
    second_brief = _brief(second)
    brief["candidates"]["combo_yield"].append(second)
    brief["candidate_index"].append(second_brief["candidate_index"][0])
    identities = [item["identity"] for item in brief["candidate_index"]]

    rendered = render_candidate_alert(
        brief,
        identities,
        limits={"max_candidates_per_strategy": 1},
    )
    selected = select_rendered_combo_candidate_rows(
        brief,
        delivery_kind="candidate_alert",
        candidate_identities=identities,
        limits={"max_candidates_per_strategy": 1},
    )

    assert "NVDA" in rendered and "AMD" in rendered
    assert {str(item["symbol"]) for item in selected} == {"NVDA", "AMD"}


@pytest.mark.parametrize(
    ("changed_symbols", "expected_symbols"),
    [
        ((), ("NVDA",)),
        (("AMD",), ("AMD",)),
        (("AMD", "AAPL"), ("AMD", "AAPL")),
    ],
)
def test_fixed_report_renderers_and_evidence_share_candidate_projection(
    changed_symbols: tuple[str, ...],
    expected_symbols: tuple[str, ...],
) -> None:
    rows = [
        _candidate_row(),
        _candidate_row(
            symbol="AMD",
            candidate_pair_id="pair-amd-100-110",
            put_contract_symbol="AMD260821P00100000",
            call_contract_symbol="AMD260821C00110000",
        ),
        _candidate_row(
            symbol="AAPL",
            candidate_pair_id="pair-aapl-100-110",
            put_contract_symbol="AAPL260821P00100000",
            call_contract_symbol="AAPL260821C00110000",
        ),
    ]
    for row in rows:
        row.update(
            build_combo_candidate_occurrence(
                row,
                account="lx",
                market="US",
                run_id="run-1",
                generated_at_utc=GENERATED_AT,
            )
        )
    brief = _brief(rows[0])
    for row in rows[1:]:
        row_brief = _brief(row)
        brief["candidates"]["combo_yield"].append(row)
        brief["candidate_index"].append(row_brief["candidate_index"][0])
    diff = {
        "changes": [
            {
                "change_type": "candidate_added",
                "action": {
                    "action_type": "open_combo_yield",
                    "strategy_family": "combo_yield",
                    "symbol": row["symbol"],
                    "expiration": row["put_expiration"],
                    "strike": row["put_strike"],
                    "option_type": "put",
                    "contract_symbol": row["put_contract_symbol"],
                },
            }
            for row in rows
            if row["symbol"] in changed_symbols
        ]
    }
    limits = {"max_candidates_per_strategy": 1}

    plain = render_fixed_report(brief, diff=diff, limits=limits)
    card = render_fixed_report_card_markdown(brief, diff=diff, limits=limits)
    selected = select_rendered_combo_candidate_rows(
        brief,
        delivery_kind="fixed_report",
        diff=diff,
        limits=limits,
    )
    selected_identities = combo_candidate_identities_for_rendered_rows(
        brief,
        selected,
    )
    exposures = derive_combo_candidate_exposures(
        brief,
        candidate_identities=selected_identities,
    )

    assert tuple(str(item["symbol"]) for item in selected) == expected_symbols
    assert {str(item["candidate_identity"]) for item in exposures} == set(
        selected_identities
    )
    assert {str(item["candidate_occurrence_id"]) for item in exposures} == {
        str(item["candidate_occurrence_id"]) for item in selected
    }
    for message in (plain, card):
        for symbol in expected_symbols:
            assert symbol in message
        for symbol in {"NVDA", "AMD", "AAPL"} - set(expected_symbols):
            assert symbol not in message


def test_multiple_confirmed_candidate_alerts_remain_replayable_for_combo_evidence(
    tmp_path,
) -> None:
    from src.application.daily_decision_brief_repository import (
        confirm_daily_decision_brief_delivery_v2,
        prepare_daily_decision_brief_delivery,
        persist_daily_decision_brief_success,
        read_combo_candidate_exposures,
        record_daily_decision_brief_candidates,
    )
    from src.application.notification_delivery_adapter import (
        build_notification_transport_key,
    )

    def _persist_confirmed_alert(
        row: dict[str, object],
        *,
        run_id: str,
        confirmed_at_utc: str,
    ) -> tuple[str, dict[str, object]]:
        row.update(
            build_combo_candidate_occurrence(
                row,
                account="lx",
                market="US",
                run_id=run_id,
                generated_at_utc=GENERATED_AT,
            )
        )
        persisted = persist_daily_decision_brief_success(
            base=tmp_path,
            brief=_brief(row, run_id=run_id),
        )
        brief = persisted["brief"]
        identity = str(brief["candidate_index"][0]["identity"])
        record_daily_decision_brief_candidates(
            base=tmp_path,
            account="lx",
            market="US",
            market_trading_date="2026-07-17",
            revision=persisted["current_revision"],
            brief_digest=persisted["current_brief_digest"],
            candidate_identities=[identity],
        )
        exposures = derive_combo_candidate_exposures(
            brief,
            candidate_identities=[identity],
        )
        render_context = {
            **combo_exposure_render_context(exposures),
            "rendered_combo_candidate_identities": [identity],
        }
        envelope = prepare_daily_decision_brief_delivery(
            base=tmp_path,
            account="lx",
            market="US",
            market_trading_date="2026-07-17",
            run_id=run_id,
            delivery_kind="candidate_alert",
            source_kind="successful_brief",
            source_digest=persisted["current_brief_digest"],
            rendered_message=f"Combo candidate {row['symbol']}",
            revision=persisted["current_revision"],
            candidate_identities=[identity],
            render_context=render_context,
            prepared_at_utc="2026-07-17T13:40:00+00:00",
        )["envelope"]
        confirm_daily_decision_brief_delivery_v2(
            base=tmp_path,
            account="lx",
            market="US",
            market_trading_date="2026-07-17",
            delivery_key=envelope["delivery_key"],
            source_digest=envelope["source_digest"],
            message_sha256=envelope["message_sha256"],
            transport_idempotency_key=build_notification_transport_key(
                envelope["delivery_key"]
            ),
            confirmed_at_utc=confirmed_at_utc,
        )
        return identity, dict(envelope)

    first_identity, first_envelope = _persist_confirmed_alert(
        _candidate_row(),
        run_id="run-1",
        confirmed_at_utc="2026-07-17T13:41:00+00:00",
    )
    second_identity, _second_envelope = _persist_confirmed_alert(
        _candidate_row(
            symbol="AMD",
            candidate_pair_id="pair-amd-100-110",
            put_contract_symbol="AMD260821P00100000",
            call_contract_symbol="AMD260821C00110000",
        ),
        run_id="run-2",
        confirmed_at_utc="2026-07-17T13:51:00+00:00",
    )
    repeated = confirm_daily_decision_brief_delivery_v2(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date="2026-07-17",
        delivery_key=str(first_envelope["delivery_key"]),
        source_digest=str(first_envelope["source_digest"]),
        message_sha256=str(first_envelope["message_sha256"]),
        transport_idempotency_key=build_notification_transport_key(
            str(first_envelope["delivery_key"])
        ),
        confirmed_at_utc="2026-07-17T13:52:00+00:00",
    )
    assert repeated["advanced"] is False
    assert repeated["reason"] == "already_confirmed"

    replayed = read_combo_candidate_exposures(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date="2026-07-17",
    )["exposures"]
    confirmed_by_identity = {
        item["candidate_identity"]: item["delivery_confirmed"]
        for item in replayed
    }
    assert confirmed_by_identity == {
        first_identity: True,
        second_identity: True,
    }
