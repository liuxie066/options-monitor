from __future__ import annotations

import json

import pytest

from src.application import current_fx_run
from src.application.tick_run_workspace import AccountRunConfigError


def test_run_fx_seals_once_and_retries_original_snapshot(monkeypatch, tmp_path):
    calls = []
    snapshot = {"schema_version": 2, "pairs": {"USDCNY": {"rate": 7.2}}}

    def fetch(**_kwargs):
        calls.append(1)
        return snapshot

    monkeypatch.setattr(current_fx_run, "current_exchange_rate_snapshot", fetch)
    first, digest = current_fx_run.seal_run_fx_snapshot(base=tmp_path, run_id="batch-1")
    snapshot["pairs"]["USDCNY"]["rate"] = 8.0
    second, repeated_digest = current_fx_run.seal_run_fx_snapshot(base=tmp_path, run_id="batch-1")

    assert calls == [1]
    assert first == second
    assert second["pairs"]["USDCNY"]["rate"] == 7.2
    assert repeated_digest == digest


def test_run_fx_rejects_existing_corrupt_or_wrong_run_artifact(monkeypatch, tmp_path):
    path = tmp_path / "output_runs" / "batch-1" / "state" / current_fx_run.RUN_FX_NAME
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    monkeypatch.setattr(
        current_fx_run,
        "current_exchange_rate_snapshot",
        lambda **_kwargs: pytest.fail("corrupt run artifact must not refetch"),
    )
    with pytest.raises(current_fx_run.RunFXError, match="invalid"):
        current_fx_run.seal_run_fx_snapshot(base=tmp_path, run_id="batch-1")

    path.write_text(json.dumps({"schema_version": 1, "run_id": "another-batch"}), encoding="utf-8")
    with pytest.raises(current_fx_run.RunFXError, match="identity"):
        current_fx_run.seal_run_fx_snapshot(base=tmp_path, run_id="batch-1")


def test_run_fx_fails_closed_when_another_writer_seals_different_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        current_fx_run, "current_exchange_rate_snapshot",
        lambda **_kwargs: {"schema_version": 2, "pairs": {}},
    )
    monkeypatch.setattr(
        current_fx_run,
        "write_run_state_bytes_once_safely",
        lambda **_kwargs: (_ for _ in ()).throw(
            AccountRunConfigError("ACCOUNT_RUN_STATE_CONFLICT", "different bytes")
        ),
    )
    with pytest.raises(current_fx_run.RunFXError, match="conflicts"):
        current_fx_run.seal_run_fx_snapshot(base=tmp_path, run_id="batch-1")
