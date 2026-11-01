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
from src.interfaces.cli.field_prompt import prompt_field
from src.interfaces.cli.setup_ops import _default_setup_dir, run_setup_init, symbol_policies_from_args

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
    def validate(value: str) -> str:
        if value in choices:
            return value
        raise ValueError(f"请选择 {' / '.join(choices)}")
    return prompt_field(f"{prompt} [{' / '.join(choices)}]{' [' + default + ']' if default else ''}: ",
                        validate=validate, default=default, input_fn=input_fn)


def _experience_allowed(runtime: Path) -> bool:
    from src.application.account_config import resolve_configured_accounts
    from src.application.experience_mode import validate_experience_request

    try:
        config = json.loads(runtime.read_text(encoding="utf-8"))
        validate_experience_request(config=config, accounts=resolve_configured_accounts(config),
                                    no_send=True, smoke=False, trigger_context={},
                                    opend_phone_verify_continue=False)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return True


def manual_scan(run: Run, source: Path, *, input_fn: Input = input) -> int | None:
    document = load_yaml_config_file(source)
    market = _market(document, input_fn)
    settings = document["markets"][market]
    print(f"本次扫描：{market.upper()} · 账户 {', '.join(settings.get('accounts') or [])} · "
          f"{len(settings.get('symbols') or [])} 个标的。")
    for account in settings.get("accounts") or []:
        broker = document["accounts"][account].get("futu") or {}
        print(f"  {account} · OpenD {broker.get('host', '127.0.0.1')}:{broker.get('port', 11111)}"
              f" · {broker.get('trd_env', '未知')}")
    print("将连接已配置的 OpenD，读取外部数据并保存本地结果、缓存和状态；本次不发送通知、不启动服务。")
    if not _yes("现在手动运行一次", input_fn):
        print("手动运行：未执行；配置和已有结果保留。")
        return None
    runtime = source.parent / f"config.{market}.json"
    argv = ["run", "tick", "--config", str(runtime), "--no-send", "--force"]
    if _experience_allowed(runtime) and _yes("使用 SIMULATE 体验模式（演示假设，不代表真实资金容量）", input_fn):
        argv.append("--experience")
    return run(argv)


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
    from src.application.config_primitives import configured_markets

    markets = tuple(configured_markets(document))
    if not markets or set(markets) != set(document.get("markets") or {}):
        raise AgentToolError(code="CONFIG_ERROR", message="config.yaml markets must configure us and/or hk objects")
    if len(markets) == 1 and not allow_new:
        return markets[0]
    return _choose("市场", ("us", "hk") if allow_new else markets, input_fn)


def _symbol_policy(symbol: str, input_fn: Input, *, keyed: bool = False,
                   market: str | None = None, config: dict | None = None) -> list[str]:
    from src.application.symbol_calibration import require_calibrated_symbol

    calibration = require_calibrated_symbol(symbol, config=config)
    if market is not None and str(calibration.market).lower() != market:
        raise ValueError(f"symbol belongs to {calibration.market}, not {market}")
    symbol = str(calibration.canonical_symbol)
    market = str(calibration.market).lower()
    policy = next(iter(symbol_policies_from_args(
        argparse.Namespace(), symbols={market: [symbol]}, markets=[market],
        interactive=True, input_fn=input_fn,
    ).values()))
    strategy = policy.pop("strategy")
    args = ["--symbol-strategy" if keyed else "--strategy", f"{symbol}={strategy}" if keyed else strategy]
    for field, value in policy.items():
        args += ["--" + field.replace("_", "-"), f"{symbol}={value}" if keyed else str(value)]
    return args


def manage_accounts(run: Run, source: Path, *, input_fn: Input = input) -> int:
    from src.application.account_config import normalize_account_label
    from src.application.config_primitives import normalize_trd_env
    from src.interfaces.cli.setup_ops import _setup_account_id, _setup_host, _setup_port

    revision = hashlib.sha256(source.read_bytes()).hexdigest()
    document = load_yaml_config_file(source)
    action = _choose("账户任务", ("list", "add", "edit", "remove"), input_fn, "list")
    if action == "list":
        return run(["accounts", "list", "--config-yaml", str(source)])
    run(["accounts", "list", "--config-yaml", str(source)])
    market = _market(document, input_fn, allow_new=action == "add")
    label = prompt_field("账户标签: ", validate=normalize_account_label, input_fn=input_fn)
    argv = ["accounts", action, "--config-yaml", str(source), "--market", market,
            "--account-label", label, "--expected-source-sha256", revision]
    existing = label in document.get("accounts", {})
    if action in {"add", "edit"} and not (action == "add" and existing):
        for flag, prompt in (("futu-acc-id", "富途账户 ID"), ("futu-host", "OpenD 地址"), ("futu-port", "OpenD 端口")):
            default = {"futu-host": "127.0.0.1", "futu-port": "11111"}.get(flag, "") if action == "add" else ""
            validator = {"futu-acc-id": _setup_account_id, "futu-host": _setup_host, "futu-port": _setup_port}[flag]
            value = prompt_field(
                f"{prompt}（{'留空保持' if action == 'edit' else default or '必填；手工填写待验证'}）: ",
                default=default, validate=lambda raw: "" if not raw and action == "edit" else validator(raw),
                input_fn=input_fn,
            )
            if value:
                argv += ["--" + flag, str(value)]
        environment = prompt_field(
            "账户环境 REAL/SIMULATE（" + ("必填" if action == "add" else "留空保持") + "）: ",
            validate=lambda raw: "" if not raw and action == "edit" else normalize_trd_env(raw), input_fn=input_fn,
        )
        if environment:
            argv += ["--trd-env", environment]
    if action == "add" and market not in document.get("markets", {}):
        from src.application.config_yaml_init import _normalize_symbols

        symbols = prompt_field(
            "新市场的监控标的（逗号或空格分隔，必填）: ",
            validate=lambda raw: _normalize_symbols(raw.replace(",", " ").split(), market=market),
            input_fn=input_fn,
        )
        for symbol in symbols:
            argv += ["--symbol", symbol, *_symbol_policy(symbol, input_fn, keyed=True, market=market, config=document)]
    else:
        print("同一市场的账户共享监控标的。")
    return preview_and_confirm(run, argv, input_fn=input_fn)


def manage_symbols(run: Run, source: Path, *, input_fn: Input = input) -> int:
    from src.application.config_yaml_symbols import read_yaml_symbol_policy, validate_symbol_policy_fields
    from src.application.symbol_calibration import require_calibrated_symbol
    from src.interfaces.cli.symbols import parse_value

    revision = hashlib.sha256(source.read_bytes()).hexdigest()
    document = load_yaml_config_file(source)
    market = _market(document, input_fn)
    action = _choose("标的任务", ("list", "add", "edit", "rm"), input_fn, "list")
    scope = ["--config-yaml", str(source), "--market", market]
    if action == "list":
        return run(["symbols", "list", *scope])
    run(["symbols", "list", *scope])
    def validate_symbol(raw: str) -> str:
        calibration = require_calibrated_symbol(raw, config=document)
        if str(calibration.market).lower() != market:
            raise ValueError(f"symbol belongs to {calibration.market}, not {market}")
        canonical = str(calibration.canonical_symbol)
        if action in {"edit", "rm"}:
            read_yaml_symbol_policy(repo_root=repo_base(), market=market, symbol=canonical, config_path=source)
        return canonical

    symbol = prompt_field("标的代码: ", validate=validate_symbol, input_fn=input_fn)
    argv = ["symbols", action, symbol, *scope, "--format", "json", "--expected-source-sha256", revision]
    if action == "add":
        argv += _symbol_policy(symbol, input_fn, market=market, config=document)
    elif action == "edit":
        effective = read_yaml_symbol_policy(repo_root=repo_base(), market=market, symbol=symbol, config_path=source)
        print("留空保持原值；选填的边界可输入 null 清除。新增启用 CSP 需 max，CC 需 min。")
        for side, key in (("CSP", "sell_put"), ("CC", "covered_call")):
            draft = dict(effective.get("sell_call" if key == "covered_call" else key) or {})
            required = "max_strike" if key == "sell_put" else "min_strike"
            for field in ("enabled", "min_strike", "max_strike"):
                def validate_field(raw: str) -> str:
                    candidate = dict(draft)
                    if raw:
                        if field == "enabled" and raw.lower() not in {"true", "false"}:
                            raise ValueError("请输入 true 或 false；留空保持原值")
                        candidate[field] = parse_value(raw)
                    validate_symbol_policy_fields(
                        candidate, label=f"{symbol} {side}",
                        required_bound=required if field in {required, "max_strike"} else None,
                        check_range=field == "max_strike",
                    )
                    return raw

                value = prompt_field(f"{side} {field}{' [true/false]' if field == 'enabled' else ''}: ",
                                     validate=validate_field, input_fn=input_fn)
                if value:
                    draft[field] = parse_value(value)
                    argv += ["--set", f"{key}.{field}={value}"]
        if "--set" not in argv:
            print("所有字段保持原值，本步骤未写入。")
            return 0
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
                    if task == "doctor":
                        market = _market(document, input_fn)
                        runtime = str(source.parent / f"config.{market}.json")
                        if _yes("诊断会连接 OpenD，是否继续", input_fn):
                            run(["doctor", "--config-path", runtime])
                    else:
                        manual_scan(run, source, input_fn=input_fn)
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
    print("富途 OpenAPI/OpenD：请先在官方程序中安装并登录；OM 不负责启动或代登录 OpenD。\n"
          "官方入口：https://openapi.futunn.com/futu-api-doc/opend/opend-cmd.html")
    completed: list[str] = []
    unresolved: set[str] = set()
    interrupted = False

    def step(name: str, action: Callable[[], int]) -> int:
        while True:
            try:
                rc = action()
            except AgentToolError as exc:
                print(exc)
                rc = 2
            if rc == 0:
                unresolved.discard(name)
                return 0
            unresolved.add(name)
            print(f"{name}失败或仍有待处理项；已保存配置和旧结果保留。")
            if _choose("下一步", ("retry", "continue"), input_fn, "continue") != "retry":
                return rc

    try:
        if source.is_file():
            print("已有配置将保留；账户与标的可在日常管理中调整。")
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
            rc = step("OpenD 检查", lambda: run(["doctor", "--config-path", runtime]))
            completed.append("OpenD 检查通过" if rc == 0 else "OpenD 检查存在未解决项")
        else:
            rc = None
            completed.append("OpenD 连接未验证")
        for name, command in (("通知通道", "channel"), ("Bot LLM", "bot")):
            if _yes(f"现在配置{name}（可以跳过）", input_fn):
                optional_rc = step(name, lambda: run([command, "configure", "--config-yaml", str(source)]))
                completed.append(f"{name}配置步骤" + ("已返回；以配置回读为准" if optional_rc == 0 else "失败或待处理"))
        if rc != 0:
            print("连接尚未验证通过：可先在 OpenD 处理登录/连接，或保存退出；如主动尝试扫描，将重新校验。")
        scan_rc = manual_scan(run, source, input_fn=input_fn)
        if scan_rc is not None:
            # Each retry re-enters confirmation and the normal scan validation.
            while scan_rc != 0:
                unresolved.add("首次扫描")
                print("首次运行失败；本次失败不会由旧成功简报替代。")
                if _choose("下一步", ("retry", "continue"), input_fn, "continue") != "retry":
                    break
                retried = manual_scan(run, source, input_fn=input_fn)
                if retried is None:
                    break
                scan_rc = retried
            if scan_rc == 0:
                unresolved.discard("首次扫描")
            completed.append("本次扫描入口已返回；以本次运行结果为准" if scan_rc == 0 else "首次运行失败，详见本次结果")
        else:
            completed.append("首次扫描尚未完成（手动运行：未执行）")
        if _yes("现在安装常驻服务定义（安装后还需单独启动）", input_fn):
            service_rc = step("服务安装", lambda: manage_service(run, source, input_fn=input_fn, action="install"))
            completed.append("服务安装步骤已返回；以服务回读为准" if service_rc == 0 else "服务安装失败或待处理")
            if service_rc == 0 and (source.parent / "service.profile.json").is_file() and _yes("现在预览启动常驻服务（包含交易回执入账和自动平仓记录维护）", input_fn):
                service_rc = step("服务启动", lambda: manage_service(run, source, input_fn=input_fn, action="start"))
                completed.append("服务启动步骤已返回；以服务回读为准" if service_rc == 0 else "服务启动失败或待处理")
        else:
            completed.append("常驻服务：本次未安装或启动；自动运行状态未知，可在日常管理查看")
    except (EOFError, KeyboardInterrupt):
        interrupted = True
        print("\n引导已中断；已确认保存的步骤保留，未确认草稿未保存。重新运行 om setup init 可以继续。")
    finally:
        print("\n安装进度" + ("（已中断）" if interrupted else "") + "：")
        for item in completed:
            print("  " + item)
        if source.is_file():
            try:
                saved = load_yaml_config_file(source)
                states = (("通知", (saved.get("notifications") or {}).get("enabled", True)),
                          ("Bot", (saved.get("bot") or {}).get("enabled", False)))
                print("  " + "；".join(f"{label}：{'配置为启用，运行效果未验证' if enabled else '未启用'}" for label, enabled in states))
                from src.application.agent_tools.runtime_status_impl import read_onboarding_status

                state = read_onboarding_status(base=source.parent, document=saved)
                for scope in state["scopes"]:
                    if scope["brief_available"]:
                        print(f"  {scope['account']} · {scope['market']} 已有可读简报数据时间："
                              f"{scope['brief_time'] or '未知'}（已有结果，不代表本次扫描完成）")
            except (AgentToolError, OSError, ValueError) as exc:
                print(f"  配置回读失败：{exc}")
                unresolved.add("配置回读")
        if unresolved:
            print("  尚未解决：" + "、".join(sorted(unresolved)))
        print("下次使用：运行 om 查看简报、重新扫描或修改关注标的与策略边界。")
    return 2 if unresolved else 0
