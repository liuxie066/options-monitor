"""Human entry for the fixed official optional capabilities."""
from __future__ import annotations

import argparse
import json
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.plugin_distribution import install_plugin, list_plugins, uninstall_plugin
from src.application.plugin_runtime import PLUGINS


def add_plugin_commands(subparsers: Any) -> None:
    parser = subparsers.add_parser("plugins", help="官方插件：查看、安装和卸载；行情至少一个，券商可不安装")
    sub = parser.add_subparsers(dest="plugin_command", required=True)
    listing = sub.add_parser("list", help="离线查看此 OM 代码实例的插件安装状态")
    listing.add_argument("--format", choices=("text", "json"), default="text")
    for action in ("install", "uninstall"):
        command = sub.add_parser(action, help="默认预览；--confirm 才修改插件文件，不修改配置或服务")
        command.add_argument("plugin", choices=tuple(PLUGINS))
        mode = command.add_mutually_exclusive_group()
        mode.add_argument("--confirm", action="store_true")
        mode.add_argument("--dry-run", action="store_true")
        command.add_argument("--format", choices=("text", "json"), default="text")


def handle_plugin_command(args: argparse.Namespace) -> int:
    try:
        if args.plugin_command == "list":
            result = list_plugins()
        else:
            fn = install_plugin if args.plugin_command == "install" else uninstall_plugin
            result = fn(args.plugin, confirm=bool(args.confirm))
    except (ValueError, OSError, TimeoutError) as exc:
        raise AgentToolError(code="PLUGIN_ERROR", message=str(exc),
                             hint="检查所用 OM 版本的官方 Release 与插件清单；不要向运行包内置 Python 手动安装") from exc
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.plugin_command == "list":
        print(f"OM {result['om_version']} · {result['repo_root']}")
        labels = {"installed": "已安装", "not_installed": "未安装", "incompatible": "版本不兼容", "invalid": "安装损坏"}
        for row in result["plugins"]:
            print(f"{row['id']}: {labels[row['status']]} · {row.get('version') or '-'} · {row.get('source') or '-'}")
        print(result["note"])
    else:
        print(f"{result['plugin']}: {result['status']}")
        if result.get("directory"):
            print(f"插件目录：{result['directory']}")
        if result.get("asset"):
            print(f"官方资产：{result['release_tag']}/{result['asset']}\nSHA256：{result['sha256']}")
        if result.get("status") == "preview":
            print(f"确认执行：om plugins {args.plugin_command} {args.plugin} --confirm")
        print(result.get("note", ""))
    return 0 if result.get("ok") else 2
