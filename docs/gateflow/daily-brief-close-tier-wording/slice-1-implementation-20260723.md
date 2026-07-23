# Gateflow Implementation — Slice 1

- Work unit: `daily-brief-close-tier-wording`
- Slice: `1 — Correct Daily Brief presentation`
- Status: implementation complete; pending deepreview

## Changes

- Added an allowlisted Daily Brief projection for standard `close` actions:
  - `strong` → `强烈建议平仓`
  - `medium` → `建议平仓`
  - `weak` → `可观察平仓`
  - `optional` → `低价买回可选`
- Preserved `建议平仓` as the fallback for missing or unknown tiers.
- Added the four tier labels to the renderer's existing actionable label set so differentiated wording does not
  hide rows or lower the “需处理” count.
- Kept all combo/special close action wording unchanged.
- Renamed the detail label from `剩余年化` to `剩余权利金毛年化`; the value and formula are unchanged.
- Added a full-message regression covering all four tiers plus missing/unknown fallback and actionable count.

## Scope Confirmation

No change was made to:

- P0 thresholds, scoring, tier generation, or decision state;
- P1/P2/P3 shadow policy;
- Daily Brief service selection or close-action derivation;
- notification scheduling, configuration, state, or delivery;
- compact/legacy notification renderers.

## Validation

```text
PYTHONPYCACHEPREFIX=/tmp/options_monitor_daily_brief_tier_pycache \
  python3.12 -m pytest -q -p no:cacheprovider tests/test_daily_decision_brief_renderer.py
25 passed in 0.13s

PYTHONPYCACHEPREFIX=/tmp/options_monitor_daily_brief_tier_pycache \
  python3.12 -m pytest -q -p no:cacheprovider \
    tests/test_daily_decision_brief_renderer.py \
    tests/test_feishu_bot.py \
    tests/test_multi_tick_notify_format.py
55 passed, 4 pre-existing legacy-renderer deprecation warnings in 1.26s

python3.12 -m ruff check \
  src/application/daily_decision_brief_renderer.py \
  tests/test_daily_decision_brief_renderer.py
All checks passed

git diff --check
passed
```

The clean worktree has no local `.venv`; validation therefore used the host's supported Python 3.12 environment
while loading source and tests from this worktree.

