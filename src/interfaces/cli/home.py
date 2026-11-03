from __future__ import annotations

import sys
from pathlib import Path
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
  om scan --help                     扫描机会；保留现有参数和输出
  om position-advice evaluate --help 主动评估持仓；默认人工展示，--format json 输出业务 JSON
  om symbols list                    查看单市场标的；双市场时指定 --market
  om symbols add --help              新增标的；编辑和删除见 om symbols --help
  om accounts list                   查看账户映射；新增、编辑、删除见 om accounts --help
  om channel configure               配置或关闭通知通道
  om bot configure                   配置或关闭 Bot；模型管理用 om bot model
  om holdings configure              开启全局持仓风险中的可选 Holdings 来源
  om close-advice configure           开关平仓建议，保留历史结果
  om plugins list                    查看官方可选插件；安装与卸载见 om plugins --help
  om wheel --help                    Wheel 激活与策略确认；Combo 用 symbols edit 的高级字段
  om doctor --help                   排查运行问题
  om run --help                      手动运行任务
  om service --help                  管理服务
  om update --help                   检查或升级已发布版本

高级与完整命令
  om config --help                   构建、校验和解释配置；脚本化初始化
  om settings --help                 查看设置来源
  om help all                        查看全部顶层命令

结构化工具给外部 Agent/脚本使用：om tools spec；om tools run --tool <name> --input-file request.json。
om-agent 的 spec/run/add-account/edit-account/remove-account 全部保留兼容，推荐 om tools。
om bot 是自然语言助手：om bot run --text "评价一下我的持仓"；om agent、om assistant 是 bot 的兼容别名。
Feishu 接入用 om channel feishu，inbound 保留兼容。

在交互式终端直接运行 om，可按首次安装或日常管理选择任务；写入操作遵循各命令的预览和确认要求。
"""


def _home_state() -> tuple[Path, dict[str, Any] | None]:
    from src.application.agent_tool_config import repo_base
    from src.application.agent_tools.runtime_status_impl import read_onboarding_status
    from src.application.config_yaml import load_yaml_config_file, resolve_yaml_config_path

    source = resolve_yaml_config_path(None, repo_root=repo_base())
    if not source.is_file():
        return source, None
    return source, read_onboarding_status(base=source.parent, document=load_yaml_config_file(source))


def _print_home_status(state: dict[str, Any]) -> None:
    print("\nOM")
    for scope in state["scopes"]:
        attempt = scope["attempt"]
        label = {"sealed": "已有封存结果", "attempted": "已尝试，终态未知",
                 "completed": "完成" if attempt.get("result_status") == "ready" else "完成，部分数据不可用",
                 "failed": "失败", "not_found": "尚未扫描", "unknown": "未知"}[attempt["status"]]
        print(f"  {scope['account']} · {scope['market']} · 最近扫描：{label}"
              + (f" · {attempt['as_of']}" if attempt.get("as_of") else ""))
        if attempt.get("reason"):
            print(f"    扫描证据：{attempt['reason']}")
        if scope["brief_available"]:
            old = "（较早结果，非最近尝试）" if attempt.get("run_id") != scope["brief_run_id"] else ""
            print(f"    可读简报数据时间：{scope['brief_time'] or '未知'}{old}")
        else:
            if scope["brief_reason"] == "not_found":
                print("    可读简报：暂无可读结果（尚未生成或来源已清理）")
            elif scope.get("brief_error") == "history_missing":
                print("    可读简报：暂无可读结果（历史来源不存在）"
                      + ("；已有扫描证据，请在其他管理中排查。" if attempt["status"] != "not_found" else ""))
            else:
                print(f"    可读简报：来源不可用（{scope['brief_reason']}）；可在其他管理中排查。")
    print("  自动运行：未知；可在其他管理中查看服务实际状态。")
    print("  1  查看决策简报\n  2  重新扫描\n  3  修改关注标的与策略边界\n  4  其他管理与可选功能\n  0  退出")


def interactive_home(run: Callable[[list[str]], int], *, input_fn: Callable[[str], str] = input) -> int:
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.agent_tool_config import repo_base
    from src.interfaces.cli.journeys import _instance_run, daily_management, manual_scan, manage_symbols

    while True:
        try:
            try:
                source, state = _home_state()
            except (AgentToolError, OSError, ValueError) as exc:
                print(f"实例配置或运行证据读取失败：{exc}；不会重建已有配置。")
                print("修复入口：om doctor --help；om config --help；om help all。")
                return 2
            if state is None:
                sys.stdout.write("\nOptions Monitor\n  1  首次安装或继续引导\n  2  日常管理\n  3  命令与高级功能\n  0  退出\n")
            else:
                _print_home_status(state)
            choice = input_fn("请选择：").strip()
            if choice == "0":
                return 0
            if state is not None:
                scoped_run = lambda argv: _instance_run(run, argv, source=source, repo_root=repo_base())
                if choice == "1":
                    scoped_run(["daily-brief", "latest"])
                elif choice == "2":
                    manual_scan(scoped_run, source, input_fn=input_fn)
                elif choice == "3":
                    manage_symbols(scoped_run, source, input_fn=input_fn)
                elif choice == "4":
                    daily_management(run, source=source, input_fn=input_fn)
                else:
                    print("请选择菜单中的编号。")
            elif choice == "1":
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
