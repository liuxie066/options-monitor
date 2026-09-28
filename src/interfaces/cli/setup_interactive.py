from __future__ import annotations

import hashlib
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_authoring_transaction import config_source_sha256, publish_yaml_config_generation
from src.application.config_yaml_init import init_yaml_config
from src.application.platform_profile import current_platform_profile
from src.application.settings import build_effective_env
from src.application.setup import run_setup_check


def _ask(label: str, *, prompt_fn: Callable[[], str]) -> str:
    sys.stderr.write(f"{label}: ")
    sys.stderr.flush()
    try:
        return prompt_fn().strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise AgentToolError(code="CANCELLED", message="setup cancelled before writing configuration") from exc


def _path(raw: str, default: Path) -> Path:
    return Path(raw).expanduser().resolve() if raw else default.expanduser().resolve()


def _ensure_create_targets(paths: list[Path]) -> None:
    conflicts = [str(path) for path in paths if path.exists() or path.is_symlink()]
    if conflicts:
        raise AgentToolError(code="CONFIG_ERROR", message="starter config target already exists", details={"conflicts": conflicts})
    for path in paths:
        parent = path.parent
        while not parent.exists():
            if parent.is_symlink():
                raise AgentToolError(code="CONFIG_ERROR", message=f"configuration target parent is a symlink: {parent}")
            parent = parent.parent
        if parent.is_symlink():
            raise AgentToolError(code="CONFIG_ERROR", message=f"configuration target parent is a symlink: {parent}")
        if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"configuration target is not writable by this user: {path}",
                hint="Choose a writable runtime root, or prepare the service directory as the deployment user before retrying.",
            )


def run_interactive_setup(
    *,
    repo_root: Path,
    prompt_fn: Callable[[], str] = input,
    input_is_tty: Callable[[], bool] = lambda: sys.stdin.isatty(),
    check_fn: Callable[..., dict[str, Any]] = run_setup_check,
) -> dict[str, Any]:
    if not input_is_tty():
        raise AgentToolError(code="INPUT_ERROR", message="setup init requires an interactive terminal")
    profile = current_platform_profile()
    sys.stderr.write(f"Options Monitor 首次配置（{profile.platform}）\n")
    mode = _ask("运行方式 [manual/service，默认 manual]", prompt_fn=prompt_fn).lower() or "manual"
    if mode not in {"manual", "service"}:
        raise AgentToolError(code="INPUT_ERROR", message="run mode must be manual or service")
    default_root = (
        Path.home() / ".local" / "share" / "options-monitor"
        if profile.platform == "linux" and mode == "manual"
        else profile.default_runtime_root
    )
    runtime_root = _path(_ask(f"运行目录 [默认 {default_root}]", prompt_fn=prompt_fn), default_root)
    default_env = profile.default_env_file if mode == "service" else runtime_root / "options-monitor.env"
    env_file = _path(_ask(f"普通 env-file [默认 {default_env}]", prompt_fn=prompt_fn), default_env)
    market_choice = _ask("市场 [us/hk/all，默认 us]", prompt_fn=prompt_fn).lower() or "us"
    if market_choice not in {"us", "hk", "all"}:
        raise AgentToolError(code="INPUT_ERROR", message="market must be us, hk, or all")
    markets = ["us", "hk"] if market_choice == "all" else [market_choice]
    account = _ask("Futu 账户标签 [默认 lx]", prompt_fn=prompt_fn) or "lx"
    futu_id = _ask("Futu 数字账户 ID", prompt_fn=prompt_fn)
    if not futu_id.isdigit():
        raise AgentToolError(code="INPUT_ERROR", message="Futu account ID must contain digits only")
    symbols: dict[str, list[str]] = {}
    for market in markets:
        raw = _ask(f"{market.upper()} 标的，逗号分隔 [默认示例标的]", prompt_fn=prompt_fn)
        if raw:
            symbols[market] = [part.strip() for part in raw.split(",") if part.strip()]
    source = runtime_root / "config.yaml"
    env_root = str(os.environ.get("OM_RUNTIME_ROOT") or "").strip()
    env_pointer = str(os.environ.get("OM_ENV_FILE") or "").strip()
    if env_root and Path(env_root).expanduser().resolve() != runtime_root:
        raise AgentToolError(code="INPUT_ERROR", message="OM_RUNTIME_ROOT conflicts with the selected runtime root")
    if env_pointer and Path(env_pointer).expanduser().resolve() != env_file:
        raise AgentToolError(code="INPUT_ERROR", message="OM_ENV_FILE conflicts with the selected env-file")
    configured_root = str(build_effective_env(repo_root=repo_root, env_file=env_file, include_local_env_file=False).values.get("OM_RUNTIME_ROOT") or "").strip()
    if configured_root and Path(configured_root).expanduser().resolve() != runtime_root:
        raise AgentToolError(code="INPUT_ERROR", message="selected runtime root conflicts with OM_RUNTIME_ROOT from effective settings")
    plan = init_yaml_config(
        repo_root=repo_root,
        output_config_yaml_path=source,
        runtime_output_dir=runtime_root,
        markets=markets,
        futu_acc_id=futu_id,
        account_label=account,
        external_holdings_account=None,
        us_symbols=symbols.get("us"),
        hk_symbols=symbols.get("hk"),
        dry_run=True,
    )
    targets = [source, *(runtime_root / f"config.{market}.json" for market in markets), runtime_root / "resolved" / "config.assistant.json"]
    _ensure_create_targets(targets)
    sys.stderr.write("\n即将创建：\n" + "\n".join(f"  {path}" for path in targets) + "\n")
    sys.stderr.write("启用市场：" + ", ".join(markets) + "；Assistant/Bot、外部持仓和通知均不在首装中启用。\n")
    sys.stderr.write("高级功能：CSP、CC、Combo Yield、Wheel、Close Advice、Assistant/Bot、通知、外部持仓；稍后运行 om config edit。\n")
    sys.stderr.write("不会安装或启动服务。\n")
    if _ask("确认创建？输入 yes", prompt_fn=prompt_fn).lower() != "yes":
        raise AgentToolError(code="CANCELLED", message="setup cancelled before writing configuration")
    published = publish_yaml_config_generation(
        repo_root=repo_root,
        config_yaml_path=source,
        config_doc=yaml.safe_load(plan["yaml"]),
        runtime_root=runtime_root,
        markets=markets,
        create=True,
        apply=True,
    )
    if config_source_sha256(source) != published["source_revision"]["after_sha256"]:
        raise AgentToolError(code="CONFIG_WRITE_FAILED", message="created config failed source readback")
    for result in [*published["markets"].values(), published["assistant"]]:
        if result is None:
            continue
        target = Path(result["output_config_path"])
        try:
            actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError as exc:
            raise AgentToolError(code="CONFIG_WRITE_FAILED", message=f"created config snapshot could not be read back: {target}") from exc
        if actual_sha != result["sha256"]:
            raise AgentToolError(code="CONFIG_WRITE_FAILED", message=f"created config failed snapshot readback: {target}")
    check = check_fn(repo_root=repo_root, markets=markets, runtime_root=runtime_root, env_file=env_file, include_local_env_file=False)
    command_prefix = shlex.quote(str(repo_root / "om"))
    path_flags = f"--runtime-root {shlex.quote(str(runtime_root))} --env-file {shlex.quote(str(env_file))}"
    return {
        "ok": True,
        "runtime_root": str(runtime_root),
        "config_yaml_path": str(source),
        "env_file": str(env_file),
        "markets": markets,
        "source_sha256": published["source_revision"]["after_sha256"],
        "setup_check": check["summary"],
        "broker_verified": False,
        "service_changed": False,
        "next_steps": [
            f"{command_prefix} setup check {path_flags} " + " ".join(f"--market {market}" for market in markets),
            f"{command_prefix} config edit {path_flags}",
        ],
    }
