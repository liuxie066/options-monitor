from __future__ import annotations

import sys
from typing import Any, Callable

from src.application.secret_store import credential_specs


def command_guide() -> str:
    return """Options Monitor (om)

首次安装
  om setup init                       配置一个账户及标的，再逐项选择通知、Bot 和常驻服务
  富途 OpenAPI/OpenD                  安装并登录 OpenD；用 om accounts edit 核对端点，om doctor 检查连接
  om settings doctor --format text     检查普通设置
  om secrets status --format text      查看脱敏的凭证状态；用 om secrets set 录入
  通知通道（需要通知时）               选择 Feishu App 或 WeChat ClawBot；用 om channel status 检查
  Bot LLM（需要 Bot 问答时）           om bot configure；终端隐藏录入密钥
  om setup check --format text         检查离线配置和安装条件；不验证外部连接或投递

日常管理（按任务选择）
  om status                          查看运行状态；按需指定 --config-key us/hk
  om daily-brief latest              查看已有决策简报
  om symbols list                    查看单市场标的；双市场时指定 --market
  om symbols add --help              新增标的；编辑和删除见 om symbols --help
  om accounts list                   查看账户映射；新增、编辑、删除见 om accounts --help
  om channel configure               配置或关闭通知通道
  om bot configure                   配置或关闭 Bot；模型管理用 om bot model
  om holdings configure              开启全局持仓风险中的可选 Holdings 来源
  om close-advice configure           开关平仓建议，保留历史结果
  om wheel --help                    Wheel 激活与策略确认；Combo 用 symbols edit 的高级字段
  om doctor --help                   排查运行问题
  om run --help                      手动运行任务
  om service --help                  管理服务
  om update --help                   检查或升级已发布版本

高级与完整命令
  om config --help                   构建、校验和解释配置；脚本化初始化
  om settings --help                 查看设置来源
  om help all                        查看全部顶层命令

结构化工具给外部 Agent/脚本使用：om-agent spec；om-agent run --tool <name> --input-json '<json>'。
om bot 是 OM 自身的消息与问答功能；assistant 保留兼容。Feishu 接入用 om channel feishu，inbound 保留兼容。

在交互式终端直接运行 om，可按首次安装或日常管理选择任务；写入操作遵循各命令的预览和确认要求。
"""


def interactive_home(run: Callable[[list[str]], int], *, input_fn: Callable[[str], str] = input) -> int:
    from src.application.agent_tool_contracts import AgentToolError
    from src.interfaces.cli.journeys import daily_management

    while True:
        sys.stdout.write("\nOptions Monitor\n  1  首次安装或继续引导\n  2  日常管理\n  3  命令与高级功能\n  0  退出\n")
        try:
            choice = input_fn("请选择：").strip()
            if choice == "0":
                return 0
            if choice == "1":
                run(["setup", "init"])
            elif choice == "2":
                daily_management(run, input_fn=input_fn)
            elif choice == "3":
                sys.stdout.write("\n" + command_guide())
            else:
                sys.stdout.write("请选择菜单中的编号。\n")
        except AgentToolError as exc:
            sys.stdout.write(f"{exc}\n")
        except (EOFError, KeyboardInterrupt):
            sys.stdout.write("\n")
            return 0


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
