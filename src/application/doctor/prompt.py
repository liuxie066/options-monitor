from __future__ import annotations

import json
from typing import Any


def build_doctor_prompt(*, evidence: dict[str, Any], diagnosis: dict[str, Any]) -> str:
    payload = {
        "task": "Analyze production quality for options-monitor from the provided redacted evidence and deterministic diagnosis.",
        "rules": [
            "Use only the provided evidence.",
            "Do not invent missing scheduler, account, holding, or market data.",
            "Distinguish expected schedule skips from runtime bugs.",
            "Only mark issue_candidate.create_issue=true when evidence points to a likely code or runtime bug.",
            "Return strict JSON with the requested fields.",
        ],
        "required_json_shape": {
            "status": "ok|warn|fail",
            "category": "ok|expected_skip|scheduler_unknown|scheduler_failed|runtime_failed|partial_account_failure|data_source_issue|notification_issue|position_maintenance_issue|suspected_runtime_bug|strategy_observation|insufficient_evidence",
            "confidence": "high|medium|low",
            "problem": "one concise paragraph",
            "impact": "one concise paragraph",
            "evidence": [{"source": "file or field", "observed": "value", "expected": "value"}],
            "ai_diagnosis": "reasoning summary",
            "strategy_observations": ["evidence-backed strategy observation"],
            "strategy_improvement_directions": ["evidence-backed strategy adjustment direction"],
            "suspected_code_area": ["src/application/...", "domain/domain/..."],
            "local_debug_steps": ["step 1", "step 2"],
            "issue_candidate": {"create_issue": False, "reason": "why or why not"},
        },
        "deterministic_diagnosis": diagnosis,
        "redacted_evidence": evidence,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
