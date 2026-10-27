"""Report identity regressions keep a second barrier even when the product fails."""

from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3

import pytest

from src.application.ledger import lot_parity_probe as probe
from src.application.ledger.manual_trades import persist_manual_open_event
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from tests.ledger_sqlite_test_support import connect_ledger_fixture


ALIASES = ("absolute", "relative", "parent", "leaf_symlink", "parent_symlink", "hardlink")


def _alias(source: Path, kind: str) -> Path:
    if kind == "absolute":
        return source
    if kind == "relative":
        return Path(os.path.relpath(source))
    if kind == "parent":
        child = source.parent / "child"
        child.mkdir()
        return child / ".." / source.name
    if kind == "parent_symlink":
        link = source.parent / "directory-link"
        link.symlink_to(source.parent, target_is_directory=True)
        return link / source.name
    link = source.parent / kind
    if kind == "leaf_symlink":
        link.symlink_to(source)
    else:
        assert kind == "hardlink"
        link.hardlink_to(source)
    return link


@contextmanager
def _write_barrier(monkeypatch, inputs, outputs=()):
    # Independent of Path.stat fault injection: never delegate an input-alias write.
    native_stat = os.stat
    real_write = Path.write_text
    identities = {(s.st_dev, s.st_ino) for s in map(native_stat, inputs)}
    before = {path: path.read_bytes() for path in inputs}
    allowed = {os.path.realpath(path) for path in outputs}
    calls = []

    def guarded(path, *args, **kwargs):
        calls.append(str(path))
        try:
            info = native_stat(path)
        except FileNotFoundError:
            info = None
        except OSError as exc:
            raise AssertionError("secondary barrier: uncertain identity") from exc
        if info is not None and (info.st_dev, info.st_ino) in identities:
            raise AssertionError("secondary barrier: input alias reached write_text")
        assert os.path.realpath(path) in allowed, "unregistered report sink"
        return real_write(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", guarded)
        try:
            yield calls
        finally:
            for path, content in before.items():
                assert path.read_bytes() == content


@pytest.mark.parametrize("kind", ALIASES)
def test_report_rejects_input_alias_before_any_sink(tmp_path, monkeypatch, kind):
    source = tmp_path / "input.sqlite3"
    source.write_bytes(b"retained synthetic input\n")
    target = _alias(source, kind)
    stdout = StringIO()
    mkdir_calls = []
    with _write_barrier(monkeypatch, [source]) as sinks:
        with monkeypatch.context() as patch, redirect_stdout(stdout):
            patch.setattr(Path, "mkdir", lambda *a, **k: mkdir_calls.append(a))
            with pytest.raises(ValueError, match="must not alias the input database"):
                probe.write_report({"sqlite_path": str(source), "green": True}, out_path=target)
        assert sinks == []
        assert mkdir_calls == []
    assert stdout.getvalue() == ""


@pytest.mark.parametrize("failure", ("source_missing", "source_permission", "target_permission", "target_io"))
def test_report_identity_errors_precede_mkdir_and_write(tmp_path, monkeypatch, failure):
    source = tmp_path / "input.sqlite3"
    source.write_bytes(b"retained synthetic input\n")
    target = tmp_path / "new-parent" / "report.json"
    real_stat = Path.stat
    exception = {
        "source_missing": FileNotFoundError,
        "source_permission": PermissionError,
        "target_permission": PermissionError,
        "target_io": OSError,
    }[failure]
    failed_path = source if failure.startswith("source") else target
    mkdir_calls = []

    def stat(path, *args, **kwargs):
        if path == failed_path:
            raise exception("injected identity inspection failure")
        return real_stat(path, *args, **kwargs)

    with _write_barrier(monkeypatch, [source]) as sinks:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "stat", stat)
            patch.setattr(Path, "mkdir", lambda *a, **k: mkdir_calls.append(a))
            with pytest.raises(exception, match="injected identity inspection failure"):
                probe.write_report({"sqlite_path": str(source)}, out_path=target)
        assert sinks == []
        assert mkdir_calls == []
    assert not target.parent.exists()


@pytest.mark.parametrize("kind", ("fresh", "overwrite", "leaf_symlink", "parent_symlink", "hardlink"))
def test_report_allows_independent_targets(tmp_path, monkeypatch, kind):
    source = tmp_path / "input.sqlite3"
    source.write_bytes(b"retained synthetic input\n")
    independent = tmp_path / "report.json"
    if kind == "fresh":
        target = tmp_path / "new-parent" / "report.json"
    else:
        independent.write_bytes(b"own separate report fixture\n")
        target = independent if kind == "overwrite" else _alias(independent, kind)
    report = {"sqlite_path": str(source), "green": True, "note": "独立报告"}
    with _write_barrier(monkeypatch, [source], [target]) as sinks:
        assert probe.write_report(report, out_path=target) == target.resolve()
        assert len(sinks) == 1
    assert json.loads(target.read_text(encoding="utf-8")) == report


def test_report_without_source_and_runtime_state_guard(tmp_path, monkeypatch):
    target = tmp_path / "partial.json"
    with _write_barrier(monkeypatch, [], [target]):
        assert probe.write_report({"green": True}, out_path=target) == target
    assert json.loads(target.read_text()) == {"green": True}
    forbidden = tmp_path / "output_shared" / "state" / "report.json"
    with _write_barrier(monkeypatch, []) as sinks:
        with pytest.raises(ValueError, match="outside output_shared/state/"):
            probe.write_report({"green": True}, out_path=forbidden)
        assert sinks == []
    assert not forbidden.parent.exists()


def _canonical_store(tmp_path, *, red=False):
    source = tmp_path / "canonical.sqlite3"
    with source.open("xb"):
        pass
    repo = SQLiteOptionPositionsRepository(source)
    repo.data_config_path = None
    persist_manual_open_event(
        repo, broker="富途", account="lx", symbol="TSLA", option_type="put",
        side="short", contracts=1, currency="USD", strike=100.0, multiplier=100,
        expiration_ymd="2026-12-18", premium_per_share=1.23,
        opened_at_ms=int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000),
    )
    conn = connect_ledger_fixture(source)
    try:
        if red:
            conn.execute("UPDATE position_lots SET strike = 999.0")
            conn.commit()
        conn.execute("PRAGMA wal_checkpoint(FULL)")
    finally:
        conn.close()
    return source


def _snapshot(source):
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    conn = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        events = conn.execute("SELECT * FROM trade_events ORDER BY event_id").fetchall()
        lots = conn.execute("SELECT * FROM position_lots ORDER BY lot_id").fetchall()
        assert len(events) == len(lots) == 1
        return digest, events, lots
    finally:
        conn.close()


@pytest.mark.parametrize("kind", ALIASES)
def test_module_cli_rejects_canonical_input_alias(tmp_path, monkeypatch, kind):
    source = _canonical_store(tmp_path)
    target = _alias(source, kind)
    before = _snapshot(source)
    stdout = StringIO()
    with _write_barrier(monkeypatch, [source]) as sinks, redirect_stdout(stdout):
        with pytest.raises(ValueError, match="must not alias the input database"):
            probe.main(["--db", str(source), "--out", str(target)])
        assert sinks == []
    assert stdout.getvalue() == ""
    assert _snapshot(source) == before


@pytest.mark.parametrize("red", (False, True))
def test_module_cli_preserves_reports_stdout_and_exit(tmp_path, monkeypatch, red):
    source = _canonical_store(tmp_path, red=red)
    before = _snapshot(source)
    target = tmp_path / "fresh-report" / "report.json"
    stdout = StringIO()
    with _write_barrier(monkeypatch, [source], [target]) as sinks, redirect_stdout(stdout):
        assert probe.main(["--db", str(source), "--out", str(target)]) == int(red)
        assert len(sinks) == 1
    report = json.loads(target.read_text())
    receipt = json.loads(stdout.getvalue())
    assert report["sqlite_path"] == str(source.resolve())
    assert receipt["report_path"] == str(target.resolve())
    assert receipt["green"] is report["green"] is (not red)
    assert receipt["difference_count"] == report["difference_count"]
    assert receipt["c_attribution"] == probe.probe_summary(report)["c_attribution"]
    assert receipt["connection_mode"] == report["connection_mode"]
    if red:
        assert report["faces"]["b_columns"]["by_column"]["strike"] == 1
        assert report["difference_count"] > 0
    else:
        assert report["difference_count"] == 0
    stdout = StringIO()
    with _write_barrier(monkeypatch, [source]) as sinks, redirect_stdout(stdout):
        assert probe.main(["--db", str(source)]) == int(red)
        assert sinks == []
    printed = json.loads(stdout.getvalue())
    assert printed["sqlite_path"] == report["sqlite_path"]
    assert printed["green"] == report["green"]
    assert printed["faces"] == report["faces"]
    forbidden = tmp_path / "output_shared" / "state" / "report.json"
    stdout = StringIO()
    with _write_barrier(monkeypatch, [source]) as sinks, redirect_stdout(stdout):
        with pytest.raises(ValueError, match="outside output_shared/state/"):
            probe.main(["--db", str(source), "--out", str(forbidden)])
        assert sinks == []
    assert stdout.getvalue() == ""
    assert not forbidden.parent.exists()
    assert _snapshot(source) == before
