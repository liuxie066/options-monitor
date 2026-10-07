"""Terminal journeys built from the existing public configuration commands."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
from typing import Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_yaml import load_yaml_config_file, resolve_yaml_config_path
from src.interfaces.cli.command_environment import command_environment
from src.interfaces.cli.setup_ops import _default_setup_dir, run_setup_init

Run = Callable[[list[str]], int]
Input = Callable[[str], str]


def terminal_run(run: Run, argv: list[str]) -> int:
    """Standalone argparse facades must not exit the enclosing task menu."""
    try:
        return run(argv)
    except SystemExit as exc:
        if isinstance(exc.code, int) or exc.code is None:
            return int(exc.code or 0)
        print(exc.code)
        return 2


def _instance_run(run: Run, argv: list[str], *, source: Path, repo_root: Path) -> int:
    with command_environment([*argv, "--runtime-root", str(source.parent)], repo_root=repo_root):
        return terminal_run(run, argv)


def _yes(prompt: str, input_fn: Input) -> bool:
    return input_fn(prompt + " [y/N]: ").strip().lower() in {"y", "yes"}


def _choose(prompt: str, choices: tuple[str, ...], input_fn: Input, default: str = "") -> str:
    value = input_fn(f"{prompt} [{' / '.join(choices)}]{' [' + default + ']' if default else ''}: ").strip() or default
    if value not in choices:
        raise AgentToolError(code="INPUT_ERROR", message=f"请选择 {' / '.join(choices)}")
    return value


def preview_and_confirm(run: Run, argv: list[str], *, input_fn: Input = input,
                        service: bool = False) -> int:
    output = io.StringIO()
    with redirect_stdout(output):
        code = terminal_run(run, argv)
    print(output.getvalue(), end="")
    if code:
        return code
    try:
        payload = json.loads(output.getvalue())
        result = payload.get("data", payload)
        revision = result["preview_sha256"] if service else result["source_revision"]["before_sha256"]
    except (ValueError, KeyError, TypeError) as exc:
        raise AgentToolError(code="CONFIG_ERROR", message="预览缺少确认凭据，未执行写入") from exc
    if not _yes("确认执行以上变更", input_fn):
        print("已取消，本步骤未写入。")
        return 0
    if service:
        return terminal_run(run, [*argv, "--confirm", "--expected-preview-sha256", revision])
    flags = ["--apply", "--expected-source-sha256", revision]
    if argv[0] == "accounts":
        flags.append("--confirm")
    return terminal_run(run, [*argv, *flags])


def _market(document: dict, input_fn: Input, *, allow_new: bool = False) -> str:
    markets = tuple(m for m in ("us", "hk") if m in document.get("markets", {}))
    if len(markets) == 1 and not allow_new:
        return markets[0]
    return _choose("市场", ("us", "hk") if allow_new else markets, input_fn)


def _symbol_policy(symbol: str, input_fn: Input, *, keyed: bool = False) -> list[str]:
    strategy = _choose(f"{symbol} 策略", ("csp", "cc", "both"), input_fn)
    args = ["--symbol-strategy" if keyed else "--strategy", f"{symbol}={strategy}" if keyed else strategy]
    for side, bound in (("csp", "min"), ("csp", "max"), ("cc", "min"), ("cc", "max")):
        if strategy not in {side, "both"}:
            continue
        required = (side, bound) in {("csp", "max"), ("cc", "min")}
        value = input_fn(f"{symbol} {side.upper()} {bound} strike（{'必填' if required else '选填'}）: ").strip()
        if required and not value:
            raise AgentToolError(code="INPUT_ERROR", message=f"{side} {bound} strike 必填")
        if value:
            args += [f"--{side}-{bound}-strike", f"{symbol}={value}" if keyed else value]
    return args


def manage_accounts(run: Run, source: Path, *, input_fn: Input = input) -> int:
    revision = hashlib.sha256(source.read_bytes()).hexdigest()
    document = load_yaml_config_file(source)
    action = _choose("账户任务", ("list", "add", "edit", "remove"), input_fn, "list")
    if action == "list":
        return run(["accounts", "list", "--config-yaml", str(source)])
    run(["accounts", "list", "--config-yaml", str(source)])
    market = _market(document, input_fn, allow_new=action == "add")
    label = input_fn("账户标签: ").strip()
    argv = ["accounts", action, "--config-yaml", str(source), "--market", market,
            "--account-label", label, "--expected-source-sha256", revision]
    existing = label in document.get("accounts", {})
    if action in {"add", "edit"} and not (action == "add" and existing):
        for flag, prompt in (("futu-acc-id", "富途账户 ID"), ("futu-host", "OpenD 地址"), ("futu-port", "OpenD 端口")):
            default = {"futu-host": "127.0.0.1", "futu-port": "11111"}.get(flag, "") if action == "add" else ""
            value = input_fn(f"{prompt}（{'留空保持' if action == 'edit' else default or '必填'}）: ").strip() or default
            if action == "add" and flag == "futu-acc-id" and not value:
                raise AgentToolError(code="INPUT_ERROR", message="富途账户 ID 必填")
            if value:
                argv += ["--" + flag, value]
        environment = input_fn("账户环境 REAL/SIMULATE（" + ("必填" if action == "add" else "留空保持") + "）: ").strip().upper()
        if action == "add" and not environment:
            raise AgentToolError(code="INPUT_ERROR", message="账户环境必填")
        if environment:
            argv += ["--trd-env", environment]
    if action == "add" and market not in document.get("markets", {}):
        symbols = input_fn("新市场的监控标的（逗号或空格分隔，必填）: ").replace(",", " ").split()
        if not symbols:
            raise AgentToolError(code="INPUT_ERROR", message="新市场必须配置监控标的")
        for symbol in symbols:
            argv += ["--symbol", symbol, *_symbol_policy(symbol, input_fn, keyed=True)]
    else:
        print("同一市场的账户共享监控标的。")
    return preview_and_confirm(run, argv, input_fn=input_fn)


def manage_symbols(run: Run, source: Path, *, input_fn: Input = input) -> int:
    revision = hashlib.sha256(source.read_bytes()).hexdigest()
    document = load_yaml_config_file(source)
    market = _market(document, input_fn)
    action = _choose("标的任务", ("list", "add", "edit", "rm"), input_fn, "list")
    scope = ["--config-yaml", str(source), "--market", market]
    if action == "list":
        return run(["symbols", "list", *scope])
    run(["symbols", "list", *scope])
    symbol = input_fn("标的代码: ").strip()
    argv = ["symbols", action, symbol, *scope, "--format", "json", "--expected-source-sha256", revision]
    if action == "add":
        argv += _symbol_policy(symbol, input_fn)
    elif action == "edit":
        print("留空保持原值；选填的边界可输入 null 清除。新增启用 CSP 需 max，CC 需 min。")
        for side, key in (("CSP", "sell_put"), ("CC", "covered_call")):
            for field in ("enabled", "min_strike", "max_strike"):
                value = input_fn(f"{side} {field}{' [true/false]' if field == 'enabled' else ''}: ").strip()
                if value:
                    argv += ["--set", f"{key}.{field}={value}"]
    return preview_and_confirm(run, argv, input_fn=input_fn)


def manage_service(run: Run, source: Path, *, input_fn: Input = input, action: str | None = None) -> int:
    action = action or _choose("服务任务", ("install", "start", "stop", "status"), input_fn, "status")
    if action == "status":
        return run(["service", "status", "--profile-path", str(source.parent / "service.profile.json"), "--include-service-status"])
    argv = ["service", action, "--config-yaml", str(source), "--runtime-root", str(source.parent)]
    if action == "install" and sys.platform.startswith("linux"):
        user = input_fn("部署用户名（普通用户留空使用自己；root 必填）: ").strip()
        if user:
            argv += ["--deploy-user", user]
    if action == "install":
        document = load_yaml_config_file(source)
        if (document.get("bot") or {}).get("enabled", False):
            if _yes("安装 Bot 的消息接入服务（后台收消息并回复）", input_fn):
                channel = _choose("Bot 接入通道", ("feishu", "wechat-clawbot"), input_fn)
                argv.append("--include-feishu-ws" if channel == "feishu" else "--include-wechat-clawbot")
                argv += ["--channel-market", _market(document, input_fn)]
    return preview_and_confirm(run, argv, input_fn=input_fn, service=True)


def manage_secrets(run: Run, *, input_fn: Input = input) -> int:
    from src.application.secret_store import credential_specs

    action = _choose("凭证任务", ("status", "set", "rotate", "delete"), input_fn, "status")
    if action == "status":
        return run(["secrets", "status", "--format", "text"])
    specs = credential_specs()
    for spec in specs:
        print(f"  {spec.logical_name} — {spec.purpose}")
    name = _choose("凭证名称（这里只填名称，值在下一步隐藏输入）", tuple(spec.logical_name for spec in specs), input_fn)
    print("密钥由既有系统存储管理；变更后会显示受影响服务，不自动重启。")
    if not _yes(f"确认{action}凭证 {name}", input_fn):
        print("已取消，本步骤未写入。")
        return 0
    argv = ["secrets", action, name, *(["--confirm"] if action == "delete" else [])]
    rc = run(argv)
    if rc and sys.platform.startswith("linux"):
        print(f'若系统凭证写入需要 root，请在终端执行：sudo "$(command -v om)" secrets {action} {name} --backend systemd'
              + (" --confirm" if action == "delete" else ""))
    return rc


def daily_management(run: Run, *, input_fn: Input = input,
                     repo_base_fn: Callable[[], Path] = repo_base, source: Path | None = None) -> int:
    caller = run
    source = source or resolve_yaml_config_path(None, repo_root=repo_base_fn())
    if not source.is_file():
        print("尚未找到 config.yaml，请先运行 om setup init。")
        return 2
    run = lambda argv: _instance_run(caller, argv, source=source, repo_root=repo_base_fn())
    while True:
        print(f"\n日常管理 · {source}\n  1  查看结果与状态\n  2  账户与标的\n"
              "  3  通知与 Bot\n  4  策略与全局持仓风险\n  5  运行与维护\n  0  返回")
        choice = input_fn("请选择：").strip()
        if choice == "0":
            return 0
        try:
            document = load_yaml_config_file(source)
            scope = ["--config-yaml", str(source)]
            if choice == "1":
                task = _choose("查看", ("status", "brief"), input_fn, "status")
                if task == "brief":
                    run(["daily-brief", "latest"])
                else:
                    market = _market(document, input_fn)
                    run(["status", "--config-path", str(source.parent / f"config.{market}.json")])
            elif choice == "2":
                task = _choose("管理", ("accounts", "symbols"), input_fn, "symbols")
                (manage_accounts if task == "accounts" else manage_symbols)(run, source, input_fn=input_fn)
            elif choice == "3":
                task = _choose("配置", ("channel", "bot", "secrets"), input_fn, "channel")
                if task == "secrets":
                    manage_secrets(run, input_fn=input_fn)
                else:
                    run([task, "configure", *scope])
            elif choice == "4":
                task = _choose("策略与风险", ("holdings", "close-advice", "wheel", "combo"), input_fn)
                if task in {"holdings", "close-advice"}:
                    run([task, "configure", *scope])
                elif task == "wheel":
                    print("Wheel 按账户激活，须确认策略及实际持仓条件；以下是现有完整命令。")
                    run(["wheel", "--help"])
                else:
                    print("Combo 使用同一标的的 yield_enhancement 配置；通过 symbols edit --set 修改并预览。")
                    market = _market(document, input_fn)
                    run(["config", "explain", *scope, "--market", market, "--key", "yield_enhancement"])
                    run(["symbols", "edit", "--help"])
            elif choice == "5":
                task = _choose("运行维护", ("doctor", "run", "service", "update", "advanced"), input_fn)
                if task == "service":
                    manage_service(run, source, input_fn=input_fn)
                elif task == "update":
                    run(["update", "--help"])
                elif task == "advanced":
                    print("账本、交易审核、修复与研究的完整命令保留；按命令的预览和授权要求操作。")
                    run(["help", "all"])
                else:
                    market = _market(document, input_fn)
                    runtime = str(source.parent / f"config.{market}.json")
                    if task == "doctor":
                        if _yes("诊断会连接 OpenD，是否继续", input_fn):
                            run(["doctor", "--config-path", runtime])
                    elif _yes("手动运行会连接 OpenD，本次不发送通知，是否继续", input_fn):
                        argv = ["run", "tick", "--config", runtime, "--no-send", "--force"]
                        if _yes("使用 SIMULATE 体验模式", input_fn):
                            argv.append("--experience")
                        run(argv)
            else:
                print("请选择菜单中的编号。")
        except AgentToolError as exc:
            print(f"{exc}\n本步骤未完成；之前已保存的步骤保留。")


def first_install(
    args: argparse.Namespace, run: Run, *, input_fn: Input = input,
    base_init: Callable = run_setup_init, repo_base_fn: Callable[[], Path] = repo_base,
    default_dir_fn: Callable[[], Path] = _default_setup_dir,
) -> int:
    caller = run
    if args.output_dir:
        source = Path(args.output_dir).expanduser().resolve() / "config.yaml"
    else:
        current = resolve_yaml_config_path(None, repo_root=repo_base_fn())
        source = current if current.is_file() else default_dir_fn() / "config.yaml"
    run = lambda argv: _instance_run(caller, argv, source=source, repo_root=repo_base_fn())
    print(f"配置与运行目录：{source.parent}")
    print("富途 OpenAPI/OpenD：请先安装并登录 OpenD；OM 不负责启动 OpenD。\n"
          "使用 OpenD 账户列表中的数字 ID；真实账户选 REAL，测试账户选 SIMULATE。")
    completed: list[str] = []
    try:
        if source.is_file():
            print("已有配置将保留；可继续配置通知、Bot 和服务，账户与标的请在日常管理中调整。")
            if not _yes("继续安装引导", input_fn):
                return 0
        else:
            scoped = argparse.Namespace(**vars(args))
            scoped.output_dir = str(source.parent)
            output, applied = base_init(scoped, repo_base_fn=repo_base_fn, input_fn=input_fn, input_is_tty=lambda: True)
            print(output, end="")
            if not applied:
                return 0
        completed.append("基本配置已保存")
        document = load_yaml_config_file(source)
        market = _market(document, input_fn)
        runtime = str(source.parent / f"config.{market}.json")
        if _yes("现在验证 OpenD 登录和账户连接（会连接 OpenD）", input_fn):
            rc = run(["doctor", "--config-path", runtime])
            completed.append("OpenD 检查通过" if rc == 0 else "OpenD 检查存在未解决项")
        else:
            completed.append("OpenD 连接未验证")
        for name, command in (("通知通道", "channel"), ("Bot LLM", "bot")):
            if _yes(f"现在配置{name}（可以跳过）", input_fn):
                rc = run([command, "configure", "--config-yaml", str(source)])
                completed.append(f"{name}配置步骤{'已返回；以配置回读为准' if rc == 0 else '失败或待处理'}")
            else:
                completed.append(f"{name}跳过；保留当前启用状态")
        if _yes("现在手动运行一次（连接 OpenD，不发送通知）", input_fn):
            argv = ["run", "tick", "--config", runtime, "--no-send", "--force"]
            if _yes("使用 SIMULATE 体验模式（仅支持全部为测试账户）", input_fn):
                argv.append("--experience")
            rc = run(argv)
            completed.append("首次运行完成" if rc == 0 else "首次运行失败，详见结果")
        else:
            completed.append("手动运行：未执行")
        if _yes("现在安装常驻服务定义（安装后还需单独启动）", input_fn):
            rc = manage_service(run, source, input_fn=input_fn, action="install")
            completed.append("服务安装步骤已返回；以服务回读为准" if rc == 0 else "服务安装失败或待处理")
            if rc == 0 and (source.parent / "service.profile.json").is_file() and _yes("现在预览启动常驻服务（包含交易回执入账和自动平仓记录维护）", input_fn):
                rc = manage_service(run, source, input_fn=input_fn, action="start")
                completed.append("服务启动步骤已返回；以服务回读为准" if rc == 0 else "服务启动失败或待处理")
        else:
            completed.append("常驻服务：本次未安装或启动；已有服务状态可在日常管理查看")
    except (EOFError, KeyboardInterrupt):
        print("\n引导已中断，已确认保存的步骤保留；重新运行 om setup init 可以继续。")
    finally:
        print("\n安装进度：")
        for item in completed:
            print("  " + item)
        if source.is_file():
            saved = load_yaml_config_file(source)
            bot_config = saved.get("bot") or {}
            states = (("通知", (saved.get("notifications") or {}).get("enabled", True)),
                      ("Bot", bot_config.get("enabled", False) and (bot_config.get("bot") or {}).get("enabled", False)))
            for label, enabled in states:
                print(f"  {label}：{'配置为启用，运行效果未验证' if enabled else '未启用'}")
            close_enabled = (saved.get("close_advice") or {}).get("enabled", True)
            print(f"  Close Advice：{'配置为开启' if close_enabled else '已关闭'}；实际持仓评估未验证")
        print("可选能力：Wheel 按账户激活；Combo 在标的高级设置中配置；Holdings 可补充全局持仓风险来源。均可在日常管理查看。")
        print("日常管理：运行 om；完整帮助：om help all。配置已保存不代表服务或外部连接已就绪。")
    return 0
