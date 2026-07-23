# Gateflow Plan — Daily Brief Close Tier Wording

## Goal

修复 Daily Brief 把所有标准平仓建议统一显示为“建议平仓”的呈现缺陷，让通知忠实显示现有 P0
策略已经产出的 tier，同时把容易被误读为总收益年化的“剩余年化”明确为“剩余权利金毛年化”。

成功信号：

- `close_action=close` 且 tier 为 `strong`、`medium`、`weak`、`optional` 时，分别显示
  “强烈建议平仓”“建议平仓”“可观察平仓”“低价买回可选”。
- tier 缺失或未知时仍保守回退为“建议平仓”，不隐藏原有可操作项。
- 组合策略和特殊 `close_action` 的既有文案不变。
- 持仓筛选、需处理数量、close action、recommendation state 和策略判断均不改变。
- 收益明细显示“剩余权利金毛年化”，数值和计算来源不变。

## Scope

### Included

- `src/application/daily_decision_brief_renderer.py`
  - 在标准 `close` 动作的用户文案投影中加入 tier 到通知文案的确定性映射。
  - 将新增 tier 文案纳入现有 actionable-label 集合，保持展示与计数语义不变。
  - 修改剩余年化指标的展示名称。
- `tests/test_daily_decision_brief_renderer.py`
  - 覆盖四个已知 tier。
  - 覆盖 tier 缺失和未知时的回退。
  - 验证数据质量降级与特殊组合动作不回归。
  - 验证明细指标的新名称和旧名称不再出现。

### Non-goals

- 不修改 `domain/domain/close_advice.py` 的 P0 策略、阈值、评分或 tier 生成。
- 不启用或迁移 P1/P2/P3 shadow 策略。
- 不修改 `daily_decision_brief_service` 的 close action、recommendation state 或通知选择逻辑。
- 不修改通知频率、账号/市场配置、运行时配置、状态文件或生产数据。
- 不改变 compact/legacy 通知模板；本修复只处理 Daily Brief。
- 不发送真实通知，不部署、不发布版本。

## Direct Code Evidence

- `daily_decision_brief_renderer._position_status_label()` 当前先读取 `close_action`，对所有
  `close_action=close` 固定返回 `_CLOSE_ACTION_LABELS["close"] == "建议平仓"`，没有消费
  row 中已有的 `tier`。
- `domain/domain/close_advice.py` 已定义稳定的四档 tier 文案：
  `strong`、`medium`、`weak`、`optional`。
- `_position_has_advice()` 通过 `_POSITION_ACTIONABLE_LABELS` 判断展示和计数；新增文案必须同步纳入，
  否则强/弱/可选 tier 会被错误隐藏。
- `_position_close_details()` 当前把 `remaining_annualized_return` 显示为“剩余年化”，标签未说明这是
  剩余权利金的毛年化估算。

## Architecture Boundary

策略 tier 的生成权仍在 `domain/domain/close_advice.py`。本切片只在通知 owner
`src/application/daily_decision_brief_renderer.py` 中做用户文案投影，不反向影响 domain，也不让 renderer
重新计算 tier。映射采用固定 allowlist，不直接信任任意上游 `tier_label` 文本；未知输入沿用现有通用文案，
保持向后兼容和 fail-safe 展示。

## Public Contract

仅改变 Daily Brief 的用户可见文案：

| Input | Before | After |
|---|---|---|
| `close`, `strong` | 建议平仓 | 强烈建议平仓 |
| `close`, `medium` | 建议平仓 | 建议平仓 |
| `close`, `weak` | 建议平仓 | 可观察平仓 |
| `close`, `optional` | 建议平仓 | 低价买回可选 |
| `close`, missing/unknown tier | 建议平仓 | 建议平仓 |
| `remaining_annualized_return` label | 剩余年化 | 剩余权利金毛年化 |

数据 payload、持久化 schema、命令接口和配置 contract 均不变。

## Implementation Slice

### Slice 1 — Correct Daily Brief presentation

1. 在 renderer 中增加标准 close tier 的私有、确定性文案映射。
2. 在 `_position_status_label()` 中仅对 `close_action=close` 应用 tier 文案；其它 action 保持现有映射。
3. 将四档 tier 文案加入 `_POSITION_ACTIONABLE_LABELS`，保留缺失/未知 tier 的通用回退文案。
4. 把 close detail 标签改为“剩余权利金毛年化”。
5. 扩展 renderer 单元测试，直接证明四档、回退、计数/筛选、特殊动作和指标名称。

该切片是原子变更；不拆分策略和通知 rollout，也不引入配置开关。

## Validation

Focused:

```bash
./.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_daily_decision_brief_renderer.py
```

Notification regression:

```bash
./.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_daily_decision_brief_renderer.py \
  tests/test_feishu_bot.py \
  tests/test_multi_tick_notify_format.py
```

Static/diff:

```bash
./.venv/bin/python -m ruff check \
  src/application/daily_decision_brief_renderer.py \
  tests/test_daily_decision_brief_renderer.py
git diff --check
```

Acceptance assertions:

- 四档已知 tier 的最终消息文案正确。
- 缺失/未知 tier 仍显示为“建议平仓”并计入需处理数量。
- `close_put_keep_call` 等特殊动作保持原文案。
- 不可评估持仓仍不展示为可操作项。
- 新指标名称存在，旧的模糊名称不再出现。

## Rollback

这是单文件呈现映射和测试变更，无数据迁移。回滚该提交即可恢复旧文案；不会留下 runtime state 或 schema
兼容问题。

## Residual Risk

- 文案变准确不代表 P0 策略本身已经优化；弱 tier 是否应该正式 `hold` 仍属于证据门控的后续策略迁移。
- “剩余权利金毛年化”仍是模型已有数值的展示名称，本切片不重新审计其计算公式。

