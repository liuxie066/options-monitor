from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


def test_no_account_notification_perception_does_not_resolve_delivery_route(monkeypatch, tmp_path: Path) -> None:
    import src.application.tick_notification_flow as mod

    audits: list[tuple[str, str, dict]] = []
    completions: list[dict] = []

    monkeypatch.setattr(
        mod,
        "_prepare_daily_brief_notification",
        lambda _request: mod.DailyBriefNotificationPreparation(
            prepared_messages=SimpleNamespace(
                messages_by_account={},
                threshold_met=False,
                used_heartbeat=False,
                heartbeat_accounts=(),
            ),
            lifecycles_by_account={},
            delivery_keys_by_account={},
            markets=("US",),
        ),
    )
    monkeypatch.setattr(
        mod,
        "resolve_notification_delivery_route",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("delivery route must not be resolved")),
    )
    monkeypatch.setattr(mod, "finalize_no_account_notification", lambda **_kwargs: 0)

    request = mod.TickNotificationRequest(
        base=tmp_path,
        cfg_path=tmp_path / "config.us.json",
        state_path=tmp_path / "state.json",
        scheduler_schedule_key="us",
        base_cfg={"notifications": {"provider": "feishu_app", "target": "https://example.invalid/webhook/token"}},
        run_id="run_no_account",
        runlog=SimpleNamespace(safe_event=lambda *_args, **_kwargs: None),
        results=[],
        tick_metrics={},
        no_send=True,
        bj_tz=ZoneInfo("Asia/Shanghai"),
        audit_helper=SimpleNamespace(
            audit=lambda event_type, action, **kwargs: audits.append((event_type, action, kwargs)),
            guard_mark_success=lambda: None,
        ),
        vpy=Path("python3"),
        complete_tick_idempotency_fn=lambda **kwargs: completions.append(dict(kwargs)),
        markets_to_run=("US",),
        scheduler_markets=("US",),
        trigger_kind="scheduled",
    )

    assert mod.run_tick_notification_flow(request) == 0
    perception_actions = [action for event_type, action, _kwargs in audits if event_type == "assistant_perception"]
    assert perception_actions == ["notification_prepared", "no_account_notification"]
    assert completions == [{"status": "skipped", "message": "no_daily_brief_delivery"}]
