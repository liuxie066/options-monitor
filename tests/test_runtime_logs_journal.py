from pathlib import Path

from src.application.runtime_logs_cli import collect_runtime_logs, format_runtime_logs


def test_empty_service_log_directory_points_to_journal(tmp_path: Path) -> None:
    data = collect_runtime_logs(repo_root=tmp_path, runs_root=tmp_path / "runs",
                                logs_root=tmp_path / "logs", kind="service")
    assert data["summary"]["log_source"] == "journal_only"
    assert "journalctl" in data["journal_hint"]
    assert "journalctl" in format_runtime_logs(data)
