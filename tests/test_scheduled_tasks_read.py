from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


def _generated_profile(
    tmp_path: Path,
    *,
    markets: list[str],
    accounts: list[str] | None = None,
) -> tuple[Path, dict]:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    configs = {market: runtime / f"config.{market}.json" for market in markets}
    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=accounts or ["lx"],
        markets=markets,
        config_paths=configs,
    )
    profile = json.loads(
        next(
            item["content"]
            for item in bundle["files"]
            if item["relative_path"] == "service.profile.json"
        )
    )
    return runtime, profile


def _state_run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
    """Report every probed unit as ``active``/``enabled``."""
    state = "active" if command[1] == "is-active" else "enabled"
    return subprocess.CompletedProcess(command, 0, stdout=state + "\n", stderr="")


def _us_inventory(profile: dict, **overrides) -> dict:
    """Run the us-market inventory authorizing ``lx`` over ``profile``."""
    from src.application.service_deploy import scheduled_tasks_from_profile

    kwargs = {"market": "us", "authorized_accounts": ["lx"]}
    kwargs.update(overrides)
    return scheduled_tasks_from_profile(profile, **kwargs)


def _write_tool_runtime(runtime: Path, profile: dict, *, accounts: list[str]) -> Path:
    """Write the runtime config plus profile that ``run_scheduled_tasks_tool`` reads."""
    config = runtime / "config.us.json"
    config.write_text(
        json.dumps(
            {
                "_generated": {"market": "us", "source_format": "yaml"},
                "_resolved": {"market": "us", "source_format": "yaml"},
                "accounts": accounts,
                "portfolio": {},
                "symbols": [],
            }
        ),
        encoding="utf-8",
    )
    (runtime / "service.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return config


def test_generated_profile_drives_deduplicated_market_inventory(tmp_path: Path) -> None:
    _runtime, profile = _generated_profile(tmp_path, markets=["us"])
    profile["services"].append({"name": "options-monitor-tick-us.timer"})
    profile["services"].append({"name": "options-monitor-unknown.timer"})

    def run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["timeout"] <= 1.0
        state = "active" if command[1] == "is-active" else "enabled"
        return subprocess.CompletedProcess(command, 0, stdout=state + "\n", stderr="")

    value = _us_inventory(profile, run_cmd=run_cmd)

    assert value["coverage"] == "partial"
    assert value["availability"] == "partial"
    assert value["reasons"] == ["task_scope_unknown"]
    assert [item["name"] for item in value["tasks"]] == [
        "options-monitor-auto-close-us.timer",
        "options-monitor-projection-verify.timer",
        "options-monitor-runtime-status.timer",
        "options-monitor-tick-us.timer",
    ]
    assert all(item["id"] == "systemd:" + item["name"] for item in value["tasks"])
    assert all(item["configured"] is True for item in value["tasks"])
    assert all(item["enabled"] == "enabled" and item["active"] == "active" for item in value["tasks"])


def test_inventory_stops_starting_probes_after_cancellation(tmp_path: Path) -> None:
    _runtime, profile = _generated_profile(tmp_path, markets=["us"])
    calls: list[list[str]] = []
    cancellation_checks = 0

    def cancelled() -> bool:
        nonlocal cancellation_checks
        cancellation_checks += 1
        return cancellation_checks > 1

    def run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="active\n", stderr="")

    value = _us_inventory(profile, cancelled=cancelled, run_cmd=run_cmd)

    assert len(calls) == 1
    assert value["availability"] == "partial"
    assert "enabled_query_cancelled" in value["reasons"]
    assert any("active_query_cancelled" in item["reasons"] for item in value["tasks"])


def test_inventory_starts_no_probes_after_shared_deadline(tmp_path: Path) -> None:
    _runtime, profile = _generated_profile(tmp_path, markets=["us"])

    value = _us_inventory(
        profile,
        deadline_monotonic=5.0,
        monotonic=lambda: 55.0,
        run_cmd=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("expired queries must not start service probes")
        ),
    )

    assert value["availability"] == "partial"
    assert "active_query_deadline_exceeded" in value["reasons"]
    assert "enabled_query_deadline_exceeded" in value["reasons"]


def test_inventory_preserves_systemd_unknown_reasons_without_raw_output(tmp_path: Path) -> None:
    _runtime, profile = _generated_profile(tmp_path, markets=["us"])

    def run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        if command[1] == "is-active":
            return subprocess.CompletedProcess(command, 3, stdout="failed\n", stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="masked\n", stderr="")

    value = _us_inventory(profile, run_cmd=run_cmd)

    assert value["availability"] == "partial"
    for task in value["tasks"]:
        assert task["active"] == "unknown"
        assert task["enabled"] == "unknown"
        assert task["reasons"] == ["active_failed", "enabled_masked"]
        assert "command" not in task and "stdout" not in task and "stderr" not in task


def test_inventory_rejects_malformed_profile_services_as_unavailable() -> None:
    from src.application.service_deploy import scheduled_tasks_from_profile

    value = scheduled_tasks_from_profile(
        {"service_provider": "systemd", "markets": ["us"], "services": {}},
        market="us",
        run_cmd=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("malformed profiles must not start service probes")
        ),
    )

    assert value == {
        "tasks": [],
        "coverage": "unavailable",
        "availability": "unavailable",
        "reasons": ["profile_services_invalid"],
    }


def test_shared_tasks_excluded_by_single_market_scope_make_coverage_partial(
    tmp_path: Path,
) -> None:
    _runtime, profile = _generated_profile(tmp_path, markets=["us", "hk"])

    value = _us_inventory(profile, run_cmd=_state_run_cmd)

    assert value["coverage"] == "partial"
    assert value["availability"] == "partial"
    assert value["reasons"] == ["shared_scope_excluded"]
    assert [item["name"] for item in value["tasks"]] == [
        "options-monitor-auto-close-us.timer",
        "options-monitor-tick-us.timer",
    ]


def test_shared_tasks_require_all_profile_accounts_even_for_one_market(
    tmp_path: Path,
) -> None:
    _runtime, profile = _generated_profile(tmp_path, markets=["us"], accounts=["lx", "sy"])

    value = _us_inventory(profile, run_cmd=_state_run_cmd)

    assert value["coverage"] == "partial"
    assert value["reasons"] == ["shared_scope_excluded"]
    assert [item["name"] for item in value["tasks"]] == [
        "options-monitor-auto-close-us.timer",
        "options-monitor-tick-us.timer",
    ]


@pytest.mark.parametrize(
    "declared_markets",
    [
        ["us", "cn"],
        ["us", ""],
        ["us", None],
        ["us", {}],
        ["us", "US"],
        [],
        {"us": True},
    ],
)
def test_invalid_profile_market_scope_never_authorizes_shared_tasks(
    tmp_path: Path,
    declared_markets: object,
) -> None:
    _runtime, profile = _generated_profile(tmp_path, markets=["us"])
    profile["markets"] = declared_markets
    calls: list[list[str]] = []

    def run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return _state_run_cmd(command, **kwargs)

    value = _us_inventory(profile, run_cmd=run_cmd)

    assert value["coverage"] == "partial"
    assert value["availability"] == "partial"
    assert "profile_market_scope_invalid" in value["reasons"]
    assert [item["name"] for item in value["tasks"]] == [
        "options-monitor-auto-close-us.timer",
        "options-monitor-tick-us.timer",
    ]
    assert calls
    assert all(command[-1] in {item["name"] for item in value["tasks"]} for command in calls)


def test_tool_checks_profile_binding_and_propagates_query_context(monkeypatch, tmp_path: Path) -> None:
    import src.application.agent_tools.scheduled_tasks_impl as impl

    runtime, profile = _generated_profile(tmp_path, markets=["us"])
    config = _write_tool_runtime(runtime, profile, accounts=["lx"])
    captured = {}

    def inventory(_profile, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {"tasks": [], "coverage": "complete", "availability": "available", "reasons": []}

    monkeypatch.setattr(impl, "scheduled_tasks_from_profile", inventory)
    cancelled = lambda: False
    with impl.scheduled_tasks_query_context(deadline_monotonic=123.0, cancelled=cancelled):
        value, warnings, _meta = impl.run_scheduled_tasks_tool({"config_path": str(config)})

    assert value["scope"] == {
        "market": "us",
        "granularity": "market_deployment",
        "inventory_source": "service_profile",
        "coverage": "complete",
        "reasons": [],
    }
    assert value["availability"] == "available"
    assert value["count"] == 0
    assert value["coverage"]["status"] == "complete"
    assert value["coverage"]["total_count"] == 0
    assert value["freshness"] == {"status": "current", "as_of": value["observed_at"]}
    assert warnings == []
    assert captured["deadline_monotonic"] == 123.0
    assert captured["cancelled"] is cancelled
    assert captured["authorized_accounts"] == ["lx"]
    assert set(value) == {
        "schema_version",
        "scope",
        "tasks",
        "count",
        "observed_at",
        "coverage",
        "freshness",
        "availability",
        "reasons",
    }


def test_tool_excludes_shared_tasks_when_config_authorizes_account_subset(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.agent_tools.scheduled_tasks_impl as impl
    from src.application.service_deploy import scheduled_tasks_from_profile

    runtime, profile = _generated_profile(tmp_path, markets=["us"], accounts=["lx", "sy"])
    config = _write_tool_runtime(runtime, profile, accounts=["lx"])

    def inventory(bound_profile, **kwargs):  # type: ignore[no-untyped-def]
        return scheduled_tasks_from_profile(
            bound_profile,
            run_cmd=_state_run_cmd,
            **kwargs,
        )

    monkeypatch.setattr(impl, "scheduled_tasks_from_profile", inventory)

    value, warnings, _meta = impl.run_scheduled_tasks_tool(
        {"config_path": str(config)}
    )

    assert value["scope"]["coverage"] == "partial"
    assert warnings == ["shared_scope_excluded"]
    assert [item["name"] for item in value["tasks"]] == [
        "options-monitor-auto-close-us.timer",
        "options-monitor-tick-us.timer",
    ]


def test_tool_rejects_mismatched_profile_binding_without_exposing_paths(tmp_path: Path) -> None:
    from src.application.agent_tools.scheduled_tasks_impl import run_scheduled_tasks_tool

    runtime, profile = _generated_profile(tmp_path, markets=["us"])
    config = runtime / "config.us.json"
    config.write_text(
        json.dumps({"_generated": {"market": "us", "source_format": "yaml"}, "portfolio": {}, "symbols": []}),
        encoding="utf-8",
    )
    profile["config_paths"]["us"] = str(runtime / "other.json")
    (runtime / "service.profile.json").write_text(json.dumps(profile), encoding="utf-8")

    value, warnings, _meta = run_scheduled_tasks_tool({"config_path": str(config)})

    assert value["availability"] == "unavailable"
    assert value["scope"]["coverage"] == "unavailable"
    assert value["reasons"] == ["profile_config_mismatch"]
    assert warnings == ["profile_config_mismatch"]
    assert str(tmp_path) not in json.dumps(value)
