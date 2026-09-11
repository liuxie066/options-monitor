from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.application.bot import channel_facade, local_harness
from src.application.bot.model_config import PiModelSettings
from tests.bot_eval import prompt_rules_eval as eval_driver


def _cash(account: str, amount: float, as_of: str) -> dict:
    return {
        "ok": True,
        "data": {
            "account": account,
            "cash_secured_used_cny": None,
            "cash_available_total_cny": None,
            "cash_free_total_cny": None,
            "cash_secured_total_by_ccy": {"USD": 100.0},
            "cash_secured_usage_reliable": True,
            "cash_available_by_currency": {"USD": amount},
            "cash_balance_reliable": True,
            "cash_balance_unavailable_by_row": {},
            "exchange_rates": {},
            "cny_conversion_complete": False,
            "cny_conversion_missing_rates": ["USDCNY"],
            "freshness": {"status": "fresh", "as_of": as_of, "kind": "source_snapshot"},
        },
    }


def _call(account: str, amount: float, as_of: str, **extra: object) -> dict:
    return {
        "tool_name": "query_cash_headroom",
        "effective_payload": {
            "config_key": "us",
            "config_path": "$CONFIG_PATH",
            "account": account,
            **extra,
        },
        "response": _cash(account, amount, as_of),
    }


def _semantics(name: str) -> dict[str, str]:
    return {key: f"human check for {key}" for key in eval_driver.CASE_SEMANTICS[name]}


def _pack() -> dict:
    as_of = "2026-09-10T20:00:00+00:00"
    scope = {
        "config_key": "us",
        "accounts": ["lx", "sy"],
        "as_of": as_of,
        "currency": "USD",
        "period": "current",
    }
    failure = {
        "tool_name": "query_cash_headroom",
        "effective_payload": {
            "config_key": "us",
            "config_path": "$CONFIG_PATH",
            "account": "lx",
            "top": 5,
        },
        "response": {
            "ok": False,
            "error": {
                "code": "INPUT_ERROR",
                "message": "remove top and retry with valid arguments",
                "retryable": False,
            },
        },
    }
    return {
        "schema_version": eval_driver.PACK_SCHEMA,
        "evidence_class": "synthetic_test_only",
        "source": {
            "authorization_ref": "contract-test",
            "captured_at": "2026-09-11T13:00:00+08:00",
            "description": "Synthetic values used only by the driver contract test.",
        },
        "cases": [
            {
                "name": "follow_up",
                "messages": [
                    {"text": "比较现金后给出判断", "expected_calls": [_call("lx", 900.0, as_of)]},
                    {"text": "结论呢？", "expected_calls": [_call("lx", 900.0, as_of)]},
                ],
                "authorized_scope": scope,
                "semantic_requirements": _semantics("follow_up"),
            },
            {
                "name": "dual_account",
                "messages": [
                    {
                        "text": "比较 lx 和 sy 的可用现金",
                        "expected_calls": [_call("lx", 900.0, as_of), _call("sy", 700.0, as_of)],
                    }
                ],
                "authorized_scope": scope,
                "semantic_requirements": _semantics("dual_account"),
            },
            {
                "name": "failure_recovery",
                "messages": [
                    {
                        "text": "读取 lx 现金，失败后按提示纠正",
                        "expected_calls": [failure, _call("lx", 900.0, as_of)],
                    }
                ],
                "authorized_scope": scope,
                "semantic_requirements": _semantics("failure_recovery"),
            },
        ],
    }


def test_pack_is_closed_uses_real_compaction_and_never_promotes_synthetic() -> None:
    pack = _pack()
    assert eval_driver.validate_pack(pack, live=False) is pack
    with pytest.raises(ValueError, match="fixed_redacted"):
        eval_driver.validate_pack(pack)

    wrong = _pack()
    wrong["cases"][1]["messages"][0]["expected_calls"][1]["response"]["data"]["account"] = "lx"
    with pytest.raises(ValueError, match="scope"):
        eval_driver.validate_pack(wrong, live=False)

    conflicting_scope = _pack()
    conflicting_scope["cases"][1]["messages"][0]["expected_calls"][0]["response"]["data"]["scope"] = {"accounts": ["sy"]}
    with pytest.raises(ValueError, match="scope"):
        eval_driver.validate_pack(conflicting_scope, live=False)


def test_fixed_reads_fail_closed_and_preserve_exact_effective_payload() -> None:
    pack = eval_driver.validate_pack(_pack(), live=False)
    case = next(item for item in pack["cases"] if item["name"] == "dual_account")
    expected = case["messages"][0]["expected_calls"]
    reads = eval_driver.FixedReads(expected, "/tmp/eval-config.json", case["authorized_scope"])

    sy_payload = eval_driver._resolve_payload(expected[1]["effective_payload"], reads.config_path)
    lx_payload = eval_driver._resolve_payload(expected[0]["effective_payload"], reads.config_path)
    assert reads("query_cash_headroom", sy_payload)["ok"] is True
    assert reads("query_cash_headroom", lx_payload)["ok"] is True
    assert reads("query_cash_headroom", lx_payload)["ok"] is False
    assert reads.passed() is False
    assert reads.violations == ["unexpected call #3: query_cash_headroom"]
    assert reads.records[0]["effective_payload"]["config_path"] == "$CONFIG_PATH"


def test_runtime_config_accounts_are_independent_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    pack = eval_driver.validate_pack(_pack(), live=False)
    monkeypatch.setattr(eval_driver, "load_runtime_config", lambda **_kwargs: (Path("config.json"), {"accounts": ["lx"]}))
    with pytest.raises(ValueError, match="sy"):
        eval_driver.validate_runtime_accounts(pack, "config.json")


def test_parent_stops_before_trials_when_model_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pack = _pack()
    pack["evidence_class"] = "fixed_redacted"
    pack_path = tmp_path / "pack.json"
    assistant_path = tmp_path / "assistant.json"
    config_path = tmp_path / "config.json"
    output_path = tmp_path / "report.json"
    pack_path.write_text(json.dumps(pack), encoding="utf-8")
    assistant_path.write_text("{}", encoding="utf-8")
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(eval_driver, "validate_runtime_accounts", lambda *_args: None)

    def unexpected_worker(*_args, **_kwargs):
        raise AssertionError("worker subprocess must not start")

    monkeypatch.setattr(eval_driver.subprocess, "run", unexpected_worker)
    report = eval_driver.run_parent(str(pack_path), str(assistant_path), str(config_path), str(output_path))

    assert report["trials"] == []
    assert report["model_evaluation_complete"] is False
    assert report["blocked_reason"]


def test_direct_cli_finds_repo_imports_outside_repo(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(Path(eval_driver.__file__).resolve()), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_baseline_compiler_matches_scene_compilation_semantics() -> None:
    compiler = eval_driver._baseline_compiler(eval_driver.BASELINE_REF)
    manifest = json.loads(
        eval_driver._git_text(
            eval_driver.BASELINE_REF,
            "src/application/bot/om_chat.scene.json",
        )
    )
    prompt, fragments = compiler(manifest["prompt_fragments"])

    expected = [
        eval_driver._git_text(
            eval_driver.BASELINE_REF,
            f"src/application/bot/{name}",
        ).strip()
        for name in manifest["prompt_fragments"]
    ]
    assert prompt == "\n\n".join(expected)
    assert fragments[0]["sha256"] == hashlib.sha256(expected[0].encode()).hexdigest()


def test_schedule_and_turn_grouping_are_explicit() -> None:
    schedule = eval_driver._schedule()
    assert len(schedule) == 18
    assert schedule[:2] == [("follow_up", 1, "A"), ("follow_up", 1, "B")]
    assert [(item[1], item[2]) for item in schedule if item[0] == "follow_up"] == [
        (1, "A"), (1, "B"), (2, "B"), (2, "A"), (3, "A"), (3, "B")
    ]

    raw = [
        {"event_type": "turn_start", "data": {}},
        {"event_type": "model_turn_completed", "data": {}},
        {"event_type": "tool_execution_start", "data": {"call_id": "lx", "tool_name": "query_cash_headroom"}},
        {"event_type": "tool_execution_end", "data": {"call_id": "lx", "tool_name": "query_cash_headroom", "ok": True}},
        {"event_type": "tool_execution_start", "data": {"call_id": "sy", "tool_name": "query_cash_headroom"}},
        {"event_type": "tool_execution_end", "data": {"call_id": "sy", "tool_name": "query_cash_headroom", "ok": True}},
        {"event_type": "turn_end", "data": {}},
    ]
    app = [
        {"type": "tool_call", "payload": {"tool_call_id": "lx", "tool_name": "query_cash_headroom"}},
        {"type": "tool_call", "payload": {"tool_call_id": "sy", "tool_name": "query_cash_headroom"}},
    ]
    groups, passed = eval_driver._groups(raw, app)
    assert passed is True
    assert [item["tool_name"] for item in groups[0]["calls"]] == ["query_cash_headroom", "query_cash_headroom"]


def test_worker_uses_real_channel_and_host_with_only_pi_and_reads_replaced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pack = eval_driver.validate_pack(_pack(), live=False)
    config_path = tmp_path / "config.us.json"
    assistant_path = tmp_path / "assistant.json"
    config_path.write_text("{}", encoding="utf-8")
    assistant_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        channel_facade,
        "resolve_trusted_config_scope",
        lambda **_kwargs: (
            "us",
            str(config_path.resolve()),
            "path:" + hashlib.sha256(str(config_path.resolve()).encode()).hexdigest(),
        ),
    )
    monkeypatch.setattr(channel_facade, "_channel_model_gate", lambda _path: None)
    monkeypatch.setattr(local_harness, "load_assistant_bot_settings", lambda **_kwargs: (frozenset(), "eager", None))
    model = PiModelSettings(
        provider="ollama",
        api_kind="openai-completions",
        model="contract-test",
        base_url="http://127.0.0.1:11434/v1",
        api_key_env="",
        credential_name="",
        timeout_seconds=90,
        context_window_tokens=24_000,
        max_output_tokens=2_048,
        max_attempts=1,
    )
    monkeypatch.setattr(local_harness, "_resolve_pi_model", lambda **_kwargs: (model, None, None))
    def fake_pi(_start, *, on_event, on_tool_call, on_proposed, **_kwargs):
        usage = {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 2}
        on_event({"event_type": "agent_start", "data": {}})
        on_event({"event_type": "turn_start", "data": {}})
        on_event({"event_type": "model_turn_completed", "data": {"stop_reason": "toolUse", "attempt_count": 1, "model_retry_count": 0, "usage": usage, "usage_total": usage}})
        for call_id, account in (("sy", "sy"), ("lx", "lx")):
            on_event({"event_type": "tool_execution_start", "data": {"call_id": call_id, "tool_name": "query_cash_headroom"}})
            observation = on_tool_call({"call_id": call_id, "tool_name": "query_cash_headroom", "arguments": {"account": account}})
            on_event({"event_type": "tool_execution_end", "data": {"call_id": call_id, "tool_name": "query_cash_headroom", "ok": observation.get("ok") is True}})
        on_event({"event_type": "turn_end", "data": {"stop_reason": "toolUse", "usage": usage}})
        on_event({"event_type": "turn_start", "data": {}})
        on_event({"event_type": "model_turn_completed", "data": {"stop_reason": "toolUse", "attempt_count": 1, "model_retry_count": 0, "usage": usage, "usage_total": usage}})
        call_id = "answer"
        on_event({"event_type": "tool_execution_start", "data": {"call_id": call_id, "tool_name": "submit_answer"}})
        submitted = on_tool_call({"call_id": call_id, "tool_name": "submit_answer", "arguments": {"mode": "conceptual", "status": "complete", "answer_markdown": "结论：已完成固定读取。", "claims": []}})
        on_event({"event_type": "tool_execution_end", "data": {"call_id": call_id, "tool_name": "submit_answer", "ok": submitted["observation"]["ok"] is True}})
        on_event({"event_type": "turn_end", "data": {"stop_reason": "toolUse", "usage": usage}})
        on_event({"event_type": "agent_end", "data": {}})
        approved = submitted["approved_answer"]["text"]
        proposal = {"status": "answered", "text": approved, "control_request": None, "termination_reason": "stop", "usage": usage}
        decision = on_proposed(proposal)
        return {"ok": True, "result": {**proposal, "committed": decision == "commit"}}

    monkeypatch.setattr(eval_driver.bot_host, "run_pi_agent", fake_pi)
    trial = eval_driver.run_worker(pack, "dual_account", "B", 1, str(assistant_path), str(config_path))

    assert trial["structural_pass"] is True, json.dumps(trial, ensure_ascii=False, default=str)
    assert trial["same_turn_independent_reads"] is True
    assert trial["messages"][0]["fixed_reads_pass"] is True
    assert [item["effective_payload"]["account"] for item in trial["messages"][0]["fixed_reads"]] == ["sy", "lx"]


def test_review_requires_exact_non_offsettable_booleans(tmp_path: Path) -> None:
    trials = []
    for name, repetition, variant in eval_driver._schedule():
        trials.append({
            "trial_id": f"{name}.{repetition}.{variant}",
            "case": name,
            "variant": variant,
            "repetition": repetition,
            "structural_pass": True,
            "semantic_review": {key: {"pass": None, "evidence": ""} for key in eval_driver.CASE_SEMANTICS[name]},
            "semantic_pass": None,
        })
    report = {"schema_version": eval_driver.REPORT_SCHEMA, "model_evaluation_complete": True, "trials": trials}
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    review = {
        "schema_version": eval_driver.REVIEW_SCHEMA,
        "report_sha256": "sha256:" + hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "trials": {
            trial["trial_id"]: {
                key: {"pass": True, "evidence": f"reviewed {key}"}
                for key in eval_driver.CASE_SEMANTICS[trial["case"]]
            }
            for trial in trials
        },
    }
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")

    reviewed = eval_driver.apply_review(str(report_path), str(review_path), str(tmp_path / "all-pass.json"))
    assert reviewed["small_sample_acceptance_pass"] is True

    review["trials"]["dual_account.1.B"]["facts_and_relation"]["pass"] = False
    review_path.write_text(json.dumps(review), encoding="utf-8")
    reviewed = eval_driver.apply_review(str(report_path), str(review_path), str(tmp_path / "out.json"))
    assert reviewed["small_sample_acceptance_pass"] is False
    assert next(item for item in reviewed["trials"] if item["trial_id"] == "dual_account.1.B")["semantic_pass"] is False

    report["model_evaluation_complete"] = False
    report_path.write_text(json.dumps(report), encoding="utf-8")
    review["report_sha256"] = "sha256:" + hashlib.sha256(report_path.read_bytes()).hexdigest()
    review["trials"]["dual_account.1.B"]["facts_and_relation"]["pass"] = True
    review_path.write_text(json.dumps(review), encoding="utf-8")
    reviewed = eval_driver.apply_review(str(report_path), str(review_path), str(tmp_path / "model-incomplete.json"))
    assert reviewed["small_sample_acceptance_pass"] is False


def test_review_rejects_duplicate_or_missing_trial_ids(tmp_path: Path) -> None:
    trials = [
        {"trial_id": f"{name}.{repetition}.{variant}", "case": name}
        for name, repetition, variant in eval_driver._schedule()
    ]
    trials[-1]["trial_id"] = trials[0]["trial_id"]
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"schema_version": eval_driver.REPORT_SCHEMA, "trials": trials}), encoding="utf-8")
    review_path = tmp_path / "review.json"
    review_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="complete"):
        eval_driver.apply_review(str(report_path), str(review_path), str(tmp_path / "out.json"))
