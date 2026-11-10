"""Thin lazy bridge; registering/help never imports the optional plugin."""
from __future__ import annotations

import argparse
import importlib
import json

from src.application.agent_tool_contracts import AgentToolError
from src.application.plugin_runtime import PluginUnavailableError, require_plugin


def add_group_query_bot_commands(subparsers) -> None:
    parser = subparsers.add_parser("group-query-bot", help="可选群查询bot：接入验证与业务入口")
    sub = parser.add_subparsers(dest="group_query_bot_command", required=True)
    preflight = sub.add_parser("preflight", help="默认只校验配置；--confirm 才连接指定新飞书应用")
    preflight.add_argument("--config", required=True, help="预检专用 JSON 配置路径")
    preflight.add_argument("--confirm", action="store_true")
    serve = sub.add_parser("serve", help="运行群查询bot进程（飞书、模型与会话存储）")
    serve.add_argument("--config", required=True, help="bot 专用 JSON 配置路径")
    serve.add_argument("--confirm", action="store_true",
                       help="确认使用专用凭据连接真实飞书与模型")
    api_serve = sub.add_parser("api-serve", help="运行群查询只读业务 API 进程")
    api_serve.add_argument("--config", required=True, help="API 专用 JSON 配置路径")


def handle_group_query_bot_command(args: argparse.Namespace) -> int:
    command = getattr(args, "group_query_bot_command", None)
    try:
        require_plugin("group-query-bot")
        module = importlib.import_module("om_group_query_bot.cli")
        if command == "preflight":
            result = module.run_preflight(args.config, confirm=args.confirm)
        elif command == "serve":
            if not args.confirm:
                raise AgentToolError(
                    code="GROUP_QUERY_BOT_CONFIRM_REQUIRED",
                    message="serve 连接真实飞书与模型，需显式 --confirm",
                    hint="核对专用配置和凭据后加 --confirm")
            result = module.run_serve(args.config)
        elif command == "api-serve":
            result = module.run_api_serve(args.config)
        else:
            raise AgentToolError(code="GROUP_QUERY_BOT_UNKNOWN_COMMAND",
                                 message="未知的群查询bot命令")
    except PluginUnavailableError as exc:
        raise AgentToolError(code=exc.code, message="群查询bot插件未安装或版本不匹配",
                             hint="显式安装匹配版本的group-query-bot插件后重试") from exc
    except AgentToolError:
        raise
    except Exception as exc:
        # Operator errors and transport responses can include secrets; return a stable code only.
        raise AgentToolError(code="GROUP_QUERY_BOT_COMMAND_FAILED",
                             message="群查询bot命令未完成，请核对专用配置、凭据和接入条件") from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
