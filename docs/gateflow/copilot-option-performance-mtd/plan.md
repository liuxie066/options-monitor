# Gateflow Implementation Plan — Copilot Option Performance MTD

- Work unit: `copilot-option-performance-mtd`
- Gate: `plan`
- Date: 2026-07-23
- Status: accepted after second adversarial re-review
- Goal confirmation: `docs/gateflow/copilot-option-performance-mtd/goal-confirmation.md`
- Initial review: `docs/reviews/plan-review-20260723-164351.md`
- First re-review: `docs/reviews/plan-review-20260723-164617.md`
- Accepted re-review: `docs/reviews/plan-review-20260723-164741.md`
- Branch: `fix/copilot-option-performance-mtd`
- Base: `origin/main@0db40d50` (`v1.4.14`)

## 1. Outcome

让 Copilot 对“7月 MTD 的期权收益”优先成功调用 canonical
`option_performance_report`，并用一个紧凑、可核对的报告同时回答：

- 本期总收益、纯期权实现收益、指派股票实现收益；
- premium activity 与真实 cash movement 的区别；
- option cash、assignment settlement principal、assigned-stock sale cash；
- 指派的开仓/卖出活动、当前剩余状态；
- 账户范围、期间状态、费用/FX/估值证据缺口。

不改变 ledger、期间或会计语义；只修复 Copilot 输入适配、补齐 canonical 聚合维度，
并改善 renderer/prompt/eval。

## 2. Root causes and ownership boundaries

### 2.1 Copilot payload failure

`src/application/copilot/tools.py::build_tool_payload()` 从
`AgentTool.safe_default_input` 原样复制所有字段。`option_performance_report` 的
默认值包含 `config_path=None`、`data_config=None` 及多种期间字段的 `None`，
而 execution schema 要求这些字段一旦出现必须是 string/integer。

同一 Copilot schema 还同时暴露 MTD/month/year/range 参数。模型在 MTD 调用中同时
提交多个非空期间参数，canonical `PeriodRequest` 按设计 fail closed，导致工具在
真正执行前返回 `INPUT_ERROR`。

Ownership decision:

- public execution contract 继续严格拒绝歧义请求；
- Copilot host 不再预填 fake null defaults，也不预填 `period=mtd` discriminator；
  真正无期间参数时仍由 canonical `PeriodRequest` 默认 MTD；
- `AgentTool` 增加一个可选、窄范围的 Copilot input normalizer，具体工具在自己的
  定义边界声明 period discriminator 的 canonicalization；
- 只移除 tool definition 自己的 fake `None` defaults；模型或 static payload 显式
  提交的 `None`/空字符串不被通用 cleanup 掩盖，继续 fail closed；
- 不在 Copilot engine 中硬编码业务关键词或工具名分支。

### 2.2 Realized PnL ambiguity

`domain/domain/performance/engine.py` 的 top-level `pnl.realized_*` 聚合 option
allocations 与 assigned-stock sale/settlement facts，所以它是组合总实现收益，不是
纯期权收益。`assigned_stock.period` 另有生命周期汇总，但当前没有稳定的纯期权/
股票分量，renderer 也未解释混合口径。

Ownership decision:

- 在 engine 构建 facts 时保留 option-realized 与 assigned-stock facts 两个精确集合；
- top-level total 不变，新增 additive component metrics：
  `option_realized_gross/net`、`assigned_stock_realized_gross/net`；
- 不在 renderer 中用总数减法推导，不更改现有 fact kind 或历史数据；
- assigned-stock period summary 直接消费 assigned-stock facts，避免相同 source ID
  把 assignment option allocation 误归为股票收益。

### 2.3 Answer quality

renderer 目前只展示总 realized 与部分 cash 字段，模型 preview/prompt 没有强制说明
scope、assignment inclusion 和 component semantics。线上失败后模型回退到 generic
SQL，进一步丢失 canonical report 的事实契约。当前 scene 没有 account provenance，
因此本 work unit 不声称能从 host 端判断账户参数来自当前消息还是历史推断。

Ownership decision:

- deterministic renderer 输出固定的“收益 / 现金流 / 指派 / 口径与证据”四段；
- tool prompt 明确 option performance 的 primary tool、scope 和混合口径；
- tool 未收到 account 时沿用 canonical 全账户聚合；收到 account 时保留该 filter；
  renderer 始终展示实际 scope；
- 保留模型工具选择，不引入关键词路由；
- exact production conversation 进入 deterministic eval。

## 3. Public and internal contract changes

### 3.1 AgentTool Copilot adapter

`AgentTool` 新增可选 callable：

```python
copilot_input_normalizer: Callable[[Mapping[str, Any]], dict[str, Any]] | None
```

行为：

1. 只复制 definition 中真实存在且非-null 的 safe defaults；option performance 不在
   adapter 层预填 `period`；
2. 合并 static/model/scene inputs，不改写显式 `None`、空字符串、`False` 或 `0`；
3. 调用该 tool 自己的 Copilot normalizer；
4. normalizer 只在 payload 显式携带合法 `period` 时删除 irrelevant period fields；
5. 再交给既有 execution schema/validator。

`option_performance_report.safe_default_input` 不再声明 fake `None` entries；
也不声明 `period="mtd"`；canonical `PeriodRequest` 的 MTD default 保持不变。
`tool_descriptions()` 和 `_copilot_input_schema()` 也防御性地不把 safe
`default: null` 发布成模型默认值。static/model 显式 null 不被静默删除。

Option performance normalizer only:

| period | retained period fields |
|---|---|
| `mtd`, `ytd` | `as_of_date` |
| `month` | `month` |
| `year` | `year` |
| `range` | `start_date`, `end_date` |

未知 period 不被修正，仍由 canonical validator 拒绝。
未提供 period 但提供任一 period-specific field 时，也不做推断或删除，交由
canonical validator fail closed。
当前 period 的相关字段若是空字符串或非法值也不被修正，仍由 canonical validator
拒绝。

### 3.2 Performance report

在现有 `pnl` 下 additive 输出：

```json
{
  "realized_gross": {"...": "combined option + assigned stock"},
  "realized_net": {"...": "combined option + assigned stock"},
  "option_realized_gross": {"...": "option allocations only"},
  "option_realized_net": {"...": "option allocations only"},
  "assigned_stock_realized_gross": {"...": "assigned stock facts only"},
  "assigned_stock_realized_net": {"...": "assigned stock facts only"}
}
```

现有字段、数值和 legacy consumers 不变。新增字段沿用 `MetricAmount` envelope，
missing fee/FX 继续以 `partial`/`not_observed` 表达，不补零。

### 3.3 Rendered answer

固定展示：

1. 期间、截止状态、账户范围；
2. period total、combined realized、option realized、assigned-stock realized；
3. premium、option cash/fee、assignment principal/fee、stock sale cash/fee；
4. assigned shares opened/sold、ending lots/review、证据缺口。

文字明确：

- premium 是 activity，不等于收益也不等于现金余额变化；
- combined realized 包含纯期权与指派股票；
- settlement principal 与 stock sale cash 是本金/资产转换现金流，不重复计为期权
  premium；
- 缺失数据保持未知。

## 4. Implementation slices

### Slice 1 — Copilot input reliability

Files:

- `src/application/agent_tools/base.py`
- `src/application/agent_tools/positions.py`
- `src/application/copilot/tools.py`
- `tests/test_copilot_phase1.py`
- `tests/test_option_performance_agent_tool.py`

Changes:

1. 在现有 `AgentTool` Copilot metadata 附近增加 optional input normalizer。
2. 删除 option performance 的 fake `None` defaults 和 adapter-level `period=mtd`；
   canonical default 不变。
3. payload merge 保留 explicit invalid inputs，不做全局 empty cleanup。
4. option performance 自有 normalizer 只按 period 删除 irrelevant fields。
5. schema/description 不再携带 incompatible `default:null`。
6. 用线上坏 payload 回归：`period=mtd` 同时带 month/year/range 与 hidden nulls，
   构建后只留下 MTD 合法字段，execution 首次成功。

Acceptance:

- public API 的 ambiguous-period rejection 测试仍通过；
- Copilot payload 不含 hidden nulls；
- MTD/YTD/month/year/range 各自字段矩阵测试通过；
- 空 payload 仍由 canonical contract 得到 MTD；`month` without `period` 仍
  fail closed；
- `account=""`、`config_key=""` 和当前 period 的 invalid/empty required field 仍
  fail closed；
- explicit account/config scene overlay 仍保持原行为。

### Slice 2 — Canonical PnL decomposition and renderer

Files:

- `domain/domain/performance/engine.py`
- `src/application/agent_tools/positions.py`
- `src/application/assistant/renderer.py`
- existing focused engine/renderer/tool contract tests
- `docs/OPTION_PERFORMANCE_DESIGN.md`

Changes:

1. engine 在生成阶段分别保存 option realized facts 与 assigned-stock facts。
2. top-level total realized 保持不变，新增四个 component metrics。
3. assigned-stock period summary 只消费 assigned-stock fact set。
4. output/model contracts暴露必要的 component metrics。
5. renderer 输出四段式答案，明确 scope、period、assignment inclusion、cash 与
   evidence。

Acceptance:

- `combined realized = option realized + assigned-stock realized` 对完整 native
  currency facts成立；
- option assignment allocation 不被误归为 assigned-stock realized；
- stock sale/settlement fee 不被误归为 option realized；
- missing fee/FX 仍 partial/null；
- 无 account argument 时保持全账户聚合；有 account argument 时保留 filter，且两种
  标题都明确展示实际范围；
- existing report fields regression unchanged。

### Slice 3 — Prompt and exact conversation eval

Files:

- `src/application/copilot/prompts/tool_rules.md`
- `src/application/copilot/prompts/om_chat.md`
- `tests/copilot_eval/test_answer_quality.py`
- `tests/test_copilot_p1_eval.py`
- `scripts/copilot_p1_eval.py` only if deterministic evidence checks require it
- related docs if public contract text changes

Changes:

1. prompt 指定 option-income/performance 优先 canonical report，不把 generic SQL
   当正常替代。
2. 要求报告 actual scope、period status、combined/component realized、cash 与
   assignment semantics。
3. 加入线上 exact intent：“7月 MTD 的期权收益 / 我写的是 MTD”。
4. eval 要求首次 primary tool observation successful，且回答包含 MTD、实际范围、
   pure option、assigned stock、premium/cash 区分与 partial evidence。

Acceptance:

- scripted deterministic eval 不需要线上模型或生产数据；
- generic `analysis_query` 单字段回答不能通过该场景；
- unrelated Copilot eval remains green。

## 5. Test and verification matrix

Focused after each slice:

```bash
./.venv/bin/python -m pytest -q \
  tests/test_copilot_phase1.py \
  tests/test_option_performance_agent_tool.py

./.venv/bin/python -m pytest -q \
  tests/test_option_performance_engine.py \
  tests/test_option_performance_agent_tool.py \
  tests/test_assistant_runtime.py

./.venv/bin/python -m pytest -q \
  tests/copilot_eval/test_answer_quality.py \
  tests/test_copilot_p1_eval.py
```

Required broader gates before aggregate review:

```bash
./.venv/bin/python -m pytest -q \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py \
  tests/test_copilot_phase1.py \
  tests/test_copilot_p1_eval.py \
  tests/copilot_eval/test_answer_quality.py \
  tests/test_option_performance_agent_tool.py \
  tests/test_option_performance_engine.py \
  tests/test_assistant_runtime.py

./.venv/bin/python -m compileall -q domain src scripts
git diff --check
```

If the actual engine test filename differs, use the existing focused option-performance test
set discovered in the repo and record the exact command in the implementation artifact.

No live Feishu send, production read/write, config mutation, release, or deployment is part of
validation.

## 6. Gateflow review and commit sequence

1. Plan -> `planreview` -> fix -> re-review.
2. Commit accepted plan artifacts.
3. Slice 1 implementation -> focused tests -> `deepreview` -> fix/re-review -> commit.
4. Slice 2 implementation -> focused tests -> `deepreview` -> fix/re-review -> commit.
5. Slice 3 implementation -> focused tests -> `deepreview` -> fix/re-review -> commit.
6. Aggregate tests -> aggregate `deepreview` -> fix/re-review -> commit.
7. Push branch, create Draft PR, confirm ready state.
8. `deepreview PR <number>` -> fix/re-review -> commit/push.
9. Record draft-PR-pass and final closeout. Do not merge.

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Copilot normalizer silently hides a genuinely invalid request | It only removes fields irrelevant to an explicitly supplied valid period. The adapter does not prefill the discriminator; explicit invalid/current-period/static/model values remain and public execution stays strict. |
| Generic tool metadata gains unnecessary complexity | One optional callable, default `None`, no registry or adapter layer; used only where a proven discriminator problem exists. |
| New split metrics diverge from total | Derive all three from the exact fact collections in one engine pass and assert reconciliation. |
| Assignment source IDs collide with option allocation IDs | Stop reclassifying from source IDs; pass exact assigned-stock facts to stock summary. |
| Renderer becomes verbose | Fixed four-section compact output; no row dump unless explicitly requested. |
| Prompt fix becomes keyword routing | Prompt only influences model choice; deterministic correctness remains in tool/payload/report contracts. |
| Account scope inherited incorrectly | Tool defaults to aggregate and renderer always names actual scope. Prompt asks the model to narrow only from explicit request/context; current scene cannot prove provenance, so no host-side parser is added. |
| Dirty original workspace is contaminated | All edits/commits occur only in the confirmed isolated worktree. |

## 8. Explicit non-actions

- No production Feishu invocation or send.
- No runtime SQLite/ledger/config mutation.
- No release/tag/deploy.
- No edits in the original dirty worktree.
- No merge or reviewer assignment without new authorization.
- No account-intent/provenance parser; deterministic provenance requires a separate scene-scope
  work unit.
