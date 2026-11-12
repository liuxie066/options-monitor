from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unicodedata
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.reports.legacy import observe_legacy_last_run
from src.application.reports.delivery import delivery_failed
from src.application.reports.registry import (
    REPORT_REGISTRY,
    ReportDefinition,
    report_definition,
    schedule_payload,
)
from src.application.reports.runner import ReportRunContext, execute_report
from src.application.reports.store import get_run, list_runs, read_run_artifact
from src.application.runtime_paths import resolve_runtime_root


_RUN_STATUS_TEXT = {"succeeded": "成功", "failed": "失败", "running": "运行中"}
_MISSING = object()


def add_report_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "report",
        help="报告中心：列出报告、手动运行、查看历史与调度描述",
    )
    report_sub = parser.add_subparsers(dest="report_command", required=True)

    listing = report_sub.add_parser("list", help="列出报告类型、频率、开关状态与上次运行")
    listing.add_argument("--runtime-root", default=None)
    listing.add_argument("--configs-root", default=None, help="显式使用本地 user.*.json；默认读取 runtime config")
    listing.add_argument("--json", action="store_true")

    run = report_sub.add_parser("run", help="手动运行一次报告")
    run.add_argument("report_id")
    run.add_argument("--dry-run", action="store_true", help="只生成并打印，不写运行目录或历史")
    run.add_argument("--no-deliver", action="store_true", help="只生成本地报告，不投递到外部渠道")
    run.add_argument("--runtime-root", default=None)
    run.add_argument("--configs-root", default=None, help="显式使用本地 user.*.json；默认读取 runtime config")
    run.add_argument("--prices-file", default=None, help="显式 JSON 价格文件；默认读取 output_runs 最近成功扫描的冻结行情")
    run.add_argument("--candidate-evidence-file", default=None, help="本期候选拒绝证据投影；默认读取 runtime 封存快照")
    run.add_argument("--trigger", choices=("manual", "schedule"), default="manual", help=argparse.SUPPRESS)
    run.add_argument("--json", action="store_true")

    history = report_sub.add_parser("history", help="查看报告最近 N 次运行")
    history.add_argument("report_id")
    history.add_argument("--limit", type=int, default=10)
    history.add_argument("--runtime-root", default=None)
    history.add_argument("--json", action="store_true")

    show = report_sub.add_parser("show", help="查看某次运行与产物")
    show.add_argument("report_id")
    show.add_argument("run_id")
    show.add_argument("--artifact", default=None, help="产物文件名；默认 markdown 报告")
    show.add_argument("--runtime-root", default=None)
    show.add_argument("--json", action="store_true")

    schedule = report_sub.add_parser("schedule", help="导出报告调度描述，供 plist 生成或人工核对")
    schedule.add_argument("--format", choices=("text", "json"), default="text")


def handle_report_command(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path],
) -> int:
    command = str(args.report_command or "")
    repo_root = repo_base_fn()
    runtime_root = resolve_runtime_root(
        repo_root=repo_root,
        runtime_root=getattr(args, "runtime_root", None),
    ).runtime_root
    configs_root = _configs_root(args, repo_root)
    if command == "list":
        return _handle_list(runtime_root=runtime_root, configs_root=configs_root, as_json=bool(args.json))
    if command == "run":
        return _handle_run(
            args,
            runtime_root=runtime_root,
            configs_root=configs_root,
        )
    if command == "history":
        return _handle_history(args, runtime_root=runtime_root)
    if command == "show":
        return _handle_show(args, runtime_root=runtime_root)
    if command == "schedule":
        return _handle_schedule(args)
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported report command: {command}")


def _handle_list(*, runtime_root: Path, configs_root: Path | None, as_json: bool) -> int:
    system_config = _load_system_config(configs_root)
    report_rows: list[dict[str, Any]] = []
    for definition in REPORT_REGISTRY:
        switches = _switch_rows(definition, system_config)
        last_run = _last_run_view(definition, runtime_root=runtime_root)
        report_rows.append(
            {
                "report_id": definition.report_id,
                "display_name": definition.display_name,
                "description": definition.description,
                "schedules": [schedule.label for schedule in definition.schedules],
                "migrated": definition.migrated,
                "delivery": definition.delivery,
                "config_keys": list(definition.config_keys),
                "switches": switches,
                "state_dir": definition.state_dir,
                "run_entry": definition.run_entry,
                "last_run": last_run,
            }
        )
    if as_json:
        return _print(
            {
                "ok": True,
                "runtime_root": str(runtime_root),
                "configs_root": str(configs_root),
                "reports": report_rows,
            }
        )
    rows = [
        [
            row["report_id"],
            row["display_name"],
            "；".join(row["schedules"]),
            _switches_text(row),
            "是" if row["migrated"] else "否",
            row["delivery"],
            row["last_run"]["at_utc"] if row["last_run"] else "—",
            _last_run_text(row["last_run"]),
        ]
        for row in report_rows
    ]
    sys.stdout.write(
        _render_table(
            ["REPORT_ID", "名称", "频率", "开关", "迁移", "投递", "上次运行(UTC)", "结果"],
            rows,
        )
        + "\n"
    )
    return 0


def _handle_run(args: argparse.Namespace, *, runtime_root: Path, configs_root: Path | None) -> int:
    definition = _definition_or_raise(args.report_id)
    prices_file = Path(args.prices_file).expanduser().resolve() if args.prices_file else None
    evidence_arg = getattr(args, "candidate_evidence_file", None)
    evidence_file = Path(evidence_arg).expanduser().resolve() if evidence_arg else None
    command = ["om", "report", "run", definition.report_id]
    if args.dry_run:
        command.append("--dry-run")
    if args.no_deliver:
        command.append("--no-deliver")
    if prices_file is not None:
        command.extend(["--prices-file", str(prices_file)])
    if evidence_file is not None:
        command.extend(["--candidate-evidence-file", str(evidence_file)])
    if args.trigger != "manual":
        command.extend(["--trigger", args.trigger])
    result = execute_report(
        definition,
        ReportRunContext(
            runtime_root=runtime_root,
            configs_root=configs_root,
            prices_file=prices_file,
            candidate_evidence_file=evidence_file,
            now=datetime.now(timezone.utc),
        ),
        command=command,
        trigger=args.trigger,
        dry_run=bool(args.dry_run),
        deliver=not args.no_deliver,
    )
    if args.dry_run:
        if args.json:
            return _print(result)
        markdown = next(
            (item["content"] for item in result.get("artifacts") or [] if item.get("name") == "report.md"),
            None,
        )
        if markdown is None:
            markdown = next((item["content"] for item in result.get("artifacts") or []), "")
        sys.stdout.write(markdown if markdown.endswith("\n") else markdown + "\n")
        return 0
    if args.json:
        _print(result)
        if delivery_failed(result.get("delivery")):
            sys.stderr.write(f"投递失败：{(result.get('delivery') or {}).get('message') or '未知错误'}\n")
            return 1
        return 0
    artifacts = "、".join(
        f"{item['name']}（{_human_bytes(int(item.get('bytes') or 0))}）" for item in result.get("artifacts") or []
    )
    delivery = result.get("delivery")
    sys.stdout.write(
        "\n".join(
            [
                f"{definition.report_id}（{definition.display_name}）运行成功",
                f"run_id：{result['run_id']}",
                f"目录：{result['run_dir']}",
                f"产物：{artifacts or '无'}",
                f"摘要：{_summary_text(result.get('summary') or {})}",
                f"投递：{_delivery_text(delivery)}",
            ]
        )
        + "\n"
    )
    if delivery_failed(delivery):
        sys.stderr.write(f"投递失败：{(delivery or {}).get('message') or '未知错误'}\n")
        return 1
    return 0


def _handle_history(args: argparse.Namespace, *, runtime_root: Path) -> int:
    definition = _definition_or_raise(args.report_id)
    if args.limit <= 0:
        raise AgentToolError(code="INPUT_ERROR", message="--limit must be a positive integer")
    runs = list_runs(runtime_root, definition.report_id, limit=args.limit)
    if args.json:
        return _print({"ok": True, "report_id": definition.report_id, "runs": runs})
    if not runs:
        sys.stdout.write(f"{definition.report_id} 暂无运行记录。\n")
        return 0
    rows = []
    for run in runs:
        artifacts = "、".join(
            f"{item.get('name')}（{_human_bytes(int(item.get('bytes') or 0))}）"
            for item in run.get("artifacts") or []
            if isinstance(item, dict)
        )
        rows.append(
            [
                str(run.get("run_id") or ""),
                str(run.get("started_at_utc") or ""),
                _RUN_STATUS_TEXT.get(str(run.get("status") or ""), str(run.get("status") or "—")),
                artifacts or "无",
            ]
        )
    sys.stdout.write(_render_table(["RUN_ID", "开始时间(UTC)", "状态", "产物"], rows) + "\n")
    return 0


def _handle_show(args: argparse.Namespace, *, runtime_root: Path) -> int:
    definition = _definition_or_raise(args.report_id)
    run = get_run(runtime_root, definition.report_id, args.run_id)
    artifact = read_run_artifact(run, artifact_name=args.artifact)
    if args.json:
        return _print(
            {
                "ok": True,
                "report_id": definition.report_id,
                "run": run,
                "artifact": {
                    "name": artifact.name,
                    "path": str(artifact.path),
                    "sha256": artifact.record.get("sha256"),
                    "content": artifact.content,
                },
            }
        )
    content = artifact.content
    sys.stdout.write(content if content.endswith("\n") else content + "\n")
    return 0


def _handle_schedule(args: argparse.Namespace) -> int:
    payload = schedule_payload()
    if args.format == "json":
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 0
    lines = [
        f"报告调度描述（时区：{payload['timezone']}；{payload['weekday_convention']}）",
        "",
    ]
    for report in payload["reports"]:
        migration = "已迁移" if report["migrated"] else "未迁移"
        lines.append(f"- {report['report_id']}（{report['display_name']}，{migration}）")
        for entry in report["entries"]:
            launchd = entry.get("launchd")
            if launchd:
                present = "plist 已安装" if launchd.get("plist_present") else "plist 未安装"
                lines.append(f"  - {entry['label']} | launchd {launchd['label']}（{present}）")
            else:
                lines.append(f"  - {entry['label']} | {entry['trigger']}")
            if entry.get("note"):
                lines.append(f"    {entry['note']}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def _definition_or_raise(report_id: str) -> ReportDefinition:
    definition = report_definition(report_id)
    if definition is None:
        raise AgentToolError(
            code="REPORT_NOT_FOUND",
            message=f"unknown report: {report_id}",
            hint="Use `om report list` to inspect registered report ids.",
            details={"available": [item.report_id for item in REPORT_REGISTRY]},
        )
    return definition


def _configs_root(args: argparse.Namespace, repo_root: Path) -> Path | None:
    raw = getattr(args, "configs_root", None)
    return Path(raw).expanduser().resolve() if raw else None


def _load_system_config(configs_root: Path | None) -> dict[str, Any] | None:
    if configs_root is None:
        return None
    path = configs_root / "system.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _switch_rows(definition: ReportDefinition, system_config: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in definition.config_keys:
        values = _switch_values(system_config, key)
        if not values:
            rows.append({"key": key, "label": "未设置", "value": None})
            continue
        rendered = [(source, _switch_value_label(value)) for source, value in values]
        labels = {label for _, label in rendered}
        label = rendered[0][1] if len(labels) == 1 else "；".join(f"{source}={text}" for source, text in rendered)
        rows.append({"key": key, "label": label, "value": values[0][1] if len(values) == 1 else dict(values)})
    return rows


def _switch_values(system_config: dict[str, Any] | None, key: str) -> list[tuple[str, Any]]:
    if system_config is None:
        return []
    found: dict[str, Any] = {}
    defaults = system_config.get("defaults")
    if isinstance(defaults, dict):
        value = _dotted_get(defaults, key)
        if value is not _MISSING:
            found["默认"] = value
    markets = system_config.get("markets")
    if isinstance(markets, dict):
        for market, section in markets.items():
            if not isinstance(section, dict):
                continue
            value = _dotted_get(section, key)
            if value is not _MISSING:
                found[str(market).upper()] = value
    if found:
        return list(found.items())
    direct = _dotted_get(system_config, key)
    return [("root", direct)] if direct is not _MISSING else []


def _switch_value_label(value: Any) -> str:
    if isinstance(value, bool):
        return "开启" if value else "关闭"
    if isinstance(value, (dict, list)):
        return "已配置"
    return str(value)


def _dotted_get(payload: dict[str, Any] | None, key: str) -> Any:
    current: Any = payload
    for part in str(key or "").split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _switches_text(row: dict[str, Any]) -> str:
    switches = row.get("switches") or []
    if switches:
        return "；".join(f"{item['key']}={item['label']}" for item in switches)
    if not row.get("migrated"):
        return "launchd（无配置开关）"
    return "—"


def _last_run_view(definition: ReportDefinition, *, runtime_root: Path) -> dict[str, Any] | None:
    if definition.migrated:
        runs = list_runs(runtime_root, definition.report_id, limit=1)
        if not runs:
            return None
        run = runs[0]
        return {
            "at_utc": run.get("finished_at_utc") or run.get("started_at_utc"),
            "status": run.get("status"),
            "source": run.get("run_dir"),
            "detail": run.get("run_id"),
        }
    return observe_legacy_last_run(definition, runtime_root=runtime_root)


def _last_run_text(last_run: dict[str, Any] | None) -> str:
    if not last_run:
        return "—"
    status = str(last_run.get("status") or "—")
    status_text = _RUN_STATUS_TEXT.get(status, status)
    detail = str(last_run.get("detail") or "")
    if len(detail) > 48:
        detail = detail[:45] + "..."
    return f"{status_text}（{detail}）" if detail else status_text


def _summary_text(summary: dict[str, Any]) -> str:
    if not summary:
        return "—"
    markets = " / ".join(f"{market} {count}" for market, count in (summary.get("market_counts") or {}).items())
    parts = [
        f"标的 {summary.get('symbol_count', 0)}"
        + (f"（{markets}）" if markets else ""),
        f"现价 {summary.get('price_available', 0)}/{summary.get('symbol_count', 0)}",
        f"看跌上限 {summary.get('put_cap_configured', 0)}",
        f"看涨下限 {summary.get('call_floor_configured', 0)}",
        f"Combo {summary.get('combo_enabled', 0)}",
    ]
    return "；".join(parts)


def _delivery_text(delivery: dict[str, Any] | None) -> str:
    if not isinstance(delivery, dict):
        return "未执行"
    channel = delivery.get("channel") or "none"
    status = delivery.get("status")
    if status == "delivered":
        message_id = delivery.get("message_id")
        return f"{channel} 已送达" + (f"（message_id={message_id}）" if message_id else "")
    if status == "skipped":
        return f"{channel} 已跳过：{delivery.get('message') or delivery.get('reason') or '未知原因'}"
    if status == "failed":
        return f"{channel} 投递失败：{delivery.get('message') or delivery.get('reason') or '未知错误'}"
    return f"{channel} {status or '未执行'}"


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [
        max([_display_width(header)] + [_display_width(row[index]) for row in rows])
        for index, header in enumerate(headers)
    ]
    lines = ["  ".join(_pad(header, widths[index]) for index, header in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(_pad(value, widths[index]) for index, value in enumerate(row)).rstrip())
    return "\n".join(lines)


def _display_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1 for character in str(value))


def _pad(value: str, width: int) -> str:
    text = str(value)
    return text + " " * max(0, width - _display_width(text))


def _human_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB"):
        if size < 1024 or unit == "MiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} MiB"


def _print(payload: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0 if payload.get("ok", True) else 2


__all__ = ["add_report_commands", "handle_report_command"]
