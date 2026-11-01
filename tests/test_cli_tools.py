"""Public launchers share gateway contracts without operator instance discovery."""
from __future__ import annotations

import json
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.interfaces.agent import cli as gateway
from src.interfaces.cli import main as cli

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def launch(tmp_path):
    env_file = tmp_path / "isolated.env"
    env_file.write_text("OM_SECRET_BACKEND=env\nOM_AGENT_ENABLE_WRITE_TOOLS=false\n")
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TMPDIR") if key in os.environ}
    env.update(HOME=str(tmp_path), OM_PYTHON=sys.executable, OM_SECRET_BACKEND="env", OM_ENV_FILE=str(env_file),
               XDG_CONFIG_HOME=str(tmp_path / "user-config"))

    def run(entry, *args):
        return subprocess.run([str(ROOT / entry), *args], cwd=tmp_path, env=env,
                              text=True, capture_output=True, timeout=30)
    return run


@pytest.mark.parametrize("args", [
    ["spec"],
    ["run", "--tool", "not_a_tool"],
    ["run", "--tool", "symbol_resolve", "--input-json", '{"symbol":"NVDA"}'],
    ["run", "--tool", "position_advice_evaluate", "--input-json", "{}"],
    ["run", "--tool", "manage_symbols", "--input-json", '{"confirm":true}'],
    ["run", "--tool", "not_a_tool", "--input-json", "{"],
    ["run", "--tool", "not_a_tool", "--input-json", "[]"],
    ["run", "--tool", "not_a_tool", "--input-file", "missing.json"],
    ["run"],
    ["run", "--tool", "not_a_tool", "--invalid"],
    *[[command, "--market", "us", "--account-label", "sample",
       *(["--account-type", "futu"] if command == "add-account" else [])]
      for command in ("add-account", "edit-account", "remove-account")],
])
def test_real_gateway_launchers_are_equivalent(launch, args):
    old = launch("om-agent", *args)
    new = launch("om", "tools", *args)
    assert new.returncode == old.returncode
    if old.stdout:
        assert json.loads(new.stdout) == json.loads(old.stdout)
        assert new.stderr == old.stderr == ""
    else:
        assert old.returncode == new.returncode == 2
        assert "error:" in old.stderr and "error:" in new.stderr
    if ("--input-json" in args and args[-1] in {"{", "[]"}) or "--input-file" in args:
        assert json.loads(new.stdout)["error"]["code"] == "INPUT_ERROR"
    if args == ["run", "--tool", "not_a_tool"]:
        assert "om tools spec" in json.loads(new.stdout)["error"]["hint"]


def test_help_and_manifest_are_offline_without_instance_discovery(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("help/spec attempted runtime or secret bootstrap")
    monkeypatch.setattr(cli, "command_environment", forbidden)
    monkeypatch.setattr(gateway, "bootstrap_process_env", forbidden)
    assert cli.main(["tools", "spec"]) == 0
    assert json.loads(capsys.readouterr().out)["tools"]
    with pytest.raises(SystemExit) as exc:
        cli.main(["tools", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    for command in ("spec", "run", "add-account", "edit-account", "remove-account"):
        assert command in output


@pytest.mark.parametrize("command", ["add-account", "edit-account", "remove-account"])
def test_account_gateway_preserves_preview_and_confirmation(tmp_path, monkeypatch, capsys, command):
    env_file = tmp_path / "write.env"
    env_file.write_text("OM_AGENT_ENABLE_WRITE_TOOLS=false\n")
    monkeypatch.setenv("OM_ENV_FILE", str(env_file))
    calls = []
    monkeypatch.setattr(gateway, command.replace("-", "_"), lambda **kwargs: calls.append(kwargs) or {"dry_run": kwargs["dry_run"]})
    argv = [command, "--market", "us", "--account-label", "sample", "--config-path", str(tmp_path / "config.yaml")]
    if command == "add-account":
        argv += ["--account-type", "futu"]
    monkeypatch.setenv("OM_AGENT_ENABLE_WRITE_TOOLS", "false")
    assert cli.main(["tools", *argv]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "PERMISSION_DENIED"
    assert not calls
    assert cli.main(["tools", *argv, "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["dry_run"] is True
    monkeypatch.setenv("OM_AGENT_ENABLE_WRITE_TOOLS", "true")
    env_file.write_text("OM_AGENT_ENABLE_WRITE_TOOLS=true\n")
    assert cli.main(["tools", *argv]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert len(calls) == 1
    assert cli.main(["tools", *argv, "--confirm"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["dry_run"] is False


@pytest.mark.parametrize("entry", [gateway.main, lambda args: cli.main(["tools", *args])])
def test_explicit_env_and_payload_scope_are_not_rebound(entry, tmp_path, monkeypatch, capsys):
    selected = tmp_path / "selected"
    other = tmp_path / "other"
    env_file = tmp_path / "fixture.env"
    env_file.write_text(f"OM_RUNTIME_ROOT={selected}\nOM_SECRET_BACKEND=auto\nOM_BOT_LLM_API_KEY=ignored-fixture\n")
    monkeypatch.setenv("OM_ENV_FILE", str(env_file))
    monkeypatch.delenv("OM_BOT_LLM_API_KEY", raising=False)
    # Register restoration even when the key was initially absent: bootstrap writes it.
    monkeypatch.setenv("OM_RUNTIME_ROOT", "")
    payload = {"config_path": str(other / "config.hk.json"), "account": "sample", "config_key": "hk"}
    request = tmp_path / "input.json"
    request.write_text(json.dumps(payload))
    calls = []
    def execute(name, body):
        calls.append((name, body, os.environ.get("OM_RUNTIME_ROOT")))
        assert "OM_BOT_LLM_API_KEY" not in os.environ
        return {"ok": True, "data": body}
    monkeypatch.setattr(gateway, "execute_tool", execute)
    assert entry(["run", "--tool", "fixture", "--input-file", str(request), "--input-json", "{",
                  "--env-file", str(env_file), "--no-local-env-file"]) == 0
    assert json.loads(capsys.readouterr().out)["data"] == payload
    assert calls == [("fixture", payload, str(selected))]


def test_task_help_identifies_gateway_and_bot(launch):
    out = launch("om", "help")
    assert out.returncode == 0
    for text in ("om tools", "om bot", "om scan", "position-advice", "om agent", "om assistant"):
        assert text in out.stdout
    assert "tools" in launch("om", "help", "all").stdout


@pytest.mark.parametrize("args", [
    ["run", "--tool", "fixture", "--input-json", "{}", "--input-file", "request.json",
     "--env-file", "settings.env", "--no-local-env-file"],
    *[[command, "--market", "hk", "--account-label", "sample", "--config-yaml", "instance/config.yaml",
       "--rebuild-runtime-root", "instance", "--dry-run", "--confirm",
       *(["--account-type", "futu", "--futu-acc-id", "123456", "--futu-host", "offline.invalid", "--futu-port", "11111"]
         if command != "remove-account" else [])]
      for command in ("add-account", "edit-account", "remove-account")],
])
def test_all_gateway_options_share_the_same_parser(args):
    old = vars(gateway.parse_args(args))
    new = vars(cli.parse_args(["tools", *args]))
    assert new.pop("command") == "tools"
    assert new.pop("tools_command") == old.pop("command")
    assert new == old


def test_real_gateway_config_selection_stays_in_explicit_instances(launch, tmp_path):
    from test_agent_plugin_smoke import _minimal_cfg

    for label, strike in (("one", 111), ("two", 222)):
        root = tmp_path / label
        root.mkdir()
        cfg = _minimal_cfg()
        cfg["symbols"][0]["sell_put"]["max_strike"] = strike
        path = root / "config.us.json"
        path.write_text(json.dumps(cfg))
        request = root / "request.json"
        request.write_text(json.dumps({"config_path": str(path), "symbol": "NVDA", "strategy": "csp", "field": "max_strike"}))
        before = {p: p.read_bytes() for p in root.iterdir()}
        started = datetime.now(timezone.utc)
        old = launch("om-agent", "run", "--tool", "symbol_config_read", "--input-file", str(request))
        new = launch("om", "tools", "run", "--tool", "symbol_config_read", "--input-file", str(request))
        assert old.returncode == new.returncode == 0, (old.stdout, new.stdout)
        old_data, new_data = json.loads(old.stdout), json.loads(new.stdout)
        # Each real invocation owns its read timestamp; all other facts must match.
        for response in (old_data, new_data):
            observed = datetime.fromisoformat(response["data"]["freshness"].pop("as_of"))
            assert started <= observed <= datetime.now(timezone.utc)
        assert old_data == new_data
        assert new_data["data"]["value"] == strike
        assert before == {p: p.read_bytes() for p in root.iterdir()}


@pytest.mark.parametrize("mode", ["text", "json"])
def test_real_position_launcher_format_and_disabled_plugin_are_isolated(launch, tmp_path, mode):
    from test_agent_plugin_smoke import _minimal_cfg

    cfg = _minimal_cfg()
    cfg["close_advice"] = {"enabled": False}
    config = tmp_path / "config.us.json"
    config.write_text(json.dumps(cfg))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"account": "user1"}))
    before = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    args = ["position-advice", "evaluate", "--config-path", str(config), "--request-file", str(request)]
    result = launch("om", *args, *(["--format", mode] if mode == "json" else []))
    assert result.returncode == 0 and result.stderr == "", result.stdout + result.stderr
    if mode == "json":
        assert json.loads(result.stdout) == {"status": "disabled", "rows": [], "returned_count": 0,
                                            "notify_rows": 0, "reason": "持仓建议未启用"}
    else:
        assert "持仓评估：持仓建议未启用" in result.stdout
    assert before == {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert not (tmp_path / "output_shared").exists()
    request.write_text("[]")
    failed = launch("om", *args, "--format", mode)
    assert failed.returncode == 2 and failed.stderr == ""
    if mode == "json":
        assert json.loads(failed.stdout)["error"]["code"] == "INPUT_ERROR"
    else:
        assert "持仓评估执行失败 [INPUT_ERROR]" in failed.stdout


@pytest.mark.parametrize("alias", ["bot", "agent", "assistant"])
def test_bot_aliases_keep_read_only_question_semantics(alias):
    args = cli.parse_args([alias, "run", "--text", "评价一下我的持仓"])
    assert args.command == "bot" and args.bot_command == "run"
    assert args.text == "评价一下我的持仓"


@pytest.mark.parametrize("format_args", [[], ["--format", "text"], ["--format=text"], ["--format", "json"], ["--format=json"]])
def test_advice_startup_error_respects_format(launch, tmp_path, format_args):
    record = tmp_path / ".config/options-monitor/runtime-root"
    record.parent.mkdir(parents=True)
    record.write_text(str(tmp_path / "missing-instance") + "\n")
    request = tmp_path / "request.json"
    request.write_text("{}")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = launch("om", "position-advice", "evaluate", "--request-file", str(request), *format_args)
    assert result.returncode == 2
    assert result.stderr == ""
    if "json" in " ".join(format_args):
        assert json.loads(result.stdout)["error"]["code"] == "CONFIG_ERROR"
    else:
        assert result.stdout.startswith("持仓评估执行失败 [CONFIG_ERROR]：")
        assert "Inspect the user runtime-root record" in result.stdout
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("args", [
    ["--request-file", "missing.json", "--format", "invalid"],
    ["--format=text"],
    ["--request-file", "missing.json", "--unknown"],
])
def test_advice_arguments_validated_before_startup(launch, tmp_path, args):
    record = tmp_path / ".config/options-monitor/runtime-root"
    record.parent.mkdir(parents=True)
    record.write_text(str(tmp_path / "missing-instance") + "\n")
    result = launch("om", "position-advice", "evaluate", *args)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "error:" in result.stderr
    help_result = launch("om", "position-advice", "evaluate", "--help")
    assert help_result.returncode == 0
    assert "--format" in help_result.stdout
    assert help_result.stderr == ""


@pytest.mark.parametrize("error_type", ["agent", "value"])
@pytest.mark.parametrize("format_name", ["text", "json"])
def test_advice_bootstrap_exception_uses_cli_error_renderer(monkeypatch, capsys, error_type, format_name):
    from src.application.agent_tool_contracts import AgentToolError

    def fail_environment(*args, **kwargs):
        if error_type == "value":
            raise ValueError("invalid isolated env")
        raise AgentToolError(code="CONFIG_ERROR", message="invalid isolated env")

    def forbidden(*args, **kwargs):
        pytest.fail("startup failure reached business handler")

    monkeypatch.setattr(cli, "command_environment", fail_environment)
    monkeypatch.setattr(cli, "handle_operator_command", forbidden)
    assert cli.main(["position-advice", "evaluate", "--request-file", "unused.json", "--format", format_name]) == 2
    output = capsys.readouterr()
    assert output.err == ""
    if format_name == "json":
        assert json.loads(output.out)["error"]["code"] == "CONFIG_ERROR"
    else:
        assert output.out.startswith("持仓评估执行失败 [CONFIG_ERROR]：")
