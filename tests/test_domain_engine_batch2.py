from __future__ import annotations


def test_build_scheduler_decision_dto_uses_normalized_payload() -> None:
    from om.domain.engine import build_scheduler_decision_dto

    raw = {'should_run_scan': False}

    out = build_scheduler_decision_dto(
        raw,
        normalize_fn=lambda payload: {
            'schema_kind': 'scheduler_decision',
            'schema_version': '1.0',
            'should_run_scan': True,
            'is_notify_window_open': False,
            'reason': 'normalized',
            'x': payload.get('should_run_scan'),
        },
    )

    assert out['should_run_scan'] is True
    assert out['reason'] == 'normalized'
    assert out['x'] is False


def test_build_scheduler_decision_dto_fallback_keeps_legacy_shape() -> None:
    from om.domain.engine import build_scheduler_decision_dto

    out = build_scheduler_decision_dto(
        {'should_run_scan': 1, 'should_notify': 0, 'reason': None, 'extra': 'v'},
        normalize_fn=lambda _: (_ for _ in ()).throw(ValueError('bad')),
    )

    assert out['schema_kind'] == 'scheduler_decision'
    assert out['schema_version'] == '1.0'
    assert out['should_run_scan'] == 1
    assert out['is_notify_window_open'] is False
    assert out['reason'] is None
    assert out['extra'] == 'v'


def test_decide_notify_window_open_prefers_account_payload() -> None:
    from om.domain.engine import decide_notify_window_open

    assert (
        decide_notify_window_open(
            scheduler_decision={'is_notify_window_open': False},
            account_scheduler_decision={'should_notify': True},
        )
        is True
    )
    assert (
        decide_notify_window_open(
            scheduler_decision={'is_notify_window_open': True},
            account_scheduler_decision=None,
        )
        is True
    )


def test_decide_notification_meaningful_keeps_existing_predicate() -> None:
    from om.domain.engine import decide_notification_meaningful

    assert decide_notification_meaningful('hello') is True
    assert decide_notification_meaningful('') is False
    assert decide_notification_meaningful('今日无需要主动提醒的内容。') is False
