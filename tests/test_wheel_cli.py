from pathlib import Path

import pytest

import src.interfaces.cli.wheel as wheel_cli


def _end_args(*extra: str):
    return wheel_cli.parse_args(
        [
            "end",
            "--account",
            "lx",
            "--stock-lot-id",
            "assigned-stock-1",
            "--expected-batch-generation-hash",
            "generation-1",
            "--request-id",
            "request-1",
            "--actor",
            "tester",
            "--config-key",
            "us",
            *extra,
        ]
    )


def _branch_args(action: str, *extra: str):
    return wheel_cli.parse_args(
        [
            "branch",
            action,
            "--account",
            "lx",
            "--stock-lot-id",
            "assigned-stock-1",
            "--expected-branch-generation-hash",
            "branch-generation-1",
            "--request-id",
            "request-1",
            "--actor",
            "tester",
            "--config-key",
            "us",
            *extra,
        ]
    )


def _put_linkage_args(action: str, *extra: str):
    args = [
        "linkage",
        action,
        "--account",
        "lx",
        "--wheel-branch-id",
        "wheel-put-1",
        "--direction",
        "put",
        "--expected-branch-generation-hash",
        "generation-1",
        "--request-id",
        "request-1",
        "--actor",
        "tester",
        "--option-record-id",
        "put-lot-1",
        "--linkage-candidate-id",
        "candidate-1",
        "--expected-input-hash",
        "input-1",
        "--config-key",
        "us",
    ]
    if action == "reject":
        args.extend(("--reason", "not this cycle"))
    return wheel_cli.parse_args([*args, *extra])


def _stub_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        wheel_cli,
        "load_runtime_config",
        lambda **_kwargs: (tmp_path / "config.us.json", {"portfolio": {}}),
    )
    monkeypatch.setattr(
        wheel_cli,
        "resolve_position_data_config_path",
        lambda **_kwargs: tmp_path / "portfolio.runtime.json",
    )
    monkeypatch.setattr(
        wheel_cli,
        "open_position_ledger_from_runtime_config",
        lambda **_kwargs: (tmp_path / "portfolio.runtime.json", object()),
    )


def test_wheel_cli_end_previews_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(
        wheel_cli,
        "load_runtime_config",
        lambda **_kwargs: (tmp_path / "config.us.json", {"portfolio": {}}),
    )
    monkeypatch.setattr(
        wheel_cli,
        "resolve_position_data_config_path",
        lambda **_kwargs: tmp_path / "portfolio.runtime.json",
    )
    monkeypatch.setattr(
        wheel_cli,
        "open_position_ledger_from_runtime_config",
        lambda **_kwargs: (tmp_path / "portfolio.runtime.json", object()),
    )

    def _end(_repo, **kwargs):
        calls.append(kwargs)
        return {"dry_run": True, "write_applied": False}

    monkeypatch.setattr(wheel_cli, "end_wheel_lifecycle", _end)

    assert wheel_cli.execute(_end_args()) == {"dry_run": True, "write_applied": False}
    assert calls[0]["apply_changes"] is False
    assert calls[0]["stock_lot_id"] == "assigned-stock-1"


def test_wheel_cli_requires_apply_with_confirmation() -> None:
    with pytest.raises(SystemExit, match="require --apply"):
        wheel_cli.execute(_end_args("--confirm"))


def test_wheel_cli_branch_resolves_legacy_call_alias_and_previews(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_runtime(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        wheel_cli,
        "build_wheel_read_model",
        lambda *_args, **_kwargs: {
            "wheel_branches": [
                {
                    "wheel_branch_id": "wheel-call-1",
                    "direction": "call",
                    "stock_lot_id": "assigned-stock-1",
                }
            ]
        },
    )
    monkeypatch.setattr(
        wheel_cli,
        "resolve_wheel_config",
        lambda *_args, **_kwargs: {
            "market": "us",
            "activation_descriptor": {"generation": 1},
            "policy_sha256": "a" * 64,
        },
    )

    def _decide(_repo, **kwargs):
        calls.append(kwargs)
        return {"dry_run": True, "write_applied": False}

    monkeypatch.setattr(wheel_cli.wheel_application, "decide_wheel_branch", _decide)

    assert wheel_cli.execute(_branch_args("start"))["dry_run"] is True
    assert calls == [
        {
            "account": "lx",
            "wheel_branch_id": "wheel-call-1",
            "decision": "start",
            "expected_branch_generation_hash": "branch-generation-1",
            "request_id": "request-1",
            "actor": "tester",
            "market": "us",
            "apply_changes": False,
            "as_of_ms": calls[0]["as_of_ms"],
            "market": "us",
            "activation_descriptor": {"generation": 1},
            "policy_sha256": "a" * 64,
        }
    ]


def test_wheel_cli_branch_identity_is_exactly_one() -> None:
    with pytest.raises(SystemExit):
        wheel_cli.parse_args(
            [
                "branch",
                "end",
                "--account",
                "lx",
                "--wheel-branch-id",
                "branch-1",
                "--stock-lot-id",
                "lot-1",
                "--expected-branch-generation-hash",
                "generation-1",
                "--request-id",
                "request-1",
                "--actor",
                "tester",
                "--config-key",
                "us",
            ]
        )


def test_wheel_cli_branch_apply_requires_confirmation() -> None:
    with pytest.raises(SystemExit, match="use --confirm or --yes"):
        wheel_cli.execute(_branch_args("end", "--apply"))


def test_wheel_cli_activation_status_and_enable_preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_runtime(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(wheel_cli, "build_wheel_policy_hash", lambda *_args, **_kwargs: "b" * 64)

    def _change(_repo, **kwargs):
        calls.append(kwargs)
        return {"dry_run": True, "write_applied": False}

    monkeypatch.setattr(wheel_cli.wheel_application, "change_wheel_activation", _change)

    status = wheel_cli.parse_args(
        ["activation", "status", "--market", "us", "--account", "lx"]
    )
    enable = wheel_cli.parse_args(
        [
            "activation",
            "enable",
            "--market",
            "us",
            "--account",
            "lx",
            "--expected-current-generation",
            "0",
            "--request-id",
            "enable-1",
            "--actor",
            "tester",
        ]
    )

    assert wheel_cli.execute(status)["dry_run"] is True
    assert wheel_cli.execute(enable)["dry_run"] is True
    assert calls == [
        {"action": "status", "market": "us", "account": "lx"},
        {
            "action": "enable",
            "market": "us",
            "account": "lx",
            "expected_current_generation": 0,
            "request_id": "enable-1",
            "actor": "tester",
            "policy_sha256": "b" * 64,
            "apply_changes": False,
        },
    ]


def test_wheel_cli_put_linkage_reject_previews_canonical_branch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_runtime(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        wheel_cli,
        "build_wheel_read_model",
        lambda *_args, **_kwargs: {
            "wheel_branches": [
                {
                    "wheel_branch_id": "wheel-put-1",
                    "direction": "put",
                    "stock_lot_id": None,
                }
            ]
        },
    )

    def _reject(_repo, **kwargs):
        calls.append(kwargs)
        return {"dry_run": True, "write_applied": False}

    monkeypatch.setattr(wheel_cli, "reject_wheel_linkage", _reject)

    assert wheel_cli.execute(_put_linkage_args("reject"))["dry_run"] is True
    assert calls[0] == {
        "account": "lx",
        "wheel_branch_id": "wheel-put-1",
        "direction": "put",
        "expected_branch_generation_hash": "generation-1",
        "request_id": "request-1",
        "actor": "tester",
        "market": "us",
        "apply_changes": False,
        "as_of_ms": calls[0]["as_of_ms"],
        "option_record_id": "put-lot-1",
        "linkage_candidate_id": "candidate-1",
        "expected_input_hash": "input-1",
        "reason": "not this cycle",
    }


def test_wheel_cli_put_rejects_legacy_stock_lot_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_runtime(monkeypatch, tmp_path)
    args = _put_linkage_args("reject")
    args.wheel_branch_id = None
    args.stock_lot_id = "legacy-lot"
    with pytest.raises(ValueError, match="Call-only alias"):
        wheel_cli.execute(args)
