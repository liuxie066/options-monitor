"""Real SQLite effects through the Wheel facade and its ledger commands."""
from __future__ import annotations

import pytest

from src.application.ledger import wheel_commands
from src.application.ledger.api import end_wheel_lifecycle
from src.application.wheel import build_wheel_read_model
from tests.test_wheel_workflows import _wheel_repo


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_manual_end_rolls_back_an_appended_event_on_failure(tmp_path, monkeypatch, failure):
    repo, lot_id = _wheel_repo(tmp_path)
    batch = build_wheel_read_model(repo, "lx", 3000, market="us")["batches"][0]
    before = repo.list_wheel_events(account="lx")
    original = wheel_commands._wheel_batch
    calls = 0

    def fail_readback(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure("projection interrupted after append")
        return original(*args, **kwargs)

    args = dict(account="lx", lot_id=lot_id, market="us", actor="tester",
                request_id="rollback-manual-end", as_of_ms=4000,
                expected_batch_generation_hash=batch["batch_generation_hash"])
    with monkeypatch.context() as patch:
        patch.setattr(wheel_commands, "_wheel_batch", fail_readback)
        with pytest.raises(failure, match="projection interrupted"):
            end_wheel_lifecycle(repo, **args, apply_changes=True)
    assert calls == 2
    assert repo.list_wheel_events(account="lx") == before
    assert build_wheel_read_model(repo, "lx", 4000, market="us")["batches"][0]["lifecycle_status"] == "active"
    applied = end_wheel_lifecycle(repo, **args, apply_changes=True)
    repeated = end_wheel_lifecycle(repo, **args, apply_changes=True)
    assert applied["write_applied"] is True
    assert repeated["idempotent"] is True and repeated["write_applied"] is False
    assert len(repo.list_wheel_events(account="lx")) == len(before) + 1
