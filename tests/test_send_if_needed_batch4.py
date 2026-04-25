from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


def _argv(cfg_path: Path) -> list[str]:
    return [
        "send_if_needed.py",
        "--config",
        str(cfg_path),
        "--state-dir",
        "output/state_test_send_if_needed_batch4",
        "--target",
        "user:test",
        "--notification",
        "README.md",
    ]


def test_send_if_needed_blocks_on_scheduler_snapshot_schema_failure(
    argv_scope,
    example_config_path,
    monkeypatch,
    send_if_needed_common_patches,
) -> None:
    mod = send_if_needed_common_patches["module"]
    monkeypatch.setattr(mod, "run_scan_scheduler_cli", lambda **_kwargs: SimpleNamespace(returncode=0, stdout='["bad"]', stderr=""))
    argv_scope(_argv(example_config_path))

    try:
        mod.main()
        assert False, "expected SystemExit"
    except SystemExit as exc:
        msg = str(exc)
        assert "SCHEMA_VALIDATION_FAILED" in msg
        assert "scheduler_decision" in msg


def test_send_if_needed_blocks_on_decision_schema_failure(
    argv_scope,
    example_config_path,
    monkeypatch,
    send_if_needed_common_patches,
) -> None:
    mod = send_if_needed_common_patches["module"]
    monkeypatch.setattr(
        mod,
        "run_scan_scheduler_cli",
        lambda **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"should_run_scan": True, "is_notify_window_open": True, "reason": "ok"}),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        mod.Decision,
        "from_payload",
        classmethod(lambda _cls, _raw: (_ for _ in ()).throw(mod.SchemaValidationError("bad decision"))),
    )
    argv_scope(_argv(example_config_path))

    try:
        mod.main()
        assert False, "expected SystemExit"
    except SystemExit as exc:
        msg = str(exc)
        assert "SCHEMA_VALIDATION_FAILED" in msg
        assert "scheduler_decision" in msg


def test_send_if_needed_skips_delivery_plan_when_not_sending(
    argv_scope,
    example_config_path,
    monkeypatch,
    send_if_needed_common_patches,
) -> None:
    mod = send_if_needed_common_patches["module"]
    seen = {"called": False}
    monkeypatch.setattr(
        mod,
        "run_scan_scheduler_cli",
        lambda **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"should_run_scan": True, "is_notify_window_open": False, "reason": "window closed"}),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        mod.DeliveryPlan,
        "from_payload",
        classmethod(lambda _cls, _raw: seen.__setitem__("called", True)),
    )

    argv_scope(_argv(example_config_path))
    assert mod.main() == 0
    assert seen["called"] is False


def test_send_if_needed_blocks_on_pipeline_subprocess_adapter_schema_failure(
    argv_scope,
    example_config_path,
    monkeypatch,
    send_if_needed_common_patches,
) -> None:
    mod = send_if_needed_common_patches["module"]
    monkeypatch.setattr(
        mod,
        "run_scan_scheduler_cli",
        lambda **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"should_run_scan": True, "is_notify_window_open": False, "reason": "ok"}),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        mod,
        "normalize_pipeline_subprocess_output",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad pipeline adapter")),
    )
    argv_scope(_argv(example_config_path))

    try:
        mod.main()
        assert False, "expected SystemExit"
    except SystemExit as exc:
        msg = str(exc)
        assert "SCHEMA_VALIDATION_FAILED" in msg
        assert "pipeline_subprocess_adapter" in msg


def test_send_if_needed_blocks_on_notify_subprocess_adapter_schema_failure(
    argv_scope,
    example_config_path,
    monkeypatch,
    send_if_needed_common_patches,
) -> None:
    mod = send_if_needed_common_patches["module"]
    monkeypatch.setattr(
        mod,
        "run_scan_scheduler_cli",
        lambda **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"should_run_scan": True, "is_notify_window_open": True, "reason": "ok"}),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        mod,
        "normalize_notify_subprocess_output",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad notify adapter")),
    )
    argv_scope(_argv(example_config_path))

    try:
        mod.main()
        assert False, "expected SystemExit"
    except SystemExit as exc:
        msg = str(exc)
        assert "SCHEMA_VALIDATION_FAILED" in msg
        assert "notify_subprocess_adapter" in msg
