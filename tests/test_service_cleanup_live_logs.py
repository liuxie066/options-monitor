import os
import plistlib
from datetime import datetime, timezone

import pytest

from src.application.service_cleanup import service_cleanup
from src.application.service_deploy import _launchd_plist


@pytest.mark.parametrize("label", ["com.options-monitor.feishu-ws", "com.options-monitor.tick-us"])
def test_cleanup_preserves_quiet_launchd_descriptors(tmp_path, label):
    release = tmp_path / "releases" / "1.0.0"
    release.mkdir(parents=True)
    (release / "VERSION").write_text("1.0.0")
    current = tmp_path / "current"
    current.symlink_to(release)
    runtime = tmp_path / "runtime"
    logs = runtime / "logs"
    logs.mkdir(parents=True)
    payload = _launchd_plist(
        label=label, repo_root=current, runtime_root=runtime,
        program_args=["fixture"], log_root=logs, keep_alive=True,
    )
    definition = plistlib.loads(payload.encode())
    stdout = logs / definition["StandardOutPath"].split("/")[-1]
    stderr = logs / definition["StandardErrorPath"].split("/")[-1]
    historical = logs / "old.log"
    for path in (stdout, stderr, historical):
        path.write_bytes(b"before\n")
        os.utime(path, (1_600_000_000, 1_600_000_000))
    args = dict(
        repo_root=current, runtime_root=runtime, cleanup_runtime_logs=True,
        now=datetime(2026, 10, 26, tzinfo=timezone.utc),
    )
    with stdout.open("ab") as out, stderr.open("ab") as err:
        preview = service_cleanup(**args)
        assert {row["path"] for row in preview["runtime_logs_cleanup"]["delete_logs"]} == {str(historical)}
        result = service_cleanup(**args, confirm=True)
        assert result["ok"] is True
        out.write(b"after\n")
        err.write(b"after\n")
    assert stdout.read_bytes() == stderr.read_bytes() == b"before\nafter\n"
    assert not historical.exists()
    assert {row["reason"] for row in result["runtime_logs_cleanup"]["protected_logs"]} == {"service_output_log"}
