from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.application.bot import host as bot_host
from src.application.bot import scene, tools as bot_tools
from src.application.bot.channel_facade import run_channel_request
from src.application.bot.model_config import load_assistant_llm_config, model_api_key_configured
from src.application.account_config import accounts_from_config
from src.application.agent_tool_config import load_runtime_config

BASELINE_REF = "HEAD"
PACK_SCHEMA = "om.bot.prompt_rules_pack.v1"
REPORT_SCHEMA = "om.bot.prompt_rules_eval.v1"
REVIEW_SCHEMA = "om.bot.prompt_rules_review.v1"
CASE_NAMES = ("follow_up", "dual_account", "failure_recovery")
COMMON_SEMANTICS = {"task_completion", "account_scope", "facts_and_relation", "no_fabrication"}
CASE_SEMANTICS = {
    "follow_up": COMMON_SEMANTICS | {"first_turn_completion", "follow_up_continuity"},
    "dual_account": COMMON_SEMANTICS,
    "failure_recovery": COMMON_SEMANTICS | {"recovery_path"},
}
PROTOCOL_TOOLS = {"tool_directory", "submit_answer", "request_control_preview"}

def _closed(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} must contain exactly {sorted(keys)}")
    return value

def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()

def validate_pack(pack: Any, *, live: bool = True) -> dict[str, Any]:
    pack = _closed(pack, {"schema_version", "evidence_class", "source", "cases"}, "pack")
    if pack["schema_version"] != PACK_SCHEMA:
        raise ValueError(f"schema_version must be {PACK_SCHEMA}")
    if pack["evidence_class"] not in ({"fixed_redacted"} if live else {"fixed_redacted", "synthetic_test_only"}):
        raise ValueError("live evaluation requires evidence_class=fixed_redacted")
    source = _closed(pack["source"], {"authorization_ref", "captured_at", "description"}, "source")
    _text(source["authorization_ref"], "source.authorization_ref")
    _text(source["description"], "source.description")
    try:
        captured = datetime.fromisoformat(_text(source["captured_at"], "source.captured_at").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("source.captured_at must be ISO-8601") from exc
    if captured.tzinfo is None:
        raise ValueError("source.captured_at must include a timezone")
    cases = pack["cases"]
    if not isinstance(cases, list) or {item.get("name") for item in cases if isinstance(item, dict)} != set(CASE_NAMES):
        raise ValueError(f"cases must contain exactly {list(CASE_NAMES)}")
    if len(cases) != len(CASE_NAMES):
        raise ValueError("case names must be unique")
    for raw_case in cases:
        case = _closed(raw_case, {"name", "messages", "authorized_scope", "semantic_requirements"}, "case")
        name = _text(case["name"], "case.name")
        scope = _closed(case["authorized_scope"], {"config_key", "accounts", "as_of", "currency", "period"}, f"{name}.authorized_scope")
        if scope["config_key"] not in {"us", "hk"}:
            raise ValueError(f"{name}.authorized_scope.config_key must be us or hk")
        if not isinstance(scope["accounts"], list) or not scope["accounts"] or any(account not in {"lx", "sy"} for account in scope["accounts"]):
            raise ValueError(f"{name}.authorized_scope.accounts must contain lx/sy")
        for key in ("as_of", "currency", "period"):
            _text(scope[key], f"{name}.authorized_scope.{key}")
        semantics = _closed(case["semantic_requirements"], CASE_SEMANTICS[name], f"{name}.semantic_requirements")
        for key, value in semantics.items():
            _text(value, f"{name}.semantic_requirements.{key}")
        messages = case["messages"]
        expected_count = 2 if name == "follow_up" else 1
        if not isinstance(messages, list) or len(messages) != expected_count:
            raise ValueError(f"{name}.messages must contain {expected_count} item(s)")
        calls: list[dict[str, Any]] = []
        for index, raw_message in enumerate(messages):
            message = _closed(raw_message, {"text", "expected_calls"}, f"{name}.messages[{index}]")
            _text(message["text"], f"{name}.messages[{index}].text")
            if not isinstance(message["expected_calls"], list) or not message["expected_calls"]:
                raise ValueError(f"{name}.messages[{index}].expected_calls must be non-empty")
            for call_index, raw_call in enumerate(message["expected_calls"]):
                call = _closed(raw_call, {"tool_name", "effective_payload", "response"}, f"{name}.call[{call_index}]")
                tool_name = _text(call["tool_name"], f"{name}.call.tool_name")
                if tool_name not in bot_tools.available_read_tools():
                    raise ValueError(f"{name}.call.tool_name must be a registered pure read tool")
                payload = call["effective_payload"]
                if not isinstance(payload, dict) or payload.get("config_path") != "$CONFIG_PATH" or payload.get("config_key") != scope["config_key"]:
                    raise ValueError(f"{name} calls require full payload with config_key and config_path=$CONFIG_PATH")
                if payload.get("account") and payload["account"] not in scope["accounts"]:
                    raise ValueError(f"{name} call account is outside authorized_scope.accounts")
                response = call["response"]
                if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
                    raise ValueError(f"{name}.call.response must contain boolean ok")
                if response["ok"]:
                    data = response.get("data")
                    account = payload.get("account")
                    if not isinstance(data, dict) or (account and data.get("account") != account) or not _response_scope_matches(response, payload, scope):
                        raise ValueError(f"{name} successful response scope must match its effective payload")
                    observation = bot_tools.compact_observation(tool_name, response, payload)
                    coverage = observation.get("coverage") if isinstance(observation, dict) else None
                    coverage_scope = coverage.get("scope") if isinstance(coverage, dict) else None
                    if observation.get("ok") is not True or (account and (not isinstance(coverage_scope, dict) or coverage_scope.get("account") != account)):
                        raise ValueError(f"{name} response must pass the real compact observation contract")
                else:
                    error = response.get("error")
                    if not isinstance(error, dict) or not isinstance(error.get("retryable"), bool):
                        raise ValueError(f"{name} failed response requires error.retryable")
                calls.append(call)
        if name == "dual_account":
            if [call["tool_name"] for call in calls] != ["query_cash_headroom", "query_cash_headroom"] or [call["effective_payload"].get("account") for call in calls] != ["lx", "sy"] or any(not call["response"]["ok"] for call in calls):
                raise ValueError("dual_account requires successful query_cash_headroom calls for lx then sy")
        if name == "failure_recovery":
            if len(calls) != 2 or calls[0]["response"]["ok"] or not calls[1]["response"]["ok"] or calls[0]["effective_payload"] == calls[1]["effective_payload"]:
                raise ValueError("failure_recovery requires one fixed failure followed by different successful arguments")
    return pack

def load_pack(path: str, *, live: bool = True) -> tuple[dict[str, Any], str]:
    raw = Path(path).read_bytes()
    return validate_pack(json.loads(raw), live=live), "sha256:" + hashlib.sha256(raw).hexdigest()

def validate_runtime_accounts(pack: dict[str, Any], config_path: str) -> None:
    _, config = load_runtime_config(config_path=config_path)
    configured = set(accounts_from_config(config, fallback=()))
    requested = {account for case in pack["cases"] for account in case["authorized_scope"]["accounts"]}
    if not requested <= configured:
        raise ValueError(f"pack accounts are outside runtime config: {sorted(requested - configured)}")

class FixedReads:
    def __init__(self, calls: list[dict[str, Any]], config_path: str, scope: dict[str, Any], *, ordered: bool = False) -> None:
        self.expected = copy.deepcopy(calls)
        self.remaining = list(range(len(calls)))
        self.config_path = str(Path(config_path).resolve())
        self.scope = scope
        self.ordered = ordered
        self.records: list[dict[str, Any]] = []
        self.violations: list[str] = []

    def __call__(self, tool_name: str, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
        candidates = self.remaining[:1] if self.ordered else self.remaining
        matched_index = next((index for index in candidates if tool_name == self.expected[index]["tool_name"] and payload == _resolve_payload(self.expected[index]["effective_payload"], self.config_path)), None)
        expected = self.expected[matched_index] if matched_index is not None else None
        matched = expected is not None
        record = {"tool_name": tool_name, "effective_payload": _display_payload(payload, self.config_path), "effective_payload_sha256": _json_hash(payload), "matched": matched}
        self.records.append(record)
        if not matched:
            self.violations.append(f"unexpected call #{len(self.records)}: {tool_name}")
            return {"ok": False, "error": {"code": "POLICY_ERROR", "message": "fixed evaluation call mismatch", "retryable": False}}
        self.remaining.remove(matched_index)
        response = copy.deepcopy(expected["response"])
        if response.get("ok") is True and not _response_scope_matches(response, payload, self.scope):
            self.violations.append(f"response scope mismatch #{len(self.records)}")
            return {"ok": False, "error": {"code": "POLICY_ERROR", "message": "fixed evaluation scope mismatch", "retryable": False}}
        record["response_sha256"] = _json_hash(response)
        record["ok"] = response.get("ok") is True
        return response

    def passed(self) -> bool:
        return not self.violations and not self.remaining

def _resolve_payload(value: dict[str, Any], config_path: str) -> dict[str, Any]:
    return {key: config_path if item == "$CONFIG_PATH" else copy.deepcopy(item) for key, item in value.items()}

def _display_payload(value: dict[str, Any], config_path: str) -> dict[str, Any]:
    return {key: "$CONFIG_PATH" if key == "config_path" and item == config_path else copy.deepcopy(item) for key, item in value.items()}

def _response_scope_matches(response: dict[str, Any], payload: dict[str, Any], scope: dict[str, Any]) -> bool:
    data = response.get("data")
    if not isinstance(data, dict):
        return False
    found = data.get("scope") if isinstance(data.get("scope"), dict) else {}
    freshness = data.get("freshness") if isinstance(data.get("freshness"), dict) else {}
    account = payload.get("account")
    declared_as_of = found.get("as_of") or freshness.get("as_of") or data.get("as_of")
    declared_accounts = found.get("accounts")
    currency_values = data.get("cash_available_by_currency") if isinstance(data.get("cash_available_by_currency"), dict) else {}
    accounts_match = declared_accounts in (None, []) or isinstance(declared_accounts, list) and (
        set(declared_accounts) == {account} if account else set(declared_accounts) <= set(scope["accounts"])
    )
    return declared_as_of == scope["as_of"] and (not found.get("config_key") or found.get("config_key") == scope["config_key"]) and (not found.get("currency") or found.get("currency") == scope["currency"]) and (not found.get("period") or found.get("period") == scope["period"]) and accounts_match and (not account or account in scope["accounts"] and data.get("account") == account and (not found.get("account") or found.get("account") == account)) and (payload.get("account") is None or scope["currency"] in currency_values)

def _json_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()

def _git_text(ref: str, path: str) -> str:
    return subprocess.run(["git", "show", f"{ref}:{path}"], cwd=REPO, check=True, capture_output=True, text=True).stdout

def _baseline_compiler(ref: str):
    manifest = json.loads(_git_text(ref, "src/application/bot/om_chat.scene.json"))
    names = manifest["prompt_fragments"]
    texts = {name: _git_text(ref, f"src/application/bot/{name}").strip() for name in names}

    def compile_fragments(value: Any) -> tuple[str, list[dict[str, Any]]]:
        if value != names:
            raise ValueError("working Scene prompt fragments differ from baseline")
        fragments = [{"path": name, "sha256": hashlib.sha256(texts[name].encode()).hexdigest(), "chars": len(texts[name])} for name in names]
        return "\n\n".join(texts[name] for name in names), fragments

    return compile_fragments

def _groups(raw_events: list[dict[str, Any]], app_events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    calls = [(event["payload"].get("tool_call_id"), event["payload"].get("tool_name")) for event in app_events if event["type"] == "tool_call"]
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for sequence, event in enumerate(raw_events):
        kind, data = event.get("event_type"), event.get("data") or {}
        if kind == "turn_start":
            current = {"turn": len(groups) + 1, "model_completed": False, "calls": [], "closed": False}
            groups.append(current)
        elif current is not None and kind == "model_turn_completed":
            current["model_completed"] = True
        elif current is not None and kind == "tool_execution_start":
            current["calls"].append({"call_id": data.get("call_id"), "tool_name": data.get("tool_name"), "start_sequence": sequence})
        elif current is not None and kind == "tool_execution_end":
            match = next((item for item in reversed(current["calls"]) if item["call_id"] == data.get("call_id") and "end_sequence" not in item), None)
            if match:
                match["end_sequence"] = sequence
                match["ok"] = data.get("ok")
        elif current is not None and kind == "turn_end":
            current["closed"] = True
            current = None
    grouped = [(item["call_id"], item["tool_name"]) for group in groups for item in group["calls"]]
    complete = bool(groups) and grouped == calls and all(group["model_completed"] and group["closed"] and all("end_sequence" in item for item in group["calls"]) for group in groups)
    sole_protocol = all(not ({item["tool_name"] for item in group["calls"]} & PROTOCOL_TOOLS) or len(group["calls"]) == 1 for group in groups)
    return groups, complete and sole_protocol

def _model_identity(path: str) -> dict[str, Any]:
    raw, error = load_assistant_llm_config(config_path=path, require_config=True)
    if not raw:
        return {"configured": False, "error": error or "model_not_configured", "attested": "unverified"}
    return {"configured": True, "provider": raw.get("provider"), "model": raw.get("model"), "settings": {key: raw.get(key) for key in ("timeout_seconds", "context_window_tokens", "max_output_tokens", "max_attempts")}, "attested": "unverified"}

def run_worker(pack: dict[str, Any], case_name: str, variant: str, repetition: int, assistant_config: str, config_path: str) -> dict[str, Any]:
    case = next(item for item in pack["cases"] if item["name"] == case_name)
    trial_id = f"{case_name}.{repetition}.{variant}"
    raw_holder: dict[str, list[dict[str, Any]]] = {"events": []}
    real_pi = bot_host.run_pi_agent

    def traced_pi(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original = kwargs.get("on_event")
        def on_event(event: dict[str, Any]) -> None:
            raw_holder["events"].append(copy.deepcopy(event))
            if original:
                original(event)
        kwargs["on_event"] = on_event
        return real_pi(*args, **kwargs)

    messages: list[dict[str, Any]] = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="om-bot-prompt-rules-") as root:
        old_root = os.environ.get("OM_RUNTIME_ROOT")
        os.environ["OM_RUNTIME_ROOT"] = root
        scene.load_general_scene.cache_clear()
        try:
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(bot_host, "run_pi_agent", traced_pi))
                if variant == "A":
                    stack.enter_context(mock.patch.object(scene, "_compile_prompt_fragments", _baseline_compiler(BASELINE_REF)))
                for index, message in enumerate(case["messages"]):
                    raw_holder["events"] = []
                    fixed = FixedReads(message["expected_calls"], config_path, case["authorized_scope"], ordered=case_name == "failure_recovery")
                    with mock.patch.object(bot_tools, "call_read_tool", fixed):
                        result = run_channel_request(user_message=message["text"], config_key=None, config_path=config_path, assistant_config_path=assistant_config, channel="bot-prompt-rules-eval", sender_id=trial_id, conversation_id=trial_id, host_db_path=str(Path(root) / "host.sqlite3"), authenticated_sender_id=trial_id)
                    events = [{"type": event.type, "payload": event.payload, "visible_ref": event.visible_ref} for event in result.events]
                    groups, grouping_ok = _groups(raw_holder["events"], events)
                    model_turns = sum(event.get("event_type") == "model_turn_completed" for event in raw_holder["events"])
                    messages.append({"index": index, "question": message["text"], "answer": result.user_response, "status": result.status, "error": result.error, "run_id": result.run_id, "events": events, "raw_agent_events": raw_holder["events"], "actual_model_turns": model_turns, "turn_groups": groups, "event_grouping_pass": grouping_ok, "fixed_reads": fixed.records, "fixed_reads_pass": fixed.passed(), "fixed_read_violations": fixed.violations})
        finally:
            scene.load_general_scene.cache_clear()
            if old_root is None:
                os.environ.pop("OM_RUNTIME_ROOT", None)
            else:
                os.environ["OM_RUNTIME_ROOT"] = old_root
    business_groups = [[item["tool_name"] for item in group["calls"] if item["tool_name"] not in PROTOCOL_TOOLS] for message in messages for group in message["turn_groups"]]
    same_turn = case_name != "dual_account" or ["query_cash_headroom", "query_cash_headroom"] in business_groups
    structural = all(item["status"] == "answered" and item["answer"].strip() and item["actual_model_turns"] > 0 and item["fixed_reads_pass"] and item["event_grouping_pass"] for item in messages) and same_turn
    provenance = [next((event["payload"] for event in item["events"] if event["type"] == "scene_prepared"), None) for item in messages]
    return {"trial_id": trial_id, "case": case_name, "variant": variant, "repetition": repetition, "elapsed_seconds": round(time.monotonic() - started, 3), "model_identity": _model_identity(assistant_config), "prompt_provenance": provenance, "messages": messages, "same_turn_independent_reads": same_turn, "structural_pass": structural, "semantic_review": {key: {"pass": None, "evidence": ""} for key in sorted(CASE_SEMANTICS[case_name])}, "semantic_pass": None}

def _schedule() -> list[tuple[str, int, str]]:
    return [(name, repetition, variant) for repetition in range(1, 4) for name in CASE_NAMES for variant in (("A", "B") if repetition % 2 else ("B", "A"))]

def _write_report(path: str, report: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(temp, target)

def _file_hash(path: str) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()

def run_parent(pack_path: str, assistant_config: str, config_path: str, output: str) -> dict[str, Any]:
    pack_raw = Path(pack_path).read_bytes()
    pack = validate_pack(json.loads(pack_raw))
    pack_hash = "sha256:" + hashlib.sha256(pack_raw).hexdigest()
    validate_runtime_accounts(pack, config_path)
    assistant_hash, config_hash = _file_hash(assistant_config), _file_hash(config_path)
    report = {"schema_version": REPORT_SCHEMA, "evidence_class": "fixed_redacted", "baseline_ref": BASELINE_REF, "pack_sha256": pack_hash, "evidence_sha256": _json_hash([call["response"] for case in pack["cases"] for message in case["messages"] for call in message["expected_calls"]]), "assistant_config_sha256": assistant_hash, "runtime_config_sha256": config_hash, "started_at": datetime.now().astimezone().isoformat(), "configured_model_identity": _model_identity(assistant_config), "provider_attested_identity": "unverified", "trials": [], "model_evaluation_complete": False, "semantic_review_status": "pending_human_review", "small_sample_acceptance_pass": False}
    _write_report(output, report)
    model_raw, model_error = load_assistant_llm_config(config_path=assistant_config, require_config=True)
    credentials_ok, credential_error = model_api_key_configured(model_raw) if model_raw else (False, None)
    if model_error or not model_raw or not credentials_ok:
        report["blocked_reason"] = model_error or credential_error or "model_not_configured"
        report["finished_at"] = datetime.now().astimezone().isoformat()
        _write_report(output, report)
        return report
    with tempfile.TemporaryDirectory(prefix="om-bot-prompt-pack-") as root:
        frozen_pack = Path(root) / "pack.json"
        frozen_pack.write_bytes(pack_raw)
        if _file_hash(str(frozen_pack)) != pack_hash:
            raise RuntimeError("frozen pack hash mismatch")
        for name, repetition, variant in _schedule():
            command = [sys.executable, str(Path(__file__).resolve()), "_worker", "--pack", str(frozen_pack), "--assistant-config", assistant_config, "--config-path", config_path, "--case", name, "--variant", variant, "--repetition", str(repetition)]
            try:
                if _file_hash(assistant_config) != assistant_hash or _file_hash(config_path) != config_hash:
                    raise RuntimeError("assistant or runtime config changed during evaluation")
                message_count = 2 if name == "follow_up" else 1
                completed = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=message_count * 180 + 30)
                if _file_hash(assistant_config) != assistant_hash or _file_hash(config_path) != config_hash:
                    raise RuntimeError("assistant or runtime config changed during evaluation")
                if completed.returncode:
                    raise RuntimeError((completed.stderr or completed.stdout or "worker failed").strip()[-800:])
                trial = json.loads(completed.stdout)
            except Exception as exc:
                trial = {"trial_id": f"{name}.{repetition}.{variant}", "case": name, "variant": variant, "repetition": repetition, "structural_pass": False, "semantic_review": {key: {"pass": None, "evidence": ""} for key in sorted(CASE_SEMANTICS[name])}, "semantic_pass": None, "worker_error": f"{type(exc).__name__}: {exc}"}
            report["trials"].append(trial)
            _write_report(output, report)
    report["finished_at"] = datetime.now().astimezone().isoformat()
    report["model_evaluation_complete"] = len(report["trials"]) == 18 and all("worker_error" not in trial and trial.get("messages") and all(message.get("actual_model_turns", 0) > 0 for message in trial["messages"]) for trial in report["trials"])
    _write_report(output, report)
    return report

def apply_review(report_path: str, review_path: str, output: str) -> dict[str, Any]:
    report_raw = Path(report_path).read_bytes()
    report = json.loads(report_raw)
    expected_ids = {f"{name}.{repetition}.{variant}" for name, repetition, variant in _schedule()}
    actual_ids = [item.get("trial_id") for item in report.get("trials") or [] if isinstance(item, dict)]
    if report.get("schema_version") != REPORT_SCHEMA or len(actual_ids) != len(expected_ids) or len(set(actual_ids)) != len(actual_ids) or set(actual_ids) != expected_ids:
        raise ValueError("review report must be a complete prompt-rules report")
    review = _closed(json.loads(Path(review_path).read_text(encoding="utf-8")), {"schema_version", "report_sha256", "trials"}, "review")
    if review["schema_version"] != REVIEW_SCHEMA or review["report_sha256"] != "sha256:" + hashlib.sha256(report_raw).hexdigest():
        raise ValueError("review does not match report bytes")
    by_id = review["trials"]
    if not isinstance(by_id, dict) or set(by_id) != {item["trial_id"] for item in report["trials"]}:
        raise ValueError("review must cover every trial exactly once")
    for trial in report["trials"]:
        keys = CASE_SEMANTICS[trial["case"]]
        verdicts = _closed(by_id[trial["trial_id"]], keys, trial["trial_id"])
        for key, raw_verdict in verdicts.items():
            verdict = _closed(raw_verdict, {"pass", "evidence"}, f"{trial['trial_id']}.{key}")
            if not isinstance(verdict["pass"], bool) or not isinstance(verdict["evidence"], str) or not verdict["evidence"].strip():
                raise ValueError(f"{trial['trial_id']}.{key} requires boolean pass and evidence")
        trial["semantic_review"] = verdicts
        trial["semantic_pass"] = all(item["pass"] for item in verdicts.values())
    report["semantic_review_status"] = "reviewed"
    after = [item for item in report["trials"] if item["variant"] == "B"]
    report["small_sample_acceptance_pass"] = report.get("model_evaluation_complete") is True and len(after) == 9 and all(item.get("structural_pass") and item.get("semantic_pass") for item in after)
    _write_report(output, report)
    return report

def main() -> int:
    parser = argparse.ArgumentParser(description="Run the fixed-redacted Bot prompt-rules evaluation.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    for name in ("pack", "assistant-config", "config-path", "output"):
        run.add_argument(f"--{name}", required=True)
    review = sub.add_parser("review")
    for name in ("report", "review", "output"):
        review.add_argument(f"--{name}", required=True)
    worker = sub.add_parser("_worker")
    for name in ("pack", "assistant-config", "config-path", "case", "variant", "repetition"):
        worker.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    if args.command == "run":
        payload = run_parent(args.pack, args.assistant_config, args.config_path, args.output)
    elif args.command == "review":
        payload = apply_review(args.report, args.review, args.output)
    else:
        pack, _ = load_pack(args.pack)
        payload = run_worker(pack, args.case, args.variant, int(args.repetition), args.assistant_config, args.config_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("small_sample_acceptance_pass") is True else (0 if args.command == "_worker" else 1)
if __name__ == "__main__":
    raise SystemExit(main())
