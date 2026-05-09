from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from domain.domain.option_positions_v2 import (
    EVENT_KIND_CLOSE_TRADE,
    EVENT_KIND_MANUAL_ADJUSTMENT,
    EVENT_KIND_OPEN_TRADE,
    SNAPSHOT_TYPE_BASELINE,
    SNAPSHOT_TYPE_VERIFICATION,
    adapt_legacy_trade_events,
    build_baseline_snapshot_from_legacy_records,
    normalize_position_event,
    normalize_position_snapshot,
    project_current_positions,
    reconcile_snapshot_against_projection,
)
from domain.storage.repositories import option_positions_v2_repo
from scripts.option_positions_core.domain import parse_exp_to_ms
from src.application.option_positions_facade import load_option_position_records


def test_option_positions_v2_projects_baseline_events_and_manual_adjustment() -> None:
    baseline = normalize_position_snapshot(
        {
            "snapshot_id": "baseline_t0",
            "snapshot_type": SNAPSHOT_TYPE_BASELINE,
            "snapshot_at_utc": "2026-05-01T00:00:00+00:00",
            "source_name": "manual_bootstrap",
            "lots": [
                {
                    "snapshot_lot_id": "lot_t0_nvda_put",
                    "account": "lx",
                    "broker": "富途证券（香港）",
                    "symbol": "nvda",
                    "option_type": "put",
                    "side": "short",
                    "strike": 100,
                    "expiration_ymd": "2026-06-19",
                    "currency": "USD",
                    "multiplier": 100,
                    "contracts": 2,
                    "verification_status": "confirmed",
                }
            ],
        }
    )
    events = [
        normalize_position_event(
            {
                "event_id": "evt_close_1",
                "event_kind": EVENT_KIND_CLOSE_TRADE,
                "event_at_utc": "2026-05-02T00:00:00+00:00",
                "source_name": "manual_trade_log",
                "account": "lx",
                "broker": "富途",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "strike": 100,
                "expiration_ymd": "2026-06-19",
                "currency": "USD",
                "multiplier": 100,
                "contracts": 1,
            }
        ),
        normalize_position_event(
            {
                "event_id": "evt_adjust_1",
                "event_kind": EVENT_KIND_MANUAL_ADJUSTMENT,
                "event_at_utc": "2026-05-03T00:00:00+00:00",
                "source_name": "manual_repair",
                "account": "lx",
                "broker": "富途",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "strike": 100,
                "expiration_ymd": "2026-06-19",
                "currency": "USD",
                "multiplier": 100,
                "target_contracts": 3,
            }
        ),
        normalize_position_event(
            {
                "event_id": "evt_open_1",
                "event_kind": EVENT_KIND_OPEN_TRADE,
                "event_at_utc": "2026-05-04T00:00:00+00:00",
                "source_name": "manual_trade_log",
                "account": "lx",
                "broker": "富途",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "strike": 100,
                "expiration_ymd": "2026-06-19",
                "currency": "USD",
                "multiplier": 100,
                "contracts": 1,
            }
        ),
    ]

    projection = project_current_positions(baseline, events)
    assert projection["baseline_snapshot_id"] == "baseline_t0"
    assert projection["open_position_count"] == 1
    assert projection["processed_event_count"] == 3
    assert projection["diagnostics"] == []
    position = projection["positions"][0]
    assert position["broker"] == "富途"
    assert position["account"] == "lx"
    assert position["current_contracts"] == 4
    assert position["baseline_contracts"] == 2
    assert [item["event_id"] for item in position["applied_events"]] == [
        "evt_close_1",
        "evt_adjust_1",
        "evt_open_1",
    ]


def test_option_positions_v2_reconcile_reports_mismatch_and_projection_orphans() -> None:
    baseline = normalize_position_snapshot(
        {
            "snapshot_id": "baseline_t0",
            "snapshot_type": SNAPSHOT_TYPE_BASELINE,
            "snapshot_at_utc": "2026-05-01T00:00:00+00:00",
            "source_name": "manual_bootstrap",
            "lots": [
                {
                    "account": "lx",
                    "broker": "富途",
                    "symbol": "NVDA",
                    "option_type": "put",
                    "side": "short",
                    "strike": 100,
                    "expiration_ymd": "2026-06-19",
                    "currency": "USD",
                    "multiplier": 100,
                    "contracts": 2,
                }
            ],
        }
    )
    projection = project_current_positions(
        baseline,
        [
            {
                "event_id": "evt_open_extra",
                "event_kind": EVENT_KIND_OPEN_TRADE,
                "event_at_utc": "2026-05-02T00:00:00+00:00",
                "source_name": "manual_trade_log",
                "account": "lx",
                "broker": "富途",
                "symbol": "AAPL",
                "option_type": "call",
                "side": "short",
                "strike": 220,
                "expiration_ymd": "2026-06-19",
                "currency": "USD",
                "multiplier": 100,
                "contracts": 1,
            }
        ],
    )
    verification = normalize_position_snapshot(
        {
            "snapshot_id": "verify_2026_05_09",
            "snapshot_type": SNAPSHOT_TYPE_VERIFICATION,
            "snapshot_at_utc": "2026-05-09T00:00:00+00:00",
            "source_name": "broker_statement",
            "lots": [
                {
                    "account": "lx",
                    "broker": "富途",
                    "symbol": "NVDA",
                    "option_type": "put",
                    "side": "short",
                    "strike": 100,
                    "expiration_ymd": "2026-06-19",
                    "currency": "USD",
                    "multiplier": 100,
                    "contracts": 1,
                }
            ],
        }
    )

    report = reconcile_snapshot_against_projection(verification, projection)
    statuses = {item["status"] for item in report["items"]}
    assert statuses == {"missing_in_snapshot", "quantity_mismatch"}
    assert report["summary"]["quantity_mismatch"] == 1
    assert report["summary"]["missing_in_snapshot"] == 1


def test_option_positions_v2_adapts_legacy_rows_and_skips_bootstrap_events() -> None:
    exp_ms = parse_exp_to_ms("2026-06-19")
    assert exp_ms is not None
    snapshot = build_baseline_snapshot_from_legacy_records(
        [
            {
                "record_id": "legacy_lot_1",
                "fields": {
                    "account": "lx",
                    "broker": "富途",
                    "symbol": "TSLA",
                    "option_type": "put",
                    "side": "short",
                    "strike": 180,
                    "expiration": exp_ms,
                    "currency": "USD",
                    "multiplier": 100,
                    "contracts_open": 2,
                },
            }
        ],
        snapshot_id="legacy_baseline",
        snapshot_at_utc="2026-05-01T00:00:00+00:00",
    )
    assert snapshot["lots"][0]["expiration_ymd"] == "2026-06-19"
    assert snapshot["skipped_records"] == []

    adapted = adapt_legacy_trade_events(
        [
            {
                "event_id": "bootstrap_1",
                "source_type": "bootstrap_snapshot",
                "source_name": "legacy_bootstrap",
                "position_effect": "open",
            },
            {
                "event_id": "trade_open_1",
                "source_type": "broker_trade_event",
                "source_name": "opend_push",
                "trade_time_ms": 1714608000000,
                "broker": "富途",
                "account": "lx",
                "symbol": "TSLA",
                "option_type": "put",
                "side": "sell",
                "position_effect": "open",
                "contracts": 1,
                "strike": 180,
                "expiration_ymd": "2026-06-19",
                "currency": "USD",
                "multiplier": 100,
            },
        ]
    )
    assert len(adapted["events"]) == 1
    assert adapted["events"][0]["event_kind"] == EVENT_KIND_OPEN_TRADE
    assert adapted["skipped"][0]["reason"] == "bootstrap_snapshot_is_not_a_post_baseline_event"


def test_option_positions_v2_repo_persists_snapshot_event_and_report() -> None:
    root = (BASE / "tests" / ".tmp_option_positions_v2_repo").resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    snapshot_payload = {
        "snapshot_id": "baseline_t0",
        "snapshot_type": SNAPSHOT_TYPE_BASELINE,
        "snapshot_at_utc": "2026-05-01T00:00:00+00:00",
        "source_name": "manual_bootstrap",
        "lots": [
            {
                "account": "lx",
                "broker": "富途",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "strike": 100,
                "expiration_ymd": "2026-06-19",
                "currency": "USD",
                "multiplier": 100,
                "contracts": 2,
            }
        ],
    }
    option_positions_v2_repo.append_position_snapshot(root, snapshot_payload)
    option_positions_v2_repo.append_position_event(
        root,
        {
            "event_id": "evt_open_1",
            "event_kind": EVENT_KIND_OPEN_TRADE,
            "event_at_utc": "2026-05-02T00:00:00+00:00",
            "source_name": "manual_trade_log",
            "account": "lx",
            "broker": "富途",
            "symbol": "NVDA",
            "option_type": "put",
            "side": "short",
            "strike": 100,
            "expiration_ymd": "2026-06-19",
            "currency": "USD",
            "multiplier": 100,
            "contracts": 1,
        },
    )

    snapshots = option_positions_v2_repo.load_position_snapshots(root)
    events = option_positions_v2_repo.load_position_events(root)
    assert len(snapshots) == 1
    assert len(events) == 1

    projection = project_current_positions(snapshots[0], events)
    report = reconcile_snapshot_against_projection(
        normalize_position_snapshot(
            {
                "snapshot_id": "verify_t1",
                "snapshot_type": SNAPSHOT_TYPE_VERIFICATION,
                "snapshot_at_utc": "2026-05-03T00:00:00+00:00",
                "source_name": "manual_check",
                "lots": [
                    {
                        "account": "lx",
                        "broker": "富途",
                        "symbol": "NVDA",
                        "option_type": "put",
                        "side": "short",
                        "strike": 100,
                        "expiration_ymd": "2026-06-19",
                        "currency": "USD",
                        "multiplier": 100,
                        "contracts": 3,
                    }
                ],
            }
        ),
        projection,
    )
    option_positions_v2_repo.write_current_projection(root, projection)
    option_positions_v2_repo.write_reconciliation_report(root, report)

    current_projection = json.loads(
        (root / "output_shared" / "state" / "option_positions_v2" / "current" / "projection.current.json").read_text(
            encoding="utf-8"
        )
    )
    latest_report = json.loads(
        (root / "output_shared" / "state" / "option_positions_v2" / "current" / "reconciliation.latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert current_projection["open_position_count"] == 1
    assert latest_report["summary"]["matched"] == 1

    shutil.rmtree(root, ignore_errors=True)


def test_option_positions_v2_facade_returns_projection_compatible_rows(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc
    from scripts.option_positions_core.domain import OpenPositionCommand

    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.data_config_path = tmp_path / "data.json"  # type: ignore[attr-defined]
    svc.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=1.5,
            opened_at_ms=1000,
        ),
    )

    records = load_option_position_records(repo, base=tmp_path)
    assert len(records) == 1
    assert records[0]["fields"]["position_key"]
    assert records[0]["fields"]["contracts_open"] == 1
    assert records[0]["fields"]["expiration_ymd"] == "2026-06-19"
