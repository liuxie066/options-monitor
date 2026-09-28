from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_authoring_transaction import config_source_sha256, publish_yaml_config_generation
from src.application.config_yaml import load_yaml_config_file
from src.application.config_validator import INLINE_SECRET_CONFIG_KEYS, INLINE_SECRET_CONFIG_SUFFIXES
from src.application.platform_profile import current_platform_profile
from src.application.settings import build_effective_env
from src.interfaces.cli.setup_interactive import _ask


FEATURES = (
    ("1", "基础：市场、账户、标的", "accounts / markets.<market>.accounts / symbols"),
    ("2", "CSP", "markets.<market>.overrides.<symbol>.sell_put"),
    ("3", "CC", "markets.<market>.overrides.<symbol>.covered_call"),
    ("4", "Combo Yield", "markets.<market>.overrides.<symbol>.combo_yield"),
    ("5", "Wheel", "markets.<market>.features.wheel"),
    ("6", "Close Advice", "markets.<market>.features.close_advice"),
    ("7", "Assistant / Bot", "assistant / inbound"),
    ("8", "通知与外部持仓", "notifications / accounts.<label>.type: external_holdings"),
)
FEATURE_NOTES = {
    "2": "依赖所选市场的 Futu 期权行情与账户。",
    "3": "依赖持仓与期权行情；YAML 使用 covered_call。",
    "4": "按标的配置，依赖 CSP/CC 候选数据。",
    "5": "按市场配置，依赖 Wheel 持仓与事件事实。",
    "6": "按市场配置，依赖持仓与 Close Advice 数据。",
    "7": "Bot 需要模型配置和相应凭据。",
    "8": "外部持仓或 Feishu 通知需另行设置凭据。",
}


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[str, Any]:
    pairs = loader.construct_pairs(node, deep=True)
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate YAML key")
        result[key] = value
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _load_candidate(path: Path) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError) as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=f"edited YAML could not be parsed safely ({type(exc).__name__})") from exc
    if not isinstance(value, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="edited config.yaml must be a YAML object")
    _reject_literal_secret_fields(value)
    return value


def _reject_literal_secret_fields(value: Any) -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).lower()
            if key in INLINE_SECRET_CONFIG_KEYS or key.endswith(INLINE_SECRET_CONFIG_SUFFIXES):
                raise AgentToolError(code="CONFIG_ERROR", message="secret-like YAML field is not allowed; use om secrets set")
            _reject_literal_secret_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_literal_secret_fields(child)


def _changed_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        result: list[str] = []
        for key in sorted(set(before) | set(after), key=str):
            name = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                result.append(name)
            else:
                result.extend(_changed_paths(before[key], after[key], name))
        return result
    return [prefix] if before != after else []


def _authoring_count(doc: dict[str, Any], choice: str) -> int:
    markets = doc.get("markets") if isinstance(doc.get("markets"), dict) else {}
    if choice in {"2", "3", "4"}:
        key = {"2": "sell_put", "3": "covered_call", "4": "combo_yield"}[choice]
        return sum(
            key in override
            for market in markets.values()
            if isinstance(market, dict)
            for override in (market.get("overrides") if isinstance(market.get("overrides"), dict) else {}).values()
            if isinstance(override, dict)
        )
    if choice in {"5", "6"}:
        key = "wheel" if choice == "5" else "close_advice"
        return sum(
            key in (market.get("features") if isinstance(market.get("features"), dict) else {})
            for market in markets.values()
            if isinstance(market, dict)
        )
    if choice == "7":
        assistant = doc.get("assistant")
        return int(isinstance(assistant, dict) and bool(assistant.get("enabled")))
    if choice == "8":
        accounts = doc.get("accounts") if isinstance(doc.get("accounts"), dict) else {}
        return int("notifications" in doc) + sum(
            isinstance(item, dict) and item.get("type") == "external_holdings" for item in accounts.values()
        )
    return len(markets)


def _launch_editor(path: Path) -> int:
    raw = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    argv = shlex.split(raw)
    if not argv or shutil.which(argv[0]) is None:
        raise AgentToolError(code="INPUT_ERROR", message="no usable terminal editor found in VISUAL or EDITOR")
    return subprocess.run([*argv, str(path)], check=False).returncode


def _resolve_paths(*, runtime_root: str | None, env_file: str | None) -> tuple[Path, Path]:
    profile = current_platform_profile()
    requested_root = Path(runtime_root).expanduser().resolve() if runtime_root else None
    inherited_root_raw = str(os.environ.get("OM_RUNTIME_ROOT") or "").strip()
    inherited_root = Path(inherited_root_raw).expanduser().resolve() if inherited_root_raw else None
    if requested_root and inherited_root and requested_root != inherited_root:
        raise AgentToolError(code="INPUT_ERROR", message="--runtime-root conflicts with OM_RUNTIME_ROOT")
    root = requested_root or inherited_root or profile.default_runtime_root
    requested_env = Path(env_file).expanduser().resolve() if env_file else None
    inherited_env_raw = str(os.environ.get("OM_ENV_FILE") or "").strip()
    inherited_env = Path(inherited_env_raw).expanduser().resolve() if inherited_env_raw else None
    if requested_env and inherited_env and requested_env != inherited_env:
        raise AgentToolError(code="INPUT_ERROR", message="--env-file conflicts with OM_ENV_FILE")
    default_env = profile.default_env_file if root == profile.default_runtime_root else root / "options-monitor.env"
    selected_env = requested_env or inherited_env or default_env
    return root.expanduser().resolve(), selected_env.expanduser().resolve()


def run_interactive_config_edit(
    *,
    repo_root: Path,
    runtime_root: str | None = None,
    env_file: str | None = None,
    prompt_fn: Callable[[], str] = input,
    input_is_tty: Callable[[], bool] = lambda: sys.stdin.isatty(),
    editor_fn: Callable[[Path], int] = _launch_editor,
) -> dict[str, Any]:
    if not input_is_tty():
        raise AgentToolError(code="INPUT_ERROR", message="config edit requires an interactive terminal")
    root, ordinary_env = _resolve_paths(runtime_root=runtime_root, env_file=env_file)
    configured_root = str(build_effective_env(repo_root=repo_root, env_file=ordinary_env, include_local_env_file=False).values.get("OM_RUNTIME_ROOT") or "").strip()
    if configured_root and Path(configured_root).expanduser().resolve() != root:
        raise AgentToolError(code="INPUT_ERROR", message="selected runtime root conflicts with OM_RUNTIME_ROOT from effective settings")
    source = root / "config.yaml"
    if source.is_symlink():
        raise AgentToolError(code="CONFIG_ERROR", message="config edit requires a regular non-symlink config.yaml")
    before = load_yaml_config_file(source)
    markets_before = list(before.get("markets") or [])
    sys.stderr.write(f"配置来源：{source}\n普通 env-file：{ordinary_env}\n")
    sys.stderr.write(f"市场：{', '.join(markets_before) or '(none)'}；账户：{', '.join(before.get('accounts') or []) or '(none)'}\n")
    for number, label, path in FEATURES:
        sys.stderr.write(f"  {number}. {label} → {path}；YAML 条目 {_authoring_count(before, number)}\n")
    sys.stderr.write("  9. 普通 env 设置 → settings inspect/doctor 与终端编辑\n")
    sys.stderr.write("  10. 密钥 → secrets status/set/rotate\n  0. 退出\n")
    choice = _ask("选择功能编号", prompt_fn=prompt_fn)
    prefix = shlex.quote(str(repo_root / "om"))
    if choice == "0":
        return {"ok": True, "write_applied": False, "config_yaml_path": str(source)}
    if choice == "9":
        editor = "sudoedit" if ordinary_env.exists() and not os.access(ordinary_env, os.W_OK) else "${EDITOR:-vi}"
        return {
            "ok": True,
            "write_applied": False,
            "env_file": str(ordinary_env),
            "commands": [
                f"{prefix} settings inspect --env-file {shlex.quote(str(ordinary_env))}",
                f"{editor} {shlex.quote(str(ordinary_env))}",
                f"{prefix} settings doctor --env-file {shlex.quote(str(ordinary_env))}",
            ],
            "note": "External editor writes immediately; run settings doctor afterward. Keep secrets in om secrets, not this file.",
        }
    if choice == "10":
        return {
            "ok": True,
            "write_applied": False,
            "commands": [f"{prefix} secrets status", f"{prefix} secrets set <logical-name>", f"{prefix} secrets rotate <logical-name>"],
            "consumer_verified": False,
            "note": "Linux manual shells need a credential-bearing runtime before systemd credentials can be consumed.",
        }
    feature = next((item for item in FEATURES if item[0] == choice), None)
    if feature is None:
        raise AgentToolError(code="INPUT_ERROR", message="choose a listed feature number")
    sys.stderr.write(f"编辑 {feature[1]}：{feature[2]}\n编辑器会打开完整 config.yaml；保存后先验证，再确认发布。\n")
    if choice in FEATURE_NOTES:
        sys.stderr.write(FEATURE_NOTES[choice] + "\n")
    before_sha = config_source_sha256(source)
    original_bytes = source.read_bytes()
    with tempfile.TemporaryDirectory(prefix="om-config-edit-") as temp_dir:
        candidate_path = Path(temp_dir) / "config.yaml"
        candidate_path.write_bytes(original_bytes)
        candidate_path.chmod(0o600)
        try:
            editor_status = editor_fn(candidate_path)
        except (OSError, KeyboardInterrupt) as exc:
            raise AgentToolError(code="CANCELLED", message="editor cancelled; configuration unchanged") from exc
        if editor_status != 0:
            raise AgentToolError(code="CANCELLED", message="editor exited without a successful save; configuration unchanged")
        candidate = _load_candidate(candidate_path)
    changed = _changed_paths(before, candidate)
    if not changed:
        return {"ok": True, "write_applied": False, "config_yaml_path": str(source), "changed_paths": []}
    markets_after = list(candidate.get("markets") or [])
    if not markets_after or any(market not in {"us", "hk"} for market in markets_after):
        raise AgentToolError(code="CONFIG_ERROR", message="edited config must select at least one supported market")
    preview = publish_yaml_config_generation(
        repo_root=repo_root,
        config_yaml_path=source,
        config_doc=candidate,
        runtime_root=root,
        markets=markets_after,
        apply=False,
        expected_source_sha256=before_sha,
    )
    serialized_sha = preview["source_revision"]["after_sha256"]
    retirement_pending = [str(root / f"config.{market}.json") for market in markets_before if market not in markets_after and (root / f"config.{market}.json").exists()]
    sys.stderr.write("将改变的 YAML 键（不显示值）：\n" + "\n".join(f"  {path}" for path in changed) + "\n")
    sys.stderr.write(f"发布后序列化 SHA256：{serialized_sha}\n注释或格式可能被规范化；原文件会备份。\n")
    if retirement_pending:
        sys.stderr.write("已移除市场的旧快照/服务不会自动停止或删除：\n" + "\n".join(retirement_pending) + "\n")
    if _ask("确认发布？输入 yes", prompt_fn=prompt_fn).lower() != "yes":
        raise AgentToolError(code="CANCELLED", message="config edit cancelled before publishing")
    published = publish_yaml_config_generation(
        repo_root=repo_root,
        config_yaml_path=source,
        config_doc=candidate,
        runtime_root=root,
        markets=markets_after,
        apply=True,
        expected_source_sha256=before_sha,
    )
    if config_source_sha256(source) != serialized_sha:
        raise AgentToolError(code="CONFIG_WRITE_FAILED", message="edited config failed source readback")
    return {
        "ok": True,
        "write_applied": published["write_applied"],
        "config_yaml_path": str(source),
        "runtime_root": str(root),
        "env_file": str(ordinary_env),
        "changed_paths": changed,
        "source_sha256": serialized_sha,
        "retirement_pending": retirement_pending,
        "service_changed": False,
    }
