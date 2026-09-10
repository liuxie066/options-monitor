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
from tests.test_bot_task_report import TASK_MARKER, task_claim
from tests.test_inbound_feishu_ws import _message_payload
from tests.test_pi_agent_process import _chat_response, _loopback_server


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


def _claim(text: str, observation_id: str) -> dict:
    return {
        "text": text,
        "kind": "current_fact",
        "required_scope": "point",
        "observation_ids": [observation_id],
    }


def _answer(text: str, claims: list[dict]) -> dict:
    return {
        "mode": "evidence",
        "status": "complete",
        "answer_markdown": text,
        "claims": claims,
    }


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


def test_feishu_key_only_scope_runs_real_pi_and_scheduler_tool(monkeypatch, tmp_path):
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
        scheduler = next(obs for obs in observations if obs.get("tool_name") == "scheduler_status")
        return _chat_response(
            tool_name="submit_answer",
            tool_arguments=_answer(
                "US 账户 lx 的业务调度状态已读取。",
                [_claim("US 账户 lx 的业务调度状态已读取。", scheduler["ref"])],
            ),
            finish_reason="tool_calls",
            call_id="answer",
        )

    replies: list[dict] = []
    responses = [
        {
            "body": _chat_response(
                tool_name="scheduler_status",
                tool_arguments={"account": "lx"},
                finish_reason="tool_calls",
                call_id="scheduler",
            )
        },
        {"body": submit},
    ]
    with _loopback_server(responses) as (url, requests):
        settings = FeishuWsSettings(
            config_key="us",
            config_path=None,
            assistant_config_path=str(_assistant_config(tmp_path, url)),
            allowed_senders="feishu:ou_1",
            app_id="test",
            app_secret="test",
            audit_db=str(tmp_path / "audit.sqlite3"),
        )
        out = handle_feishu_ws_event(
            _message_payload(text="读取 lx 当前业务调度状态"),
            settings=settings,
            reply_fn=_reply_collector(replies),
            reaction_fn=lambda **kwargs: {"code": 0},
            execute_tool_fn=lambda *args, **kwargs: pytest.fail("unexpected Control execution"),
        )

    assert out["ok"], out
    assert len(requests) == 2
    assert replies and "业务调度状态已读取" in _reply_text(replies[-1])
    scheduler = next(obs for obs in seen if obs.get("tool_name") == "scheduler_status")
    assert scheduler["ok"] is True
    assert scheduler["value"]["filters"] == {
        "account": "lx",
        "force": False,
        "market": "us",
        "schedule_key": "schedule",
    }
    assert scheduler["value"]["state"]["last_run_utc_for_account"] == "2026-09-11T01:40:00+00:00"
    assert scheduler["value"]["state"]["selection"] == "production_default"
    assert scheduler["value"]["schedule"]["selection"] == "production_default"
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
        settings = FeishuWsSettings(
            config_key=None,
            config_path=str(config),
            assistant_config_path=str(_assistant_config(tmp_path, url)),
            allowed_senders="feishu:ou_1",
            app_id="test",
            app_secret="test",
            audit_db=str(tmp_path / "audit.sqlite3"),
        )
        out = handle_feishu_ws_event(
            _message_payload(text="读取当前调度状态"),
            settings=settings,
            reply_fn=_reply_collector(replies),
            reaction_fn=lambda **kwargs: {"code": 0},
            execute_tool_fn=lambda *args, **kwargs: pytest.fail("unexpected Control execution"),
        )

    assert out["ok"], out
    assert requests == []
    assert replies and expected in _reply_text(replies[-1])
    with sqlite3.connect(tmp_path / "audit.sqlite3") as conn:
        bot_runs = conn.execute("SELECT count(*) FROM bot_runs").fetchone()[0]
    assert bot_runs == 0


def test_feishu_hk_scheduler_history_and_disabled_timer_remain_distinct(monkeypatch, tmp_path):
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

    explanation = (
        "账户 lx 的最近调度记录来自 scheduler_state_hk.json；业务通知窗口由运行配置的业务日程单独判断。"
        "任务启用和活动状态来自 OS 读取，均不代表业务窗口或扫描成功。"
    )

    def submit(payload):
        observations = _observations(payload)
        seen.extend(observations)
        scheduler = next(obs for obs in observations if obs.get("tool_name") == "scheduler_status")
        tasks = next(obs for obs in observations if obs.get("tool_name") == "scheduled_tasks_read")
        return _chat_response(
            tool_name="submit_answer",
            tool_arguments=_answer(
                TASK_MARKER + "\n\n" + explanation,
                [task_claim(tasks["ref"]), _claim(explanation, scheduler["ref"])],
            ),
            finish_reason="tool_calls",
            call_id="answer",
        )

    replies: list[dict] = []
    responses = [
        {
            "body": _chat_response(
                tool_name="scheduler_status",
                tool_arguments={"account": "lx"},
                finish_reason="tool_calls",
                call_id="scheduler",
            )
        },
        {
            "body": _chat_response(
                tool_name="scheduled_tasks_read",
                tool_arguments={},
                finish_reason="tool_calls",
                call_id="tasks",
            )
        },
        {"body": submit},
    ]
    with _loopback_server(responses) as (url, requests):
        settings = FeishuWsSettings(
            config_key="hk",
            config_path=None,
            assistant_config_path=str(_assistant_config(tmp_path, url)),
            allowed_senders="feishu:ou_1",
            app_id="test",
            app_secret="test",
            audit_db=str(tmp_path / "audit.sqlite3"),
        )
        out = handle_feishu_ws_event(
            _message_payload(text="查看 HK lx 调度历史、业务窗口和系统定时任务状态"),
            settings=settings,
            reply_fn=_reply_collector(replies),
            reaction_fn=lambda **kwargs: {"code": 0},
            execute_tool_fn=lambda *args, **kwargs: pytest.fail("unexpected Control execution"),
        )

    assert out["ok"], out
    assert len(requests) == 3
    scheduler = next(obs for obs in seen if obs.get("tool_name") == "scheduler_status")
    assert scheduler["value"]["schedule"]["key"] == "schedule"
    assert scheduler["value"]["state"]["last_run_utc_for_account"] == "2026-09-11T01:40:00+00:00"
    assert scheduler["value"]["state"]["last_notify_utc_for_account"] == "2026-09-11T01:41:00+00:00"
    tasks = next(obs for obs in seen if obs.get("tool_name") == "scheduled_tasks_read")
    assert tasks["value"]["tasks"]
    assert all(row["enabled"] == "disabled" for row in tasks["value"]["tasks"])
    assert all(row["active"] == "inactive" for row in tasks["value"]["tasks"])
    text = _reply_text(replies[-1])
    assert "启用=disabled；活动=inactive" in text
    assert explanation in text
    assert "未运行" not in text and "未通知" not in text
    assert probes
