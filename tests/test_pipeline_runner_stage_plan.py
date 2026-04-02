"""Regression: stage plan semantics for --stage/--stage-only."""

from __future__ import annotations


def test_stage_plan_fetch_only() -> None:
    from scripts.pipeline_runner import build_stage_plan

    plan = build_stage_plan(stage='fetch', stage_only=None)
    assert plan.want('fetch') is True
    assert plan.want('scan') is False
    assert plan.want('notify') is False


def test_stage_plan_scan_includes_fetch() -> None:
    from scripts.pipeline_runner import build_stage_plan

    plan = build_stage_plan(stage='scan', stage_only=None)
    assert plan.want('fetch') is True
    assert plan.want('scan') is True
    assert plan.want('alert') is False


def test_stage_plan_stage_only_notify() -> None:
    from scripts.pipeline_runner import build_stage_plan

    plan = build_stage_plan(stage='all', stage_only='notify')
    assert plan.want('fetch') is False
    assert plan.want('notify') is True
