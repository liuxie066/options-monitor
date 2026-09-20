"""Real Feishu entry coverage for config scope and scheduler facts."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from functools import partial
from pathlib import Path

import pytest

from src.application import service_deploy
from src.application.agent_tools import scheduled_tasks_impl
from src.application.config_yaml import build_yaml_runtime_config_file
from src.application.inbound.feishu_ws import FeishuWsSettings, handle_feishu_ws_event
from tests.test_inbound_feishu_ws import _message_payload
from tests.bot_http_test_support import _chat_response, _loopback_server


REPO = Path(__file__).resolve().parents[1]


def _assistant_config(tmp_path: Path, provider_url: str) -> Path:
    path = tmp_path / "config.assistant.json"
    path.write_text(
        json.dumps(
            {
                "assistant": {
                    "enabled": True,
                    "bot": {"enabled": True},
                    "llm": {
                        "provider": "ollama",
                        "model": "om-test",
                        "base_url": provider_url + "/v1",
                        "context_window_tokens": 128000,
                        "max_output_tokens": 2048,
                        "max_attempts": 1,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _build_runtime_config(runtime_root: Path, *, market: str) -> Path:
    path = runtime_root / f"config.{market}.json"
    build_yaml_runtime_config_file(
        repo_root=REPO,
        market=market,
        config_path=REPO / "configs" / "examples" / "config.yaml.example",
        output_config_path=path,
    )
    return path


def _observations(payload: dict) -> list[dict]:
    return [
        json.loads(row["content"])
        for row in payload["messages"]
        if row.get("role") == "tool"
        and isinstance(row.get("content"), str)
        and row["content"].startswith("{")
    ]


def _reply_collector(target: list[dict]):
    def reply(**kwargs):
        target.append(kwargs)
        return {"code": 0, "data": {"message_id": "reply"}}

    return reply


def _reply_text(reply: dict) -> str:
    return reply["content"]["body"]["elements"][0]["content"]


def _feishu_settings(
    tmp_path: Path, url: str, *, config_key: str | None, config_path: str | None = None
) -> FeishuWsSettings:
    return FeishuWsSettings(
        config_key=config_key,
        config_path=config_path,
        assistant_config_path=str(_assistant_config(tmp_path, url)),
        allowed_senders="feishu:ou_1",
        app_id="test",
        app_secret="test",
        audit_db=str(tmp_path / "audit.sqlite3"),
    )


def _run_ws_event(text: str, settings: FeishuWsSettings, replies: list[dict]) -> dict:
    return handle_feishu_ws_event(
        _message_payload(text=text),
        settings=settings,
        reply_fn=_reply_collector(replies),
        reaction_fn=lambda **kwargs: {"code": 0},
        execute_tool_fn=lambda *args, **kwargs: pytest.fail("unexpected Control execution"),
    )


def test_feishu_key_only_scope_runs_python_bot_and_runtime_status(monkeypatch, tmp_path):
    config = _build_runtime_config(tmp_path, market="us")
    state = tmp_path / "output_shared" / "state" / "scheduler_state_us.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps({"last_run_utc_by_account": {"lx": "2026-09-11T01:40:00+00:00"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("OM_PI_SESSION_DB", str(tmp_path / "pi.sqlite3"))
    seen: list[dict] = []

    def submit(payload):
        observations = _observations(payload)
        seen.extend(observations)
        status = next(obs for obs in observations if obs.get("tool_name") == "runtime_status")
        assert status["ok"] is True
        return _chat_response(text="US 账户 lx 的运行状态已读取。")

    replies: list[dict] = []
    responses = [
        {
            "body": _chat_response(
                tool_name="runtime_status",
                tool_arguments={},
                finish_reason="tool_calls",
                call_id="runtime",
            )
        },
        {"body": submit},
    ]
    with _loopback_server(responses) as (url, requests):
        settings = _feishu_settings(tmp_path, url, config_key="us")
        out = _run_ws_event("读取 lx 当前业务调度状态", settings, replies)

    assert out["ok"], out
    assert len(requests) == 2
    assert replies and "运行状态已读取" in _reply_text(replies[-1])
    status = next(obs for obs in seen if obs.get("tool_name") == "runtime_status")
    assert status["ok"] is True
    assert config.exists()


@pytest.mark.parametrize("failure", ["mismatch", "stale"])
def test_feishu_initial_config_failure_precedes_model_and_tool(monkeypatch, tmp_path, failure):
    config = _build_runtime_config(tmp_path, market="us")
    payload = json.loads(config.read_text(encoding="utf-8"))
    if failure == "mismatch":
        payload["_generated"]["market"] = "hk"
        expected = "运行配置与已授权市场身份不一致"
    else:
        source = next(item for item in payload["_generated"]["sources"] if item.get("loaded"))
        source["sha256"] = "0" * 64
        expected = "已授权市场的运行配置已过期"
    config.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    replies: list[dict] = []

    with _loopback_server([]) as (url, requests):
        settings = _feishu_settings(tmp_path, url, config_key=None, config_path=str(config))
        out = _run_ws_event("读取当前调度状态", settings, replies)

    assert out["ok"], out
    assert requests == []
    assert replies and expected in _reply_text(replies[-1])
    with sqlite3.connect(tmp_path / "audit.sqlite3") as conn:
        bot_runs = conn.execute("SELECT count(*) FROM bot_runs").fetchone()[0]
    assert bot_runs == 0


def test_feishu_hk_can_use_two_active_read_tools(monkeypatch, tmp_path):
    config = _build_runtime_config(tmp_path, market="hk")
    state = tmp_path / "output_shared" / "state" / "scheduler_state_hk.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "last_run_utc_by_account": {"lx": "2026-09-11T01:40:00+00:00"},
                "last_notify_utc_by_account": {"lx": "2026-09-11T01:41:00+00:00"},
            }
        ),
        encoding="utf-8",
    )
    bundle = service_deploy.render_service_bundle(
        target="systemd",
        repo_root=REPO,
        runtime_root=tmp_path,
        accounts=["lx"],
        markets=["hk"],
        config_paths={"hk": config},
    )
    profile = json.loads(
        next(
            item for item in bundle["files"] if item["relative_path"] == "service.profile.json"
        )["content"]
    )
    (tmp_path / "service.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    probes: list[list[str]] = []

    def run_os(command, **kwargs):
        probes.append(command)
        assert 0 < kwargs["timeout"] <= 1
        if "is-enabled" in command:
            return subprocess.CompletedProcess(command, 1, "disabled\n", "")
        return subprocess.CompletedProcess(command, 3, "inactive\n", "")

    monkeypatch.setattr(
        scheduled_tasks_impl,
        "scheduled_tasks_from_profile",
        partial(service_deploy.scheduled_tasks_from_profile, run_cmd=run_os),
    )
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("OM_PI_SESSION_DB", str(tmp_path / "pi.sqlite3"))
    seen: list[dict] = []

    def submit(payload):
        observations = _observations(payload)
        seen.extend(observations)
        status = next(obs for obs in observations if obs.get("tool_name") == "runtime_status")
        context = next(obs for obs in observations if obs.get("tool_name") == "project_context")
        assert status["ok"] is True and context["ok"] is True
        return _chat_response(text="HK 运行状态和项目范围已读取。")

    replies: list[dict] = []
    responses = [
        {
            "body": _chat_response(
                tool_name="runtime_status",
                tool_arguments={},
                finish_reason="tool_calls",
                call_id="runtime",
            )
        },
        {
            "body": _chat_response(
                tool_name="project_context",
                tool_arguments={},
                finish_reason="tool_calls",
                call_id="context",
            )
        },
        {"body": submit},
    ]
    with _loopback_server(responses) as (url, requests):
        settings = _feishu_settings(tmp_path, url, config_key="hk")
        out = _run_ws_event("查看 HK lx 调度历史、业务窗口和系统定时任务状态", settings, replies)

    assert out["ok"], out
    assert len(requests) == 3
    assert all(obs["ok"] is True for obs in seen)
    assert "运行状态和项目范围已读取" in _reply_text(replies[-1])
    assert probes == []
