"""Exercise the optional template with real descriptors and real logrotate."""
from __future__ import annotations

import gzip
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(os.name != "posix", reason="logrotate is a POSIX deployment tool")
def test_closed_archive_ages_out_without_rotating_open_launchd_logs(tmp_path):
    import grp
    import pwd

    binary = os.environ.get("LOGROTATE_BIN") or shutil.which("logrotate")
    if not binary:
        pytest.skip("set LOGROTATE_BIN to exercise the optional deployment tool")
    logs = tmp_path / "logs"
    closed = logs / "closed"
    closed.mkdir(parents=True)
    archive = closed / "batch.log"
    archive.write_bytes(b"closed-once\n")
    template = Path(__file__).resolve().parents[1] / "deploy/logrotate/options-monitor.conf.in"
    content = template.read_text().replace("@RUNTIME_ROOT@", str(tmp_path))
    # macOS normally uses staff as the user's primary group.
    content = content.replace(
        "create 0600 @DEPLOY_USER@ @DEPLOY_USER@",
        f"create 0600 {pwd.getpwuid(os.getuid()).pw_name} {grp.getgrgid(os.getgid()).gr_name}",
    ).replace("@DEPLOY_USER@", pwd.getpwuid(os.getuid()).pw_name)
    config = tmp_path / "rotate.conf"
    config.write_text(content)
    live = [logs / "service.out.log", logs / "service.err.log"]
    with live[0].open("ab", buffering=0) as stdout, live[1].open("ab", buffering=0) as stderr:
        inodes = [os.fstat(stream.fileno()).st_ino for stream in (stdout, stderr)]
        for cycle in range(169):
            stdout.write(b"out\n")
            stderr.write(b"err\n")
            result = subprocess.run(
                [binary, "--force", "--state", str(tmp_path / "state"), str(config)],
                capture_output=True, text=True, timeout=10, check=False,
            )
            assert result.returncode == 0, result.stderr
            if cycle == 1:
                with gzip.open(str(archive) + ".2.gz", "rb") as segment:
                    assert segment.read() == b"closed-once\n"
        assert [path.stat().st_ino for path in live] == inodes
        assert live[0].read_bytes() == b"out\n" * 169
        assert live[1].read_bytes() == b"err\n" * 169
    assert not list(logs.glob("*.log.*"))
    assert archive.read_bytes() == b""
    assert archive.stat().st_mode & 0o777 == 0o600
    segments = list(closed.glob("batch.log.*"))
    assert len(segments) == 168
    for segment in segments:
        if segment.suffix == ".gz":
            with gzip.open(segment, "rb") as stream:
                assert stream.read() == b""
        else:
            assert segment.read_bytes() == b""
