from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades.review import preview_repair_trade_event
from tests.test_trade_events_cli import _bind_cli_repo


OLD_MS = 1788835417674
NEW_MS = 1788878617674
REASON = "OpenD original fill timezone preview"


def _event(event_type="open", **raw_overrides):
    return TradeEvent(
        event_id="futu:lx:123:test-fill",
        event_type=event_type,
        event_time_ms=OLD_MS,
        contract_key=ContractKey.from_values(
            broker="富途", account="lx", underlying_symbol="NVDA", option_type="put",
            strike=100, expiration_ymd="2026-10-16",
        ),
        contracts=1, price=0 if event_type == "expire_close" else 2,
        currency="USD", multiplier=100, source="opend_push",
        target_lot_id=None if event_type == "open" else "lot-test-open",
        raw_payload={
            "code": "US.NVDA261016P100000", "create_time": "2026-09-08 10:43:37.674",
            "side": "sell" if event_type == "open" else "buy", "qty": 1,
            "deal_id": "test-fill", "futu_account_id": "123",
            "cash_conversions": {"option_trade_cash_gross": {"status": "observed"}},
            **raw_overrides,
        },
    )


def _repo(tmp_path, event):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    assert repo.upsert_trade_event(event)
    return repo


def _dump(repo):
    with repo._connect() as conn:
        return list(conn.iterdump())


def _preview(repo, requested=NEW_MS):
    return preview_repair_trade_event(
        repo, event_id="futu:lx:123:test-fill", overrides={"trade_time_ms": requested}, reason=REASON,
    )


@pytest.mark.parametrize("event_type", ["open", "close", "expire_close"])
def test_cli_raw_preview_and_apply_rejection_preserve_all_stored_state(tmp_path, monkeypatch, capsys, event_type):
    import src.interfaces.cli.trade_events as cli

    repo = _repo(tmp_path, _event(event_type))
    # An old archived normalized instant is not authoritative raw evidence.
    with repo._connect() as conn:
        row = conn.execute("SELECT event_json FROM trade_events").fetchone()
        payload = json.loads(row["event_json"])
        payload["raw_payload"]["execution_input"] = {
            "occurred_at_utc": "2026-09-08T02:43:37.674Z", "source_timezone": "Asia/Shanghai",
        }
        stored = json.dumps(payload, ensure_ascii=False)
        conn.execute("UPDATE trade_events SET event_json=?", (stored,))
    _bind_cli_repo(monkeypatch, cli, repo, tmp_path / "data.json")
    before = _dump(repo)
    args = ["repair", "futu:lx:123:test-fill", "--trade-time-ms", str(NEW_MS), "--reason", REASON]
    assert cli.main([*args, "--dry-run", "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["operation"] == "futu_raw_trade_time_preview"
    assert result["mode"] == "dry_run"
    assert result["apply_supported"] is False
    assert result["write_applied"] is False
    assert result["evidence_scope"] == "stored_raw_time_only"
    assert result["expected_before_sha256"] == hashlib.sha256(stored.encode()).hexdigest()
    assert result["before_trade_time_ms"] == OLD_MS
    assert result["after_trade_time_ms"] == NEW_MS
    assert result["source_timezone"] == "America/New_York"
    assert result["stored_execution_time"] == "2026-09-08T02:43:37.674Z"
    assert result["cash_conversion_keys"] == ["option_trade_cash_gross"]
    assert "raw_futu_time_apply_not_supported" in result["apply_blockers"]
    assert _dump(repo) == before
    assert cli.main([*args, "--dry-run"]) == 0
    assert "不支持 apply" in capsys.readouterr().out
    # A repeat preview remains deterministic; even a supplied hash never enables apply.
    assert cli.main([*args, "--confirm", "--expected-input-hash", result["expected_before_sha256"]]) == 2
    assert "preview-only; apply is not supported" in capsys.readouterr().out
    assert _dump(repo) == before


@pytest.mark.parametrize("raw,requested", [
    ({"create_time": "2026-01-08 10:43:37.674"}, 1767887017674),
    ({"create_timestamp": "1788878617.674", "create_time": "bad"}, NEW_MS),
    ({"create_time": "2026-09-08T10:43:37.674+08:00"}, OLD_MS),
])
def test_preview_uses_shared_time_precedence_and_dst(tmp_path, raw, requested):
    repo = _repo(tmp_path, _event(**raw))
    result = _preview(repo, requested)
    assert result["after_trade_time_ms"] == requested
    assert result["time_change_required"] == (requested != OLD_MS)
    assert result["apply_supported"] is False


@pytest.mark.parametrize("raw,message", [
    ({"create_time": None}, "raw Futu trade time unavailable"),
    ({"create_time": "2026-11-01 01:30:00"}, "ambiguous_or_nonexistent"),
    ({"create_time": "2026-03-08 02:30:00"}, "ambiguous_or_nonexistent"),
    ({"market": "HK"}, "unknown_or_conflicting_market"),
    ({"create_timestamp": "bad"}, "raw Futu trade time unavailable"),
    ({"opend_order_evidence": None}, "stored OpenD order evidence"),
    ({"trade_time_correction_provenance": {}}, "provenance already exists"),
])
def test_raw_preview_rejects_invalid_or_conflicting_evidence(tmp_path, raw, message):
    repo = _repo(tmp_path, _event(**raw))
    before = _dump(repo)
    with pytest.raises(ValueError, match=message):
        _preview(repo)
    assert _dump(repo) == before


def test_raw_preview_requires_exact_time_and_rejects_voided_event(tmp_path):
    event = _event()
    repo = _repo(tmp_path, event)
    with pytest.raises(ValueError, match="must equal the stored raw Futu time"):
        _preview(repo, NEW_MS + 1)
    repo.upsert_trade_event(replace(
        event, event_id="void-test", event_type="void", contracts=0,
        target_event_id=event.event_id, raw_payload={},
    ))
    with pytest.raises(ValueError, match="already voided"):
        _preview(repo)


def test_raw_preview_rejects_sql_json_time_conflict(tmp_path, monkeypatch):
    repo = _repo(tmp_path, _event())
    rows = repo.list_position_projection_event_rows()
    rows[0]["trade_time_ms"] += 1
    monkeypatch.setattr(repo, "list_position_projection_event_rows", lambda **kwargs: rows)
    with pytest.raises(ValueError, match="SQL and JSON times conflict"):
        _preview(repo)
