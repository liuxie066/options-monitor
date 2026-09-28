from __future__ import annotations

import sys
from typing import Any, Callable

from src.application.secret_store import credential_specs


def command_guide() -> str:
    return """Options Monitor (om)

从这里开始
  om setup init                     预览并确认首次配置
  om setup check --format text       检查首次运行条件和下一步
  om settings doctor --format text   检查普通设置
  om secrets status --format text    查看脱敏的凭证状态

配置
  om setup init --dry-run            不写文件，只看首次配置预览
  om config init --help              使用完整的初始配置参数
  om config validate --help          校验配置
  om secrets set --help              用隐藏终端提示输入凭证

运行与诊断
  om doctor --help                   检查运行依赖
  om status --help                   查看运行状态
  om run --help                      显式运行任务

高级功能
  om config --help                   构建、解释和编辑配置
  om settings --help                 查看设置来源
  om service --help                  渲染和管理服务
  om --help                          查看全部命令

在交互式终端直接运行 om，会打开导航菜单；首次配置写入前会预览并要求确认。
"""


def interactive_home(run: Callable[[list[str]], int], *, input_fn: Callable[[str], str] = input) -> int:
    actions = {
        "1": ["setup", "check", "--format", "text"],
        "2": ["settings", "doctor", "--format", "text"],
        "3": ["secrets", "status", "--format", "text"],
        "4": ["setup", "init"],
    }
    while True:
        sys.stdout.write(
            "\nOptions Monitor\n"
            "  1  首次运行检查（只读）\n"
            "  2  普通设置检查（只读）\n"
            "  3  凭证状态（脱敏）\n"
            "  4  初始化配置（预览后确认）\n"
            "  5  命令与高级功能\n"
            "  0  退出\n"
        )
        try:
            choice = input_fn("请选择：").strip()
        except (EOFError, KeyboardInterrupt):
            sys.stdout.write("\n")
            return 0
        if choice == "0":
            return 0
        if choice == "5":
            sys.stdout.write("\n" + command_guide())
        elif choice in actions:
            sys.stdout.write("\n")
            run(actions[choice])
        else:
            sys.stdout.write("请输入 0–5。\n")


def render_setup_check(data: dict[str, Any]) -> str:
    summary = data.get("summary") or {}
    lines = [
        "首次运行离线配置检查：" + ("基本条件通过" if summary.get("ok") else "需要处理问题"),
        "可选 Bot：" + ("就绪" if summary.get("bot_ready") else "未就绪或未配置"),
        f"错误：{summary.get('error_count', 0)} · 提醒：{summary.get('warning_count', 0)}",
    ]
    for item in data.get("checks") or []:
        if item.get("status") in {"error", "warn"}:
            lines.append(f"  [{item['status']}] {item['name']}: {item['message']}")
            if item.get("hint"):
                lines.append(f"    {item['hint']}")
    steps = data.get("next_steps") or []
    if steps:
        lines.append("下一步（按需要执行）：")
        lines.extend(f"  {index}. {step}" for index, step in enumerate(steps, 1))
    lines.append("完整结果：om setup check")
    return "\n".join(lines) + "\n"


def render_settings_doctor(data: dict[str, Any]) -> str:
    summary = data.get("summary") or {}
    lines = [
        "普通设置检查",
        f"错误：{summary.get('error_count', 0)} · 提醒：{summary.get('warning_count', 0)}",
    ]
    for item in data.get("checks") or []:
        if item.get("status") in {"error", "warn"}:
            lines.append(f"  [{item['status']}] {item['name']}: {item['message']}")
    lines.extend(("查看来源：om settings inspect", "解释单项设置：om settings explain --key <name>"))
    return "\n".join(lines) + "\n"


def render_credential_readiness(data: dict[str, Any]) -> str:
    summary = data.get("summary") or {}
    purposes = {spec.logical_name: spec.purpose for spec in credential_specs()}
    lines = [f"凭证状态（后端：{summary.get('backend', 'unknown')}；不显示值）"]
    for item in data.get("credentials") or []:
        name = str(item.get("logical_name") or "")
        state = "已配置" if item.get("configured") else "未配置"
        lines.append(f"  [{state}] {name} — {purposes.get(name, '')}")
    lines.extend(("录入凭证：om secrets set <name>（终端隐藏输入）", "完整结果：om secrets status"))
    return "\n".join(lines) + "\n"
