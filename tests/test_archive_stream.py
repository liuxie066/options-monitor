from __future__ import annotations
import hashlib
from pathlib import Path
import subprocess
import sys
import time

import pytest

from src.application.research.archive_stream import READ_FILE_SCRIPT, TransferError, WriteBudget, receive_file


def transfer(tmp_path, command, *, size=3, budget=None, seconds=3, stderr_limit=1024):
    return receive_file(
        command,
        destination=tmp_path / "target",
        size=size,
        sha256=hashlib.sha256(b"abc").hexdigest(),
        budget=budget or WriteBudget(size),
        deadline=time.monotonic() + seconds,
        grace=0.05,
        stderr_limit=stderr_limit,
        reserve_bytes=0,
    )


def test_real_frozen_stream_roundtrip(tmp_path):
    (tmp_path / "source").write_bytes(b"abc")
    command = [sys.executable, "-c", READ_FILE_SCRIPT, str(tmp_path), "source", "3", hashlib.sha256(b"abc").hexdigest()]
    result = transfer(tmp_path, command)
    assert result["written_bytes"] == 3
    assert (tmp_path / "target").read_bytes() == b"abc"


@pytest.mark.parametrize("payload", [b"ab", b"abcd", b"xyz"])
def test_wrong_length_or_hash_never_exceeds_budget(tmp_path, payload):
    budget = WriteBudget(3)
    with pytest.raises(TransferError):
        transfer(tmp_path, [sys.executable, "-c", f"import os; os.write(1,{payload!r})"], budget=budget)
    assert budget.written <= 3
    assert (tmp_path / "target").stat().st_size <= 3


def test_deadline_stops_real_child(tmp_path):
    started = time.monotonic()
    with pytest.raises(TransferError, match="deadline"):
        transfer(tmp_path, [sys.executable, "-c", "import time; time.sleep(5)"], seconds=0.1)
    assert time.monotonic() - started < 2


def test_stderr_is_bounded_without_retaining_payload(tmp_path):
    with pytest.raises(TransferError, match="stderr"):
        transfer(tmp_path, [sys.executable, "-c", 'import os; os.write(2,b"x"*4096)'], stderr_limit=10)


def test_source_growth_before_open_rejected(tmp_path):
    (tmp_path / "source").write_bytes(b"abcd")
    command = [sys.executable, "-c", READ_FILE_SCRIPT, str(tmp_path), "source", "3", hashlib.sha256(b"abc").hexdigest()]
    process = subprocess.run(command, capture_output=True)
    assert process.returncode != 0
    assert process.stdout == b""


def test_existing_destination_is_never_overwritten(tmp_path):
    (tmp_path / "target").write_bytes(b"original")
    with pytest.raises(FileExistsError):
        transfer(tmp_path, [sys.executable, "-c", 'raise Exception("must not start")'])
    assert (tmp_path / "target").read_bytes() == b"original"


def test_publish_batch_real_no_replace_and_content(tmp_path):
    from src.application.research.archive_stream import publish_batch

    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "receipt.json").write_text('{"state":"validated"}')
    target = tmp_path / "batch"
    publish_batch(staging, target, deadline=time.monotonic() + 3)
    assert not staging.exists()
    assert (target / "receipt.json").read_text() == '{"state":"validated"}'
    another = tmp_path / "another"
    another.mkdir()
    with pytest.raises(FileExistsError):
        publish_batch(another, target, deadline=time.monotonic() + 3)
    assert another.exists()
    assert (target / "receipt.json").exists()


def test_post_rename_fsync_failure_keeps_batch_without_success(tmp_path, monkeypatch):
    from src.application.research.archive_stream import publish_batch
    import os

    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "receipt").write_bytes(b"validated")
    target = tmp_path / "batch"
    real_sync = os.fsync

    def fail_after_rename(fd):
        if target.exists():
            raise OSError("synthetic directory sync failure")
        real_sync(fd)

    monkeypatch.setattr(os, "fsync", fail_after_rename)
    with pytest.raises(OSError, match="synthetic"):
        publish_batch(staging, target, deadline=time.monotonic() + 3)
    assert target.exists()
    assert (target / "receipt").read_bytes() == b"validated"


def test_group_cleanup_after_leader_exit(tmp_path):
    import os, signal
    from src.application.research.archive_stream import process_table, stop_group

    marker = tmp_path / "child.pid"
    script = """import os,signal,time,sys
pid=os.fork()
if pid:
    time.sleep(.2)
    sys.exit(0)
signal.signal(signal.SIGTERM,signal.SIG_IGN)
marker = sys.argv[1]
with open(marker + '.tmp', 'w') as stream:
    stream.write(str(os.getpid()))
os.replace(marker + '.tmp', marker)
time.sleep(10)
"""
    proc = subprocess.Popen([sys.executable, "-c", script, str(marker)], start_new_session=True)
    try:
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        child = int(marker.read_text())
        proc.wait(timeout=2)
        try:
            stop_group(proc, 0.2)
        except TransferError:
            # OS may retain an orphan zombie until init reaps it. Unknown is
            # required in that case, never reported as confirmed success.
            rows = process_table()
            assert child in rows
        else:
            assert not any(row["group"] == proc.pid for row in process_table().values())
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_single_stream_boundaries_and_rejection(tmp_path):
    import hashlib, sys, time
    from src.application.research.archive_stream import receive_files, READ_FILES_SCRIPT, WriteBudget, TransferError
    import pytest

    source = tmp_path / "source"
    source.mkdir()
    rows = []
    for name, data in [("empty", b""), ("a", b"a" * 70000), ("b", b"bbb")]:
        p = source / name
        p.write_bytes(data)
        st = p.stat()
        rows.append(
            dict(
                path=name,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                identity=[st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns],
            )
        )

    def run(command, target):
        target.mkdir()
        return receive_files(
            command,
            root=target,
            rows=rows,
            budget=WriteBudget(100000),
            deadline=time.monotonic() + 5,
            grace=0.2,
            stderr_limit=10000,
            reserve_bytes=1,
            evidence_limit=100000,
        )

    result = run([sys.executable, "-c", READ_FILES_SCRIPT, str(source)], tmp_path / "ok")
    assert len(result["files"]) == 3
    for row in rows:
        assert (tmp_path / "ok" / row["path"]).read_bytes() == (source / row["path"]).read_bytes()
    for name, program in [
        ("short", "import sys;sys.stdout.buffer.write(b'a'*5)"),
        ("wrong", "import sys;sys.stdout.buffer.write(b'b'*70003)"),
        ("extra", "import sys;sys.stdout.buffer.write(b'a'*70000+b'bbbx')"),
    ]:
        with pytest.raises(TransferError):
            run([sys.executable, "-c", program], tmp_path / name)
    (source / "a").write_bytes(b"changed")
    with pytest.raises(TransferError):
        run([sys.executable, "-c", READ_FILES_SCRIPT, str(source)], tmp_path / "changed")
