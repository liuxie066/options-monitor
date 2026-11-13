from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from domain.domain.daily_decision_brief import (
    decide_daily_brief_notification,
    daily_brief_roll_notification_identities,
    daily_brief_position_roll_identity,
    diff_daily_decision_briefs,
)
from domain.domain.combo_candidate_evidence import (
    combo_candidate_identities_for_rendered_rows,
    combo_exposure_render_context,
    derive_combo_candidate_exposures,
)
from domain.domain.multi_tick import FEISHU_APP_NOTIFICATION_PROVIDER, evaluate_dnd_quiet_hours
from src.application.channels.feishu_notification_renderer import (
    feishu_notification_blocks_fit,
    feishu_notification_visible_markdown,
    render_feishu_notification_card,
)
from domain.domain.engine import (
    build_failure_audit_fields,
    decide_notification_delivery,
)
from domain.storage.repositories import state_repo
from src.application.cron_runtime import apply_notify_results_to_tick_metrics, build_notify_summary
from src.application.daily_decision_brief_renderer import (
    build_attribution_reminder_context,
    render_candidate_alert,
    render_candidate_alert_card_blocks,
    render_candidate_alert_text,
    render_fixed_failure,
    render_fixed_failure_text,
    render_fixed_report,
    render_fixed_report_card_blocks,
    render_fixed_report_text,
    resolve_daily_brief_render_limits,
    select_daily_brief_notification,
    select_rendered_combo_candidate_rows,
    select_rendered_position_roll_identities,
)
from src.application.daily_decision_brief_repository import (
    DailyBriefReadScope,
    DailyDecisionBriefStateError,
    begin_daily_decision_brief_recovery_attempt,
    bind_daily_decision_brief_recovery_report,
    classify_retryable_daily_decision_brief_payload,
    confirm_daily_decision_brief_delivery_v2,
    prepare_daily_decision_brief_delivery,
    list_daily_decision_brief_revisions,
    read_daily_decision_brief,
    read_daily_decision_brief_delivery_state,
    read_confirmed_attribution_render_context,
    read_daily_decision_brief_fixed_recovery,
    read_daily_decision_brief_recovery_report,
    read_daily_decision_brief_recovery_source,
    expire_daily_decision_brief_recovery_reports,
    read_latest_daily_decision_brief,
    read_retryable_daily_decision_brief_delivery,
    record_daily_decision_brief_candidates,
    record_daily_decision_brief_delivery_attempt,
    record_daily_decision_brief_fixed_recovery,
    read_daily_brief_fanout_children,
    read_confirmed_fanout_attribution_context,
    read_daily_brief_fanout_recovery_source,
    record_daily_brief_fanout_plan,
    list_pending_legacy_daily_brief_deliveries,
)
from src.application.tick_brief_preparation import TickBriefPreparation
from src.infrastructure.futu_gateway import check_fetch_control, wait_fetch_control
from src.application.multi_tick.misc import _safe_runlog_data, parse_hhmm
from src.application.multi_tick.bot_perception_event import build_notification_perception_event
from src.application.multi_tick_audit import daily_brief_timing_scope, record_tick_latency
from src.application.multi_tick_finalization import _record_finalize_degraded
from src.application.notification_delivery_route import notifications_enabled, resolve_notification_delivery_route, notification_reminder_route_key, resolve_notification_delivery_routes
from src.application.notification_fanout import (
    FanoutRetryPolicy, prepare_notification_fanout, read_notification_fanout,
    list_notification_fanouts, notification_fanout_id,
)
from src.application.notification_fanout_delivery import uses_proactive_fanout
from src.application.notification_delivery_adapter import (
    build_notification_transport_key,
    normalize_notification_delivery_result,
    notification_target_reference,
    resolve_feishu_bot_send_target,
    select_notification_delivery_adapter,
)
from src.application.scheduled_notification import (
    PreparedPerAccountMessages,
    build_per_account_delivery_batch,
    execute_per_account_delivery,
    dispatch_scheduled_notification_fanout,
)
from src.application.system_alerts import report_system_meta_signal
from src.infrastructure.io_utils import utc_now
from src.application.scan_scheduler import daily_brief_recovery_cutoff


@dataclass(frozen=True)
class TickNotificationRequest:
    base: Path
    cfg_path: Path
    state_path: Path
    scheduler_schedule_key: str
    base_cfg: dict[str, Any]
    run_id: str
    runlog: Any
    results: list[Any]
    tick_metrics: dict[str, Any]
    no_send: bool
    bj_tz: ZoneInfo
    audit_helper: Any
    vpy: Path
    repo_root: Path | None = None
    markets_to_run: tuple[str, ...] | list[str] = ()
    scheduler_markets: tuple[str, ...] | list[str] = ()
    scheduler_decision: dict[str, Any] | None = None
    ran_pipeline_accounts: tuple[str, ...] | list[str] = ()
    account_ids: tuple[str, ...] | list[str] = ()
    scheduler_decisions_by_account: dict[str, dict[str, Any]] | None = None
    scheduled_scan_targets_by_account: dict[str, str | None] | None = None
    delivery_only: bool = False
    trigger_kind: str = "manual"
    preparation: DailyBriefNotificationPreparation | None = None
    deadline: float | None = None
    cancelled: Callable[[], bool] | None = None


@dataclass(frozen=True)
class DailyBriefNotificationPreparation:
    prepared_messages: Any
    lifecycles_by_account: dict[str, dict[str, Any]]
    delivery_keys_by_account: dict[str, str]
    markets: tuple[str, ...]
    transport_envelopes_by_account: dict[str, dict[str, Any]] = field(default_factory=dict)
    multi_market_delivery_unsupported: bool = False
    blocked_error_code: str | None = None
    fanout_ids: tuple[str, ...] = ()
    legacy_deliveries: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class TickNotificationOutcome:
    sent_accounts: tuple[str, ...] = ()
    would_send_accounts: tuple[str, ...] = ()
    notify_failures: tuple[dict[str, Any], ...] = ()
    notify_summary: dict[str, Any] = field(default_factory=dict)
    channel: str | None = None
    target: str | None = None
    skip_reason: str | None = None
    error_code: str | None = None
    failure_source_confirmed_accounts: tuple[str, ...] = ()


def _fanout_now_ms() -> int:
    value = utc_now()
    now = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(now.timestamp() * 1000)


def _daily_brief_day_fanouts(request, account, market, market_date):
    return list_notification_fanouts(base=request.base, account=account,
        logical_key_prefix=f"daily-brief:{market}:{market_date}:")


def _daily_brief_recovery_send_deadline(request, envelope):
    deadline = request.deadline
    if envelope.get("delivery_kind") != "fixed_recovery_report" or envelope.get("status") == "ambiguous":
        return deadline
    now = datetime.fromisoformat(str(utc_now()).replace("Z", "+00:00"))
    remaining = (datetime.fromisoformat(envelope["cutoff_utc"]) - now).total_seconds()
    cutoff_deadline = monotonic() + max(0.0, remaining)
    return cutoff_deadline if deadline is None else min(deadline, cutoff_deadline)


def _wait_daily_brief_delivery(request, seconds, *, deadline):
    # A local expiry ends this account's next attempt, not the remaining batch.
    if deadline is not None:
        seconds = min(seconds, max(0.0, deadline - monotonic()))
    wait_fetch_control(seconds, deadline=request.deadline, cancelled=request.cancelled)


def _eligible_daily_brief_recovery(day, now):
    recoveries = day.get("recovery_reports", {}).values()
    for status in ("ambiguous", "pending"):
        for envelope in recoveries:
            if envelope.get("status") == status and (
                status == "ambiguous" or now < datetime.fromisoformat(envelope["cutoff_utc"])
            ):
                return envelope
    return None


def has_daily_brief_recovery_to_resume(*, base, config, account, market, scheduler):
    """Read existing frozen obligations without binding or admitting new work."""
    if not notifications_enabled(config):
        return False
    market_date = _daily_brief_market_date(scheduler)
    if not market_date:
        return False
    now = datetime.fromisoformat(str(utc_now()).replace("Z", "+00:00"))
    state = read_daily_decision_brief_delivery_state(base=base, account=account, market=market)
    if state.get("reason") == "state_invalid":
        raise DailyDecisionBriefStateError("legacy daily brief delivery state invalid")
    day = (state.get("state") or {}).get("days", {}).get(market_date, {})
    if _eligible_daily_brief_recovery(day, now) is not None:
        return True
    for target, failure in day.get("fixed_reports", {}).items():
        if (failure.get("delivery_kind") != "fixed_failure" or failure.get("status") != "confirmed"
                or target in day.get("recovery_reports", {})):
            continue
        source = read_daily_decision_brief_recovery_source(
            base=base, account=account, market=market, market_trading_date=market_date,
            scheduled_target_market=target, original_failure_key=failure["delivery_key"],
            original_failure_digest=failure["source_digest"],
        )
        if source.get("available") and now < datetime.fromisoformat(source["recovery_intent"]["cutoff_utc"]):
            return True
    if uses_proactive_fanout(config):
        children = read_daily_brief_fanout_children(
            base=base, account=account, market=market, market_trading_date=market_date,
        )
        if any(child["spec"]["metadata"].get("delivery_kind") == "fixed_recovery_report"
               and child["status"] in {"pending", "explicit_failed", "send_started"}
               and now < datetime.fromisoformat(child["spec"]["metadata"]["cutoff_utc"])
               for child in children):
            return True
        return next(_daily_brief_fanout_recovery_sources(
            base, account, market, market_date, int(now.timestamp() * 1000)), None) is not None
    return False


def _daily_brief_represented_identities(metadata: Mapping[str, Any]) -> set[str]:
    evidence = metadata.get("display_evidence")
    if evidence is None:
        return set(metadata.get("candidate_identities", []))
    return {row["identity"] for row in evidence
            if row["state"] == "expanded"
            or (row["state"] == "summary_notified" and not row["identity"].startswith("position-roll:"))}


def _daily_brief_deliverable_candidates(
    brief: Mapping[str, Any], identities: Any, *, now_utc: datetime | str | None = None,
) -> list[str]:
    """Drop roll identities whose advice window has closed at delivery time.

    Pending candidates are recorded against the brief's generated_at, but a tick
    can reach delivery minutes later. An expired roll is a normal state (do not
    send it), never a broken identity.
    """
    ordered = list(identities or [])
    rolls = daily_brief_roll_notification_identities(brief)
    if not rolls:
        return ordered
    current = daily_brief_roll_notification_identities(
        brief, now_utc=now_utc or utc_now())
    return [identity for identity in ordered if identity not in rolls or identity in current]


def _record_expired_roll_candidates(
    request, *, brief: Mapping[str, Any], account: str, market: str,
    requested: list[str], delivered: list[str],
) -> None:
    """Leave an audit trail for roll candidates dropped by the closed window."""
    kept = set(delivered)
    dropped = [identity for identity in requested if identity not in kept]
    if not dropped:
        return
    windows: dict[str, Any] = {}
    for row in brief.get("positions") or []:
        if not isinstance(row, Mapping):
            continue
        identity = daily_brief_position_roll_identity(
            row, account=brief.get("account"), market=brief.get("market"))
        if identity:
            roll = row.get("roll_advice")
            windows[identity] = (
                roll.get("actionable_until_utc") if isinstance(roll, Mapping) else None
            )
    try:
        request.runlog.safe_event(
            "daily_brief", "roll_window_expired",
            message=f"{len(dropped)} roll candidate(s) expired before delivery",
            data={
                "account": account,
                "market": market,
                "run_id": brief.get("run_id"),
                "expired": [
                    {"identity": identity, "actionable_until_utc": windows.get(identity)}
                    for identity in dropped
                ],
            },
        )
    except Exception:
        pass


def _select_daily_brief_delivery(
    brief, *, kind, identities, diff, limits, context, card, open_id, preserve_context=False,
):
    now = utc_now()
    selected_identities = list(identities)
    if kind == "candidate_alert":
        selected_identities = _daily_brief_deliverable_candidates(
            brief, selected_identities, now_utc=now)
        if not selected_identities:
            return "", None, []
    render_brief = _daily_brief_roll_render_source(
        brief, selected_identities, now_utc=now)
    if not preserve_context:
        context.update(_daily_brief_candidate_render_context(
            render_brief, delivery_kind="candidate_alert" if kind == "candidate_alert" else "fixed_report",
            candidate_identities=selected_identities, diff=diff, limits=limits, context=context,
        ))
    selection = select_daily_brief_notification(
        render_brief, delivery_kind="candidate_alert" if kind == "candidate_alert" else "fixed_report",
        candidate_identities=selected_identities, diff=diff, limits=limits, context=context,
        fits=(lambda blocks, text: feishu_notification_blocks_fit(blocks, text, open_id=open_id))
        if card else None,
    )
    states = {row["identity"]: row["state"] for row in selection["display_evidence"]}
    selection["display_evidence"] = [
        {"identity": identity, "state": states.get(identity, "not_shown")}
        for identity in selected_identities]
    blocks, message = selection["markdown_blocks"], selection["text"]
    transport = render_feishu_notification_card(
        markdown="\n\n".join(blocks), markdown_blocks=blocks, fallback_text=message,
        fallback_open_id=open_id, preserve_blocks=True,
    ) if card else None
    return message, transport, selection["display_evidence"]


def _daily_brief_fanout_spec(request, route, brief, *, kind, identities, diff, scheduler,
                             limits, source=None, cutoff=None, original_target=None, recovery_intent=None):
    context = (dict(recovery_intent["render_context"]) if recovery_intent is not None
        else _daily_brief_render_context(request, scheduler_decision=scheduler))
    if original_target:
        context.update(scheduled_target_market=original_target, recovery_original_target=original_target,
            recovery_scan_at_utc=brief["generated_at_utc"])
    if kind != "fixed_failure":
        previous = (recovery_intent["previous_attribution_context"] if recovery_intent is not None
            else read_confirmed_fanout_attribution_context(base=request.base, account=brief["account"],
                market=brief["market"], identity_key=route["identity_key"]))
        context.update(build_attribution_reminder_context(brief, previous_context=previous))
        context["attribution_route_key"] = route["identity_key"]
    card = route["provider"] == FEISHU_APP_NOTIFICATION_PROVIDER
    if kind == "fixed_failure":
        message = (render_fixed_failure if card else render_fixed_failure_text)(brief, context=context)
        envelope = render_feishu_notification_card(markdown=message, markdown_blocks=[message],
            fallback_text=message, fallback_open_id=route["target"]) if card else None
        display_evidence = []
    else:
        message, envelope, display_evidence = _select_daily_brief_delivery(
            brief, kind=kind, identities=identities, diff=diff, limits=limits,
            context=context, card=card, open_id=route["target"],
        )
    # Secrets stay in the transport's credential owner. Only binding locators
    # and provider selection are frozen alongside the final rendered envelope.
    options = {key: value for key, value in route["notifications"].items()
        if key in {"enabled", "provider", "channel", "target", "_explicit_route_target",
                   "wechat_clawbot_label", "wechat_clawbot_state_dir"}}
    return {"recipient": {**{key: route[key] for key in
        ("route_id", "provider", "channel", "target", "identity_key")}, "transport_options": options},
        "message": message, "transport_envelope": envelope,
        "metadata": {"account": brief["account"], "market": brief["market"], "market_trading_date": brief["market_trading_date"],
            "run_id": request.run_id, "delivery_kind": kind,
            "scheduled_target_market": original_target or scheduler.get("scheduled_target_market"),
            "candidate_identities": identities, "display_evidence": display_evidence,
            "render_context": context,
            "revision": (source or {}).get("current_revision"),
            "source_digest": (source or {}).get("current_brief_digest"),
            "source_reference": (source or {}).get("source_reference"),
            "generated_at_utc": brief["generated_at_utc"], "cutoff_utc": cutoff}}


def _prepare_daily_brief_fanout(request, account, market, scheduler, brief, persisted, diff, limits, reliable):
    market_date = str(brief["market_trading_date"])
    fixed_target = str(scheduler.get("scheduled_target_market") or "")
    if scheduler.get("in_run_window") is not True or not notifications_enabled(request.base_cfg):
        return
    prefix = f"daily-brief:{market}:{market_date}:"
    key = prefix + ("fixed:" + fixed_target if fixed_target else "candidate:" + str(brief["revision"]))
    existing = read_notification_fanout(base=request.base,
        fanout_id=notification_fanout_id(account=account, logical_key=key))
    if existing is not None:
        return  # The old recipient set and content survive configuration changes.
    legacy = read_daily_decision_brief_delivery_state(base=request.base, account=account, market=market)
    if legacy.get("reason") == "state_invalid":
        raise ValueError("daily_brief_legacy_delivery_state_invalid")
    day = (legacy.get("state") or {}).get("days", {}).get(market_date, {})
    if fixed_target and fixed_target in day.get("fixed_reports", {}):
        return  # Introducing routes never backfills an existing legacy report.
    if not fixed_target:
        _prepare_daily_brief_fanout_recoveries(request, account, market, market_date, scheduler, limits)
    if not reliable and not fixed_target:
        return
    routes = resolve_notification_delivery_routes(config=request.base_cfg, account=account,
        base=(request.repo_root or request.base).resolve())
    children = read_daily_brief_fanout_children(base=request.base, account=account, market=market,
        market_trading_date=market_date)
    deliveries = []
    legacy_owned = set(day.get("alerted_candidates", {}))
    for envelope in [*day.get("fixed_reports", {}).values(), *day.get("recovery_reports", {}).values(),
                     *([day["candidate_delivery"]] if day.get("candidate_delivery") else [])]:
        legacy_owned.update(_daily_brief_represented_identities(envelope))
    for route in routes:
        identities = list((persisted or {}).get("current_candidate_identities", []))
        kind = "fixed_report" if reliable else "fixed_failure"
        if not fixed_target:
            # Existing obligations already own these candidates, including
            # unknown siblings: a new scan cannot recreate their send effect.
            owned = set(legacy_owned)
            for child in children:
                if child["identity_key"] == route["identity_key"]:
                    owned.update(_daily_brief_represented_identities(child["spec"]["metadata"]))
            identities = [identity for identity in identities if identity not in owned]
            requested_identities = list(identities)
            identities = _daily_brief_deliverable_candidates(brief, identities)
            _record_expired_roll_candidates(
                request, brief=brief, account=account, market=market,
                requested=requested_identities, delivered=identities,
            )
            if not identities:
                continue
            kind = "candidate_alert"
        spec = _daily_brief_fanout_spec(request, route, brief, kind=kind,
            identities=identities, diff=diff, scheduler=scheduler, limits=limits, source=persisted)
        if kind != "candidate_alert" or _daily_brief_represented_identities(spec["metadata"]):
            deliveries.append(spec)
    if deliveries:
        prepare_notification_fanout(base=request.base, account=account, logical_key=key,
            deliveries=deliveries, retry_policy=FanoutRetryPolicy(2, (1000,)), now_ms=_fanout_now_ms())


def _daily_brief_fanout_recovery_intent(request, account, market, scheduler, brief, limits):
    """Bind eligible scan provenance in the successful history transaction."""
    from src.application.receipt_query import receipt_digest

    generated = datetime.fromisoformat(brief["generated_at_utc"].replace("Z", "+00:00"))
    now = datetime.fromtimestamp(_fanout_now_ms() / 1000, timezone.utc)
    recoveries = []
    for child in read_daily_brief_fanout_children(base=request.base, account=account, market=market,
            market_trading_date=brief["market_trading_date"]):
        meta = child["spec"]["metadata"]
        if meta["delivery_kind"] != "fixed_failure" or not child["delivery_confirmed"]:
            continue
        target = meta["scheduled_target_market"]
        cutoff = daily_brief_recovery_cutoff(schedule_cfg=request.base_cfg.get(request.scheduler_schedule_key) or {},
            scheduled_target_market=target)
        after = max(datetime.fromisoformat(target),
            datetime.fromtimestamp(child["confirmed_at_ms"] / 1000, timezone.utc))
        if not after < generated <= now < datetime.fromisoformat(cutoff):
            continue
        recoveries.append({"original_failure_child_id": child["child_id"],
            "original_failure_spec_digest": receipt_digest(child["spec"]),
            "scheduled_target_market": target, "cutoff_utc": cutoff,
            "scheduled_scan_target_market": request.scheduled_scan_targets_by_account[account],
            "render_context": _daily_brief_render_context(request, scheduler_decision=scheduler),
            "render_limits": dict(limits),
            "previous_attribution_context": read_confirmed_fanout_attribution_context(
                base=request.base, account=account, market=market,
                identity_key=child["identity_key"])})
    return {"fanout_recoveries": recoveries} if recoveries else None


def _daily_brief_fanout_recovery_sources(base, account, market, market_date, now_ms):
    from src.application.receipt_query import receipt_digest

    for child in read_daily_brief_fanout_children(base=base, account=account, market=market,
            market_trading_date=market_date):
        meta = child["spec"]["metadata"]
        if meta["delivery_kind"] != "fixed_failure" or not child["delivery_confirmed"]:
            continue
        target = meta["scheduled_target_market"]
        key = f"daily-brief:{market}:{market_date}:recovery:{child['child_id']}"
        if read_notification_fanout(base=base,
                fanout_id=notification_fanout_id(account=account, logical_key=key)):
            continue
        after = max(datetime.fromisoformat(target),
            datetime.fromtimestamp(child["created_at_ms"] / 1000, timezone.utc))
        source = read_daily_brief_fanout_recovery_source(base=base, account=account, market=market,
            market_trading_date=market_date, after_utc=after.isoformat(),
            before_utc=datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(),
            original_failure_child_id=child["child_id"], original_failure_spec_digest=receipt_digest(child["spec"]))
        if source is None or datetime.fromisoformat(source["brief"]["generated_at_utc"]).timestamp() * 1000 > now_ms:
            continue
        if now_ms >= int(datetime.fromisoformat(source["recovery_intent"]["cutoff_utc"]).timestamp() * 1000):
            continue
        yield child, source


def _prepare_daily_brief_fanout_recoveries(request, account, market, market_date, scheduler, limits):
    if request.no_send or not notifications_enabled(request.base_cfg):
        return
    now_ms = _fanout_now_ms()
    for child, source in _daily_brief_fanout_recovery_sources(request.base, account, market, market_date, now_ms):
        intent = source["recovery_intent"]
        cutoff = intent["cutoff_utc"]
        target = child["spec"]["metadata"]["scheduled_target_market"]
        key = f"daily-brief:{market}:{market_date}:recovery:{child['child_id']}"
        recipient = child["spec"]["recipient"]
        route = {**recipient, "notifications": recipient["transport_options"]}
        spec = _daily_brief_fanout_spec(request, route, source["brief"], kind="fixed_recovery_report",
            identities=source["current_candidate_identities"], diff={}, scheduler=scheduler,
            limits=intent["render_limits"], source=source, cutoff=cutoff, original_target=target,
            recovery_intent=intent)
        prepare_notification_fanout(base=request.base, account=account, logical_key=key,
            deliveries=[spec], retry_policy=FanoutRetryPolicy(2, (1000,)), now_ms=now_ms)


def _continue_legacy_daily_brief_deliveries(request, deliveries):
    """Resume the original owner so transport keys, retry budget and facts stay intact."""
    from src.application.channels.plugins import channel_plugin_installed

    result = {"sent_accounts": [], "failures": [], "attempt_count": 0, "confirmed_count": 0, "blocked": []}
    dnd = evaluate_dnd_quiet_hours(
        quiet_hours=(request.base_cfg.get("notifications") or {}).get("quiet_hours_beijing"),
        no_send=request.no_send, now_bj_time=datetime.now(timezone.utc).astimezone(request.bj_tz).time(),
        parse_hhmm_fn=parse_hhmm)
    if request.no_send or dnd.get("is_quiet") or not notifications_enabled(request.base_cfg):
        return result
    for lifecycle in deliveries:
        account = lifecycle["account"]
        envelope = lifecycle["envelope"]
        if classify_retryable_daily_decision_brief_payload(base=request.base, account=account,
                market=lifecycle["market"], market_trading_date=lifecycle["market_trading_date"],
                envelope=envelope) != "clean":
            result["failures"].append({"account": account, "error_code": "legacy_ai_payload_retired"})
            continue
        identity = (envelope.get("render_context") or {}).get("attribution_route_key")
        routes = resolve_notification_delivery_routes(config=request.base_cfg, account=account,
            base=(request.repo_root or request.base).resolve())
        route = next((route for route in routes if identity and route["identity_key"] == identity), None)
        if route is None:
            result["blocked"].append({"account": account, "delivery_key": envelope["delivery_key"],
                "reason": "legacy_route_unproven" if not identity else "route_disabled_or_changed"})
            if not identity and routes:
                result["failures"].append({"account": account, "delivery_key": envelope["delivery_key"],
                    "error_code": "LEGACY_ROUTE_UNPROVEN"})
            continue
        if not channel_plugin_installed(route["provider"]):
            result["failures"].append({"account": account, "error_code": "CHANNEL_PLUGIN_NOT_INSTALLED"})
            continue
        adapter = select_notification_delivery_adapter(route["provider"])
        delivery_deadline = _daily_brief_recovery_send_deadline(request, envelope)
        common = {"base": request.base, "account": account, "market": lifecycle["market"],
            "market_trading_date": lifecycle["market_trading_date"], "delivery_key": envelope["delivery_key"],
            "source_digest": envelope["source_digest"], "message_sha256": envelope["message_sha256"],
            "transport_idempotency_key": build_notification_transport_key(envelope["delivery_key"])}

        def send(**kwargs):
            check_fetch_control(request.deadline, request.cancelled)
            if envelope["delivery_kind"] == "fixed_recovery_report":
                started = begin_daily_decision_brief_recovery_attempt(**common, attempted_at_utc=utc_now())
                if not started.get("started"):
                    raise RuntimeError(f"recovery delivery blocked: {started.get('reason')}")
            check_fetch_control(delivery_deadline, request.cancelled)
            try:
                sent = normalize_notification_delivery_result(
                    adapter.send_fn(**kwargs, notifications=route["notifications"], **(
                        {"deadline": delivery_deadline, "cancelled": request.cancelled}
                        if delivery_deadline is not None or request.cancelled is not None else {}
                    )), normalize_fn=adapter.normalize_fn)
            except Exception:
                if envelope["delivery_kind"] == "fixed_recovery_report":
                    record_daily_decision_brief_delivery_attempt(**common, attempted_at_utc=utc_now(), ambiguous=True)
                raise
            if sent.get("delivery_confirmed") and sent.get("attribution_route_key") != identity:
                sent = {**sent, "delivery_confirmed": False, "ambiguous_send": True}
            if envelope["delivery_kind"] == "fixed_recovery_report" and not sent.get("delivery_confirmed"):
                record_daily_decision_brief_delivery_attempt(**common, attempted_at_utc=utc_now(),
                    ambiguous=bool(sent.get("ambiguous_send") or sent.get("duplicate_risk") or sent.get("command_ok")))
            return sent

        _decision, batch, _reason = build_per_account_delivery_batch(account_messages={account: envelope["rendered_message"]},
            channel=route["channel"], target=route["target"])
        execution = execute_per_account_delivery(delivery_batch=batch, base=(request.repo_root or request.base).resolve(),
            run_id=request.run_id, runlog=request.runlog, audit_fn=request.audit_helper.audit,
            safe_data_fn=_safe_runlog_data, failure_fields_builder=build_failure_audit_fields,
            send_fn=send, normalize_fn=adapter.normalize_fn, failure_stage=adapter.failure_stage,
            idempotency_keys_by_account={account: envelope["delivery_key"]},
            transport_envelopes_by_account={account: envelope["rendered_transport"]}
                if envelope.get("rendered_transport") is not None else {},
            **({"sleep_fn": partial(_wait_daily_brief_delivery, request, deadline=delivery_deadline)}
               if delivery_deadline is not None or request.cancelled is not None else {}))
        preparation = DailyBriefNotificationPreparation(
            prepared_messages=PreparedPerAccountMessages({account: envelope["rendered_message"]}, True, False),
            lifecycles_by_account={account: lifecycle}, delivery_keys_by_account={account: envelope["delivery_key"]},
            markets=(lifecycle["market"],))
        confirmed, failures = _confirm_daily_brief_execution(
            request=request, preparation=preparation, send_results=execution.send_results,
        )
        result["sent_accounts"].extend(confirmed)
        result["confirmed_count"] += len(confirmed)
        result["failures"].extend(execution.notify_failures)
        result["failures"].extend(failures)
        result["attempt_count"] += sum(int(send.get("attempts", 0)) for send in execution.send_results)
    return result


def _deliver_daily_brief_fanouts(request, preparation) -> TickNotificationOutcome:
    from src.application.channels.plugins import channel_plugin_installed
    from src.application.notification_delivery_route import notification_delivery_route_is_current

    if preparation.multi_market_delivery_unsupported:
        _record_daily_brief_multi_market_failure(request, preparation)
        code = "daily_brief_multi_market_delivery_unsupported"
        return TickNotificationOutcome(skip_reason=code, error_code=code)
    blocked = []
    def allowed(spec):
        check_fetch_control(request.deadline, request.cancelled)
        if request.no_send:
            return False
        recipient = spec["recipient"]
        if (request.delivery_only and spec["metadata"].get("delivery_kind") != "fixed_recovery_report"
                and _daily_brief_scheduler_decision(request, spec["metadata"]["account"]).get("in_run_window") is not True):
            return False
        if not channel_plugin_installed(recipient["provider"]):
            blocked.append({"account": spec["metadata"]["account"],
                "route_id": recipient["route_id"], "error_code": "CHANNEL_PLUGIN_NOT_INSTALLED"})
            return False
        if not notification_delivery_route_is_current(config=request.base_cfg,
                account=spec["metadata"]["account"],
                recipient=recipient, base=(request.repo_root or request.base).resolve()):
            return False
        cutoff = spec["metadata"].get("cutoff_utc")
        if cutoff:
            if _fanout_now_ms() >= int(datetime.fromisoformat(cutoff).timestamp() * 1000):
                return False
        dnd = evaluate_dnd_quiet_hours(
            quiet_hours=(request.base_cfg.get("notifications") or {}).get("quiet_hours_beijing"),
            no_send=request.no_send,
            now_bj_time=datetime.now(timezone.utc).astimezone(request.bj_tz).time(), parse_hhmm_fn=parse_hhmm)
        return not dnd.get("is_quiet")

    def send(*, spec, idempotency_key):
        deadline = _daily_brief_recovery_send_deadline(request, spec["metadata"])
        check_fetch_control(deadline, request.cancelled)
        recipient = spec["recipient"]
        adapter = select_notification_delivery_adapter(recipient["provider"])
        kwargs = dict(base=(request.repo_root or request.base).resolve(),
            channel=recipient["channel"], target=recipient["target"], message=spec["message"],
            notifications=recipient["transport_options"], idempotency_key=idempotency_key)
        if spec["transport_envelope"] is not None:
            kwargs["transport_envelope"] = spec["transport_envelope"]
        if deadline is not None or request.cancelled is not None:
            kwargs.update(deadline=deadline, cancelled=request.cancelled)
        return normalize_notification_delivery_result(adapter.send_fn(**kwargs), normalize_fn=adapter.normalize_fn)

    summaries = []
    before = {}
    plans_by_account = {}
    for fanout_id in preparation.fanout_ids:
        snapshot = read_notification_fanout(base=request.base, fanout_id=fanout_id)
        before[fanout_id] = snapshot
        plans_by_account.setdefault(snapshot["account"], []).append(snapshot)
    for account, fanouts in plans_by_account.items():
        record_daily_brief_fanout_plan(base=request.base, account=account, market=preparation.markets[0],
            run_id=request.run_id, fanouts=fanouts)
    for fanout_id in preparation.fanout_ids:
        summaries.append(dispatch_scheduled_notification_fanout(base=request.base, fanout_id=fanout_id,
            config=request.base_cfg, send_fn=send, now_ms=_fanout_now_ms(), can_send_fn=allowed))
    for item in summaries:
        blocked.extend({"account": item["account"], "child_id": child["child_id"],
            "error_code": "NOTIFICATION_ROUTE_UNAVAILABLE", "reason": child["reason"]}
            for child in item.get("blocked_children", []) if child["reason"] != "route_disabled_or_changed")
    request.tick_metrics["notification_fanouts"] = summaries
    request.tick_metrics["notification_config_failures"] = blocked
    attempted_count = sum(child["attempt_count"] for item in summaries for child in item["children"]) - sum(
        child["attempt_count"] for item in before.values() for child in item["children"])
    confirmed_count = sum(child["delivery_confirmed"] for item in summaries for child in item["children"]) - sum(
        child["delivery_confirmed"] for item in before.values() for child in item["children"])
    failed = [item for item in summaries if item["status"] in {"partial", "unknown", "explicit_failed"}]
    sent = sorted({item["account"] for item in summaries if item["delivery_confirmed"]})
    failures = [{"account": item["account"], "fanout_id": item["fanout_id"],
        "status": item["status"], "has_retryable_children": item["has_retryable_children"]} for item in failed]
    failures.extend(blocked)
    legacy = _continue_legacy_daily_brief_deliveries(request, preparation.legacy_deliveries)
    request.tick_metrics["legacy_notification_deliveries"] = legacy
    sent = sorted(set(sent) | set(legacy["sent_accounts"]))
    failures.extend(legacy["failures"])
    attempted_count += legacy["attempt_count"]
    confirmed_count += legacy["confirmed_count"]
    request.runlog.safe_event("notify_fanout", "error" if failures else "ok", data={
        "statuses": [item["status"] for item in summaries],
        "has_retryable_children": any(item["has_retryable_children"] for item in summaries)})
    summary = build_notify_summary(sent_accounts=sent, notify_failures=failures,
        total_accounts=len({item["account"] for item in summaries}),
        send_attempted_count=attempted_count, send_confirmed_count=confirmed_count)
    apply_notify_results_to_tick_metrics(tick_metrics=request.tick_metrics, no_send=request.no_send,
        sent_accounts=sent, notify_failures=failures, notify_summary=summary)
    state_repo.write_tick_metrics(request.base, request.run_id, request.tick_metrics)
    failure_source_confirmed_accounts = tuple(sorted({
        item["account"] for item in summaries
        if not request.no_send and item["delivery_confirmed"] and all(
            child["spec"]["metadata"]["delivery_kind"] == "fixed_failure"
            and child["spec"]["metadata"]["source_reference"]
                == f"output_runs/{request.run_id}/accounts/{item['account']}/state/daily_decision_brief_failure.{child['spec']['metadata']['market']}.json"
            for child in item["children"]
        )
    }))
    return TickNotificationOutcome(sent_accounts=tuple(sent), notify_failures=tuple(failures), notify_summary=summary,
        failure_source_confirmed_accounts=failure_source_confirmed_accounts)


def run_tick_notification_flow(request: TickNotificationRequest) -> TickNotificationOutcome:
    """Deliver prepared obligations and report delivery facts to the Tick owner."""
    process_root = (request.repo_root or request.base).resolve()
    daily_brief_prep = request.preparation
    if daily_brief_prep is None:
        raise ValueError("notification delivery requires prepared Brief obligations")
    check_fetch_control(request.deadline, request.cancelled)
    prepared_messages = daily_brief_prep.prepared_messages
    notify_candidates: list[Any] = []
    results_count = len(request.results)

    if not notifications_enabled(request.base_cfg):
        request.runlog.safe_event("notify", "skip", message="notifications_disabled")
        return TickNotificationOutcome(skip_reason="notifications_disabled")

    if str(request.trigger_kind or "manual").strip().lower() != "scheduled":
        reason = "non_scheduled_ordinary_notification_disabled"
        request.audit_helper.audit(
            "notify",
            reason,
            run_id=request.run_id,
            status="skip",
            extra={"trigger_kind": request.trigger_kind, "provider_attempted": False},
        )
        return TickNotificationOutcome(skip_reason=reason)

    if uses_proactive_fanout(request.base_cfg):
        return _deliver_daily_brief_fanouts(request, daily_brief_prep)

    request.runlog.safe_event(
        "notify",
        "prepare",
        data=_safe_runlog_data(
            {
                "results_count": results_count,
                "notify_candidates": len(notify_candidates),
            }
        ),
    )
    account_messages = prepared_messages.messages_by_account

    route_hint = _notification_perception_route_hint(request.base_cfg)
    current_identity = notification_reminder_route_key(config=request.base_cfg, base=process_root) if account_messages else None
    paused_accounts = []
    for account in account_messages:
        envelope = (daily_brief_prep.lifecycles_by_account.get(account) or {}).get("envelope") or {}
        context = envelope.get("render_context") or {}
        frozen_identity = context.get("attribution_route_key")
        if frozen_identity is not None and frozen_identity != current_identity:
            paused_accounts.append(account)
    if paused_accounts:
        account_messages = {account: message for account, message in account_messages.items() if account not in paused_accounts}
        request.runlog.safe_event("notify", "skip", message="notification_route_changed",
                                   data={"accounts": paused_accounts})
        if not account_messages:
            return TickNotificationOutcome(skip_reason="notification_route_changed")

    if daily_brief_prep.multi_market_delivery_unsupported:
        error_code = "daily_brief_multi_market_delivery_unsupported"
        _record_daily_brief_multi_market_failure(request, daily_brief_prep)
        return TickNotificationOutcome(skip_reason=error_code, error_code=error_code)

    if daily_brief_prep.blocked_error_code and not bool(prepared_messages.threshold_met):
        return TickNotificationOutcome(skip_reason=daily_brief_prep.blocked_error_code,
                                       error_code=daily_brief_prep.blocked_error_code)

    if not bool(prepared_messages.threshold_met):
        _audit_notification_perception(
            request,
            build_notification_perception_event(
                event_kind="notification_prepared",
                run_id=request.run_id,
                results_count=results_count,
                notify_candidates=notify_candidates,
                account_messages=account_messages,
                threshold_met=prepared_messages.threshold_met,
                used_heartbeat=prepared_messages.used_heartbeat,
                heartbeat_accounts=prepared_messages.heartbeat_accounts,
                provider=route_hint.get("provider"),
                channel=route_hint.get("channel"),
                target=route_hint.get("target"),
                no_send=request.no_send,
            ),
        )
        _audit_notification_perception(
            request,
            build_notification_perception_event(
                event_kind="no_account_notification",
                run_id=request.run_id,
                results_count=results_count,
                notify_candidates=notify_candidates,
                account_messages=account_messages,
                threshold_met=prepared_messages.threshold_met,
                used_heartbeat=prepared_messages.used_heartbeat,
                heartbeat_accounts=prepared_messages.heartbeat_accounts,
                provider=route_hint.get("provider"),
                channel=route_hint.get("channel"),
                target=route_hint.get("target"),
                no_send=request.no_send,
            ),
        )
        return TickNotificationOutcome(skip_reason=(
            "no_retryable_delivery" if request.delivery_only else "no_daily_brief_delivery"
        ))

    notify_route = resolve_notification_delivery_route(config=request.base_cfg)
    notif_cfg = notify_route.get("notifications") or {}
    provider = notify_route.get("provider")
    channel = notify_route.get("channel")
    target = notify_route.get("target")
    perception_scope = _notification_perception_conversation_scope(
        provider=provider,
        channel=channel,
        target=target,
        base=process_root,
        notifications=notif_cfg,
    )
    _audit_notification_perception(
        request,
        build_notification_perception_event(
            event_kind="notification_prepared",
            run_id=request.run_id,
            results_count=results_count,
            notify_candidates=notify_candidates,
            account_messages=account_messages,
            threshold_met=prepared_messages.threshold_met,
            used_heartbeat=prepared_messages.used_heartbeat,
            heartbeat_accounts=prepared_messages.heartbeat_accounts,
            provider=provider,
            channel=channel,
            target=target,
            no_send=request.no_send,
            conversation_scope=perception_scope,
        ),
    )
    quiet_hours = notif_cfg.get("quiet_hours_beijing")
    dnd_decision = evaluate_dnd_quiet_hours(
        quiet_hours=quiet_hours,
        no_send=request.no_send,
        now_bj_time=datetime.now(timezone.utc).astimezone(request.bj_tz).time(),
        parse_hhmm_fn=parse_hhmm,
    )
    parse_error = dnd_decision.get("parse_error")
    if parse_error:
        request.runlog.safe_event("notify", "error", message=f"failed to parse quiet_hours: {parse_error}")

    try:
        notify_delivery, delivery_batch, target = build_per_account_delivery_batch(
            channel=channel,
            target=target,
            account_messages=account_messages,
            no_send=request.no_send,
            is_quiet=bool(dnd_decision.get("is_quiet")),
            quiet_window=str(dnd_decision.get("quiet_window") or ""),
            decision_builder=decide_notification_delivery,
        )
    except ValueError as err:
        request.runlog.safe_event("notify", "error", error_code="CONFIG_ERROR", message=str(err))
        _record_notification_meta(request, account_messages=account_messages,
                                  markets=daily_brief_prep.markets, confirmed_accounts=(), reason="route_missing")
        raise SystemExit(f"[CONFIG_ERROR] {err}") from err

    request.audit_helper.audit(
        "notify",
        "delivery_decision",
        run_id=request.run_id,
        status=("ok" if not notify_delivery.get("config_error") else "error"),
        target=notification_target_reference(target),
        extra={
            "reason": notify_delivery.get("reason"),
            "should_send": bool(notify_delivery.get("should_send")),
            "account_keys": list(account_messages.keys()),
            "account_count": len(account_messages),
            "account_messages_count": len(account_messages),
            "message_len_by_account": {str(acct): len(str(msg)) for acct, msg in account_messages.items()},
            "provider": str(provider) if provider else None,
            "channel": str(channel) if channel else None,
            "target_set": bool(target),
        },
    )
    _audit_notification_perception(
        request,
        build_notification_perception_event(
            event_kind="notification_delivery_decided",
            run_id=request.run_id,
            results_count=results_count,
            notify_candidates=notify_candidates,
            account_messages=account_messages,
            threshold_met=prepared_messages.threshold_met,
            used_heartbeat=prepared_messages.used_heartbeat,
            heartbeat_accounts=prepared_messages.heartbeat_accounts,
            provider=provider,
            channel=channel,
            target=target,
            no_send=request.no_send,
            quiet_hours=quiet_hours,
            delivery_decision=notify_delivery,
            conversation_scope=perception_scope,
        ),
    )
    if str(notify_delivery.get("action") or "") == "skip_quiet_hours":
        if daily_brief_prep.blocked_error_code:
            return TickNotificationOutcome(skip_reason=daily_brief_prep.blocked_error_code,
                                           error_code=daily_brief_prep.blocked_error_code)
        quiet_window = str(notify_delivery.get("quiet_window") or "")
        request.runlog.safe_event("notify", "skip", message=f"in quiet hours ({quiet_window})")
        print(f"[SKIP] Currently in quiet hours (DND). Target was: {notification_target_reference(target)}")
        _audit_notification_perception(
            request,
            build_notification_perception_event(
                event_kind="quiet_hours_skipped",
                run_id=request.run_id,
                results_count=results_count,
                notify_candidates=notify_candidates,
                account_messages=account_messages,
                threshold_met=prepared_messages.threshold_met,
                used_heartbeat=prepared_messages.used_heartbeat,
                heartbeat_accounts=prepared_messages.heartbeat_accounts,
                provider=provider,
                channel=channel,
                target=target,
                no_send=request.no_send,
                quiet_hours=quiet_hours,
                delivery_decision=notify_delivery,
                conversation_scope=perception_scope,
            ),
        )
        return TickNotificationOutcome(skip_reason="quiet_hours")

    sent_accounts: list[str] = []
    would_send_accounts: list[str] = []
    notify_failures: list[dict[str, object]] = []
    if daily_brief_prep.blocked_error_code:
        notify_failures.append(
            {
                "account": None,
                "error_code": daily_brief_prep.blocked_error_code,
                "attempts": 0,
                "final_returncode": 0,
                "command_ok": False,
                "delivery_confirmed": False,
                "local_blocked": True,
            }
        )
    send_attempted_count = 0
    send_confirmed_count = 0
    retry_attempt_count = 0
    provider_retry_attempt_count = 0
    outer_retry_attempt_count = 0
    fallback_attempt_count = 0
    ambiguous_send_count = 0
    duplicate_risk_count = 0
    provider_delivery_started = monotonic()
    if bool(notify_delivery.get("should_send")):
        assert delivery_batch is not None
        try:
            delivery_adapter = select_notification_delivery_adapter(provider)
        except ValueError as err:
            request.runlog.safe_event("notify", "error", error_code="CONFIG_ERROR", message=str(err))
            raise SystemExit(f"[CONFIG_ERROR] {err}") from err

        delivery_deadline = request.deadline

        def _send_with_route_notifications(**kwargs: Any) -> Any:
            nonlocal delivery_deadline
            check_fetch_control(request.deadline, request.cancelled)
            delivery_deadline = request.deadline
            recovery_identity = None
            for account, lifecycle in daily_brief_prep.lifecycles_by_account.items():
                envelope = lifecycle.get("envelope") or {}
                if (envelope.get("delivery_kind") == "fixed_recovery_report"
                        and build_notification_transport_key(str(envelope["delivery_key"])) == kwargs.get("idempotency_key")):
                    delivery_deadline = _daily_brief_recovery_send_deadline(request, envelope)
                    recovery_identity = {
                        "base": request.base, "account": account, "market": lifecycle["market"],
                        "market_trading_date": lifecycle["market_trading_date"],
                        "delivery_key": envelope["delivery_key"], "source_digest": envelope["source_digest"],
                        "message_sha256": envelope["message_sha256"],
                        "transport_idempotency_key": kwargs["idempotency_key"],
                    }
                    started = begin_daily_decision_brief_recovery_attempt(
                        **recovery_identity, attempted_at_utc=utc_now(),
                    )
                    if not started.get("started"):
                        raise RuntimeError(f"recovery delivery blocked: {started.get('reason')}")
                    check_fetch_control(delivery_deadline, request.cancelled)
                    break
            try:
                sent = delivery_adapter.send_fn(
                    **kwargs,
                    notifications=notify_route.get("notifications") or {},
                    **({"deadline": delivery_deadline, "cancelled": request.cancelled}
                       if delivery_deadline is not None or request.cancelled is not None else {}),
                )
                if recovery_identity is not None:
                    result = normalize_notification_delivery_result(sent, normalize_fn=delivery_adapter.normalize_fn)
                    if not result.get("delivery_confirmed"):
                        record_daily_decision_brief_delivery_attempt(
                            **recovery_identity,
                            ambiguous=bool(result.get("ambiguous_send") or result.get("duplicate_risk") or result.get("command_ok")),
                            attempted_at_utc=utc_now(),
                        )
                return sent
            except Exception:
                if recovery_identity is not None:
                    record_daily_decision_brief_delivery_attempt(
                        **recovery_identity, ambiguous=True, attempted_at_utc=utc_now(),
                    )
                raise

        confirmation_failures: list[dict[str, object]] = []

        def _persist_account_result(send_result: dict[str, object]) -> None:
            confirmed, failures = _confirm_daily_brief_execution(
                request=request, preparation=daily_brief_prep, send_results=(send_result,),
            )
            sent_accounts.extend(confirmed)
            confirmation_failures.extend(failures)

        execution = execute_per_account_delivery(
            delivery_batch=delivery_batch,
            run_id=request.run_id,
            runlog=request.runlog,
            audit_fn=request.audit_helper.audit,
            safe_data_fn=_safe_runlog_data,
            send_fn=_send_with_route_notifications,
            normalize_fn=delivery_adapter.normalize_fn,
            failure_fields_builder=build_failure_audit_fields,
            on_failure=lambda _error_code: None,
            base=process_root,
            failure_stage=delivery_adapter.failure_stage,
            idempotency_keys_by_account=daily_brief_prep.delivery_keys_by_account,
            transport_envelopes_by_account=daily_brief_prep.transport_envelopes_by_account,
            on_account_result=_persist_account_result,
            **({"sleep_fn": lambda seconds: _wait_daily_brief_delivery(request, seconds, deadline=delivery_deadline)}
               if request.deadline is not None or request.cancelled is not None
               or any((item.get("envelope") or {}).get("delivery_kind") == "fixed_recovery_report"
                      for item in daily_brief_prep.lifecycles_by_account.values()) else {}),
        )
        notify_failures.extend(execution.notify_failures)
        notify_failures.extend(confirmation_failures)
        send_attempted_count = execution.send_attempted_count
        send_confirmed_count = len(sent_accounts)
        retry_attempt_count = execution.retry_attempt_count
        provider_retry_attempt_count = execution.provider_retry_attempt_count
        outer_retry_attempt_count = execution.outer_retry_attempt_count
        fallback_attempt_count = execution.fallback_attempt_count
        ambiguous_send_count = execution.ambiguous_send_count + sum(
            1 for item in confirmation_failures if bool(item.get("ambiguous_send"))
        )
        duplicate_risk_count = execution.duplicate_risk_count + sum(
            1 for item in confirmation_failures if bool(item.get("duplicate_risk"))
        )
    else:
        would_send_accounts = list(account_messages.keys())
        sent_accounts = []
        request.runlog.safe_event("notify", "skip", message="no_send mode")

    failure_source_confirmed_accounts = tuple(
        account for account in sent_accounts
        if not request.no_send
        and (daily_brief_prep.lifecycles_by_account.get(account, {}).get("envelope") or {}).get("delivery_kind") == "fixed_failure"
        and (daily_brief_prep.lifecycles_by_account[account]["envelope"]).get("source_reference")
            == f"output_runs/{request.run_id}/accounts/{account}/state/daily_decision_brief_failure.{daily_brief_prep.lifecycles_by_account[account]['market']}.json"
        and (daily_brief_prep.lifecycles_by_account[account]["envelope"]).get("source_kind") == "scan_failure"
    )
    if bool(notify_delivery.get("should_send")):
        reasons = {str(item.get("account") or ""): str(item.get("error_code") or "send_unconfirmed")
                   for item in notify_failures}
        _record_notification_meta(
            request, account_messages=account_messages, markets=daily_brief_prep.markets,
            confirmed_accounts=sent_accounts, reason_by_account=reasons,
        )

    record_tick_latency(
        runlog=request.runlog,
        stage="provider_delivery",
        started=provider_delivery_started,
    )
    report_refs = []
    if not request.no_send:
        for account in sent_accounts:
            lifecycle = daily_brief_prep.lifecycles_by_account[account]
            envelope = lifecycle["envelope"]
            report_refs.append({
                "account": account,
                "market": lifecycle["market"],
                "market_date": lifecycle["market_trading_date"],
                **{key: envelope.get(key) for key in (
                    "revision", "source_digest", "delivery_key", "source_kind", "source_run_id",
                )},
            })

    _audit_notification_perception(
        request,
        build_notification_perception_event(
            event_kind="notification_delivery_completed",
            run_id=request.run_id,
            results_count=results_count,
            notify_candidates=notify_candidates,
            account_messages=account_messages,
            threshold_met=prepared_messages.threshold_met,
            used_heartbeat=prepared_messages.used_heartbeat,
            heartbeat_accounts=prepared_messages.heartbeat_accounts,
            provider=provider,
            channel=channel,
            target=target,
            no_send=request.no_send,
            quiet_hours=quiet_hours,
            delivery_decision=notify_delivery,
            conversation_scope=perception_scope,
            sent_accounts=[] if request.no_send else sent_accounts,
            report_refs=report_refs,
            notify_failures=notify_failures,
            send_attempted_count=send_attempted_count,
            send_confirmed_count=send_confirmed_count,
        ),
    )

    notify_summary = build_notify_summary(
        sent_accounts=sent_accounts,
        notify_failures=notify_failures,
        total_accounts=len(account_messages),
        send_attempted_count=send_attempted_count,
        send_confirmed_count=send_confirmed_count,
        retry_attempt_count=retry_attempt_count,
        provider_retry_attempt_count=provider_retry_attempt_count,
        outer_retry_attempt_count=outer_retry_attempt_count,
        fallback_attempt_count=fallback_attempt_count,
        ambiguous_send_count=ambiguous_send_count,
        duplicate_risk_count=duplicate_risk_count,
    )
    metrics_action = "write_tick_metrics"
    try:
        apply_notify_results_to_tick_metrics(
            tick_metrics=request.tick_metrics,
            no_send=request.no_send,
            sent_accounts=sent_accounts,
            notify_failures=notify_failures,
            notify_summary=notify_summary,
        )
        state_repo.write_tick_metrics(request.base, request.run_id, request.tick_metrics)
        metrics_action = "append_tick_metrics_history"
        state_repo.append_tick_metrics_history(request.base, request.run_id, request.tick_metrics)
        metrics_action = "write_tick_metrics"
        request.audit_helper.audit(
            "write",
            "write_tick_metrics",
            run_id=request.run_id,
            extra={"sent": bool(request.tick_metrics.get("sent"))},
        )
    except Exception as exc:
        _record_finalize_degraded(
            runlog=request.runlog,
            run_id=request.run_id,
            safe_data_fn=_safe_runlog_data,
            audit_fn=request.audit_helper.audit,
            action=metrics_action,
            exc=exc,
            extra={"notification_delivery_confirmed": bool(sent_accounts)},
        )

    return TickNotificationOutcome(
        sent_accounts=tuple(sent_accounts), would_send_accounts=tuple(would_send_accounts),
        notify_failures=tuple(notify_failures),
        notify_summary=notify_summary, channel=(str(channel) if channel else None),
        target=(str(target) if target else None), error_code=daily_brief_prep.blocked_error_code,
        failure_source_confirmed_accounts=failure_source_confirmed_accounts,
        skip_reason="delivery_only_no_send" if request.delivery_only and request.no_send else None,
    )


def _record_notification_meta(
    request: TickNotificationRequest, *, account_messages: dict[str, str],
    markets: tuple[str, ...], confirmed_accounts: list[str] | tuple[str, ...],
    reason: str = "send_unconfirmed", reason_by_account: dict[str, str] | None = None,
) -> None:
    market = str(markets[0] if markets else "unknown").lower()
    confirmed = set(confirmed_accounts)
    for account in account_messages:
        degraded = account not in confirmed
        code = "NOTIFICATION_DELIVERY_UNCONFIRMED"
        detail = (reason_by_account or {}).get(account, reason)
        try:
            outcome = report_system_meta_signal(
                base=request.base, unit=f"options-monitor-tick-{market}.service",
                market=market, account=account, failure_code=code, stage="delivery",
                run_id=request.run_id, degraded=degraded, reason=detail,
                config=request.base_cfg, external=True,
                **({"deadline": request.deadline, "cancelled": request.cancelled}
                   if request.deadline is not None or request.cancelled is not None else {}),
            )
        except Exception:
            outcome = "infra_failed"
            print("<3>NOTIFICATION_META_ALERT_INFRA_FAILED", file=sys.stderr)
        if degraded or outcome in {"recovered", "infra_failed"}:
            request.runlog.safe_event("notify", "error" if degraded else "ok",
                                      error_code=code if degraded else None,
                                      data={"account": account, "reason": detail, "meta_outcome": outcome})
            request.audit_helper.audit(
                "notify", "notification_meta_signal", run_id=request.run_id,
                account=account, status="error" if degraded else "ok",
                extra={"failure_code": code, "reason": detail, "meta_outcome": outcome},
            )


def _daily_brief_limits(config: dict[str, Any]) -> dict[str, Any]:
    notifications = config.get("notifications") if isinstance(config, dict) else {}
    notification_cfg = notifications if isinstance(notifications, dict) else {}
    daily_cfg = notification_cfg.get("daily_brief")
    return dict(daily_cfg) if isinstance(daily_cfg, dict) else {}


def _daily_brief_recovery_intent_for_preparation(
    request: TickNotificationRequest, account: str, market: str,
    scheduler: dict[str, Any], brief: dict[str, Any],
) -> dict[str, Any] | None:
    """Select a new recovery obligation; same-run absence is frozen by history."""
    if (request.trigger_kind != "scheduled" or request.no_send
            or not notifications_enabled(request.base_cfg)
            or scheduler.get("in_run_window") is not True
            or str(scheduler.get("scheduled_target_market") or "").strip()):
        return None
    limits = resolve_daily_brief_render_limits(_daily_brief_limits(request.base_cfg))
    if uses_proactive_fanout(request.base_cfg):
        return _daily_brief_fanout_recovery_intent(request, account, market, scheduler, brief, limits)
    market_date = str(brief["market_trading_date"])
    retry = read_retryable_daily_decision_brief_delivery(
        base=request.base, account=account, market=market, market_trading_date=market_date, now_utc=utc_now(),
    )
    envelope = retry.get("envelope")
    if isinstance(envelope, dict) and classify_retryable_daily_decision_brief_payload(
        base=request.base, account=account, market=market, market_trading_date=market_date, envelope=envelope,
    ) == "legacy_ai_payload_retired":
        return None
    return _daily_brief_recovery_intent(
        request=request, account=account, market=market, scheduler=scheduler, brief=brief, daily_limits=limits,
    )


def _prepare_daily_brief_notification(
    request: TickNotificationRequest,
    *, brief_preparation: TickBriefPreparation,
) -> DailyBriefNotificationPreparation:
    check_fetch_control(request.deadline, request.cancelled)
    markets = tuple(
        dict.fromkeys(
            str(value or "").strip().upper()
            for value in (request.markets_to_run or request.scheduler_markets or ())
            if str(value or "").strip()
        )
    )
    if not markets:
        raise ValueError("daily brief requires at least one scheduler market")
    if len(markets) != 1:
        request.tick_metrics["daily_brief"] = {
            "enabled": True,
            "renderer": "daily_brief",
            "markets": list(markets),
            "prepared": [],
            "error_code": "daily_brief_multi_market_delivery_unsupported",
        }
        return DailyBriefNotificationPreparation(
            prepared_messages=PreparedPerAccountMessages(
                messages_by_account={},
                threshold_met=False,
                used_heartbeat=False,
            ),
            lifecycles_by_account={},
            delivery_keys_by_account={},
            transport_envelopes_by_account={},
            markets=markets,
            multi_market_delivery_unsupported=True,
        )

    if markets != brief_preparation.markets:
        raise ValueError("daily brief source preparation market scope changed")
    if not notifications_enabled(request.base_cfg):
        return DailyBriefNotificationPreparation(
            prepared_messages=PreparedPerAccountMessages(messages_by_account={}, threshold_met=False, used_heartbeat=False),
            lifecycles_by_account={}, delivery_keys_by_account={}, markets=markets,
        )
    multi_market = len(markets) != 1
    lifecycles_by_account: dict[str, dict[str, Any]] = {}
    delivery_keys_by_account: dict[str, str] = {}
    transport_envelopes_by_account: dict[str, dict[str, Any]] = {}
    messages_by_account: dict[str, str] = {}
    lifecycle_audit: list[dict[str, Any]] = []
    blocked_error_code: str | None = None
    daily_limits = resolve_daily_brief_render_limits(_daily_brief_limits(request.base_cfg))
    explicit_routes = uses_proactive_fanout(request.base_cfg)
    fanout_ids: list[str] = []
    legacy_deliveries: list[dict[str, Any]] = []

    if request.delivery_only and explicit_routes:
        for account in _daily_brief_request_accounts(request):
            scheduler = _daily_brief_scheduler_decision(request, account)
            in_run_window = scheduler.get("in_run_window") is True
            if not in_run_window and not has_daily_brief_recovery_to_resume(
                base=request.base, config=request.base_cfg, account=account, market=markets[0], scheduler=scheduler,
            ):
                continue
            market_date = _daily_brief_market_date(scheduler)
            if not market_date:
                continue
            _prepare_daily_brief_fanout_recoveries(request, account, markets[0], market_date, scheduler, daily_limits)
            fanout_ids.extend(item["fanout_id"] for item in _daily_brief_day_fanouts(request, account, markets[0], market_date))
            legacy_deliveries.extend({"account": account, "market": markets[0], "market_trading_date": market_date,
                "envelope": envelope} for envelope in list_pending_legacy_daily_brief_deliveries(
                    base=request.base, account=account, market=markets[0], market_trading_date=market_date, now_utc=utc_now())
                if in_run_window or envelope["delivery_kind"] == "fixed_recovery_report")
        return DailyBriefNotificationPreparation(
            prepared_messages=PreparedPerAccountMessages({}, False, False),
            lifecycles_by_account={}, delivery_keys_by_account={}, markets=markets,
            fanout_ids=tuple(fanout_ids),
            legacy_deliveries=tuple(legacy_deliveries),
        )

    if request.delivery_only:
        if not multi_market:
            market = markets[0]
            for account in _daily_brief_request_accounts(request):
                scheduler = _daily_brief_scheduler_decision(request, account)
                in_run_window = scheduler.get("in_run_window") is True
                if not in_run_window and not has_daily_brief_recovery_to_resume(
                    base=request.base, config=request.base_cfg, account=account, market=market, scheduler=scheduler,
                ):
                    continue
                market_date = _daily_brief_market_date(scheduler)
                if not market_date:
                    continue
                with daily_brief_timing_scope(runlog=request.runlog, account=account, market=market):
                    read_scope = _daily_brief_delivery_read_scope(request, account, market, market_date)
                    retry = read_retryable_daily_decision_brief_delivery(
                        base=request.base,
                        account=account,
                        market=market,
                        market_trading_date=market_date,
                        read_scope=read_scope,
                        now_utc=utc_now(),
                    )
                    envelope = retry.get("envelope")
                    if not in_run_window:
                        state = read_scope.delivery_state(base=request.base, account=account, market=market)
                        day = (state.get("state") or {}).get("days", {}).get(market_date, {})
                        envelope = _eligible_daily_brief_recovery(
                            day, datetime.fromisoformat(str(utc_now()).replace("Z", "+00:00")),
                        )
                        if envelope and not envelope.get("rendered_message"):
                            envelope = None
                        retry = {**retry, "envelope": envelope, "reason": "frozen_recovery"}
                    if not isinstance(envelope, dict) or (
                        envelope.get("delivery_kind") == "candidate_alert"
                        and envelope.get("status") == "pending"
                    ):
                        recovery_report = read_daily_decision_brief_recovery_report(
                            base=request.base, account=account, market=market,
                            market_trading_date=market_date, now_utc=utc_now(), read_scope=read_scope,
                        )
                        if not in_run_window and not recovery_report.get("available"):
                            continue
                        rebuilt = _rebuild_daily_brief_delivery(
                            request=request,
                            account=account,
                            market=market,
                            market_date=market_date,
                            scheduler=scheduler,
                            daily_limits=daily_limits,
                            read_scope=read_scope,
                            recovery_report=recovery_report if recovery_report.get("available") else None,
                        ) if recovery_report.get("available") or not isinstance(envelope, dict) else {}
                        rebuilt_envelope = rebuilt.get("envelope")
                        if isinstance(rebuilt_envelope, dict):
                            read_scope = DailyBriefReadScope(base=request.base, account=account, market=market)
                            if not in_run_window:
                                # Outside-window admission authorizes only this original recovery.
                                retry = {"envelope": rebuilt_envelope, "reason": "frozen_recovery"}
                            else:
                                retry = read_retryable_daily_decision_brief_delivery(
                                    base=request.base,
                                    account=account,
                                    market=market,
                                    market_trading_date=market_date,
                                    read_scope=read_scope,
                                    now_utc=utc_now(),
                                )
                            envelope = retry.get("envelope")
                    if not isinstance(envelope, dict):
                        continue
                    retired_classification = classify_retryable_daily_decision_brief_payload(
                        base=request.base,
                        account=account,
                        market=market,
                        market_trading_date=market_date,
                        envelope=envelope,
                    )
                    if retired_classification != "clean":
                        blocked_error_code = "legacy_ai_payload_retired"
                        lifecycle_audit.append(
                            _daily_brief_retired_envelope_audit(
                                account,
                                market,
                                market_date,
                                envelope,
                            )
                        )
                        continue
                    messages_by_account[account] = str(envelope["rendered_message"])
                    delivery_keys_by_account[account] = str(envelope["delivery_key"])
                    if isinstance(envelope.get("rendered_transport"), dict):
                        transport_envelopes_by_account[account] = dict(envelope["rendered_transport"])
                    lifecycles_by_account[account] = {
                        "envelope": envelope,
                        "market": market,
                        "market_trading_date": market_date,
                        "retry_reason": retry.get("reason"),
                    }
                    lifecycle_audit.append(_daily_brief_envelope_audit(account, market, envelope, retry=True))
    else:
        feishu_card_delivery = (
            str(_notification_perception_route_hint(request.base_cfg).get("provider") or "").strip().lower()
            == FEISHU_APP_NOTIFICATION_PROVIDER
        )
        ran_pipeline_accounts = {
            str(value or "").strip().lower() for value in request.ran_pipeline_accounts if str(value or "").strip()
        }
        scan_targets = {
            str(account).strip().lower(): str(target).strip()
            for account, target in (request.scheduled_scan_targets_by_account or {}).items()
            if str(account).strip() and target and str(target).strip()
        }
        scheduled_trigger = str(request.trigger_kind or "scheduled").strip().lower() == "scheduled"
        for result in request.results:
            account = _daily_brief_result_account(result)
            if not account:
                raise ValueError("daily brief account result is missing account")
            ran_scan = bool(
                account in scan_targets
                or account in ran_pipeline_accounts
                or _daily_brief_result_value(result, "ran_scan") is True
            )
            if not ran_scan:
                continue
            with daily_brief_timing_scope(runlog=request.runlog, account=account, market=markets[0]):
                scheduler = _daily_brief_scheduler_decision(request, account)
                blocked_retry: dict[str, Any] | None = None
                blocked_retry_envelope: dict[str, Any] | None = None
                blocked_retry_classification: str | None = None
                blocked_market_date = _daily_brief_market_date(scheduler)
                if scheduled_trigger and not explicit_routes and not multi_market and blocked_market_date:
                    read_scope = _daily_brief_delivery_read_scope(request, account, markets[0], blocked_market_date)
                    if not request.no_send:
                        recovery_report = read_daily_decision_brief_recovery_report(
                            base=request.base, account=account, market=markets[0],
                            market_trading_date=blocked_market_date, now_utc=utc_now(), read_scope=read_scope,
                        )
                        if recovery_report.get("available"):
                            _rebuild_daily_brief_delivery(
                                request=request, account=account, market=markets[0],
                                market_date=blocked_market_date, scheduler=scheduler, daily_limits=daily_limits,
                                recovery_report=recovery_report,
                            )
                            read_scope = DailyBriefReadScope(base=request.base, account=account, market=markets[0])
                    retry_before_writes = read_retryable_daily_decision_brief_delivery(
                        base=request.base,
                        account=account,
                        market=markets[0],
                        market_trading_date=blocked_market_date,
                        read_scope=read_scope,
                        now_utc=utc_now(),
                    )
                    envelope_before_writes = retry_before_writes.get("envelope")
                    if not isinstance(envelope_before_writes, dict):
                        fixed_recovery = read_daily_decision_brief_fixed_recovery(
                            base=request.base,
                            account=account,
                            market=markets[0],
                            market_trading_date=blocked_market_date,
                            read_scope=read_scope,
                        )
                        if fixed_recovery.get("available"):
                            rebuilt = _rebuild_daily_brief_delivery(
                                request=request,
                                account=account,
                                market=markets[0],
                                market_date=blocked_market_date,
                                scheduler=scheduler,
                                daily_limits=daily_limits,
                                fixed_recovery=fixed_recovery,
                            )
                            envelope_before_writes = rebuilt.get("envelope")
                            if isinstance(envelope_before_writes, dict):
                                retry_before_writes = {
                                    **retry_before_writes,
                                    "reason": "rebuilt_from_fixed_recovery",
                                    "envelope": envelope_before_writes,
                                }
                    if isinstance(envelope_before_writes, dict):
                        blocked_retry_classification = classify_retryable_daily_decision_brief_payload(
                            base=request.base,
                            account=account,
                            market=markets[0],
                            market_trading_date=blocked_market_date,
                            envelope=envelope_before_writes,
                        )
                        if blocked_retry_classification == "legacy_ai_payload_retired":
                            blocked_error_code = "legacy_ai_payload_retired"
                            blocked_retry = retry_before_writes
                            blocked_retry_envelope = envelope_before_writes
                for market in markets:
                    source = brief_preparation.sources[(account, market)]
                    brief = source["brief"]
                    fixed_target = (
                        str(scheduler.get("scheduled_target_market") or "").strip()
                        if request.trigger_kind == "scheduled"
                        else ""
                    )
                    pipeline_reliable = source["pipeline_reliable"]
                    pending: list[str] = []
                    candidate_record_reason: str | None = None
                    persisted = source["persisted"]
                    diff: dict[str, Any] = {}
                    failure_source = source["failure_source"]
                    recovery_due = False
                    if pipeline_reliable:
                        previous = persisted.get("previous_successful_brief")
                        if (
                            isinstance(previous, dict)
                            and previous.get("market_trading_date") == persisted["brief"]["market_trading_date"]
                        ):
                            diff = diff_daily_decision_briefs(previous, persisted["brief"])
                        if scheduled_trigger and blocked_retry_envelope is None:
                            recorded = record_daily_decision_brief_candidates(
                                base=request.base,
                                account=account,
                                market=market,
                                market_trading_date=str(persisted["brief"]["market_trading_date"]),
                                revision=int(persisted["current_revision"]),
                                brief_digest=str(persisted["current_brief_digest"]),
                                candidate_identities=persisted["current_candidate_identities"],
                            )
                            candidate_record_reason = str(recorded["reason"])
                            if recorded["applied"]:
                                pending = _daily_brief_deliverable_candidates(
                                    persisted["brief"],
                                    recorded["pending_candidate_identities"],
                                )
                        if not explicit_routes and fixed_target and blocked_retry_envelope is None and not request.no_send:
                            record_daily_decision_brief_fixed_recovery(
                                base=request.base,
                                account=account,
                                market=market,
                                market_trading_date=str(persisted["brief"]["market_trading_date"]),
                                scheduled_target_market=fixed_target,
                                revision=int(persisted["current_revision"]),
                                brief_digest=str(persisted["current_brief_digest"]),
                                candidate_identities=persisted["current_candidate_identities"],
                            )
                        if (scheduled_trigger and not explicit_routes and not fixed_target and not request.no_send
                                and blocked_retry_envelope is None and candidate_record_reason != "stale_revision"
                                and scheduler.get("in_run_window") is True):
                            recovery_due = _bind_daily_brief_recovery(
                                request=request, account=account, market=market,
                                market_date=str(persisted["brief"]["market_trading_date"]),
                            )

                    if explicit_routes:
                        if scheduled_trigger and not request.no_send:
                            _prepare_daily_brief_fanout(
                                request, account, market, scheduler, persisted["brief"] if persisted else brief,
                                persisted or {"current_brief_digest": failure_source[1],
                                    "source_reference": failure_source[0]}, diff, daily_limits, pipeline_reliable,
                            )
                            fanout_ids.extend(item["fanout_id"] for item in _daily_brief_day_fanouts(
                                request, account, market, str(brief["market_trading_date"])))
                            legacy_deliveries.extend({"account": account, "market": market,
                                "market_trading_date": str(brief["market_trading_date"]), "envelope": envelope}
                                for envelope in list_pending_legacy_daily_brief_deliveries(base=request.base,
                                    account=account, market=market, market_trading_date=str(brief["market_trading_date"]), now_utc=utc_now()))
                        lifecycle_audit.append({"account": account, "market": market,
                            "market_trading_date": str(brief["market_trading_date"]),
                            "pipeline_reliable": pipeline_reliable, "fanout": True})
                        continue

                    decision = (
                        decide_daily_brief_notification(
                            ran_scan=True,
                            pipeline_reliable=pipeline_reliable,
                            fixed_due=bool(fixed_target),
                            pending_candidate_identities=pending,
                            recovery_due=recovery_due,
                        )
                        if scheduled_trigger
                        else {"action": "none", "reason": "non_scheduled_snapshot_only"}
                    )
                    if candidate_record_reason == "stale_revision" and not fixed_target:
                        decision = {"action": "none", "reason": "stale_candidate_revision"}
                    action = str(decision["action"])
                    if recovery_due:
                        recovery_report = read_daily_decision_brief_recovery_report(
                            base=request.base, account=account, market=market,
                            market_trading_date=str(brief["market_trading_date"]), now_utc=utc_now(),
                        )
                        if recovery_report.get("available"):
                            _rebuild_daily_brief_delivery(
                                request=request, account=account, market=market,
                                market_date=str(brief["market_trading_date"]), scheduler=scheduler,
                                daily_limits=daily_limits, recovery_report=recovery_report,
                            )
                    existing_retry = None
                    if scheduled_trigger and not multi_market:
                        existing_retry = blocked_retry or read_retryable_daily_decision_brief_delivery(
                            base=request.base,
                            account=account,
                            market=market,
                            market_trading_date=str(brief["market_trading_date"]),
                            now_utc=utc_now(),
                        )
                    existing_envelope = existing_retry.get("envelope") if isinstance(existing_retry, dict) else None
                    pending_delivery_status = (
                        "existing_pending_preserved" if isinstance(existing_envelope, Mapping) else "not_applicable"
                    )
                    should_prepare = not (isinstance(existing_retry, dict) and isinstance(existing_envelope, dict))
                    if (
                        action == "fixed_report"
                        and isinstance(existing_envelope, dict)
                        and existing_envelope.get("delivery_kind") == "candidate_alert"
                        and existing_envelope.get("status") == "pending"
                    ):
                        # A fixed batch has its own envelope; the selector keeps ambiguous retries first.
                        should_prepare = True
                    if (
                        not multi_market
                        and not request.no_send
                        and should_prepare
                        and action in {"fixed_report", "candidate_alert", "fixed_failure"}
                    ):
                        render_context = _daily_brief_render_context(request, scheduler_decision=scheduler)
                        if action == "fixed_failure":
                            message = (render_fixed_failure if feishu_card_delivery else render_fixed_failure_text)(
                                brief,
                                context=render_context,
                            )
                            assert failure_source is not None
                            prepared = prepare_daily_decision_brief_delivery(
                                base=request.base,
                                account=account,
                                market=market,
                                market_trading_date=str(brief["market_trading_date"]),
                                run_id=request.run_id,
                                delivery_kind="fixed_failure",
                                source_kind="scan_failure",
                                source_digest=failure_source[1],
                                source_reference=failure_source[0],
                                scheduled_target_market=fixed_target,
                                rendered_message=message,
                                render_context=render_context,
                            )
                        else:
                            assert persisted is not None
                            render_context.update(_daily_brief_attribution_context(
                                request, persisted["brief"], account=account, market=market,
                            ))
                            identities = persisted["current_candidate_identities"] if action == "fixed_report" else pending
                            message, rendered_transport, display_evidence = _select_daily_brief_delivery(
                                persisted["brief"], kind=action, identities=identities, diff=diff,
                                limits=daily_limits, context=render_context, card=feishu_card_delivery,
                                open_id=resolve_feishu_bot_send_target(notifications=request.base_cfg.get("notifications"))
                                if feishu_card_delivery else "",
                            )
                            represented = _daily_brief_represented_identities({
                                "candidate_identities": identities, "display_evidence": display_evidence})
                            if action != "candidate_alert" or represented:
                                prepare_daily_decision_brief_delivery(
                                    base=request.base,
                                    account=account,
                                    market=market,
                                    market_trading_date=str(persisted["brief"]["market_trading_date"]),
                                    run_id=request.run_id,
                                    delivery_kind=action,
                                    source_kind="successful_brief",
                                    revision=int(persisted["current_revision"]),
                                    source_digest=str(persisted["current_brief_digest"]),
                                    scheduled_target_market=(fixed_target or None),
                                    candidate_identities=identities,
                                    display_evidence=display_evidence,
                                    rendered_message=message,
                                    rendered_transport=rendered_transport,
                                    render_context=render_context,
                                )

                    retry = None
                    retired_classification: str | None = None
                    if scheduled_trigger and not multi_market:
                        retry = blocked_retry or read_retryable_daily_decision_brief_delivery(
                            base=request.base,
                            account=account,
                            market=market,
                            market_trading_date=str(brief["market_trading_date"]),
                            now_utc=utc_now(),
                        )
                        envelope = retry.get("envelope")
                        if isinstance(envelope, dict):
                            retired_classification = (
                                blocked_retry_classification
                                or classify_retryable_daily_decision_brief_payload(
                                    base=request.base,
                                    account=account,
                                    market=market,
                                    market_trading_date=str(brief["market_trading_date"]),
                                    envelope=envelope,
                                )
                            )
                            if retired_classification == "clean":
                                messages_by_account[account] = str(envelope["rendered_message"])
                                delivery_keys_by_account[account] = str(envelope["delivery_key"])
                                if isinstance(envelope.get("rendered_transport"), dict):
                                    transport_envelopes_by_account[account] = dict(envelope["rendered_transport"])
                                lifecycles_by_account[account] = {
                                    "brief": persisted["brief"] if persisted is not None else brief,
                                    "diff": diff,
                                    "delivery_kind": "full" if envelope["delivery_kind"].startswith("fixed_") else "delta",
                                    "delivery_key": envelope["delivery_key"],
                                    "envelope": envelope,
                                    "market": market,
                                    "market_trading_date": str(brief["market_trading_date"]),
                                }
                            else:
                                blocked_error_code = "legacy_ai_payload_retired"
                    selected_envelope = retry.get("envelope") if isinstance(retry, dict) else None
                    lifecycle_audit.append(
                        {
                            "account": account,
                            "market": market,
                            "market_trading_date": str(brief["market_trading_date"]),
                            "brief_id": (persisted or {}).get("brief", brief).get("brief_id"),
                            "pipeline_reliable": pipeline_reliable,
                            "decision": action,
                            "decision_reason": decision["reason"],
                            "fixed_target": fixed_target or None,
                            "pending_candidate_count": len(pending),
                            "candidate_record_reason": candidate_record_reason,
                            "pending_delivery_status": pending_delivery_status,
                            "retry_reason": retry.get("reason") if isinstance(retry, dict) else None,
                            "selected_delivery_kind": (
                                selected_envelope.get("delivery_kind") if isinstance(selected_envelope, dict) else None
                            ),
                            "delivery_key": selected_envelope.get("delivery_key")
                            if isinstance(selected_envelope, dict)
                            else None,
                            "message_sha256": selected_envelope.get("message_sha256")
                            if isinstance(selected_envelope, dict)
                            else None,
                            "rendered_transport_sha256": (
                                selected_envelope.get("rendered_transport_sha256")
                                if isinstance(selected_envelope, dict)
                                else None
                            ),
                            "message_chars": len(str(selected_envelope.get("rendered_message") or ""))
                            if isinstance(selected_envelope, dict)
                            else 0,
                            "render_limits": dict(daily_limits),
                            "error_code": (
                                "legacy_ai_payload_retired"
                                if retired_classification == "legacy_ai_payload_retired"
                                else None
                            ),
                        }
                    )

    request.tick_metrics["daily_brief"] = {
        "enabled": True,
        "renderer": "daily_brief",
        "delivery_only": bool(request.delivery_only),
        "markets": list(markets),
        "prepared": lifecycle_audit,
    }
    for item in lifecycle_audit:
        request.audit_helper.audit(
            "daily_brief",
            "prepared",
            run_id=request.run_id,
            account=str(item["account"]),
            status="error" if item.get("error_code") else "ok",
            extra={key: value for key, value in item.items() if key != "account"},
        )
    return DailyBriefNotificationPreparation(
        prepared_messages=PreparedPerAccountMessages(
            messages_by_account=messages_by_account,
            threshold_met=bool(messages_by_account),
            used_heartbeat=False,
            heartbeat_accounts=(),
        ),
        lifecycles_by_account=lifecycles_by_account,
        delivery_keys_by_account=delivery_keys_by_account,
        transport_envelopes_by_account=transport_envelopes_by_account,
        markets=markets,
        multi_market_delivery_unsupported=multi_market,
        blocked_error_code=blocked_error_code,
        fanout_ids=tuple(dict.fromkeys(fanout_ids)),
        legacy_deliveries=tuple(legacy_deliveries),
    )


def _daily_brief_request_accounts(request: TickNotificationRequest) -> list[str]:
    values = request.account_ids or [_daily_brief_result_account(item) for item in request.results]
    return list(dict.fromkeys(str(value or "").strip().lower() for value in values if str(value or "").strip()))


def _daily_brief_result_account(result: Any) -> str:
    return str(_daily_brief_result_value(result, "account") or "").strip().lower()


def _daily_brief_result_value(result: Any, field: str) -> Any:
    return result.get(field) if isinstance(result, dict) else getattr(result, field, None)


def _daily_brief_delivery_read_scope(
    request: TickNotificationRequest, account: str, market: str, market_date: str,
) -> DailyBriefReadScope:
    scope = DailyBriefReadScope(base=request.base, account=account, market=market)
    state = scope.delivery_state(base=request.base, account=account, market=market)
    day = (state.get("state") or {}).get("days", {}).get(market_date, {})
    if not request.no_send and any(
        item.get("delivery_kind") == "fixed_failure" and item.get("status") == "confirmed"
        and target not in day.get("recovery_reports", {})
        for target, item in day.get("fixed_reports", {}).items()
    ):
        if _bind_daily_brief_recovery(request=request, account=account, market=market, market_date=market_date):
            scope = DailyBriefReadScope(base=request.base, account=account, market=market)
            state = scope.delivery_state(base=request.base, account=account, market=market)
            day = (state.get("state") or {}).get("days", {}).get(market_date, {})
    if not request.no_send and day.get("recovery_reports"):
        expired = expire_daily_decision_brief_recovery_reports(
            base=request.base, account=account, market=market, market_trading_date=market_date, now_utc=utc_now(),
        )
        if expired.get("updated"):
            return DailyBriefReadScope(base=request.base, account=account, market=market)
    return scope


def _daily_brief_displayed_roll_identities(
    brief: Mapping[str, Any], identities: list[str], *, delivery_kind: str,
    diff: Mapping[str, Any], limits: Any, rendered_message: str | None = None,
) -> list[str]:
    rolls = daily_brief_roll_notification_identities(brief)
    now = utc_now()
    current = daily_brief_roll_notification_identities(brief, now_utc=now)
    rendered = select_rendered_position_roll_identities(
        brief, delivery_kind=delivery_kind, candidate_identities=identities, diff=diff, limits=limits,
        rendered_message=rendered_message, now_utc=now)
    return [identity for identity in identities if identity not in rolls or identity in rendered & current]


def _daily_brief_roll_render_source(
    brief: Mapping[str, Any], identities: list[str], *,
    now_utc: datetime | str | None = None,
) -> Mapping[str, Any]:
    """Filter requested current rolls without applying display limits."""
    if not daily_brief_roll_notification_identities(brief):
        return brief
    allowed = set(identities).intersection(
        daily_brief_roll_notification_identities(
            brief, now_utc=now_utc or utc_now()))
    return {**brief, "positions": [
        row if daily_brief_position_roll_identity(
            row, account=brief.get("account"), market=brief.get("market")) in allowed
        else {key: value for key, value in row.items()
              if key not in {"roll_advice", "roll_notification_eligible"}}
        for row in brief.get("positions") or []
    ]}


def _daily_brief_transport_roll_identities(
    brief: Mapping[str, Any], identities: list[str], *, delivery_kind: str,
    diff: Mapping[str, Any], limits: Any, rendered_message: str,
    rendered_transport: Mapping[str, Any] | None,
) -> list[str]:
    """Select complete rolls from existing final outputs without dispatching renders."""
    rolls = daily_brief_roll_notification_identities(brief)
    allowed = set(identities) & rolls
    if not allowed:
        return identities
    bodies = (rendered_message,)
    if rendered_transport is not None:
        bodies += feishu_notification_visible_markdown(rendered_transport)
    visible_sets = [set(_daily_brief_displayed_roll_identities(
        brief, identities, delivery_kind=delivery_kind, diff=diff, limits=limits,
        rendered_message=body)) & allowed for body in bodies]
    common = set.intersection(*visible_sets)
    # Only remove rolls: a smaller message must not refill its budget with other lots.
    return [identity for identity in identities if identity not in rolls or identity in common]


def _daily_brief_candidate_render_context(
    brief: Mapping[str, Any], *, delivery_kind: str, candidate_identities: list[str],
    diff: Mapping[str, Any], limits: Any, context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rows = select_rendered_combo_candidate_rows(
        brief, delivery_kind=delivery_kind, candidate_identities=candidate_identities,
        diff=diff, limits=limits, context=context,
    )
    identities = combo_candidate_identities_for_rendered_rows(brief, rows)
    return {
        **combo_exposure_render_context(derive_combo_candidate_exposures(brief, candidate_identities=identities)),
        "rendered_combo_candidate_identities": identities,
    }


def _daily_brief_recovery_intent(
    *, request: TickNotificationRequest, account: str, market: str, scheduler: dict[str, Any],
    brief: dict[str, Any], daily_limits: Any,
) -> dict[str, Any] | None:
    market_date = str(brief["market_trading_date"])
    state = read_daily_decision_brief_delivery_state(base=request.base, account=account, market=market)
    day = (state.get("state") or {}).get("days", {}).get(market_date, {})
    schedule = request.base_cfg.get(request.scheduler_schedule_key) or {}
    now = utc_now()
    now_dt = now if isinstance(now, datetime) else datetime.fromisoformat(now.replace("Z", "+00:00"))
    for target, failure in sorted(day.get("fixed_reports", {}).items(), reverse=True):
        if (failure.get("delivery_kind") != "fixed_failure" or failure.get("status") != "confirmed"
                or target in day.get("recovery_reports", {})):
            continue
        cutoff = daily_brief_recovery_cutoff(schedule_cfg=schedule, scheduled_target_market=target)
        generated = datetime.fromisoformat(str(brief["generated_at_utc"]).replace("Z", "+00:00"))
        failure_at = datetime.fromisoformat(failure["first_prepared_at_utc"])
        if not max(datetime.fromisoformat(target), failure_at) < generated <= now_dt < datetime.fromisoformat(cutoff):
            continue
        context = _daily_brief_render_context(request, scheduler_decision=scheduler)
        context.update({
            "scheduled_target_market": target,
            "recovery_original_target": target,
            "recovery_scan_at_utc": brief["generated_at_utc"],
            "recovery_card_delivery": (
                str(_notification_perception_route_hint(request.base_cfg).get("provider") or "").strip().lower()
                == FEISHU_APP_NOTIFICATION_PROVIDER
            ),
            "recovery_fallback_open_id": resolve_feishu_bot_send_target(notifications=request.base_cfg.get("notifications")),
        })
        context.update(_daily_brief_attribution_context(request, brief, account=account, market=market))
        return {
            "scheduled_target_market": target, "cutoff_utc": cutoff,
            "bound_at_utc": now_dt.astimezone(timezone.utc).isoformat(),
            "scheduled_scan_target_market": request.scheduled_scan_targets_by_account[account],
            "original_failure_key": failure["delivery_key"], "original_failure_digest": failure["source_digest"],
            "render_context": context, "render_limits": dict(daily_limits),
        }
    return None


def _bind_daily_brief_recovery(
    *, request: TickNotificationRequest, account: str, market: str, market_date: str,
) -> bool:
    state = read_daily_decision_brief_delivery_state(base=request.base, account=account, market=market)
    day = (state.get("state") or {}).get("days", {}).get(market_date, {})
    for target, failure in sorted(day.get("fixed_reports", {}).items(), reverse=True):
        if (failure.get("delivery_kind") != "fixed_failure" or failure.get("status") != "confirmed"
                or target in day.get("recovery_reports", {})):
            continue
        persisted = read_daily_decision_brief_recovery_source(
            base=request.base, account=account, market=market, market_trading_date=market_date,
            scheduled_target_market=target, original_failure_key=failure["delivery_key"],
            original_failure_digest=failure["source_digest"],
        )
        if not persisted.get("available"):
            continue
        intent = persisted["recovery_intent"]
        brief = persisted["brief"]
        previous = persisted.get("previous_successful_brief")
        diff = diff_daily_decision_briefs(previous, brief) if previous else {}
        identities = persisted["current_candidate_identities"]
        context = dict(intent["render_context"])
        context.update(_daily_brief_candidate_render_context(
            brief, delivery_kind="fixed_report", candidate_identities=identities,
            diff=diff, limits=intent["render_limits"], context=context,
        ))
        bound = bind_daily_decision_brief_recovery_report(
            base=request.base, account=account, market=market, market_trading_date=market_date,
            scheduled_target_market=target, cutoff_utc=intent["cutoff_utc"],
            revision=int(persisted["current_revision"]), brief_digest=str(persisted["current_brief_digest"]),
            candidate_identities=identities, render_context=context, diff=diff, render_limits=intent["render_limits"],
            now_utc=intent["bound_at_utc"],
        )
        # Materialize the intent's original binding time, then apply the real
        # current cutoff before any reader can select a newly rebuilt payload.
        if bound.get("available"):
            expire_daily_decision_brief_recovery_reports(
                base=request.base, account=account, market=market,
                market_trading_date=market_date, now_utc=utc_now(),
            )
        return bool(bound.get("available"))
    return False


def _rebuild_daily_brief_delivery(
    *,
    request: TickNotificationRequest,
    account: str,
    market: str,
    market_date: str,
    scheduler: dict[str, Any],
    daily_limits: Any,
    fixed_recovery: Mapping[str, Any] | None = None,
    read_scope: DailyBriefReadScope | None = None,
    recovery_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Recreate only the missing envelope from the canonical persisted Brief."""

    if request.no_send:
        return {"envelope": None, "reason": "no_send"}
    supplement = recovery_report
    if supplement is None:
        supplement = read_daily_decision_brief_recovery_report(
            base=request.base, account=account, market=market, market_trading_date=market_date,
            now_utc=utc_now(), read_scope=read_scope,
        )
    recovering_report = bool(supplement.get("available"))
    recovery_result = (
        dict(fixed_recovery)
        if isinstance(fixed_recovery, Mapping)
        else read_daily_decision_brief_fixed_recovery(
            base=request.base,
            account=account,
            market=market,
            market_trading_date=market_date,
            read_scope=read_scope,
        )
    )
    recovering_fixed = bool(recovery_result.get("available"))
    if recovering_report:
        latest = supplement
    elif recovering_fixed:
        latest = recovery_result
    else:
        latest = read_latest_daily_decision_brief(
            base=request.base,
            account=account,
            market=market,
        )
    brief = latest.get("brief")
    if (
        not latest.get("available")
        or not isinstance(brief, dict)
        or brief.get("market_trading_date") != market_date
        or brief.get("status") not in {"ready", "degraded"}
        or brief.get("actionability") == "blocked"
    ):
        return {"envelope": None, "reason": "persisted_brief_unavailable"}
    delivery_state = read_daily_decision_brief_delivery_state(
        base=request.base,
        account=account,
        market=market,
        read_scope=read_scope,
    )
    state = delivery_state.get("state") if delivery_state.get("available") else None
    day = state.get("days", {}).get(market_date, {}) if isinstance(state, dict) else {}
    if not recovering_report and not recovering_fixed:
        expired_source = any(
            item.get("status") == "expired_unconfirmed" and item.get("revision") == brief.get("revision")
            and item.get("source_digest") == latest.get("brief_digest")
            for item in day.get("recovery_reports", {}).values()
        )
        if expired_source:
            return {"envelope": None, "reason": "recovery_window_expired"}
    pending = sorted((day.get("pending_candidates") or {}).keys()) if isinstance(day, dict) else []
    requested_pending = list(pending)
    pending = _daily_brief_deliverable_candidates(brief, pending)
    _record_expired_roll_candidates(
        request, brief=brief, account=account, market=market,
        requested=requested_pending, delivered=pending,
    )
    recovery = supplement.get("recovery") if recovering_report else recovery_result.get("recovery")
    fixed_target = (
        str(recovery.get("scheduled_target_market") or "").strip()
        if (recovering_report or recovering_fixed) and isinstance(recovery, Mapping)
        else str(scheduler.get("scheduled_target_market") or "").strip()
    )
    action = "fixed_recovery_report" if recovering_report else "fixed_report" if fixed_target else ("candidate_alert" if pending else "none")
    if action == "none":
        return {"envelope": None, "reason": "no_delivery_due"}
    full_report = action in {"fixed_report", "fixed_recovery_report"}
    identities = list(latest.get("candidate_identities") or []) if full_report else pending
    render_context = {} if recovering_report else _daily_brief_render_context(request, scheduler_decision=scheduler)
    if not recovering_report:
        render_context.update(_daily_brief_attribution_context(
            request, brief, account=account, market=market, read_scope=read_scope,
        ))
    diff: dict[str, Any] = {}
    if action == "fixed_report":
        revision_result = list_daily_decision_brief_revisions(
            base=request.base,
            account=account,
            market=market,
            market_trading_date=market_date,
        )
        prior_revisions = [
            int(revision)
            for revision in revision_result.get("revisions") or []
            if int(revision) < int(brief["revision"])
        ]
        if prior_revisions:
            previous = read_daily_decision_brief(
                base=request.base,
                account=account,
                market=market,
                market_trading_date=market_date,
                revision=max(prior_revisions),
            ).get("brief")
            if isinstance(previous, dict):
                diff = diff_daily_decision_briefs(previous, brief)
    if recovering_report:
        render_context = dict(recovery["render_context"])
        daily_limits = dict(recovery["render_limits"])
        diff = dict(recovery["diff"])
    feishu_card_delivery = (
        str(_notification_perception_route_hint(request.base_cfg).get("provider") or "").strip().lower()
        == FEISHU_APP_NOTIFICATION_PROVIDER
    )
    if recovering_report:
        feishu_card_delivery = bool(render_context["recovery_card_delivery"])
    message, rendered_transport, display_evidence = _select_daily_brief_delivery(
        brief, kind=action, identities=identities, diff=diff, limits=daily_limits,
        context=render_context, card=feishu_card_delivery, preserve_context=recovering_report,
        open_id=(render_context.get("recovery_fallback_open_id") if recovering_report
                 else resolve_feishu_bot_send_target(notifications=request.base_cfg.get("notifications")))
        if feishu_card_delivery else "",
    )
    if action == "candidate_alert" and not _daily_brief_represented_identities({
        "candidate_identities": identities, "display_evidence": display_evidence}):
        return {"envelope": None, "reason": "no_rendered_current_position_advice"}
    prepared = prepare_daily_decision_brief_delivery(
        base=request.base,
        account=account,
        market=market,
        market_trading_date=market_date,
        run_id=request.run_id,
        delivery_kind=action,
        source_kind="successful_brief",
        source_digest=str(latest["brief_digest"]),
        revision=int(brief["revision"]),
        scheduled_target_market=fixed_target or None,
        candidate_identities=identities,
        display_evidence=display_evidence,
        rendered_message=message,
        rendered_transport=rendered_transport,
        render_context=render_context,
        prepared_at_utc=utc_now(),
    )
    return {"envelope": prepared.get("envelope"), "reason": "rebuilt"}


def _daily_brief_scheduler_decision(request: TickNotificationRequest, account: str) -> dict[str, Any]:
    raw = (request.scheduler_decisions_by_account or {}).get(account)
    if isinstance(raw, dict):
        return raw
    return dict(request.scheduler_decision or {})


def _daily_brief_market_date(scheduler: dict[str, Any]) -> str | None:
    value = str(scheduler.get("now_market") or scheduler.get("scheduled_scan_target_market") or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _daily_brief_envelope_audit(account: str, market: str, envelope: dict[str, Any], *, retry: bool) -> dict[str, Any]:
    return {
        "account": account,
        "market": market,
        "market_trading_date": str(envelope.get("scheduled_target_market") or "")[:10],
        "delivery_kind": envelope.get("delivery_kind"),
        "delivery_key": envelope.get("delivery_key"),
        "message_sha256": envelope.get("message_sha256"),
        "rendered_transport_sha256": envelope.get("rendered_transport_sha256"),
        "render_mode": (
            envelope.get("rendered_transport", {}).get("render_mode")
            if isinstance(envelope.get("rendered_transport"), dict)
            else "post_markdown"
        ),
        "retry": retry,
    }


def _daily_brief_retired_envelope_audit(
    account: str,
    market: str,
    market_trading_date: str,
    envelope: dict[str, Any],
) -> dict[str, Any]:
    return {
        **_daily_brief_envelope_audit(account, market, envelope, retry=True),
        "market_trading_date": market_trading_date,
        "error_code": "legacy_ai_payload_retired",
        "blocked": True,
    }


def _daily_brief_attribution_context(
    request: TickNotificationRequest, brief: dict[str, Any], *, account: str, market: str,
    read_scope: DailyBriefReadScope | None = None,
) -> dict[str, Any]:
    route_key = notification_reminder_route_key(
        config=request.base_cfg, base=(request.repo_root or request.base).resolve())
    previous = read_confirmed_attribution_render_context(
        base=request.base, account=account, market=market, read_scope=read_scope)
    if not route_key or previous.get("attribution_route_key") != route_key:
        previous = {}
    return {**build_attribution_reminder_context(brief, previous_context=previous),
            "attribution_route_key": route_key}


def _daily_brief_render_context(
    request: TickNotificationRequest,
    *,
    scheduler_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schedule = request.base_cfg.get(request.scheduler_schedule_key) if isinstance(request.base_cfg, dict) else {}
    schedule_map = schedule if isinstance(schedule, dict) else {}
    scheduler = scheduler_decision or (
        request.scheduler_decision if isinstance(request.scheduler_decision, dict) else {}
    )
    user_timezone = getattr(request.bj_tz, "key", None) or str(request.bj_tz)
    trigger_kind = str(request.trigger_kind or "scheduled").strip().lower()
    advice_time = utc_now()
    return {
        "trigger_kind": trigger_kind,
        "scheduled_target_market": (
            (scheduler.get("scheduled_target_market") or scheduler.get("scheduled_scan_target_market"))
            if trigger_kind == "scheduled"
            else None
        ),
        "market_timezone": str(schedule_map.get("timezone") or "").strip(),
        "user_timezone": str(user_timezone or "Asia/Shanghai"),
        "user_timezone_label": "北京",
        "advice_time_utc": advice_time.isoformat() if isinstance(advice_time, datetime) else advice_time,
    }


def _record_daily_brief_multi_market_failure(
    request: TickNotificationRequest,
    preparation: DailyBriefNotificationPreparation,
) -> None:
    markets = list(preparation.markets)
    request.runlog.safe_event(
        "daily_brief",
        "error",
        error_code="daily_brief_multi_market_delivery_unsupported",
        message="daily_brief_multi_market_delivery_unsupported",
        data=_safe_runlog_data({"markets": markets}),
    )
    request.audit_helper.audit(
        "daily_brief",
        "daily_brief_multi_market_delivery_unsupported",
        run_id=request.run_id,
        status="error",
        message="daily_brief_multi_market_delivery_unsupported",
        extra={
            "markets": markets,
            "delivery_pointer_advanced": False,
            "provider_attempted": False,
            "error_code": "daily_brief_multi_market_delivery_unsupported",
        },
    )


def _confirm_daily_brief_execution(
    *,
    request: TickNotificationRequest,
    preparation: DailyBriefNotificationPreparation,
    send_results: Iterable[dict[str, object]],
) -> tuple[list[str], list[dict[str, object]]]:
    confirmed_accounts: list[str] = []
    failures: list[dict[str, object]] = []
    for send_result in send_results:
        if not isinstance(send_result, dict):
            continue
        account = str(send_result.get("account") or "").strip().lower()
        lifecycle = preparation.lifecycles_by_account.get(account)
        if lifecycle is None:
            continue
        envelope = lifecycle.get("envelope")
        if not isinstance(envelope, dict):
            continue
        common = {
            "base": request.base,
            "account": account,
            "market": str(lifecycle["market"]),
            "market_trading_date": str(lifecycle["market_trading_date"]),
            "delivery_key": str(envelope["delivery_key"]),
            "source_digest": str(envelope["source_digest"]),
            "message_sha256": str(envelope["message_sha256"]),
            "transport_idempotency_key": str(send_result.get("idempotency_key") or ""),
        }
        try:
            if bool(send_result.get("ok")) and bool(send_result.get("delivery_confirmed")):
                confirmation = confirm_daily_decision_brief_delivery_v2(
                    **common,
                    confirmed_at_utc=utc_now(),
                    attribution_route_key=send_result.get("attribution_route_key"),
                )
                confirmed_accounts.append(account)
                request.audit_helper.audit(
                    "daily_brief",
                    "delivery_confirmed",
                    run_id=request.run_id,
                    account=account,
                    status="ok",
                    extra={
                        "delivery_kind": envelope["delivery_kind"],
                        "delivery_key": envelope["delivery_key"],
                        "confirmation_reason": confirmation.get("reason"),
                        "render_mode": send_result.get("render_mode"),
                        "fallback_used": bool(send_result.get("fallback_used")),
                        "effective_idempotency_key": send_result.get("effective_idempotency_key"),
                    },
                )
                continue
            record_daily_decision_brief_delivery_attempt(
                **common,
                ambiguous=bool(
                    send_result.get("ambiguous_send")
                    or send_result.get("duplicate_risk")
                    or send_result.get("command_ok")
                ),
                attempted_at_utc=utc_now(),
            )
        except Exception as exc:
            if bool(send_result.get("delivery_confirmed")):
                try:
                    record_daily_decision_brief_delivery_attempt(
                        **common,
                        ambiguous=True,
                        attempted_at_utc=utc_now(),
                    )
                except Exception:
                    pass
            failures.append(
                {
                    "account": account,
                    "error_code": "DAILY_BRIEF_CONFIRM_FAILED",
                    "attempts": int(send_result.get("attempts") or 1),
                    "final_returncode": int(send_result.get("final_returncode") or 0),
                    "message_id": send_result.get("message_id"),
                    "upstream_message_id": send_result.get("upstream_message_id"),
                    "command_ok": bool(send_result.get("command_ok")),
                    "delivery_confirmed": bool(send_result.get("delivery_confirmed")),
                    "provider_response_code": send_result.get("provider_response_code"),
                    "idempotency_key": send_result.get("idempotency_key"),
                    "local_receipt_id": send_result.get("local_receipt_id"),
                    "retry_attempt_count": int(send_result.get("retry_attempt_count") or 0),
                    "ambiguous_send": True,
                    "duplicate_risk": True,
                    "exception_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )
    return confirmed_accounts, failures


def _audit_notification_perception(request: TickNotificationRequest, event: dict[str, Any]) -> None:
    try:
        request.audit_helper.audit(
            "assistant_perception",
            str(event.get("event_kind") or "notification_event"),
            run_id=request.run_id,
            status="ok",
            extra=event,
        )
    except Exception:
        pass


def _notification_perception_route_hint(config: dict[str, Any]) -> dict[str, Any]:
    notifications = config.get("notifications") if isinstance(config, dict) else {}
    notif_cfg = notifications if isinstance(notifications, dict) else {}
    provider = notif_cfg.get("provider") or notif_cfg.get("channel") or "wechat_clawbot"
    channel = notif_cfg.get("channel") or provider
    return {
        "provider": provider,
        "channel": channel,
        "target": notif_cfg.get("target"),
    }


def _notification_perception_conversation_scope(
    *,
    provider: Any,
    channel: Any,
    target: Any,
    base: Path,
    notifications: dict[str, Any],
) -> dict[str, str | None]:
    if str(provider or "").strip() == "wechat_clawbot" or str(channel or "").strip().lower() == "wechat":
        try:
            from src.application.channels.wechat_clawbot.state import load_wechat_clawbot_binding
            from src.application.conversation_scope import wechat_window_conversation_id

            binding = load_wechat_clawbot_binding(base=base, target=str(target or ""), notifications=notifications)
            conversation_id = wechat_window_conversation_id(
                chat_key=getattr(binding, "chat_key", None),
                group_id=getattr(binding, "group_id", None),
                sender_id=getattr(binding, "to_user_id", None),
            )
            if conversation_id:
                return {"channel": "wechat", "conversation_id": conversation_id}
        except Exception:
            pass
    from src.application.conversation_scope import conversation_scope_from_notification_route

    return conversation_scope_from_notification_route(provider=provider, channel=channel, target=target)
