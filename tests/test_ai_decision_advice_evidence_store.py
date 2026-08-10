from __future__ import annotations

import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.application.ai_decision_advice.evidence_store import (
    COVERAGE_COMPLETED,
    COVERAGE_IDENTITY_CHANGED,
    COVERAGE_IDENTITY_UNAVAILABLE,
    COVERAGE_NO_EVIDENCE,
    COVERAGE_STALE,
    COVERAGE_SNAPSHOT_INVALID,
    append_evidence_records,
    build_evidence_snapshot_hash,
    content_fingerprint,
    evidence_path,
    freeze_evidence_index,
    read_evidence_records,
)

IDENTITY_HASH = "a" * 64


def _status(
    symbol: str,
    *,
    checked: str,
    success: bool = True,
    identity: str = "ok",
    active_rows: list[dict] | None = None,
    identity_hash: str = IDENTITY_HASH,
    mode: str = "full",
) -> dict:
    active_rows = active_rows or []
    row = {
        "kind": "symbol_status",
        "symbol": symbol,
        "identity_status": identity,
        "identity_semantic_sha256": identity_hash,
        "last_checked_at": checked,
    }
    if success:
        row["last_success_at"] = checked
        row["search_status"] = "completed"
        row["search_mode"] = mode
        row["query_cutoff"] = "2026-07-10T00:00:00+00:00"
        row["active_evidence_refs"] = sorted(item["ref"] for item in active_rows)
        ordered_rows = sorted(active_rows, key=lambda item: item["ref"])
        row["semantic_snapshot_hash"] = build_evidence_snapshot_hash(
            symbol=symbol,
            identity_semantic_sha256=identity_hash,
            evidence_rows=ordered_rows,
        )
    else:
        row["search_status"] = "failed" if identity == "ok" else None
    return row


def _evidence(
    symbol: str,
    url: str,
    claim: str,
    *,
    appended: str,
    ref: str | None = None,
    identity_hash: str = IDENTITY_HASH,
) -> dict:
    return {
        "kind": "symbol_evidence",
        "symbol": symbol,
        "identity_semantic_sha256": identity_hash,
        "ref": ref or f"ev-{claim}",
        "url": url,
        "claim": claim,
        "topic": "regulatory",
        "event_status": "developing",
        "content_fingerprint": content_fingerprint(url, claim),
        "source": {
            "title": claim,
            "publisher": "example.com",
            "visible_domain": "example.com",
            "url": url,
            "published_at": None,
        },
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


def test_freeze_index_remains_fresh_across_daily_refresh_interval(tmp_path: Path) -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    checked = (now - timedelta(hours=25)).isoformat()
    append_evidence_records(
        base=tmp_path,
        records=[_status("NVDA", checked=checked)],
        evidence_run_id="run-1",
        appended_at=checked,
    )
    index = freeze_evidence_index(tmp_path, symbols=["NVDA"], now=now)
    view = index.view_for("NVDA")
    assert view is not None
    assert view.coverage == COVERAGE_COMPLETED


def test_freeze_index_stale_after_48_hours(tmp_path: Path) -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    checked = (now - timedelta(hours=49)).isoformat()
    append_evidence_records(
        base=tmp_path,
        records=[_status("NVDA", checked=checked)],
        evidence_run_id="run-1",
        appended_at=checked,
    )
    index = freeze_evidence_index(
        tmp_path,
        symbols=["NVDA"],
        now=now,
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


def test_freeze_index_reconstructs_only_declared_active_members(tmp_path: Path) -> None:
    checked = "2026-08-09T04:00:00+00:00"
    older = _evidence("NVDA", "https://a", "claim-1", appended="2026-08-09T01:00:00+00:00")
    other = _evidence("NVDA", "https://b", "claim-2", appended="2026-08-09T02:00:00+00:00")
    append_evidence_records(
        base=tmp_path,
        records=[older, other, _status("NVDA", checked=checked, active_rows=[other])],
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
    assert len(view.evidence) == 1
    assert view.evidence[0]["url"] == "https://b"


def test_index_hash_changes_with_semantics_not_checked_time(tmp_path: Path) -> None:
    checked = "2026-08-09T04:00:00+00:00"
    append_evidence_records(
        base=tmp_path,
        records=[
            (evidence := _evidence("NVDA", "https://a", "claim", appended=checked)),
            _status("NVDA", checked=checked, active_rows=[evidence]),
        ],
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
        records=[
            (evidence := _evidence("NVDA", "https://a", "claim", appended=first_checked)),
            _status("NVDA", checked=first_checked, active_rows=[evidence]),
        ],
        evidence_run_id="run-1",
        appended_at=first_checked,
    )
    first = freeze_evidence_index(tmp_path, symbols=["NVDA"], now=datetime(2026, 8, 9, 9, tzinfo=timezone.utc))
    append_evidence_records(
        base=tmp_path,
        records=[
            _status(
                "NVDA",
                checked=second_checked,
                active_rows=[evidence],
                mode="incremental",
            )
        ],
        evidence_run_id="run-2",
        appended_at=second_checked,
    )
    second = freeze_evidence_index(tmp_path, symbols=["NVDA"], now=datetime(2026, 8, 9, 9, tzinfo=timezone.utc))
    assert first.index_hash() == second.index_hash()


def test_content_fingerprint_stable() -> None:
    assert content_fingerprint("u", "c") == content_fingerprint("u", "c")
    assert content_fingerprint("u", "c") != content_fingerprint("u", "c2")


def test_failed_refresh_does_not_hide_last_success(tmp_path: Path) -> None:
    success_at = "2026-08-09T04:00:00+00:00"
    failed_at = "2026-08-09T05:00:00+00:00"
    evidence = _evidence("NVDA", "https://a", "claim", appended=success_at)
    append_evidence_records(
        base=tmp_path,
        records=[evidence, _status("NVDA", checked=success_at, active_rows=[evidence])],
        evidence_run_id="run-success",
        appended_at=success_at,
    )
    append_evidence_records(
        base=tmp_path,
        records=[_status("NVDA", checked=failed_at, success=False)],
        evidence_run_id="run-failed",
        appended_at=failed_at,
    )
    view = freeze_evidence_index(
        tmp_path,
        symbols=["NVDA"],
        now=datetime(2026, 8, 9, 6, tzinfo=timezone.utc),
        identity_hash_by_symbol={"NVDA": IDENTITY_HASH},
    ).view_for("NVDA")
    assert view is not None
    assert view.coverage == COVERAGE_COMPLETED
    assert view.last_success_at == success_at
    assert view.last_checked_at == failed_at
    assert [row["claim"] for row in view.evidence] == ["claim"]


@pytest.mark.parametrize("tamper", ["missing", "duplicate", "wrong_symbol", "wrong_identity", "hash"])
def test_snapshot_member_tamper_fails_closed(tmp_path: Path, tamper: str) -> None:
    checked = "2026-08-09T04:00:00+00:00"
    evidence = _evidence("NVDA", "https://a", "claim", appended=checked)
    status = _status("NVDA", checked=checked, active_rows=[evidence])
    records = [evidence, status]
    if tamper == "missing":
        records = [status]
    elif tamper == "duplicate":
        status["active_evidence_refs"] = [evidence["ref"], evidence["ref"]]
    elif tamper == "wrong_symbol":
        evidence["symbol"] = "AAPL"
    elif tamper == "wrong_identity":
        evidence["identity_semantic_sha256"] = "b" * 64
    elif tamper == "hash":
        status["semantic_snapshot_hash"] = "0" * 64
    append_evidence_records(
        base=tmp_path,
        records=records,
        evidence_run_id="run-1",
        appended_at=checked,
    )
    view = freeze_evidence_index(
        tmp_path,
        symbols=["NVDA"],
        now=datetime(2026, 8, 9, 6, tzinfo=timezone.utc),
        identity_hash_by_symbol={"NVDA": IDENTITY_HASH},
    ).view_for("NVDA")
    assert view is not None
    assert view.coverage == COVERAGE_SNAPSHOT_INVALID
    assert view.evidence == ()


def test_identity_change_excludes_old_evidence(tmp_path: Path) -> None:
    checked = "2026-08-09T04:00:00+00:00"
    evidence = _evidence("NVDA", "https://a", "claim", appended=checked)
    append_evidence_records(
        base=tmp_path,
        records=[evidence, _status("NVDA", checked=checked, active_rows=[evidence])],
        evidence_run_id="run-1",
        appended_at=checked,
    )
    view = freeze_evidence_index(
        tmp_path,
        symbols=["NVDA"],
        now=datetime(2026, 8, 9, 6, tzinfo=timezone.utc),
        identity_hash_by_symbol={"NVDA": "b" * 64},
    ).view_for("NVDA")
    assert view is not None
    assert view.coverage == COVERAGE_IDENTITY_CHANGED
    assert view.evidence == ()


def test_evidence_as_of_is_actual_minimum_success(tmp_path: Path) -> None:
    nvda_at = "2026-08-09T05:00:00+00:00"
    aapl_at = "2026-08-09T03:00:00+00:00"
    append_evidence_records(
        base=tmp_path,
        records=[
            _status("NVDA", checked=nvda_at),
            _status("AAPL", checked=aapl_at),
        ],
        evidence_run_id="run-1",
        appended_at="2026-08-09T05:00:00+00:00",
    )
    index = freeze_evidence_index(
        tmp_path,
        symbols=["NVDA", "AAPL"],
        now=datetime(2026, 8, 9, 6, tzinfo=timezone.utc),
        identity_hash_by_symbol={"NVDA": IDENTITY_HASH, "AAPL": IDENTITY_HASH},
    )
    assert index.evidence_as_of == aapl_at
