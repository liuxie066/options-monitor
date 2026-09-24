from pathlib import Path

from src.application.runtime_logs_cli import collect_runtime_logs, format_runtime_logs


def test_empty_service_log_directory_points_to_journal(tmp_path: Path) -> None:
    data = collect_runtime_logs(repo_root=tmp_path, runs_root=tmp_path / "runs",
                                logs_root=tmp_path / "logs", kind="service")
    assert data["summary"]["log_source"] == "journal_only"
    assert "journalctl" in data["journal_hint"]
    assert "journalctl" in format_runtime_logs(data)


def test_large_audit_file_reads_only_bounded_tail(tmp_path: Path) -> None:
    audit = tmp_path / "runs" / "run-1" / "state" / "audit_events.jsonl"
    audit.parent.mkdir(parents=True)
    with audit.open("wb") as stream:
        stream.truncate(92 * 1024 * 1024)
        stream.seek(-len(b"\nlast-line\n"), 2)
        stream.write(b"\nlast-line\n")
    data = collect_runtime_logs(repo_root=tmp_path, runs_root=tmp_path / "runs",
                                run_id="run-1", kind="audit", lines=1)
    assert data["files"][0]["tail"] == ["last-line"]
    assert data["files"][0]["tail_truncated"] is True


def test_log_read_failure_emits_local_meta_signal_on_retry(tmp_path: Path, monkeypatch, capsys) -> None:
    from src.application import runtime_logs_cli

    path = tmp_path / "logs" / "service.log"
    path.parent.mkdir()
    path.write_text("data\n", encoding="utf-8")

    def unreadable(*_args, **_kwargs):
        raise OSError("fixture read failure")

    monkeypatch.setattr(runtime_logs_cli, "_tail_lines", unreadable)
    for _ in range(2):
        out = collect_runtime_logs(repo_root=tmp_path, logs_root=path.parent,
                                   log_file=path, lines=1)
        assert out["summary"]["ok"] is False
        assert out["files"][0]["error_code"] == "LOG_READ_FAILED"
    assert capsys.readouterr().err.count("<3>READ_DIAGNOSTIC_DEGRADED") == 2
