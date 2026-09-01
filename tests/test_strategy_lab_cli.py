from __future__ import annotations

from pathlib import Path

import pytest

from src.interfaces.cli.main import parse_args
from src.interfaces.cli.strategy_lab_ops import handle_strategy_lab_command


NOW = "2026-08-30T12:00:00Z"
CUTOFF = "2026-08-29T08:00:00Z"


def _patch_read_only_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, object]]:
    import src.interfaces.cli.strategy_lab_ops as cli

    profile_path = tmp_path / "service.profile.json"
    fee_plan_path = tmp_path / "fee-plan.json"
    context = {"market": "hk", "account": "lx"}
    monkeypatch.setattr(cli, "load_service_profile", lambda path: {"path": str(path)})
    monkeypatch.setattr(cli, "resolve_strategy_lab_context", lambda _profile: context)
    monkeypatch.setattr(
        cli,
        "resolve_strategy_lab_runtime_context",
        lambda _profile, *, market: context,
    )
    monkeypatch.setattr(
        cli,
        "build_futu_gateway",
        lambda **_kwargs: pytest.fail("read-only Strategy Lab commands must not build a gateway"),
    )
    return profile_path, fee_plan_path, context


def test_strategy_lab_help_exposes_phase3_confirmation_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        parse_args(["strategy-lab", "--help"])

    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert (
            "{readiness,canary,recipes,preview,confirm-research,preview-validation,"
            "confirm-validation,advance,status,research,receipt}" in help_text
    )

    with pytest.raises(SystemExit) as research_help:
        parse_args(["strategy-lab", "research", "--help"])
    assert research_help.value.code == 0
    assert "{execute}" in capsys.readouterr().out


def test_canary_uses_light_context_one_clock_and_no_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.interfaces.cli.strategy_lab_ops as cli

    profile_path, _fee_plan_path, context = _patch_read_only_context(monkeypatch, tmp_path)
    clock_values = iter([NOW])
    monkeypatch.setattr(cli, "_now_utc", lambda: next(clock_values))
    monkeypatch.setattr(
        cli,
        "resolve_strategy_lab_context",
        lambda _profile: pytest.fail("canary must not resolve ledger or OpenD context"),
    )
    runtime_calls: list[tuple[object, str]] = []

    def resolve_runtime(profile: object, *, market: str) -> dict[str, object]:
        runtime_calls.append((profile, market))
        return context

    monkeypatch.setattr(cli, "resolve_strategy_lab_runtime_context", resolve_runtime)
    received: dict[str, object] = {}

    def preview(fake_context: object, **kwargs: object) -> dict[str, object]:
        received.update(context=fake_context, **kwargs)
        return {"authoritative": False, "status": "blocked"}

    monkeypatch.setattr(cli, "preview_engineering_canary", preview)

    response = handle_strategy_lab_command(parse_args(["strategy-lab", "canary", "--profile-path", str(profile_path)]))

    assert response["tool_name"] == "strategy-lab.canary"
    assert response["data"] == {"authoritative": False, "status": "blocked"}
    assert received == {"context": context, "occurred_at_utc": NOW}
    assert runtime_calls == [({"path": str(profile_path)}, "hk")]
    with pytest.raises(StopIteration):
        next(clock_values)
    assert list(tmp_path.iterdir()) == []


def test_recipes_freezes_one_clock_and_calls_only_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.interfaces.cli.strategy_lab_ops as cli

    profile_path, fee_plan_path, context = _patch_read_only_context(monkeypatch, tmp_path)
    clock_calls = 0

    def now() -> str:
        nonlocal clock_calls
        clock_calls += 1
        return NOW

    monkeypatch.setattr(cli, "_now_utc", now)
    monkeypatch.setattr(
        cli,
        "preview_experiment",
        lambda *_args, **_kwargs: pytest.fail("recipes must not call preview"),
    )
    received: dict[str, object] = {}

    def list_recipes(fake_context: object, **kwargs: object) -> dict[str, object]:
        received.update(context=fake_context, **kwargs)
        return {"recipes": []}

    monkeypatch.setattr(cli, "list_recipes", list_recipes)
    response = handle_strategy_lab_command(
        parse_args(
            [
                "strategy-lab",
                "recipes",
                "--profile-path",
                str(profile_path),
                "--fee-plan-receipt-path",
                str(fee_plan_path),
                "--maturity-cutoff-utc",
                CUTOFF,
            ]
        )
    )

    assert response["ok"] is True
    assert response["tool_name"] == "strategy-lab.recipes"
    assert response["data"] == {"recipes": []}
    assert clock_calls == 1
    assert received == {
        "context": context,
        "fee_plan_receipt_path": str(fee_plan_path),
        "maturity_cutoff_utc": CUTOFF,
        "occurred_at_utc": NOW,
    }
    assert not profile_path.exists()
    assert not fee_plan_path.exists()


def test_preview_passes_exact_request_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.interfaces.cli.strategy_lab_ops as cli

    profile_path, fee_plan_path, context = _patch_read_only_context(monkeypatch, tmp_path)
    clock_values = iter([NOW])
    monkeypatch.setattr(cli, "_now_utc", lambda: next(clock_values))
    monkeypatch.setattr(
        cli,
        "list_recipes",
        lambda *_args, **_kwargs: pytest.fail("preview must not list recipes"),
    )
    received: dict[str, object] = {}

    def preview(fake_context: object, request: object, **kwargs: object) -> dict[str, object]:
        received.update(context=fake_context, request=request, **kwargs)
        return {"status": "blocked", "blockers": ["fixture"]}

    monkeypatch.setattr(cli, "preview_experiment", preview)
    response = handle_strategy_lab_command(
        parse_args(
            [
                "strategy-lab",
                "preview",
                "--profile-path",
                str(profile_path),
                "--hypothesis",
                "降低期权持仓集中度是否改善收益",
                "--recipe-id",
                "sell_put_option_position_concentration",
                "--fee-plan-receipt-path",
                str(fee_plan_path),
                "--maturity-cutoff-utc",
                CUTOFF,
            ]
        )
    )

    assert response["ok"] is True
    assert response["tool_name"] == "strategy-lab.preview"
    assert received == {
        "context": context,
        "request": {
            "hypothesis": "降低期权持仓集中度是否改善收益",
            "recipe_id": "sell_put_option_position_concentration",
            "market": "hk",
            "account": "lx",
            "maturity_cutoff_utc": CUTOFF,
            "fee_plan_receipt_path": str(fee_plan_path),
        },
        "occurred_at_utc": NOW,
    }
    with pytest.raises(StopIteration):
        next(clock_values)
    assert list(tmp_path.iterdir()) == []


def test_confirm_research_freezes_one_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.interfaces.cli.strategy_lab_ops as cli

    profile_path, fee_plan_path, context = _patch_read_only_context(monkeypatch, tmp_path)
    clock_values = iter([NOW])
    monkeypatch.setattr(cli, "_now_utc", lambda: next(clock_values))
    received: dict[str, object] = {}

    def confirm(fake_context: object, request: object, **kwargs: object) -> dict[str, object]:
        received.update(context=fake_context, request=request, **kwargs)
        return {"status": "confirmed"}

    monkeypatch.setattr(cli, "confirm_research", confirm)
    response = handle_strategy_lab_command(
        parse_args(
            [
                "strategy-lab",
                "confirm-research",
                "--profile-path",
                str(profile_path),
                "--hypothesis",
                "集中度假设",
                "--recipe-id",
                "sell_put_option_position_concentration",
                "--fee-plan-receipt-path",
                str(fee_plan_path),
                "--maturity-cutoff-utc",
                CUTOFF,
                "--confirmed-preview-sha256",
                "a" * 64,
                "--actor",
                "tester",
                "--idempotency-key",
                "confirm-1",
            ]
        )
    )

    assert response["tool_name"] == "strategy-lab.confirm-research"
    assert received["context"] == context
    assert received["occurred_at_utc"] == NOW
    assert received["confirmed_preview_sha256"] == "a" * 64
    with pytest.raises(StopIteration):
        next(clock_values)


@pytest.mark.parametrize("command", ["preview-validation", "confirm-validation"])
def test_validation_confirmation_commands_freeze_one_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    import src.interfaces.cli.strategy_lab_ops as cli

    profile_path, _fee_plan_path, context = _patch_read_only_context(monkeypatch, tmp_path)
    clock_values = iter([NOW])
    monkeypatch.setattr(cli, "_now_utc", lambda: next(clock_values))
    received: dict[str, object] = {}
    service_name = command.replace("-", "_")

    def invoke(fake_context: object, experiment_id: str, requested_start: str, **kwargs: object):
        received.update(
            context=fake_context,
            experiment_id=experiment_id,
            requested_start=requested_start,
            **kwargs,
        )
        return {"status": "available"}

    monkeypatch.setattr(cli, service_name, invoke)
    argv = [
        "strategy-lab",
        command,
        "--profile-path",
        str(profile_path),
        "--experiment-id",
        "exp-1",
        "--requested-start",
        "2026-09-01",
    ]
    if command == "confirm-validation":
        argv.extend(
            [
                "--confirmed-preview-sha256",
                "a" * 64,
                "--actor",
                "tester",
                "--idempotency-key",
                "validation-confirm-1",
            ]
        )

    response = handle_strategy_lab_command(parse_args(argv))

    assert response["tool_name"] == f"strategy-lab.{command}"
    assert received["context"] == context
    assert received["occurred_at_utc"] == NOW
    assert received["experiment_id"] == "exp-1"
    assert received["requested_start"] == "2026-09-01"
    if command == "confirm-validation":
        assert received["confirmed_preview_sha256"] == "a" * 64
    with pytest.raises(StopIteration):
        next(clock_values)


@pytest.mark.parametrize(
    ("command", "service_name", "tool_name"),
    [
        ("status", "get_experiment_status", "strategy-lab.status"),
        ("receipt", "read_receipt", "strategy-lab.receipt"),
    ],
)
def test_status_and_receipt_do_not_read_clock_or_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    service_name: str,
    tool_name: str,
) -> None:
    import src.interfaces.cli.strategy_lab_ops as cli

    profile_path, _fee_plan_path, context = _patch_read_only_context(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli,
        "resolve_strategy_lab_context",
        lambda _profile: pytest.fail("historical reads must not resolve current accounts"),
    )
    monkeypatch.setattr(cli, "_now_utc", lambda: pytest.fail("read path must not read clock"))
    monkeypatch.setattr(
        cli,
        service_name,
        lambda fake_context, experiment_id, **_kwargs: {
            "context_matches": fake_context == context,
            "experiment_id": experiment_id,
        },
    )
    response = handle_strategy_lab_command(
        parse_args(
            [
                "strategy-lab",
                command,
                "--profile-path",
                str(profile_path),
                "--experiment-id",
                "exp-1",
            ]
        )
    )

    assert response["tool_name"] == tool_name
    assert response["data"] == {"context_matches": True, "experiment_id": "exp-1"}


def test_research_execute_freezes_one_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.interfaces.cli.strategy_lab_ops as cli

    profile_path, _fee_plan_path, context = _patch_read_only_context(monkeypatch, tmp_path)
    clock_values = iter([NOW])
    monkeypatch.setattr(cli, "_now_utc", lambda: next(clock_values))
    received: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "execute_research",
        lambda fake_context, experiment_id, **kwargs: received.update(
            context=fake_context, experiment_id=experiment_id, **kwargs
        )
        or {"status": "progress"},
    )

    response = handle_strategy_lab_command(
        parse_args(
            [
                "strategy-lab",
                "research",
                "execute",
                "--profile-path",
                str(profile_path),
                "--experiment-id",
                "exp-1",
                "--actor",
                "tester",
            ]
        )
    )

    assert response["tool_name"] == "strategy-lab.research.execute"
    assert received == {
        "context": context,
        "experiment_id": "exp-1",
        "actor": "tester",
        "occurred_at_utc": NOW,
    }
    with pytest.raises(StopIteration):
        next(clock_values)
