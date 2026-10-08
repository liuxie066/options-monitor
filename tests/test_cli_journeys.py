from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.application.config_yaml_init import init_yaml_config
from src.application.agent_tool_contracts import AgentToolError
from src.application.runtime_paths import resolve_runtime_root
from src.interfaces.cli import main as cli
from src.interfaces.cli.command_environment import command_environment
from src.interfaces.cli.journeys import daily_management, first_install, manage_accounts, manage_secrets, manage_symbols, preview_and_confirm
from src.interfaces.cli.setup_ops import run_setup_init

REPO = Path(__file__).resolve().parents[1]


def _answers(*values):
    iterator = iter(values)
    return lambda _prompt: next(iterator)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "runtime"
    init_yaml_config(account_label="lx", repo_root=REPO, output_config_yaml_path=root / "config.yaml", runtime_output_dir=root,
                     markets=["us"], us_symbols=["NVDA"], futu_acc_id="12345", trd_env="REAL", dry_run=False,
                     symbol_policies={"NVDA": {"strategy": "csp", "csp_max_strike": 100}})
    return root / "config.yaml"


def test_instance_environment_does_not_leak_between_commands(tmp_path, monkeypatch):
    import os
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "caller")
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        (root / "options-monitor.env").write_text(f"OM_FEISHU_BOT_APP_ID={name}\nOM_RUNTIME_ROOT=/wrong\n")
        with command_environment(["channel", "configure", "--config-yaml", str(root / "config.yaml")], repo_root=tmp_path):
            assert os.environ["OM_FEISHU_BOT_APP_ID"] == name
            assert os.environ["OM_RUNTIME_ROOT"] == str(root)
            assert resolve_runtime_root(repo_root=tmp_path).runtime_root == root
            assert resolve_runtime_root(repo_root=tmp_path).source == "argument"
        assert os.environ["OM_FEISHU_BOT_APP_ID"] == "caller"
        assert "OM_RUNTIME_ROOT" not in os.environ
        assert "OM_ENV_FILE" not in os.environ


def test_environment_restored_after_exception_and_explicit_file_wins(tmp_path, monkeypatch):
    import os
    explicit = tmp_path / "explicit.env"
    explicit.write_text("OM_FEISHU_BOT_APP_ID=explicit\n")
    monkeypatch.setenv("OM_ENV_FILE", str(explicit))
    monkeypatch.delenv("OM_FEISHU_BOT_APP_ID", raising=False)
    with pytest.raises(RuntimeError):
        with command_environment(["status", "--config-path", str(tmp_path / "config.us.json")], repo_root=tmp_path):
            assert os.environ["OM_FEISHU_BOT_APP_ID"] == "explicit"
            raise RuntimeError()
    assert "OM_FEISHU_BOT_APP_ID" not in os.environ
    assert os.environ["OM_ENV_FILE"] == str(explicit)


def test_partial_bootstrap_failure_restores_recipient_and_next_instance(tmp_path, monkeypatch):
    import os
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "caller")
    invalid = tmp_path / "invalid.env"
    invalid.write_bytes(b"OM_FEISHU_BOT_USER_OPEN_ID=from-a\nINVALID=bad\x00value\n")
    with pytest.raises(ValueError, match="null byte"):
        with command_environment(["status", "--env-file", str(invalid)], repo_root=tmp_path):
            pytest.fail("invalid bootstrap must not enter command")
    assert os.environ["OM_FEISHU_BOT_USER_OPEN_ID"] == "caller"
    assert "OM_ENV_FILE" not in os.environ
    with command_environment(["status", "--runtime-root", str(tmp_path / "b")], repo_root=tmp_path):
        assert os.environ["OM_FEISHU_BOT_USER_OPEN_ID"] == "caller"
        assert resolve_runtime_root(repo_root=tmp_path).runtime_root == tmp_path / "b"


@pytest.mark.parametrize("journey", ["first", "daily"])
def test_journey_selected_instance_overrides_caller_runtime_for_doctor_and_scan(source, tmp_path, monkeypatch, journey):
    import os
    old = tmp_path / "old-instance"
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(old))
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    seen = []
    def dispatch(argv):
        # Exercise the same nested scope as the public dispatcher without I/O.
        with command_environment(argv, repo_root=REPO):
            seen.append((argv[0], resolve_runtime_root(repo_root=REPO).runtime_root))
        return 0
    if journey == "first":
        args = cli.parse_args(["setup", "init", "--output-dir", str(source.parent)])
        rc = first_install(args, dispatch, input_fn=_answers("y", "y", "n", "n", "y", "n", "n"))
    else:
        rc = daily_management(dispatch, source=source, input_fn=_answers("5", "doctor", "y", "5", "run", "y", "n", "0"))
    assert rc == 0
    assert seen == [("doctor", source.parent), ("run", source.parent)]
    assert os.environ["OM_RUNTIME_ROOT"] == str(old)


def test_menu_symbol_edit_preserves_other_bounds_and_publishes(source):
    assert manage_symbols(cli.main, source, input_fn=_answers("edit", "NVDA", "", "", "120", "", "", "", "y")) == 0
    document = yaml.safe_load(source.read_text())
    assert document["markets"]["us"]["overrides"]["NVDA"]["sell_put"]["max_strike"] == 120
    runtime = json.loads((source.parent / "config.us.json").read_text())
    assert runtime["symbols"][0]["sell_put"]["max_strike"] == 120
    assert runtime["symbols"][0]["sell_call"]["enabled"] is False


def test_menu_symbol_cancel_and_stale_confirmation_do_not_write(source):
    before = source.read_bytes()
    assert manage_symbols(cli.main, source, input_fn=_answers("add", "AAPL", "cc", "90", "", "n")) == 0
    assert source.read_bytes() == before
    replies = iter(("edit", "NVDA", "", "", "120", "", "", ""))
    def answer(prompt):
        if "确认执行" in prompt:
            source.write_text(source.read_text() + "\n# concurrent edit\n")
            return "y"
        return next(replies)
    assert manage_symbols(cli.main, source, input_fn=answer) == 2
    assert source.read_bytes() == before + b"\n# concurrent edit\n"


def test_menu_second_account_shares_symbols(source):
    assert manage_accounts(cli.main, source, input_fn=_answers("add", "us", "second", "67890", "", "", "SIMULATE", "y")) == 0
    document = yaml.safe_load(source.read_text())
    assert document["markets"]["us"]["symbols"] == ["NVDA"]
    assert document["accounts"]["second"]["futu"]["trd_env"] == "SIMULATE"


@pytest.mark.parametrize("environment", ["REAL", "SIMULATE"])
def test_first_install_all_optional_skipped_is_saved_not_ready(tmp_path, environment, capsys):
    root = tmp_path / "runtime"
    args = cli.parse_args(["setup", "init", "--output-dir", str(root)])
    def init(args, **kwargs):
        return run_setup_init(args, user_home=tmp_path / "home", **kwargs)
    calls = []
    replies = _answers("us", "", "", environment, "one", "12345", "NVDA", "csp", "100", "", "yes",
                       "n", "n", "n", "n", "n")
    assert first_install(args, lambda argv: calls.append(argv) or 0, input_fn=replies,
                         base_init=init, repo_base_fn=lambda: REPO) == 0
    assert calls == []
    document = yaml.safe_load((root / "config.yaml").read_text())
    assert document["accounts"]["one"]["futu"]["trd_env"] == environment
    assert document["bot"]["enabled"] is False
    assert document["bot"]["enabled"] is False
    assert document["notifications"]["enabled"] is False
    assert "OpenD 连接未验证" in capsys.readouterr().out


def test_first_install_resume_preserves_source_and_experience_no_send(source):
    before = source.read_bytes()
    args = cli.parse_args(["setup", "init", "--output-dir", str(source.parent)])
    calls = []
    def forbidden(*args, **kwargs):
        raise AssertionError("must not overwrite existing source")
    assert first_install(args, lambda argv: calls.append(argv) or 0,
                         input_fn=_answers("y", "n", "n", "n", "y", "y", "n"), base_init=forbidden) == 0
    assert source.read_bytes() == before
    assert calls == [["run", "tick", "--config", str(source.parent / "config.us.json"), "--no-send", "--force", "--experience"]]


def test_setup_does_not_inherit_discovered_environment_for_nested_instance(tmp_path, monkeypatch):
    import os
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    home = tmp_path / "home"
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        (root / "config.yaml").write_text("{}")
        (root / "options-monitor.env").write_text(f"OM_FEISHU_BOT_APP_ID={name}\nOM_RUNTIME_ROOT=/wrong\n")
    record = home / ".config/options-monitor/runtime-root"
    record.parent.mkdir(parents=True)
    record.write_text(str(tmp_path / "a") + "\n")
    with command_environment(["setup", "init", "--output-dir", str(tmp_path / "b")], repo_root=tmp_path,
                             discover_local=True, user_home=home):
        assert "OM_ENV_FILE" not in os.environ
        with command_environment(["channel", "configure", "--config-yaml", str(tmp_path / "b/config.yaml")], repo_root=tmp_path):
            assert os.environ["OM_FEISHU_BOT_APP_ID"] == "b"
            assert resolve_runtime_root(repo_root=tmp_path).runtime_root == tmp_path / "b"


def test_runtime_scope_restores_nested_owner_and_explicit_argument_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    with command_environment(["status", "--runtime-root", str(tmp_path / "a")], repo_root=tmp_path):
        with command_environment(["status", "--runtime-root", str(tmp_path / "b")], repo_root=tmp_path):
            assert resolve_runtime_root(repo_root=tmp_path).runtime_root == tmp_path / "b"
            assert resolve_runtime_root(repo_root=tmp_path, runtime_root=tmp_path / "c").runtime_root == tmp_path / "c"
        assert resolve_runtime_root(repo_root=tmp_path).runtime_root == tmp_path / "a"
    assert resolve_runtime_root(repo_root=tmp_path, environ={}).runtime_root == tmp_path


def test_explicit_environment_preserves_separate_runtime_and_its_provenance(tmp_path, monkeypatch):
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "state"))
    with command_environment(["scheduler", "--config", str(tmp_path / "config.us.json")], repo_root=tmp_path):
        resolved = resolve_runtime_root(repo_root=tmp_path)
        assert resolved.runtime_root == tmp_path / "state"
        assert resolved.source == "env:OM_RUNTIME_ROOT"


def test_first_install_records_rejected_experience_and_continues(source, capsys):
    from src.application.experience_mode import validate_experience_request
    args = cli.parse_args(["setup", "init", "--output-dir", str(source.parent)])
    def rejected_run(argv):
        assert "--force" in argv and "--no-send" in argv and "--experience" in argv
        runtime = json.loads((source.parent / "config.us.json").read_text())
        try:
            validate_experience_request(config=runtime, accounts=list(runtime["accounts"]), no_send=True,
                                        smoke=False, trigger_context={}, opend_phone_verify_continue=False)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        raise AssertionError("REAL account must reject experience before provider access")
    assert first_install(args, rejected_run, input_fn=_answers("y", "n", "n", "n", "y", "y", "n")) == 0
    assert "首次运行失败" in capsys.readouterr().out


def test_daily_manual_run_forces_one_no_send_scan(source):
    calls = []
    assert daily_management(lambda argv: calls.append(argv) or 0, source=source,
                            input_fn=_answers("5", "run", "y", "n", "0")) == 0
    assert calls == [["run", "tick", "--config", str(source.parent / "config.us.json"), "--no-send", "--force"]]


@pytest.mark.parametrize("provenance", ["config", "env", "record"])
def test_command_scope_reaches_real_trusted_readers(source, tmp_path, monkeypatch, provenance):
    from src.application.agent_tools import project, runtime
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    root = source.parent
    (root / "output_runs").mkdir()
    config = root / "config.us.json"
    (root / "options-monitor.env").write_text("OM_RUNTIME_ROOT=/wrong\n")
    argv = ["bot", "run"]
    home = tmp_path / "home"
    if provenance == "config":
        argv += ["--config-path", str(config)]
    elif provenance == "env":
        monkeypatch.setenv("OM_RUNTIME_ROOT", str(root))
    else:
        record = home / ".config/options-monitor/runtime-root"
        record.parent.mkdir(parents=True)
        record.write_text(str(root) + "\n")
    account = next(iter(yaml.safe_load(source.read_text())["accounts"]))
    payload = {"config_path": str(config), "action": "scoped", "account": account}
    with command_environment(argv, repo_root=REPO, discover_local=True, user_home=home):
        data, _, meta = runtime._runtime_runs_tool(payload)
        assert data["runs"] == [] and data["read_status"] == "empty"
        assert meta["read_only"] is True
        for handler, request in (
            (project._files, {**payload, "action": "list", "resource": "run", "run_id": "missing"}),
            (runtime._runtime_logs_tool, {**payload, "run_id": "missing"}),
        ):
            with pytest.raises(AgentToolError) as missing:
                handler(request)
            assert missing.value.message == "not_found"
        with pytest.raises(AgentToolError) as denied:
            runtime._runtime_runs_tool({**payload, "account": "not-authorized"})
        assert denied.value.code == "PERMISSION_DENIED"


def test_daily_brief_uses_selected_owner_after_env_overlay(source, monkeypatch):
    from src.interfaces.cli import daily_brief_ops
    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    (source.parent / "options-monitor.env").write_text("OM_RUNTIME_ROOT=/wrong\n")
    captured = []
    def read(**kwargs):
        captured.append(kwargs["base"])
        return {"rendered_markdown": "fixture brief"}
    monkeypatch.setattr(daily_brief_ops, "read_daily_brief_view", read)
    assert daily_management(cli.main, source=source, input_fn=_answers("1", "brief", "0")) == 0
    assert captured == [source.parent]


def test_daily_status_and_brief_only_read_existing_results(source):
    calls = []
    assert daily_management(lambda argv: calls.append(argv) or 0, source=source,
                            input_fn=_answers("1", "status", "1", "brief", "0")) == 0
    assert calls == [["status", "--config-path", str(source.parent / "config.us.json")], ["daily-brief", "latest"]]


def test_preview_without_revision_never_applies():
    calls = []
    def run(argv):
        calls.append(argv)
        print('{"ok": true}')
        return 0
    with pytest.raises(Exception, match="预览缺少确认凭据"):
        preview_and_confirm(run, ["accounts", "edit"], input_fn=_answers("y"))
    assert len(calls) == 1


def test_secret_management_collects_only_name_and_requires_delete_confirmation():
    calls = []
    run = lambda argv: calls.append(argv) or 0
    assert manage_secrets(run, input_fn=_answers("rotate", "feishu.bot.app_secret", "y")) == 0
    assert calls == [["secrets", "rotate", "feishu.bot.app_secret"]]
    assert manage_secrets(run, input_fn=_answers("delete", "feishu.bot.app_secret", "n")) == 0
    assert len(calls) == 1
    assert manage_secrets(run, input_fn=_answers("delete", "feishu.bot.app_secret", "y")) == 0
    assert calls[-1] == ["secrets", "delete", "feishu.bot.app_secret", "--confirm"]
