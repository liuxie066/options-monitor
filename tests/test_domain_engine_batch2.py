from __future__ import annotations


def test_build_scheduler_decision_dto_uses_normalized_payload() -> None:
    from domain.domain.engine import build_scheduler_decision_dto

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
    from domain.domain.engine import build_scheduler_decision_dto

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


def test_build_scheduler_decision_dto_default_normalizer_supports_legacy_notify_alias() -> None:
    from domain.domain.engine import build_scheduler_decision_dto

    out = build_scheduler_decision_dto(
        {'should_run_scan': 1, 'should_notify': 0, 'reason': 'legacy-alias'},
    )

    assert out['schema_kind'] == 'scheduler_decision'
    assert out['schema_version'] == '1.0'
    assert out['should_run_scan'] is True
    assert out['is_notify_window_open'] is False
    assert out['should_notify'] == 0
    assert out['reason'] == 'legacy-alias'


def test_decide_notify_window_open_prefers_account_payload() -> None:
    from domain.domain.engine import AccountSchedulerDecisionView, decide_notify_window_open

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
    assert (
        decide_notify_window_open(
            scheduler_decision={'is_notify_window_open': False},
            account_scheduler_decision=AccountSchedulerDecisionView(is_notify_window_open=True),
        )
        is True
    )


def test_scheduler_decision_view_from_payload_enforces_fields() -> None:
    from domain.domain.engine import SchedulerDecisionView

    view = SchedulerDecisionView.from_payload(
        {
            'should_run_scan': 1,
            'should_notify': 0,
            'reason': None,
        }
    )
    assert view.should_run_scan is True
    assert view.is_notify_window_open is False
    assert view.reason == ''


def test_build_account_scheduler_decision_dto_uses_global_fallback() -> None:
    from domain.domain.engine import build_account_scheduler_decision_dto

    out = build_account_scheduler_decision_dto(
        None,
        scheduler_decision={'is_notify_window_open': False, 'should_notify': True},
    )
    assert out['schema_kind'] == 'scheduler_decision_account'
    assert out['schema_version'] == '1.0'
    assert out['is_notify_window_open'] is False

    out2 = build_account_scheduler_decision_dto(
        {'should_notify': True},
        scheduler_decision={'is_notify_window_open': False, 'should_notify': False},
    )
    assert out2['is_notify_window_open'] is True

    out3 = build_account_scheduler_decision_dto(
        True,
        scheduler_decision={'is_notify_window_open': False},
    )
    assert out3['is_notify_window_open'] is True


def test_decide_account_notify_window_open_uses_explicit_account_dto() -> None:
    from domain.domain.engine import (
        build_account_scheduler_decision_dto,
        decide_account_notify_window_open,
    )

    account_dto = build_account_scheduler_decision_dto(
        {'is_notify_window_open': True},
        scheduler_decision={'is_notify_window_open': False},
    )
    assert (
        decide_account_notify_window_open(
            scheduler_decision={'is_notify_window_open': False},
            account_scheduler_decision=account_dto,
        )
        is True
    )


def test_decide_notification_meaningful_keeps_existing_predicate() -> None:
    from domain.domain.engine import decide_notification_meaningful

    assert decide_notification_meaningful('hello') is True
    assert decide_notification_meaningful('') is False
    assert decide_notification_meaningful('今日无需要主动提醒的内容。') is False


def test_resolve_scheduler_decision_centralizes_legacy_alias_reads() -> None:
    from domain.domain.engine import resolve_scheduler_decision

    dto, view = resolve_scheduler_decision(
        {'should_run_scan': 1, 'should_notify': 0, 'reason': 'compat-alias'}
    )

    assert dto['schema_kind'] == 'scheduler_decision'
    assert dto['schema_version'] == '1.0'
    assert dto['is_notify_window_open'] is False
    assert view.should_run_scan is True
    assert view.is_notify_window_open is False
    assert view.reason == 'compat-alias'
