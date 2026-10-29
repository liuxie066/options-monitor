"""Issue #652: public time repair must not silently corrupt a cursor stream."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from src.application.ledger.api import (
    TradeEventPaginationError,
    open_position_query_ledger_from_data_config,
    preview_trade_event_repair,
    record_trade_event_repair,
    refresh_position_lot_projection,
)
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.trade_event_pagination import decode_cursor, encode_cursor
from tests.test_trade_event_pagination import CURSOR_KEY, _continuation, _event, _page


BASE_MS = 1_767_225_600_000  # 2026-01-01 UTC, plus seconds below.
REASON = "OpenD stored evidence: verified order time"


def _append(repo, event_id, before, after, *, account="lx", symbol="NVDA"):
    event = _event(event_id, event_time_ms=BASE_MS + before * 1000, account=account, symbol=symbol)
    raw = dict(event.raw_payload) | {
        "opend_order_evidence": {
            "schema_version": "opend_order_evidence.v1",
            "provider": "opend",
            "classification": "single_order",
            "observed_at_ms": BASE_MS + 500_000,
            "orders": [
                {
                    "futu_account_id": "123",
                    "order_id": f"order-{event_id}",
                    "quantity": 1,
                    "trade_time_ms": BASE_MS + after * 1000,
                }
            ],
        }
    }
    repo.upsert_trade_event(replace(event, raw_payload=raw))
    refresh_position_lot_projection(repo)


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteOptionPositionsRepository(tmp_path / "output_shared/state/option_positions.sqlite3")
    for event_id, before, after in (("a", 300, 150), ("b", 200, 400), ("c", 100, 100)):
        _append(repository, event_id, before, after)
    return repository


def _storage(repo):
    with repo._connect() as conn:
        return list(conn.iterdump())


def _repair(repo, event_id, after):
    return record_trade_event_repair(
        repo,
        event_id=event_id,
        overrides={"trade_time_ms": BASE_MS + after * 1000},
        reason=REASON,
    )


def _drain(repo, first, **kwargs):
    ids = [row["event_id"] for row in first["rows"]]
    page = first
    while page["next_cursor"]:
        page = _page(
            repo,
            {"cursor": page["next_cursor"], "limit": 1, "include_total": True},
            account=None,
            now_epoch_s=1001,
            **kwargs,
        )
        assert page["stream_id"] == first["stream_id"]
        assert page["as_of"] == first["as_of"]
        assert page["total_count"] == first["total_count"]
        ids.extend(row["event_id"] for row in page["rows"])
        assert len(ids) <= first["total_count"], ids
    assert page["snapshot_exhausted"] is True
    return ids


@pytest.mark.parametrize(("event_id", "after", "order"), [("a", 150, ["b", "a", "c"]), ("b", 400, ["b", "a", "c"])])
def test_public_time_repair_rejects_old_cursor_and_restart_is_complete(repo, event_id, after, order):
    first = _page(repo, {"limit": 1, "include_total": True})
    assert [row["event_id"] for row in first["rows"]] == ["a"]
    before = _storage(repo)
    preview = preview_trade_event_repair(
        repo,
        event_id=event_id,
        overrides={"trade_time_ms": BASE_MS + after * 1000},
        reason=REASON,
    )
    assert preview["mode"] == "dry_run" and _storage(repo) == before
    with repo._connect() as conn:
        before_row = dict(conn.execute("SELECT * FROM trade_events WHERE event_id=?", (event_id,)).fetchone())
        triggers = list(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
    applied = _repair(repo, event_id, after)
    assert applied["mode"] == "applied"
    assert applied["projection_source_generation_after"] == applied["projection_source_generation_before"] + 1
    with repo._connect() as conn:
        after_row = dict(conn.execute("SELECT * FROM trade_events WHERE event_id=?", (event_id,)).fetchone())
        assert list(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")) == triggers
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert list(conn.execute("PRAGMA foreign_key_check")) == []
    assert after_row["ingest_seq"] == before_row["ingest_seq"]
    payload = json.loads(after_row["event_json"])
    assert payload["event_time_ms"] == after_row["trade_time_ms"] == BASE_MS + after * 1000
    assert (
        payload["raw_payload"]["trade_time_correction_provenance"]["expected_before_sha256"]
        == preview["expected_before_sha256"]
    )
    committed = _storage(repo)
    assert _repair(repo, event_id, after)["mode"] == "no_op"
    assert _storage(repo) == committed
    with pytest.raises(TradeEventPaginationError, match="start a new query") as error:
        _continuation(repo, first["next_cursor"], limit=1, include_total=True)
    assert error.value.code == "invalid_cursor"
    restarted = _page(repo, {"limit": 1, "include_total": True})
    assert restarted["stream_id"] != first["stream_id"]
    assert _drain(repo, restarted) == order


@pytest.mark.parametrize(
    "control",
    [
        "unchanged",
        "rollback",
        "late_append",
        "late_repair",
        "other_account",
        "other_market",
        "other_symbol",
        "rejected_evidence",
        "no_op",
    ],
)
def test_unaffected_snapshot_remains_valid(repo, monkeypatch, control):
    if control == "other_account":
        _append(repo, "sy", 250, 400, account="sy")
    elif control == "other_market":
        _append(repo, "hk", 250, 400, symbol="0700.HK")
    elif control == "other_symbol":
        _append(repo, "other", 250, 400, symbol="AAPL")
    first = _page(repo, {"symbol": "NVDA", "limit": 1, "include_total": True}, authorized_accounts=("lx", "sy"))
    before = _storage(repo)
    if control == "rollback":

        def fail_after_projection(*args, **kwargs):
            raise RuntimeError("injected projection readback failure")

        with monkeypatch.context() as patch:
            patch.setattr(
                "src.application.ledger.interventions._assert_only_target_lot_opened_at_changed", fail_after_projection
            )
            with pytest.raises(RuntimeError, match="injected"):
                _repair(repo, "a", 150)
        assert _storage(repo) == before
    elif control in {"late_append", "late_repair"}:
        _append(repo, "late", 50, 400)
        if control == "late_repair":
            assert _repair(repo, "late", 400)["mode"] == "applied"
    elif control in {"other_account", "other_market", "other_symbol"}:
        target = {"other_account": "sy", "other_market": "hk", "other_symbol": "other"}[control]
        assert _repair(repo, target, 400)["mode"] == "applied"
    elif control == "no_op":
        assert _repair(repo, "c", 100)["mode"] == "no_op"
        assert _storage(repo) == before
    elif control == "rejected_evidence":
        with pytest.raises(ValueError, match="earliest stored OpenD order time"):
            _repair(repo, "a", 149)
        assert _storage(repo) == before
    assert _drain(repo, first, authorized_accounts=("lx", "sy")) == ["a", "b", "c"]


@pytest.mark.parametrize("mutation", ["legacy", "missing_revision", "invalid_revision"])
def test_compatible_signed_cursor_without_revision_proof_is_rejected(repo, mutation):
    first = _page(repo, {"limit": 1})
    state = decode_cursor(first["next_cursor"], CURSOR_KEY, now=1001)
    if mutation == "legacy":
        state["version"] = 1
    elif mutation == "missing_revision":
        state.pop("snapshot_order_revision", None)
    else:
        state["snapshot_order_revision"] = "invalid"
    cursor = encode_cursor(state, CURSOR_KEY, now=1001)
    with pytest.raises(TradeEventPaginationError) as error:
        _continuation(repo, cursor, limit=1)
    assert error.value.code == "invalid_cursor"


@pytest.mark.parametrize(("event_id", "after"), [("a", 150), ("b", 400)])
def test_public_read_only_reader_rejects_repaired_cursor(repo, tmp_path, event_id, after):
    data_config = tmp_path / "portfolio.runtime.json"
    data_config.write_text("{}")
    _, reader = open_position_query_ledger_from_data_config(
        base=tmp_path,
        data_config=data_config,
        runtime_root=tmp_path,
    )
    first = _page(reader, {"limit": 1, "include_total": True})
    assert _repair(repo, event_id, after)["mode"] == "applied"
    committed = _storage(repo)
    with pytest.raises(TradeEventPaginationError, match="start a new query"):
        _continuation(reader, first["next_cursor"], limit=1)
    restarted = _page(reader, {"limit": 1, "include_total": True})
    assert _drain(reader, restarted) == ["b", "a", "c"]
    assert _storage(repo) == committed


def test_all_authorized_accounts_cursor_rejects_in_scope_account_repair(repo):
    _append(repo, "sy", 250, 400, account="sy")
    scope = {"account": None, "authorized_accounts": ("lx", "sy")}
    first = _page(repo, {"limit": 1, "include_total": True}, **scope)
    assert first["total_count"] == 4
    assert _repair(repo, "sy", 400)["mode"] == "applied"
    with pytest.raises(TradeEventPaginationError, match="start a new query"):
        _page(repo, {"cursor": first["next_cursor"], "limit": 1}, **scope)
    restarted = _page(repo, {"limit": 1, "include_total": True}, **scope)
    assert _drain(repo, restarted, authorized_accounts=("lx", "sy")) == ["sy", "a", "b", "c"]


def test_revision_validation_and_page_share_the_sqlite_read_snapshot(repo):
    with repo._connect() as reader:
        reader.execute("BEGIN DEFERRED")
        first = repo.list_trade_events_page(market="US", authorized_accounts=("lx",), limit=1, conn=reader)
        assert _repair(repo, "b", 400)["mode"] == "applied"
        kwargs = {
            "market": "US",
            "authorized_accounts": ("lx",),
            "limit": 1,
            "snapshot_max_ingest_seq": first["snapshot_max_ingest_seq"],
            "snapshot_order_revision": first["snapshot_order_revision"],
            "last_trade_time_ms": first["last_trade_time_ms"],
            "last_event_id": first["last_event_id"],
        }
        pinned = repo.list_trade_events_page(**kwargs, conn=reader)
        assert [row["event_id"] for row in pinned["rows"]] == ["b"]
        assert pinned["rows"][0]["event_time_ms"] == BASE_MS + 200_000
        reader.rollback()
    with pytest.raises(TradeEventPaginationError, match="start a new query"):
        repo.list_trade_events_page(**kwargs)
