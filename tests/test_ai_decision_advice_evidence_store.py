from __future__ import annotations

import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.application.ai_decision_advice.evidence_store import (
    COVERAGE_COMPLETED,
    COVERAGE_IDENTITY_UNAVAILABLE,
    COVERAGE_NO_EVIDENCE,
    COVERAGE_STALE,
    append_evidence_records,
    content_fingerprint,
    evidence_path,
    freeze_evidence_index,
    read_evidence_records,
)


def _status(symbol: str, *, checked: str, success: bool = True, identity: str = "ok") -> dict:
    row = {
        "kind": "symbol_status",
        "symbol": symbol,
        "identity_status": identity,
        "last_checked_at": checked,
    }
    if success:
        row["last_success_at"] = checked
        row["search_status"] = "completed"
    return row


def _evidence(symbol: str, url: str, claim: str, *, appended: str) -> dict:
    return {
        "kind": "symbol_evidence",
        "symbol": symbol,
        "url": url,
        "claim": claim,
        "topic": "regulatory",
        "event_status": "developing",
        "content_fingerprint": content_fingerprint(url, claim),
        "appended_at": appended,
    }


def test_append_and_read_roundtrip(tmp_path: Path) -> None:
    count = append_evidence_records(
        base=tmp_path,
        records=[_status("NVDA", checked="2026-08-09T00:00:00+00:00")],
        evidence_run_id="run-1",
        appended_at="2026-08-09T00:00:01+00:00",
    )
    assert count == 1
    assert evidence_path(tmp_path).exists()
    rows = read_evidence_records(tmp_path)
    assert len(rows) == 1
    assert rows[0]["evidence_run_id"] == "run-1"
    assert rows[0]["appended_at"] == "2026-08-09T00:00:01+00:00"
    assert stat.S_IMODE(evidence_path(tmp_path).stat().st_mode) == 0o600
    assert stat.S_IMODE(evidence_path(tmp_path).parent.stat().st_mode) == 0o700


def test_read_rejects_symlinked_evidence_log(tmp_path: Path) -> None:
    path = evidence_path(tmp_path)
    path.parent.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"kind":"symbol_status"}\n', encoding="utf-8")
    path.symlink_to(outside)

    with pytest.raises(OSError, match="symlink"):
        read_evidence_records(tmp_path)


def test_read_tolerates_bad_lines(tmp_path: Path) -> None:
    path = evidence_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"kind":"symbol_status","symbol":"NVDA"}\nnot-json\n\n', encoding="utf-8")
    rows = read_evidence_records(tmp_path)
    assert len(rows) == 1


def test_freeze_index_no_evidence(tmp_path: Path) -> None:
    index = freeze_evidence_index(
        tmp_path,
        symbols=["NVDA"],
        now=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )
    view = index.view_for("NVDA")
    assert view is not None
    assert view.coverage == COVERAGE_NO_EVIDENCE
    assert view.unavailable_reason == "no_evidence"


def test_freeze_index_completed_with_zero_evidence(tmp_path: Path) -> None:
    checked = "2026-08-09T04:00:00+00:00"
    append_evidence_records(
        base=tmp_path,
        records=[_status("NVDA", checked=checked)],
        evidence_run_id="run-1",
        appended_at=checked,
    )
    index = freeze_evidence_index(
        tmp_path,
        symbols=["NVDA"],
        now=datetime(2026, 8, 9, 6, tzinfo=timezone.utc),
    )
    view = index.view_for("NVDA")
    assert view is not None
    assert view.coverage == COVERAGE_COMPLETED
    assert view.evidence == ()
    assert view.last_success_at == checked


def test_freeze_index_stale_after_8_hours(tmp_path: Path) -> None:
    checked = (datetime(2026, 8, 9, tzinfo=timezone.utc) - timedelta(hours=9)).isoformat()
    append_evidence_records(
        base=tmp_path,
        records=[_status("NVDA", checked=checked)],
        evidence_run_id="run-1",
        appended_at=checked,
    )
    index = freeze_evidence_index(
        tmp_path,
        symbols=["NVDA"],
        now=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )
    view = index.view_for("NVDA")
    assert view is not None
    assert view.coverage == COVERAGE_STALE
    assert view.unavailable_reason == "evidence_stale"


def test_freeze_index_identity_unavailable(tmp_path: Path) -> None:
    checked = "2026-08-09T04:00:00+00:00"
    append_evidence_records(
        base=tmp_path,
        records=[_status("NVDA", checked=checked, success=False, identity="identity_unavailable")],
        evidence_run_id="run-1",
        appended_at=checked,
    )
    index = freeze_evidence_index(
        tmp_path,
        symbols=["NVDA"],
        now=datetime(2026, 8, 9, 6, tzinfo=timezone.utc),
    )
    view = index.view_for("NVDA")
    assert view is not None
    assert view.coverage == COVERAGE_IDENTITY_UNAVAILABLE


def test_freeze_index_dedupes_by_fingerprint_keeping_latest(tmp_path: Path) -> None:
    checked = "2026-08-09T04:00:00+00:00"
    older = _evidence("NVDA", "https://a", "claim-1", appended="2026-08-09T01:00:00+00:00")
    newer = _evidence("NVDA", "https://a", "claim-1", appended="2026-08-09T03:00:00+00:00")
    other = _evidence("NVDA", "https://b", "claim-2", appended="2026-08-09T02:00:00+00:00")
    append_evidence_records(
        base=tmp_path,
        records=[older, other, newer, _status("NVDA", checked=checked)],
        evidence_run_id="run-1",
        appended_at=checked,
    )
    index = freeze_evidence_index(
        tmp_path,
        symbols=["NVDA"],
        now=datetime(2026, 8, 9, 6, tzinfo=timezone.utc),
    )
    view = index.view_for("NVDA")
    assert view is not None
    assert len(view.evidence) == 2
    urls = sorted(row["url"] for row in view.evidence)
    assert urls == ["https://a", "https://b"]
    kept_a = [row for row in view.evidence if row["url"] == "https://a"][0]
    assert kept_a["appended_at"] == "2026-08-09T03:00:00+00:00"


def test_index_hash_changes_with_semantics_not_checked_time(tmp_path: Path) -> None:
    checked = "2026-08-09T04:00:00+00:00"
    append_evidence_records(
        base=tmp_path,
        records=[_evidence("NVDA", "https://a", "claim", appended=checked), _status("NVDA", checked=checked)],
        evidence_run_id="run-1",
        appended_at=checked,
    )
    first = freeze_evidence_index(tmp_path, symbols=["NVDA"], now=datetime(2026, 8, 9, 6, tzinfo=timezone.utc))
    second = freeze_evidence_index(tmp_path, symbols=["NVDA"], now=datetime(2026, 8, 9, 7, tzinfo=timezone.utc))
    assert first.index_hash() == second.index_hash()


def test_index_hash_stable_when_only_last_success_refreshes(tmp_path: Path) -> None:
    """checked-at-only collector refresh must not invalidate reuse (docs 13.2)."""

    first_checked = "2026-08-09T04:00:00+00:00"
    second_checked = "2026-08-09T08:00:00+00:00"
    append_evidence_records(
        base=tmp_path,
        records=[_evidence("NVDA", "https://a", "claim", appended=first_checked), _status("NVDA", checked=first_checked)],
        evidence_run_id="run-1",
        appended_at=first_checked,
    )
    first = freeze_evidence_index(tmp_path, symbols=["NVDA"], now=datetime(2026, 8, 9, 9, tzinfo=timezone.utc))
    append_evidence_records(
        base=tmp_path,
        records=[_status("NVDA", checked=second_checked)],
        evidence_run_id="run-2",
        appended_at=second_checked,
    )
    second = freeze_evidence_index(tmp_path, symbols=["NVDA"], now=datetime(2026, 8, 9, 9, tzinfo=timezone.utc))
    assert first.index_hash() == second.index_hash()


def test_content_fingerprint_stable() -> None:
    assert content_fingerprint("u", "c") == content_fingerprint("u", "c")
    assert content_fingerprint("u", "c") != content_fingerprint("u", "c2")
