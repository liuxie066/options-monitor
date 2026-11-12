"""Declarative registry of every report the system knows about.

The registry is metadata only.  A report becomes runnable through this package
only when ``migrated`` is true and a runner is registered; legacy reports stay
on their existing implementation and are listed here for discovery, ownership,
and schedule inspection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any


SCHEDULE_SCHEMA_VERSION = "report_schedule.v1"
LOCAL_TIMEZONE = "Asia/Shanghai"
DELIVERY_NONE = "none"
DELIVERY_FEISHU_APP = "feishu_app"
DELIVERY_CHANNELS = (DELIVERY_NONE, DELIVERY_FEISHU_APP)
_WEEKDAY_LABELS = {
    1: "周一",
    2: "周二",
    3: "周三",
    4: "周四",
    5: "周五",
    6: "周六",
    7: "周日",
}
_TIMEZONE_SHORT = {"Asia/Shanghai": "CST"}


@dataclass(frozen=True)
class ReportSchedule:
    """One declared run time (a report may have several, e.g. daily + weekly)."""

    frequency: str  # daily | weekly | tick | manual
    trigger: str  # launchd | systemd | tick | manual
    timezone: str = LOCAL_TIMEZONE
    hour: int | None = None
    minute: int | None = None
    weekday: int | None = None  # ISO 1=Monday .. 7=Sunday
    launchd_label: str | None = None
    plist_path: str | None = None
    command: tuple[str, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        if self.frequency not in {"daily", "weekly", "tick", "manual"}:
            raise ValueError(f"unsupported report frequency: {self.frequency}")
        if self.trigger not in {"launchd", "systemd", "tick", "manual"}:
            raise ValueError(f"unsupported report trigger: {self.trigger}")
        if self.frequency in {"daily", "weekly"}:
            if self.hour is None or self.minute is None:
                raise ValueError(f"{self.frequency} schedule requires hour and minute")
            if not 0 <= int(self.hour) <= 23 or not 0 <= int(self.minute) <= 59:
                raise ValueError(f"invalid schedule time: {self.hour}:{self.minute}")
        if self.frequency == "weekly" and self.weekday is None:
            raise ValueError("weekly schedule requires weekday")
        if self.weekday is not None and not 1 <= int(self.weekday) <= 7:
            raise ValueError("weekday must use ISO numbering 1=Monday .. 7=Sunday")

    @property
    def label(self) -> str:
        if self.frequency == "daily":
            return f"每日 {int(self.hour):02d}:{int(self.minute):02d} {_timezone_name(self.timezone)}"
        if self.frequency == "weekly":
            return (
                f"每{_WEEKDAY_LABELS[int(self.weekday)]} "
                f"{int(self.hour):02d}:{int(self.minute):02d} {_timezone_name(self.timezone)}"
            )
        if self.frequency == "tick":
            return "tick 触发"
        return "手动"

    def launchd_interval(self) -> dict[str, int] | None:
        if self.frequency == "daily":
            return {"Hour": int(self.hour), "Minute": int(self.minute)}
        if self.frequency == "weekly":
            # launchd accepts 0 and 7 for Sunday; emit 0 for consistency.
            weekday = 0 if int(self.weekday) == 7 else int(self.weekday)
            return {"Weekday": weekday, "Hour": int(self.hour), "Minute": int(self.minute)}
        return None


@dataclass(frozen=True)
class RetentionPolicy:
    """Declared retention for reports owned by the report center.

    This PR records the policy but does not delete artifacts automatically;
    deletion remains an explicit operator action.
    """

    keep_count: int | None
    keep_days: int | None = None
    note: str = ""


@dataclass(frozen=True)
class LegacyProbe:
    """How to read the last run of a report that still lives outside the center."""

    kind: str  # json_object | jsonl_tail | daily_brief | close_advice
    path: str | None = None
    stamp_fields: tuple[str, ...] = ()
    status_field: str = "status"


@dataclass(frozen=True)
class ReportDefinition:
    report_id: str
    display_name: str
    description: str
    schedules: tuple[ReportSchedule, ...]
    run_entry: str
    render_entries: tuple[str, ...]
    state_dir: str
    config_keys: tuple[str, ...]
    migrated: bool
    retention: RetentionPolicy
    delivery: str = DELIVERY_NONE
    legacy_probe: LegacyProbe | None = None
    plist_template: str | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.report_id):
            raise ValueError(f"invalid report_id: {self.report_id!r}")
        if not self.schedules:
            raise ValueError(f"{self.report_id}: at least one schedule is required")
        if self.delivery not in DELIVERY_CHANNELS:
            raise ValueError(f"{self.report_id}: unsupported report delivery channel: {self.delivery!r}")
        if not self.migrated and self.legacy_probe is None:
            raise ValueError(f"{self.report_id}: legacy report requires a legacy_probe")


def _timezone_name(timezone: str) -> str:
    return _TIMEZONE_SHORT.get(timezone, timezone)


def _plist_exists(plist_path: str | None, *, home: Path | None) -> bool | None:
    if not plist_path:
        return None
    expanded = Path(plist_path).expanduser()
    if home is not None and plist_path.startswith("~/"):
        expanded = Path(home) / plist_path[2:]
    try:
        return expanded.is_file()
    except OSError:
        return False


REPORT_REGISTRY: tuple[ReportDefinition, ...] = (
    ReportDefinition(
        report_id="daily_brief",
        display_name="每日决策简报",
        description="按账户与市场生成的 Daily Decision Brief，由 tick 在交易时段内触发。",
        schedules=(
            ReportSchedule(
                frequency="tick",
                trigger="tick",
                note="每个交易日的 tick 扫描生成；固定报告点见简报自身的 next_fixed_report_at_utc。",
            ),
        ),
        run_entry="src/application/daily_decision_brief_service.py::assemble_daily_decision_briefs",
        render_entries=(
            "src/application/daily_decision_brief_renderer.py",
            "src/application/daily_decision_brief_repository.py",
        ),
        state_dir="<runtime_root>/output_accounts/<account>/state",
        config_keys=("notifications.enabled",),
        migrated=False,
        retention=RetentionPolicy(
            keep_count=None,
            note="由现有 tick / 通知状态与 output_accounts 清理策略管理；报告中心不接管。",
        ),
        legacy_probe=LegacyProbe(kind="daily_brief"),
        notes="投递状态、恢复补报与决策修订语义强耦合，迁移风险最高。",
    ),
    ReportDefinition(
        report_id="close_advice",
        display_name="平仓建议报告",
        description="持仓剩余收益与止盈评估报告，在平仓窗口由 tick 触发。",
        schedules=(
            ReportSchedule(
                frequency="tick",
                trigger="tick",
                note="交易时段内由 tick 的平仓建议阶段生成；不单独配置 launchd 定点。",
            ),
        ),
        run_entry="src/application/close_advice_runner.py::run_close_advice",
        render_entries=(
            "src/application/close_advice_report_manifest.py",
            "src/application/close_advice_runner.py",
        ),
        state_dir="<runtime_root>/output_accounts/<account>/reports",
        config_keys=("close_advice.enabled",),
        migrated=False,
        retention=RetentionPolicy(
            keep_count=None,
            note="随账户报告目录清理；报告中心不接管。",
        ),
        legacy_probe=LegacyProbe(kind="close_advice"),
        notes="与 tick 事务、报价封存和通知路径强耦合，不能只搬产物目录。",
    ),
    ReportDefinition(
        report_id="code_health",
        display_name="代码健康每日巡检",
        description="本机 launchd 每天运行的代码健康审查、修复与交付流程。",
        schedules=(
            ReportSchedule(
                frequency="daily",
                trigger="launchd",
                hour=9,
                minute=17,
                launchd_label="local.options-monitor.code-health-review",
                plist_path="~/Library/LaunchAgents/local.options-monitor.code-health-review.plist",
                note="现有 launchd job 保持原样，不在本框架内重写。",
            ),
        ),
        run_entry="scripts/code_health_review.py",
        render_entries=("scripts/code_health_delivery.py",),
        state_dir="~/Library/Application Support/options-monitor/code-health-review",
        config_keys=(),
        migrated=False,
        retention=RetentionPolicy(
            keep_count=None,
            note="由 code-health 自身的 runs/ 与状态目录策略管理。",
        ),
        legacy_probe=LegacyProbe(
            kind="json_object",
            path="~/Library/Application Support/options-monitor/code-health-review/last-run.json",
            stamp_fields=("finished_at", "started_at"),
        ),
        notes="状态目录是本机运维产物，不在 OM runtime root 下。",
    ),
    ReportDefinition(
        report_id="code_entropy",
        display_name="代码熵评分卡",
        description="每日与每周代码熵扫描、评分和 issue 发布。",
        schedules=(
            ReportSchedule(
                frequency="daily",
                trigger="launchd",
                hour=4,
                minute=20,
                launchd_label="com.options-monitor.code-entropy.daily",
                plist_path="~/Library/LaunchAgents/com.options-monitor.code-entropy.daily.plist",
                note="mode=daily，预算 900 秒。",
            ),
            ReportSchedule(
                frequency="weekly",
                trigger="launchd",
                weekday=7,
                hour=1,
                minute=20,
                launchd_label="com.options-monitor.code-entropy.weekly",
                plist_path="~/Library/LaunchAgents/com.options-monitor.code-entropy.weekly.plist",
                note="mode=weekly，预算 5400 秒。",
            ),
        ),
        run_entry="scripts/code_entropy_review.py",
        render_entries=("scripts/code_entropy_scan.py",),
        state_dir="~/Library/Application Support/options-monitor/code-entropy-review",
        config_keys=(),
        migrated=False,
        retention=RetentionPolicy(
            keep_count=None,
            note="由 scorecards.jsonl 与状态目录自身管理；报告中心不接管。",
        ),
        legacy_probe=LegacyProbe(
            kind="jsonl_tail",
            path="~/Library/Application Support/options-monitor/code-entropy-review/scorecards.jsonl",
            stamp_fields=("finished_at", "started_at"),
        ),
        notes="两个现有 launchd job 保持原样。",
    ),
    ReportDefinition(
        report_id="config_review",
        display_name="周度配置清单报告",
        description="每周读取用户配置中的行权价边界，结合最新现价观察输出中文清单。",
        schedules=(
            ReportSchedule(
                frequency="weekly",
                trigger="systemd",
                weekday=7,
                hour=20,
                minute=0,
                command=("om", "report", "run", "config_review", "--trigger", "schedule"),
                note="远端 options-monitor-report-config-review.timer；确认投递后才撤本机旧 launchd。",
            ),
        ),
        run_entry="src/application/reports/config_review.py::build_config_review",
        render_entries=("src/application/reports/config_review.py",),
        state_dir="<runtime_root>/reports/config_review",
        config_keys=(),
        migrated=True,
        delivery=DELIVERY_FEISHU_APP,
        retention=RetentionPolicy(
            keep_count=None,
            note="远端周报追加保存全部运行与投递回执，不自动删除；清理需另行安排。",
        ),
        notes="默认读取 runtime config 与 output_runs 冻结行情；--configs-root 仅显式本地使用，缺失/过期行情明确标注。",
    ),
)

REPORTS_BY_ID: dict[str, ReportDefinition] = {item.report_id: item for item in REPORT_REGISTRY}


def report_definition(report_id: str) -> ReportDefinition | None:
    return REPORTS_BY_ID.get(str(report_id or "").strip())


def schedule_payload(
    *,
    generated_at_utc: str | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Export the schedule description used by plist generation and inspection."""

    generated = generated_at_utc or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    reports: list[dict[str, Any]] = []
    for definition in REPORT_REGISTRY:
        entries: list[dict[str, Any]] = []
        for schedule in definition.schedules:
            entry: dict[str, Any] = {
                "frequency": schedule.frequency,
                "trigger": schedule.trigger,
                "timezone": schedule.timezone,
                "label": schedule.label,
                "hour": schedule.hour,
                "minute": schedule.minute,
                "weekday": schedule.weekday,
                "note": schedule.note,
            }
            interval = schedule.launchd_interval()
            if schedule.launchd_label:
                entry["launchd"] = {
                    "label": schedule.launchd_label,
                    "plist_path": schedule.plist_path,
                    "plist_present": _plist_exists(schedule.plist_path, home=home),
                    "start_calendar_interval": interval,
                }
            if schedule.command:
                entry["command"] = list(schedule.command)
            entries.append(entry)
        reports.append(
            {
                "report_id": definition.report_id,
                "display_name": definition.display_name,
                "migrated": definition.migrated,
                "config_keys": list(definition.config_keys),
                "state_dir": definition.state_dir,
                "retention": {
                    "keep_count": definition.retention.keep_count,
                    "keep_days": definition.retention.keep_days,
                    "note": definition.retention.note,
                },
                "plist_template": definition.plist_template,
                "entries": entries,
            }
        )
    return {
        "schema_version": SCHEDULE_SCHEMA_VERSION,
        "generated_at_utc": generated,
        "timezone": LOCAL_TIMEZONE,
        "weekday_convention": "1=Monday .. 7=Sunday; launchd emits 0 for Sunday",
        "reports": reports,
    }


__all__ = [
    "LOCAL_TIMEZONE",
    "REPORT_REGISTRY",
    "ReportDefinition",
    "ReportSchedule",
    "RetentionPolicy",
    "LegacyProbe",
    "report_definition",
    "schedule_payload",
]
