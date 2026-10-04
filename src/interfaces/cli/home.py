from __future__ import annotations

import sys
from typing import Any, Callable

from src.application.secret_store import credential_specs


def command_guide() -> str:
    return """Options Monitor (om)

首次安装
  om setup init                       选择市场、账户、标的、策略和行权价；预览后确认
  富途 OpenAPI/OpenD                  安装并登录 OpenD；用 om accounts edit 核对端点，om doctor 检查连接
  om settings doctor --format text     检查普通设置
  om secrets status --format text      查看脱敏的凭证状态；用 om secrets set 录入
  通知通道（需要通知时）               选择 Feishu App 或 WeChat ClawBot；用 om channel status 检查
  Bot LLM（需要 Bot 问答时）           用 om assistant model catalog 选模型、录入凭证并检查
  om setup check --format text         检查离线配置和安装条件；不验证外部连接或投递

日常管理（按任务选择）
  om status                          查看运行状态；按需指定 --config-key us/hk
  om daily-brief latest              查看已有决策简报
  om symbols list                    查看单市场标的；双市场时指定 --market
  om symbols add --help              新增标的；编辑和删除见 om symbols --help
  om accounts edit --help            调整账户
  om doctor --help                   排查运行问题
  om run --help                      手动运行任务
  om service --help                  管理服务
  om update --help                   检查或升级已发布版本

高级与完整命令
  om config --help                   构建、校验和解释配置；脚本化初始化
  om settings --help                 查看设置来源
  om help all                        查看全部顶层命令

结构化工具给外部 Agent/脚本使用：om-agent spec；om-agent run --tool <name> --input-json '<json>'。
om assistant / om bot 是 OM 自身的消息与 Bot 功能，不是 Tool Gateway。

在交互式终端直接运行 om，可按首次安装或日常管理选择任务；写入操作遵循各命令的预览和确认要求。
"""


def interactive_home(run: Callable[[list[str]], int], *, input_fn: Callable[[str], str] = input) -> int:
    first_run_guides = {
        "2": "富途 OpenAPI/OpenD：按富途官方步骤安装并登录 OpenD；用 om accounts edit --help 核对账户 ID、host 和 port；按市场运行 om doctor --config-key us 或 om doctor --config-key hk 检查连接。OM 不负责启动 OpenD。\n",
        "5": "通知通道：需要通知时选择 Feishu App 或 WeChat ClawBot。WeChat 用 om channel wechat-clawbot connect 绑定；Feishu 配置见 CONFIGURATION_GUIDE.md，密钥用 om secrets set 录入。用 om channel status 检查本地状态；它不证明消息已送达。\n",
        "6": "Bot LLM：需要 Bot 问答时，先运行 om assistant model catalog，再分别查看 om assistant model add --help 和 om assistant model use --help；用 om secrets set 录入对应 API key，最后运行 om assistant model check --active；检查不调用模型。\n",
    }
    sections = {
        "1": ("首次安装", {
            "1": ("初始化配置（预览后确认）", ["setup", "init"]),
            "2": ("接入富途 OpenAPI/OpenD（步骤）", None),
            "3": ("检查普通设置（只读）", ["settings", "doctor", "--format", "text"]),
            "4": ("查看凭证状态（脱敏）", ["secrets", "status", "--format", "text"]),
            "5": ("配置通知通道（需要通知时）", None),
            "6": ("配置 Bot LLM（需要 Bot 问答时）", None),
            "7": ("检查离线配置和安装条件（只读）", ["setup", "check", "--format", "text"]),
        }),
        "2": ("日常管理", {
            "1": ("查看运行状态", ["status"]),
            "2": ("查看最新决策简报", ["daily-brief", "latest"]),
            "3": ("运行诊断", ["doctor"]),
            "4": ("查看监控标的", ["symbols", "list"]),
        }),
    }
    section: str | None = None
    while True:
        if section is None:
            sys.stdout.write("\nOptions Monitor\n  1  首次安装\n  2  日常管理\n  3  命令与高级功能\n  0  退出\n")
        else:
            title, actions = sections[section]
            sys.stdout.write(f"\n{title}\n")
            for key, (label, _) in actions.items():
                sys.stdout.write(f"  {key}  {label}\n")
            sys.stdout.write("  0  返回\n")
        try:
            choice = input_fn("请选择：").strip()
        except (EOFError, KeyboardInterrupt):
            sys.stdout.write("\n")
            return 0
        if choice == "0":
            if section is None:
                return 0
            section = None
        elif section is None and choice in sections:
            section = choice
        elif section is None and choice == "3":
            sys.stdout.write("\n" + command_guide())
        elif section is not None and choice in sections[section][1]:
            command = sections[section][1][choice][1]
            if section == "1" and command is None:
                sys.stdout.write("\n" + first_run_guides[choice])
                continue
            if command in (["status"], ["doctor"], ["symbols", "list"]):
                try:
                    market = input_fn("市场 [us/hk]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    sys.stdout.write("\n")
                    return 0
                if market not in {"us", "hk"}:
                    sys.stdout.write("请输入 us 或 hk。\n")
                    continue
                option = "--market" if command[0] == "symbols" else "--config-key"
                command = [*command, option, market]
            sys.stdout.write("\n")
            run(command)
        else:
            sys.stdout.write("请选择菜单中的编号。\n")


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
