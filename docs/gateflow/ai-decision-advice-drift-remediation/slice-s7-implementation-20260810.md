# Gateflow S7 Implementation

- Work unit: `ai-decision-advice-drift-remediation`
- Slice: `S7 - Material diff, safe receipt/Agent parity and docs closeout`
- Status: implementation complete; DeepReview fixes verified

## Implemented contract

- Daily Brief diff treats action transitions, `needs_review -> keep`, and a
  same-action `switch` target change as material. Source, wording, cutoff,
  record-id and reuse-only changes remain non-material.
- Plain and Feishu-card renderers place aggregate `AI建议` inside Sell Put and
  Covered Call, followed by `策略候选` / `新增策略候选`. Legal zero candidate
  families remain visible, and candidate alerts can name an older selected
  contract without relisting it as a new candidate.
- `keep` makes no safety claim. `switch`, `defer`, and `needs_review` render a
  concise reason and at most three sanitized supporting HTTPS sources with the
  actual hostname visible.
- `daily_decision_brief_read` uniquely binds the brief projection back to the
  same run/account formal Advice JSONL. It returns allowlisted bindings,
  actions, selected candidate, internal/external refs, validation, versions,
  reuse state and evidence cutoff without running search or a model. Missing,
  duplicate, malformed or identity-mismatched records fail closed.
- The design, Agent handbook and deployment guide now document the prepared PM
  authority, prepared option authority, anonymous observation set, formal
  Advice location, optional-provider degradation and controlled rollout checks.

## Scope amendment

S7's Agent contract requires the actual evidence cutoff to be part of the
formal record rather than inferred from display copy. The owner-boundary change
therefore also touches `src/application/ai_decision_advice/advice.py` and its
focused test so new and reused records persist `evidence_as_of`. The shared
notification-format assertion was updated only to admit the two approved
candidate subheadings. No provider, scheduling, Candidate Engine, notification
transport or public command behavior was added.

## Verification before review

```text
python3 -m pytest -q
  tests/test_ai_decision_advice_domain_diff.py
  tests/test_ai_decision_advice_render.py
  tests/test_daily_decision_brief_renderer.py
  tests/test_daily_decision_brief_agent_tool.py
  tests/test_ai_decision_advice_advice.py
  tests/test_notify_symbols_markdown.py
  tests/test_multi_tick_notify_format.py
=> 125 passed, 4 pre-existing legacy-renderer deprecation warnings

ruff check <S7 Python files and tests>
=> All checks passed

python3 -m py_compile <S7 Python files>
=> passed

git diff --check
=> passed
```

No DeepSeek, portfolio-management, OpenD, notification, release or deployment
side effect was invoked.
