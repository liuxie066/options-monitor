# Combo Yield 策略对齐实施方案

> 日期：2026-08-08
>
> 分支：`feat/combo-yield-policy-alignment`
>
> 策略确认文档：[`combo-yield-policy-confirmation-20260808.md`](combo-yield-policy-confirmation-20260808.md)
>
> 状态：S1-S4 已实施并通过 code review 与 aggregate deepreview（`b1170cfc`）；待 push 与 draft PR。
>
> 基线：`main` 最新（1.10.17）

## 1. Goal / Motivation / Success Signal

### Goal

把已确认的 Combo Yield 策略口径落到代码：put 腿独立扫描但继承 Sell Put 全部硬门槛；资金占用使用扣净权利金口径；统一用 `min_net_credit_retention=0.60` 表达 call 成本约束；排序以 `net_credit_retention` 优先、跨标的以期间非年化净收益主导；候选写入独立 sealed snapshot，Agent / Daily Brief 只消费快照。

### Success signal

1. Combo put universe 与 Sell Put 硬门槛筛选结果在相同配置下一致（含 `min_net_income`）；
2. `funding_mode` / `max_call_cost_to_put_credit` 从配置、计算和校验路径移除，`min_net_credit_retention=0.60` 成为唯一成本约束；
3. Combo 排序主键是 `net_credit_retention`，跨标的排序主键是 put 腿期间非年化净收益；
4. 每个账户/run 生成可校验的 Combo sealed snapshot，Daily Brief 不再读 `*_combo_yield_candidates.csv`；
5. focused tests 与全量测试通过，config validate/build dry-run 通过；
6. Sell Put / Covered Call 决策与输出保持不变。

## 2. Non-goals / Scope Boundary

- 不修改 Sell Put / Covered Call 主策略的召回、筛选、排序或快照语义；
- 不改变 Combo Yield 的持仓生命周期、Close Advice、combo identity / reconciliation；
- 不修改生产 `config.yaml`、`config.us.json`、`config.hk.json`；只改 schema 默认值、示例和校验；
- 不引入新的外部数据源或 OpenD 调用；
- 不发布版本、不创建 tag/Release、不升级远端、不重启服务；
- 不发送真实通知、不写 Feishu / 交易 / 券商数据；
- 不把 Shadow Replay 或 research 层的历史 `yield_enhancement` 兼容读取移除。

## 3. Design Document Alignment

本方案实现 [`combo-yield-policy-confirmation-20260808.md`](combo-yield-policy-confirmation-20260808.md) 的 8 节结论，并把第 8 节 6 条差距映射到 slices：

| 差距 | Slice |
|---|---|
| 1. put 扫描继承 Sell Put 硬门槛 | S1 |
| 2. 删除 `funding_mode` / `max_call_cost_to_put_credit` | S2 |
| 3. 排序主键改为 `net_credit_retention` 优先 | S3 |
| 4. 跨标的排序改为期间非年化净收益 | S3 |
| 5. 保留 `min_net_credit_annualized` 年化硬门槛 | S2（保留，不改） |
| 6. Combo sealed snapshot 与消费者切换 | S4 |

## 4. First-principles Judgment and Direct Code Evidence

### 4.1 put 腿来源

当前 `combo_yield_steps.py:233` 调用 `run_put_scan_fn(..., min_net_income=0.0)`，显式绕过了 Sell Put 的 `min_net_income` 门槛；`configs/examples/config.yaml.example:20` 中 Sell Put 的 `min_net_income=50.0`。确认结论是 Combo put 必须继承 Sell Put 硬门槛，因此 `min_net_income=0.0` 是明确的策略违背，必须改为从 `sell_put_cfg` 解析。

### 4.2 成本约束冗余

`config_defaults.py` 中 Combo 同时配置 `funding_mode=credit_or_even`、`max_call_cost_to_put_credit=None`、`min_net_credit_retention=0.60`。其中 `funding_mode` 与 retention 在“call 不能吃掉 put 权利金”上重叠，`max_call_cost_to_put_credit` 是 retention 的补数。确认结论保留 retention 一个约束。

### 4.3 排序主键

`domain/domain/engine/yield_enhancement.py` 中 `yield_enhancement_rank_key()` 当前首键是 `funding_accepted`，次键是 `premium_funding_score`（年化主导组合分）；文档方案要求 retention 优先。`select_best_yield_enhancement_per_symbol()` 复用同一 rank key，因此跨标的排序也受影响。

### 4.4 候选真源

Daily Brief 在 `daily_decision_brief_service.py:143` 用 `run_account_dir.glob("*_combo_yield_candidates.csv")` 读 CSV，并在 `:306-308` 自己调用 `select_best_yield_enhancement_per_symbol` 二次排序。Sell Put / Covered Call 已切到 sealed snapshot（`opening_candidate_snapshot.py`），Combo 是唯一仍以 CSV 为正式读路径的开仓策略，与已确认的唯一真源原则冲突。

## 5. Affected Files / Modules

### Application

- `src/application/combo_yield_steps.py`：put 扫描参数、候选产出、snapshot seal 装配
- `src/application/sell_put_call_helper.py`：`_build_pair_row` / funding decision 参数、pair 行字段
- `src/application/yield_enhancement_config.py`：policy 字段、默认值与 validator
- `src/application/config_validator.py`：废弃字段拒绝、新字段校验
- `src/application/config_defaults.py`：Combo 默认值
- `src/application/daily_decision_brief_service.py`：Combo 消费从 CSV 切到 snapshot
- `src/application/agent_tools/candidate_filter_impl.py` / `candidate_rank_impl.py`：Combo 快照读取支持（如当前未覆盖）
- 新增 `src/application/combo_yield_candidate_snapshot.py`：Combo sealed snapshot 装配/校验/读取（或复用 opening snapshot 模式）

### Domain

- `domain/domain/engine/yield_enhancement.py`：排序 key、跨标的排序、funding decision 计算
- `domain/domain/sell_put_config.py`（如需要）：`min_net_income` 解析复用

### Config / Examples

- `configs/system.json`
- `configs/examples/config.yaml.example`
- `configs/examples/user.example.us.json`

### Tests

- `tests/test_combo_yield_pairing.py`
- `tests/test_combo_yield_steps.py`
- `tests/test_sell_put_yield_enhancement_validate_config.py`
- `tests/test_sell_put_linked_call_helper.py`
- 新增 `tests/test_combo_yield_candidate_snapshot.py`
- `tests/test_daily_decision_brief_service.py`（Combo 消费断言）

## 6. Contract / Schema / State-machine / Public-interface Changes

### Config schema

- 删除 `combo_yield.funding_mode`
- 删除 `combo_yield.max_call_cost_to_put_credit`
- 删除 `combo_yield.max_debit` / `combo_yield.max_debit_native`（`funding_mode=max_debit` 的配套字段）
- 保留 `combo_yield.min_net_credit_retention`（默认 0.60）
- 保留 `combo_yield.min_combo_net_credit`
- 保留 `combo_yield.min_net_credit_annualized`（默认 0.08）
- `config_validator.py` 对 `funding_mode` / `max_call_cost_to_put_credit` / `max_debit` / `max_debit_native` 四个已废弃字段给出明确拒绝错误，而非静默忽略；同步清理 `YIELD_ENHANCEMENT_ALLOWED_FIELDS`、`YIELD_ENHANCEMENT_DEFAULTS`、`YIELD_ENHANCEMENT_STRUCTURE_DEFAULTS`、`configs/system.json` 与示例

### Snapshot schema

新增 `combo_yield_candidate_snapshot.v1`：

```json
{
  "schema_version": "combo_yield_candidate_snapshot.v1",
  "run_id": "...",
  "account": "lx",
  "market": "us",
  "account_config_sha256": "...",
  "strategy_policy_sha256": "...",
  "sealed_at_utc": "...",
  "opening_status": "accepted | no_candidate",
  "candidate_decisions": [],
  "ranked_pairs": [],
  "reject_reasons": [],
  "content_sha256": "..."
}
```

空结果也必须封存 `opening_status=no_candidate` 与拒绝原因（含 put 无合格候选、retention 拒绝、结构不合法等）。

### Rank key contract

- 同结构排序：`funding_accepted`（保底）→ `net_credit_retention` 降序 → call 参与度（`abs(call_delta)` 降序）→ 两腿 max spread 升序 → OI 降序
- 跨标的排序：`funding_accepted` → put 腿期间非年化净收益降序 → 接货安全折价 → call delta → retention → 流动性
- 移除 `premium_funding_score` 作为主键；字段是否保留为诊断输出由 S3 测试决定

## 7. Implementation Decisions

1. **Combo sealed snapshot 独立文件**：`combo_yield_candidate_snapshot.json`，不混入 `opening_candidate_snapshot.json`。理由：Combo 是独立策略、pair 结构（两腿）与单腿 put/call 快照的 `strategy_mode` 语义不同；确认文档明确“Combo Yield 有自己的独立 snapshot 语义，不修改 Sell Put / Covered Call 决策”。但复用同一 canonical hash / 不可变写入 / account-run 路径模式。
2. **put 腿期间非年化收益字段**：pair row 新增 `put_only_period_net_return`，优先取 put universe row 已有的 `period_net_return_on_cash_basis`；缺失时才用 `put_only_net_credit / (put_strike * multiplier - put_only_net_credit)` 计算；分母非正（`net_cash_basis <= 0`）按主策略 `net_cash_basis_non_positive` 语义 fail closed / 不参与排序。不做额外 OpenD 调用。
3. **`min_net_income` 继承**：从 `yield_sp`（Combo 的 funding-put 配置，已继承 Sell Put 配置）解析 `min_net_income`，默认与 Sell Put 一致（50.0），不再写死 0.0。
4. **删除 `funding_mode` 后的净 credit 语义**：retention ≥ 0.60 隐含 `combo_net_credit >= 0.6 * put_net_credit > 0`，因此组合净 credit 为负的 pair 天然被拒；`min_combo_net_credit` 继续提供绝对值保底。
5. **Daily Brief 二次排序移除**：`daily_decision_brief_service.py` 改为读取 snapshot 中的 `ranked_pairs`，不再对 CSV 调用 `select_best_yield_enhancement_per_symbol`。
6. **S4 封存位置（run/account 级）**：Combo sealed snapshot 在 run/account 级聚合处（`pipeline_watchlist.py` 或 Combo 专属 run 级 collector）封存一次，收集全部 symbol 的 pair 结果与拒绝证据；`combo_yield_steps.py` 是 symbol 级函数，只产出 pair 结果与拒绝证据，不直接 seal，避免多 symbol 重复写入 `write_account_run_state_bytes_once_safely` 冲突。
7. **Agent 端范围**：本轮不新增 Combo 专用 Agent 查询入口，仅保证不回归（`candidate_filter_impl.py` / `candidate_rank_impl.py` 维持现状）；Combo 专用 Agent 查询作为后续 work unit。
8. **snapshot 渲染字段契约**：snapshot `ranked_pairs` 字段必须覆盖 `daily_decision_brief_service._candidate_view` 引用的全部渲染字段（至少含 put/call 合约、strike、dte、net_credit、retention、delta、IV、OI、volume、spread、收益、风险标签）。

## 8. Small Implementation Slices

### S1: put 扫描继承 Sell Put 硬门槛

- **事实修正（implementation finding 6）**：Sell Put 主策略 `sell_put_steps.py:100` 的 scan 同样传 `min_net_income=0.0`，真实硬门槛在 underwriting 层（`enrich_and_filter_sell_put_underwriting`，默认 `min_net_income=50.0`）；Combo 已通过 `combo_yield_steps.py:267-272` 的 `underwriting_filter_put_candidates_fn(sell_put_cfg=yield_sp)` 调用同一 underwriting。因此 scan 层 `min_net_income=0.0` 与主策略一致，不是绕过。
- S1 目标改为：**验证并测试 Combo put 腿经过与主策略相同的 underwriting 硬门槛（含 `min_net_income` 继承）**。
- 实现：确认 `combo_yield_steps.py` 的 underwriting 调用传入完整 `yield_sp`（已继承 Sell Put 配置），必要时补充 `max_spread_ratio` 注入（与主策略 `sell_put_steps.py:114-118` 对齐）；scan 层保持 `0.0`。
- 测试：构造含 `min_net_income=50` / `min_annualized_net_return` 的 `sell_put_cfg`，断言 Combo 的 underwriting 调用收到继承值；同一 put row 在主策略与 Combo 路径下 underwriting 决策一致。

### S2: 配置与计算路径简化

- `config_defaults.py` / `configs/system.json` / `configs/examples/*`：删除 `funding_mode`、`max_call_cost_to_put_credit`
- `yield_enhancement_config.py`：删除对应字段解析，保留 retention / min_combo_net_credit / min_net_credit_annualized
- `sell_put_call_helper.py`：删除 `funding_mode` / `max_debit_native` / `max_call_cost_to_put_credit` 分支，retention 检查保留
- `domain/domain/engine/yield_enhancement.py`：`compute_yield_enhancement_funding_decision` 删除 `credit_or_even` / `max_debit` 拒绝分支
- `config_validator.py`：拒绝旧字段
- 测试：旧配置校验报错、retention 独立决定通过/拒绝

### S3: 排序对齐

- `yield_enhancement_rank_key`：主键改为 retention 优先（保留 `funding_accepted` 保底）
- `yield_enhancement_staggered_rank_key` / `yield_enhancement_pair_shadow_rank_key`：`put_only_annualized_net_return` → `put_only_period_net_return`
- `_build_pair_row`：新增 `put_only_period_net_return` 字段
- 测试：构造 retention 相同/不同 pair，断言排序稳定

### S4: Combo sealed snapshot 与消费者切换

- 新增 `combo_yield_candidate_snapshot.py`：`seal_combo_yield_candidate_snapshot` / `load_*` / `validate_*`，复用 canonical hash 与不可变写入模式
- `combo_yield_steps.py`：产出 symbol 级 pair 结果与拒绝证据（含空结果），不直接 seal
- `pipeline_watchlist.py`（或 Combo 专属 run 级 collector）：收集全部 symbol 结果后，在 run/account 级 seal 一次，输入 `run_id / account / physical_account / account_config_sha256 / strategy_policy_sha256`，与 opening snapshot 同级
- `daily_decision_brief_service.py`：`combo_rows` 从 snapshot 读取，删除 CSV glob 与二次排序
- Agent 端本轮不新增 Combo 专用查询入口，仅保证不回归（`candidate_filter_impl.py` / `candidate_rank_impl.py` 维持现状）
- 测试：有候选 / 无候选两种 run 均封存一份快照（多 symbol 只生成一份）且 hash 可校验；Daily Brief 只消费快照；snapshot 字段 ⊇ CSV 渲染字段

## 9. Tests / Validation Commands

```bash
# focused
PYTHONPYCACHEPREFIX=/tmp/om_combo_pycache python3.12 -m pytest \
  tests/test_combo_yield_pairing.py \
  tests/test_combo_yield_steps.py \
  tests/test_sell_put_yield_enhancement_validate_config.py \
  tests/test_sell_put_linked_call_helper.py \
  tests/test_combo_yield_candidate_snapshot.py \
  tests/test_daily_decision_brief_service.py

# config
./om config validate --source yaml --market us --config-yaml configs/examples/config.yaml.example
./om config validate --source yaml --market hk --config-yaml configs/examples/config.yaml.example
./om config build --source yaml --market us --config-yaml configs/examples/config.yaml.example --dry-run
./om config build --source yaml --market hk --config-yaml configs/examples/config.yaml.example --dry-run

# full suite + gates
PYTHONPYCACHEPREFIX=/tmp/om_combo_pycache scripts/release_preflight.sh --full --allow-dirty
```

Expected assertions：

- `run_put_scan_fn` 收到继承的 `min_net_income`
- 配置校验对 `funding_mode` / `max_call_cost_to_put_credit` / `max_debit` / `max_debit_native` 报错
- retention=0.6 拒绝 net credit 为负或 retention 不足的 pair
- 排序结果主键顺序符合 retention → call delta → spread → OI
- 跨标的排序使用 `put_only_period_net_return`，不使用年化
- `put_only_period_net_return` 与主策略 `period_net_return_on_cash_basis` 同源一致，分母非正 fail closed
- 有候选与无候选的 run 均生成 sealed snapshot（多 symbol 仅一份），`content_sha256` 可复验
- Daily Brief 不再读取 `*_combo_yield_candidates.csv`
- snapshot `ranked_pairs` 字段覆盖 `_candidate_view` 渲染所需字段

## 10. Docs Decision

实现完成后必须同步更新以下现有设计文档，禁止只交付代码而让文档停留在旧口径：

### 10.1 `docs/STRATEGY_ARCHITECTURE.md`（Combo Yield 小节，第 54-147 行）

1. **配对硬约束**（第 104-108 行附近）：删除 `funding_mode=credit_or_even` 与 `max_call_cost_to_put_credit` 描述，改为“`min_net_credit_retention=0.60` 是唯一成本约束：至少保留 Funding Put 60% 净权利金，Participation Call 最多使用 40%”。
2. **费用与资金定义**（第 110-117 行）：保留现有 `put_net_credit / call_total_cost / combo_net_credit / net_credit_retention` 公式，删除 `funding_mode` 相关说明；补充 `cash_required = put_strike * multiplier - net_credit`（扣净权利金口径）与期间收益率 `net_credit / cash_required`。
3. **排序与通知入选-同一 Funding Put**（第 126-133 行）：主键从 `abs(call_delta)` 降序改为 `net_credit_retention` 降序优先；注明弃用 `premium_funding_score` 作为主键。
4. **排序与通知入选-不同标的**（第 136-144 行）：`put_only_annualized_net_return` 降序改为 `put_only_period_net_return`（期间非年化）降序。
5. **候选快照**：新增小节说明 Combo Yield 候选写入独立 sealed snapshot（`combo_yield_candidate_snapshot.json`），Agent / Daily Brief 只消费快照，不再读 `*_combo_yield_candidates.csv`；空结果也封存。

### 10.2 `docs/PRODUCT_ARCHITECTURE.md`

1. 第 48 行：补充“Combo Yield 候选已有独立 sealed snapshot，消费者从 CSV 切换为快照”。
2. 第 63 行：补充排序口径（retention 优先、跨标的期间非年化）与成本约束（retention 唯一）。

### 10.3 `docs/candidate_strategy.md`

1. 第 10-11 行 / 第 48-49 行：保持“Combo 独立策略、独立 snapshot”不变；
2. 第 486 行 A08 条目：补充“Combo Yield 使用独立 `combo_yield_candidate_snapshot.v1`，与 opening snapshot 同级但独立”。

### 10.4 `docs/plans/combo-yield-policy-confirmation-20260808.md`

- 状态从“待实施方案”改为“实施方案完成”，补实现 commit 引用与验证摘要。

### 10.5 一致性检查

- `rg -n "funding_mode|max_call_cost_to_put_credit|put_only_annualized_net_return|premium_funding_score" docs/` 不应再出现“作为当前 Combo 正式口径”的描述；
- `rg -n "combo_yield_candidates.csv" docs/` 只保留在历史兼容说明中。

## 11. Risks / Open Questions

- `premium_funding_score` 是否完全删除还是保留为诊断字段：倾向保留为诊断输出，不作为排序键（S3 时确认）
- `max_combo_spread_ratio` 与 `min_net_credit_annualized` 在 same-expiry 下保留，staggered 下按文档不计算：需要 S2 明确分支
- Combo snapshot 与 opening snapshot 的关系：独立文件已决定，但 Agent 工具聚合展示时需保证 account/run 一致
- 历史 `yield_enhancement` artifact 兼容：只读兼容保留，不迁移

## 12. Completion Report Format

完成报告需包含：

- 每个 slice 的 commit SHA；
- focused tests 数量与结果；
- config validate/build dry-run 结果；
- 全量 pytest 数量与结果；
- snapshot 有候选/无候选两条证据路径（run_id + account + hash）；
- Daily Brief / Agent 消费端验证；
- 遗留 risk 与 open questions。

## 13. Why This Plan Is Not Over-engineered

本方案只做确认文档明确要求的行为变化，不做推测性扩展：

- put 腿继承 Sell Put 硬门槛：只改 `combo_yield_steps.py` 一处参数解析，不新建抽象或数据流；
- 删除 `funding_mode` / `max_call_cost_to_put_credit`：只删除冗余配置与计算分支，retention 语义已存在；
- 排序对齐：只改 domain rank key 的键顺序与跨标的收益字段，复用现有排序函数，不引入新评分模型；
- sealed snapshot：复用 `opening_candidate_snapshot.py` 已有的 canonical hash / 不可变写入 / account-run 路径模式，只新增 Combo 的装配与读取，不新建通用 snapshot framework；
- 不新增外部数据源、不改变持仓生命周期 / Close Advice / combo identity，这些是明确 non-goal；
- `premium_funding_score` 是否保留为诊断字段作为 open question，由 S3 测试决定，避免提前设计。
