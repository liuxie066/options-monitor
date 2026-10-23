"""Retained audit contracts shared by candidate resolution and history windows."""
from __future__ import annotations

import errno
import io
import json
import os
import time
from datetime import date, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.candidate_filter_impl import _resolve_notification_run
from src.application.agent_tools.project import _QUERY_CONTEXT
from src.application.agent_tools.project_reader import ProjectReaderError
from src.application.notification_perception_read import (
    iter_notification_perception_events,
    read_notification_perception_events,
)
from tests.test_candidate_filter_run_resolution import _perception_audit_row

CURRENT = "audit_events.jsonl"
SEGMENT = "audit_events.20261021.000001.jsonl"


def _event(*, inner="2026-10-21T04:00:00Z", outer=None, account="case_a", source="source"):
    from datetime import datetime

    row = _perception_audit_row(run_id="send-attempt", accounts=[account],
        event_at_utc=datetime.fromisoformat(outer or inner))
    row["extra"]["created_at_utc"] = inner
    row["extra"]["report_refs"][0]["source_run_id"] = source
    return row


def _write(root, files):
    folder = root / "output_shared/state"
    folder.mkdir(parents=True, exist_ok=True)
    for name, rows in files.items():
        raw = rows if isinstance(rows, bytes) else b"".join(json.dumps(row).encode() + b"\n" for row in rows)
        with (folder / name).open("xb") as handle:
            handle.write(raw)


def _resolve(root, day="2026-10-21", tz=timezone.utc, **kwargs):
    return _resolve_notification_run(base=root, account="case_a", notification_date=date.fromisoformat(day), tz=tz, **kwargs)


@pytest.mark.parametrize("outer", ["2026-09-01T00:00:00Z", "2026-11-30T00:00:00Z"])
def test_inner_clock_is_not_pruned_by_filename_or_outer_time(tmp_path, outer):
    _write(tmp_path, {SEGMENT: [_event(inner="2026-10-01T04:00:00Z", outer=outer)], CURRENT: []})
    result = _resolve(tmp_path, "2026-10-01")
    assert result["run_id"] == "source"
    assert result["market"] == "US"
    assert result["matched_event_created_at_utc"] == "2026-10-01T04:00:00Z"


@pytest.mark.parametrize("kind", ["other_account", "no_send", "failed_account", "invalid_source", "valid"])
def test_history_hint_is_decided_after_account_delivery_and_source(tmp_path, kind):
    row = _event(inner="2026-10-01T04:00:00Z", outer="2026-10-21T04:00:00Z")
    if kind == "other_account":
        row = _event(inner="2026-10-01T04:00:00Z", account="case_b")
    elif kind == "no_send":
        row["extra"]["no_send"] = True
    elif kind == "failed_account":
        row["extra"]["send_summary"]["sent_accounts"] = ["case_b"]
    elif kind == "invalid_source":
        row["extra"]["report_refs"] = []
    _write(tmp_path, {SEGMENT: [row], CURRENT: []})
    result = _resolve(tmp_path, "2026-10-01")
    if kind == "valid":
        assert result["run_id"] == "source"
    else:
        expected = "notification_source_unavailable" if kind == "invalid_source" else "notification_audit_incomplete"
        assert result["reason"] == expected
        assert "run_id" not in result


@pytest.mark.parametrize("corruption", [b"{bad\n", b"[]\n", b"\xff\n"])
def test_damaged_retained_evidence_blocks_good_current_target(tmp_path, corruption):
    _write(tmp_path, {SEGMENT: corruption, CURRENT: [_event()]})
    assert _resolve(tmp_path)["reason"] == "notification_audit_incomplete"


@pytest.mark.parametrize("count", [5000, 5001])
def test_date_filter_precedes_internal_row_cap(tmp_path, count):
    filler = {"event_type": "assistant_perception", "action": "notification_delivery_completed",
              "event_at_utc": "2026-10-21T00:00:00Z"}
    other_day = {**filler, "event_at_utc": "2026-10-20T00:00:00Z"}
    _write(tmp_path, {SEGMENT: [_event(), *([filler] * (count - 1)), *([other_day] * 5001)], CURRENT: []})
    result = _resolve(tmp_path)
    assert result["total_count"] == count
    if count == 5000:
        assert result["run_id"] == "source"
    else:
        assert result["reason"] == "audit_window_truncated"


def test_default_iterator_limit_zero_and_stable_string_order(tmp_path):
    rows = [_event(source="first"), _event(source="second"),
            _event(outer="2026-10-21T05:00:00+02:00", source="lexically-newest")]
    _write(tmp_path, {SEGMENT: rows[:1], CURRENT: rows[1:]})
    result = iter_notification_perception_events(repo_root=tmp_path)
    assert [r["report_refs"][0]["source_run_id"] for r in result["events"]] == ["lexically-newest", "first", "second"]
    zero = iter_notification_perception_events(repo_root=tmp_path, limit=0)
    assert zero["events"] == [] and zero["truncated"] and zero["total_count"] == 3


@pytest.mark.parametrize("day,zone,inside,outside", [
    ("2026-10-21", "Asia/Shanghai", "2026-10-20T16:00:00Z", "2026-10-21T16:00:00Z"),
    ("2026-03-08", "America/New_York", "2026-03-08T07:00:00Z", "2026-03-09T04:00:00Z"),
    ("2026-11-01", "America/New_York", "2026-11-01T06:30:00Z", "2026-11-02T05:00:00Z"),
])
def test_candidate_local_calendar_boundaries(tmp_path, day, zone, inside, outside):
    _write(tmp_path, {CURRENT: [_event(inner=inside), _event(inner=outside, source="next-day")]})
    assert _resolve(tmp_path, day, ZoneInfo(zone))["run_id"] == "source"


def test_blank_and_valid_final_line_keep_internal_parser_contract(tmp_path):
    _write(tmp_path, {CURRENT: b"\n" + json.dumps(_event()).encode()})
    assert _resolve(tmp_path)["run_id"] == "source"
    window = read_notification_perception_events(repo_root=tmp_path,
        start_utc="2026-10-21T00:00:00Z", end_utc="2026-10-22T00:00:00Z")
    assert window["summary"]["malformed_count"] == 1
    assert window["coverage"]["stop_reason"] == "incomplete_line"
    assert window["pagination"]["total_count"] is None


@pytest.mark.parametrize("sources", [64, 65])
def test_source_bound_includes_current_and_never_hides_gap(tmp_path, sources):
    files = {f"audit_events.20261021.{n:06d}.jsonl": [] for n in range(sources - 1)}
    files[CURRENT] = [_event()]
    _write(tmp_path, files)
    result = _resolve(tmp_path)
    assert result.get("reason") == ("audit_window_truncated" if sources > 64 else None)
    assert bool(result.get("run_id")) is (sources == 64)


@pytest.mark.parametrize("mid_read", [False, True])
def test_candidate_cancellation_and_recovery(tmp_path, mid_read):
    _write(tmp_path, {SEGMENT: [_event()] * 20, CURRENT: []})
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= (12 if mid_read else 1)

    token = _QUERY_CONTEXT.set((None, cancelled))
    try:
        with pytest.raises(AgentToolError) as caught:
            _resolve(tmp_path)
        assert caught.value.code == "CANCELLED"
    finally:
        _QUERY_CONTEXT.reset(token)
    assert _resolve(tmp_path)["run_id"] == "source"
    token = _QUERY_CONTEXT.set((time.monotonic() - 1, None))
    try:
        with pytest.raises(AgentToolError) as caught:
            _resolve(tmp_path)
        assert caught.value.code == "BUDGET_EXHAUSTED"
    finally:
        _QUERY_CONTEXT.reset(token)


@pytest.mark.parametrize("source", [CURRENT, SEGMENT])
@pytest.mark.parametrize("change", ["append", "truncate", "replace", "disappear"])
def test_internal_source_change_during_read_is_incomplete(tmp_path, monkeypatch, source, change):
    # Native physical mutations are CI-only under the local fixture-retention policy.
    import src.application.agent_tools.candidate_filter_impl as candidate_filter

    files = {source: [_event(), _event()]}
    if source == SEGMENT:
        files[CURRENT] = []
    _write(tmp_path, files)
    path = tmp_path / "output_shared/state" / source
    original = path.read_bytes()
    before = path.stat()
    prefix = original.splitlines(keepends=True)[0]
    # Keep both complete records within one read buffer, so truncation cannot
    # leave a buffered partial JSON record and mask the identity failure.
    assert len(original) < io.DEFAULT_BUFFER_SIZE
    assert _resolve(tmp_path)["run_id"] == "source"
    mutation_count = 0
    scans = []
    opened = []
    original_open = os.open
    original_iterator = candidate_filter.iter_notification_perception_events

    def observed_open(name, flags, mode=0o777, *, dir_fd=None):
        descriptor = original_open(name, flags, mode, dir_fd=dir_fd)
        if name == source:
            opened.append(descriptor)
        return descriptor

    def observed_iterator(**kwargs):
        original_filter = kwargs["event_filter"]

        def change_after_first_event(event):
            nonlocal mutation_count
            accepted = original_filter(event)
            if mutation_count == 0:
                if change == "append":
                    with path.open("ab") as handle:
                        handle.write(prefix)
                    after = path.stat()
                    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
                    assert after.st_size > before.st_size
                elif change == "truncate":
                    with path.open("r+b") as handle:
                        handle.truncate(len(prefix))
                    after = path.stat()
                    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
                    assert after.st_size == len(prefix) < before.st_size
                    assert path.read_bytes() == prefix
                    assert json.loads(prefix) == _event()
                elif change == "replace":
                    replacement = path.with_suffix(".replacement")
                    with replacement.open("xb") as handle:
                        handle.write(original)
                    os.replace(replacement, path)
                    assert path.stat().st_ino != before.st_ino
                    assert path.read_bytes() == original
                else:
                    path.unlink()
                    assert not path.exists()
                mutation_count += 1
            return accepted

        result = original_iterator(**{**kwargs, "event_filter": change_after_first_event})
        scans.append(result)
        return result

    monkeypatch.setattr(os, "open", observed_open)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, observed_open})
    monkeypatch.setattr(candidate_filter, "iter_notification_perception_events", observed_iterator)

    resolution = _resolve(tmp_path)

    assert mutation_count == 1
    assert len(scans) == 1
    assert scans[0]["summary"]["status"] == "failed"
    assert scans[0]["summary"]["malformed_count"] == 0
    status = next(item for item in scans[0]["read_statuses"] if Path(item["path"]).name == source)
    assert status["status"] == "unreadable"
    assert status["reason"] == "source_changed"
    assert resolution["reason"] == "notification_audit_incomplete"
    assert "run_id" not in resolution
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError) as caught:
            os.fstat(descriptor)
        assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize("leaf", ["directory", "fifo", "hardlink", "symlink"])
def test_unsafe_current_leaf_is_never_candidate_evidence(tmp_path, monkeypatch, leaf):
    _write(tmp_path, {SEGMENT: [_event()]})
    path = tmp_path / "output_shared/state" / CURRENT
    if leaf == "directory":
        path.mkdir()
    elif leaf == "fifo":
        os.mkfifo(path)
    else:
        target = tmp_path / "invented.jsonl"
        target.write_text(json.dumps(_event()) + "\n")
        if leaf == "hardlink":
            os.link(target, path)
        else:
            path.symlink_to(target)
    original_open = os.open
    opened = []

    def observed_open(name, flags, mode=0o777, *, dir_fd=None):
        descriptor = original_open(name, flags, mode, dir_fd=dir_fd)
        if name == CURRENT:
            opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", observed_open)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, observed_open})
    assert _resolve(tmp_path)["reason"] == "notification_audit_incomplete"
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_raw_iterator_deadline_keeps_existing_reader_error(tmp_path):
    _write(tmp_path, {CURRENT: []})
    with pytest.raises(ProjectReaderError, match="time_deadline"):
        iter_notification_perception_events(repo_root=tmp_path, deadline_monotonic=time.monotonic() - 1)
