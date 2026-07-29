# Daily Brief Frozen Snapshot / Close Advice 修复方案

> 2026-07-29 revision：根据 `docs/reviews/plan-review-20260729-123647.md` 收敛账户级 integrity failure、持仓 symbol routing、frozen freshness authority、`prefetch_done` 恢复和 run-scoped business date 契约。
>
> 2026-07-29 revision 2：根据 `docs/reviews/plan-review-20260729-130011.md` 收敛 candidate route precedence 与 effective `close_advice.enabled` eligibility。

## 1. 目标

修复同一 HK tick 中 `lx` 正常收到消息、`sy` 因 required-data 快照字节被改写而没有生成可投递消息的问题。

完成后必须满足：

1. `required_data_snapshot_manifest` 发布后，所有账户流水线和 Close Advice 都只能读取并校验快照，不能补取、合并或覆盖其中的 raw/CSV/receipt；
2. seal 前的 run-scoped prefetch plan 同时覆盖：
   - Sell Put / Covered Call / Combo Yield 的候选扫描需求；
   - 本轮所有 effective `close_advice.enabled=true` scanning accounts 的 active option positions 所需精确合约；
3. `lx,sy`、`sy,lx`、串行和并行执行都消费同一组行情 bytes，账户顺序不影响 source availability；
4. Close Advice 某个持仓缺行情时，该持仓显式 `not_evaluable`，不得在 seal 后联网补取，也不得破坏其他账户的 candidate authority；
5. 原有 manual / agent-tool 非 frozen 入口保持兼容，仍可按现有策略补齐 Close Advice 行情；
6. frozen Close Advice 检出 manifest/receipt/bytes integrity failure 时，该账户必须退出 reliable pipeline，不能生成 normal actionable Daily Brief；
7. frozen mode 的行情 freshness/provenance 只由 sealed manifest + receipt 决定，不能再受可变 quote-cache metadata 支配；
8. 不通过放宽 receipt/bytes 校验、交换账户顺序或复制可变目录来掩盖问题。

## 2. 已确认的根因

### 2.1 生产证据

2026-07-29 HK fixed runs 中：

- `lx` 在 Close Advice 之前完成 candidate scan，因此 required-data receipt 校验通过并正常准备/确认投递；
- Close Advice 随后为 `9992.HK`、`3690.HK`、`0700.HK` 的持仓合约补取行情，并通过 `save_outputs(..., output_root=required_data_root)` 覆盖本 run 已 seal 的 CSV；
- `sy` 之后读取同一 manifest 时，`resolve_frozen_required_data()` 检出 `required-data bytes do not match the sealed receipt`；
- `sy` 因 `position_advice_identity_source_missing` 进入 fail-closed，`delivery_key=null`，没有 provider attempt；
- systemd unit 成功只证明 tick 进程完成，不代表每个账户都生成或发送了消息。

US 2026-07-28 market date 的 fixed reports 有独立 confirmed evidence；本方案修复的是本次已确认的 HK 跨账户链路。

### 2.2 当前源码缺口

当前 `main` 已有 run barrier、唯一 prefetch、terminal manifest 和 frozen candidate consumer：

```text
tick_account_execution
  -> prepare portfolio contexts
  -> build cross-account candidate plan
  -> prefetch once
  -> seal required-data manifest
  -> start account pipelines
```

但 Close Advice 在 account pipeline 之后独立执行：

```text
account_run
  -> run_pipeline_script(... required_data_snapshot_manifest ...)
  -> run_close_advice(... required_data_root ...)
       -> _ensure_required_data_coverage_for_positions()
            -> fetch_symbol()
            -> save_outputs(required_data_root)   # post-seal overwrite
       -> _fetch_missing_quotes_via_opend()       # unsealed in-memory quote
```

同时，option-position context 目前要到各账户 pipeline 内才生成；run barrier 的全局行情计划不知道 Close Advice 的精确到期日和行权价。因此只禁止后写虽然能止血，但会让临近到期持仓缺少报价，不能作为完整修复。

## 3. 边界与非目标

### 3.1 本 work unit 包含

- 补齐 frozen Close Advice consumer contract；
- 在 seal 前读取 canonical option-position ledger，构造跨账户 Close Advice quote requirements；
- 把精确持仓合约需求合入现有 required-data fetch plan；
- 将 requirements、fetch plan、manifest 和 Close Advice 读取绑定到同一 run；
- 明确 watchlist 内外持仓的 market-data routing、route conflict 和 fail-closed 语义；
- 将 frozen integrity failure 投影到现有 `AccountRunOutcome.ran_pipeline` / Daily Brief reliability authority；
- 增加双账户顺序/并发、post-seal immutability 和 typed gap 回归；
- 更新内部运行证据说明。

### 3.2 本 work unit 不包含

- 不修改候选排名、IV/RV、收益率、事件风险、Close Advice thesis 或通知阈值；
- 不改变 scheduler、fixed-report 时点、账户路由、Feishu webhook 或重试策略；
- 不修改生产 config；
- 不新建行情数据库或通用 snapshot framework；
- 不自动补发 2026-07-29 历史消息；
- 不在本计划阶段 commit、release、upgrade、重启服务或发送真实通知。

## 4. 核心设计决策

### 4.1 单一不可变边界

`seal_required_data_snapshot()` 是本 run 行情事实的唯一 commit marker：

```text
canonical option-position snapshot
          +
candidate strategy requirements
          |
          v
cross-account required-data plan
          |
          v
prefetch once -> multiplier -> receipt -> seal manifest
          |
          +-----------------------+
          |                       |
          v                       v
candidate consumers          Close Advice consumers
validate + read only         validate + read only
```

manifest 发布后：

- 禁止 `fetch_symbol()`；
- 禁止 `save_outputs()`；
- 禁止 multiplier writer；
- 禁止把未绑定 receipt 的临时 OpenD 返回值合入 Close Advice；
- 缺失数据只能成为 typed gap，不能触发 snapshot repair。

其中只有 ordinary coverage/quality gap 保持 position-scoped；manifest、receipt 或实际消费 bytes 的 integrity failure 会使该账户本轮 snapshot authority 失效，并通过现有 account pipeline reliability 阻断 normal Daily Brief。

### 4.2 Close Advice requirements 是 planning input，不是新行情源

新增窄模块 `src/application/close_advice_required_data.py`，只负责：

- 从 canonical open option positions 解析 quote key；
- 统一 active lifecycle、market filter、expiration、strike 和 RV requirement 语义；
- 生成/校验 canonical requirements payload；
- 将运行时 position 与本 run 已规划 requirement 做匹配。

它不得：

- 调用 OpenD；
- 写 required-data；
- 计算 Close Advice thesis/tier/action；
- 读写 option-position ledger。

I/O 仍由 application orchestration 持有；`domain/domain/close_advice.py` 继续拥有 deterministic Close Advice policy。

### 4.3 不冻结整个 option-position context

本修复只在 barrier 中读取一次 canonical ledger snapshot，并冻结“本轮行情需要哪些合约”：

- 避免把持仓 context 的资金换算、全局风险和缓存生命周期全部拉进 required-data work unit；
- account pipeline 继续按现有方式生成完整 `option_positions_context.json`；
- Close Advice 运行时若出现 seal 后新增的 active position，该 position 标为 `required_data_position_not_planned`，不得补拉；
- seal 时存在、运行时已消失的 position 只形成多取的行情，不产生错误。

这样既保持 root fix，又避免把本次问题扩大成 option-position context 重构。

### 4.4 Frozen mode 只有一个 freshness/provenance authority

scheduled frozen Close Advice 只信任：

```text
required_data_snapshot_manifest
  -> bound quote receipt
  -> source_observed_at / expires_at
  -> exact raw / CSV bytes
```

当前 `<symbol>_required_data.meta.json` 是 legacy/manual quote-cache 的可变 metadata，不是 run snapshot 的一部分：

- frozen mode 不调用 `validate_quote_cache_metadata()`；
- `.meta.json` 缺失、过期或与 sealed CSV hash 不一致，不得覆盖已验证 manifest/receipt 的结论；
- receipt/manifest 无效时，即使 `.meta.json` 更新也不能恢复 frozen quote；
- legacy/manual mode 继续使用现有 quote-cache metadata 和 max-age 行为。

不把 `.meta.json` 加入 requirements artifact 或 manifest binding，避免形成第二套需要同步发布的 snapshot authority。

### 4.5 Account-level integrity failure 复用现有通知可靠性 authority

不在 Close Advice 内新建通知规则。`run_close_advice()` 返回 typed execution status：

```text
status=ok|degraded|snapshot_integrity_failed
snapshot_authority=valid|invalid
```

- `missing_contract`、`quote_unusable`、`position_not_planned` 等普通 gap：`status=degraded`、`snapshot_authority=valid`，仍是 position-scoped；
- manifest-bound receipt/bytes 在消费或发布前重验时 mismatch：`status=snapshot_integrity_failed`、`snapshot_authority=invalid`；
- `account_run.py` 收到 `snapshot_authority=invalid` 后不得发布成功 Close Advice manifest，也不得把账户计入 reliable pipeline；
- 返回 `AccountRunOutcome(ran_pipeline=False)`，同时保留 `AccountResult.ran_scan=True`、`should_notify=False` 和 typed `decision_reason=required_data_snapshot_integrity_failed`；
- `tick_account_execution.py` 因此不把账户加入 `ran_pipeline_accounts`；
- `tick_notification_flow.py` 继续通过现有 `pipeline_succeeded=false` 写 Daily Brief failure artifact，并阻断 normal delivery envelope/provider call。

candidate artifacts 即使此前已生成也不改变这一结论；Daily Brief 的成功持久化和发送发生在 account futures 之后，因此无需回滚 candidate 文件。

## 5. 新增 planning artifact

路径：

```text
output_runs/<run_id>/state/close_advice_required_data_plan.json
```

schema：

```json
{
  "schema_version": "close_advice_required_data_plan.v1",
  "run_id": "<run_id>",
  "run_started_at_utc": "2026-07-29T01:40:04Z",
  "business_date": "2026-07-29",
  "status": "complete|partial|failed",
  "accounts": {
    "lx": {
      "close_advice_enabled": true,
      "status": "ready",
      "requirements": [
        {
          "requirement_id": "<sha256>",
          "position_lot_id": "<canonical lot id>",
          "market": "HK",
          "symbol": "0700.HK",
          "option_type": "call",
          "expiration": "2026-07-30",
          "strike": "500",
          "requires_realized_volatility": true,
          "fetch_binding": {
            "source": "opend",
            "host": "127.0.0.1",
            "port": 11111,
            "config_scope": "account",
            "binding_id": "<sha256>"
          }
        }
      ],
      "planning_errors": []
    },
    "sy": {
      "close_advice_enabled": true,
      "status": "ready",
      "requirements": []
    }
  },
  "summary": {
    "accounts_total": 2,
    "accounts_eligible": 2,
    "accounts_not_applicable": 0,
    "accounts_ready": 2,
    "requirements_total": 3
  },
  "content_sha256": "<sha256>"
}
```

约束：

- account、symbol、option type、expiration、strike 均 canonicalize；
- requirement 只包含 lifecycle=`active` 且属于本轮 markets 的 position；
- `business_date` 在进入 barrier 时从同一个 `run_started_at_utc` 计算一次；所有账户 planning 和 frozen consumer 都使用该值；
- `fetch_binding` 只保存 resolved runtime config 中非敏感的 source/host/port、scope 和 canonical hash，不保存 token/secret；
- `binding_id` 只 hash normalized `(source, host, port)`；`config_scope` 仅供审计，不参与 route equality，避免 account/base 相同 endpoint 被误判为冲突；
- `requirement_id` 由 account + lot id + canonical quote key + RV requirement + binding id 生成；
- 同一合约的多 lot 只有在 fetch binding 相同时才可在 fetch plan 去重；plan artifact 保留每个 lot 的可审计映射；
- runtime config 中找不到 symbol binding 时写 `required_data_symbol_config_missing`；同一 canonical symbol 出现不同 binding 时写 `required_data_route_conflict`，两者都不得使用隐式默认 endpoint；
- account status 固定为 `not_applicable|ready|partial|unavailable`：effective `close_advice.enabled=false` 为 `not_applicable`，全部 requirement 可路由为 `ready`，部分 lot 有 typed planning error 为 `partial`，ledger/context 整体不可读为 `unavailable`；
- `not_applicable` account 只记录 `close_advice_enabled=false`、空 requirements/planning errors；不读 ledger、不参与 routing、budget、plan merge 或 complete/partial/failed denominator；
- 顶层 status 固定为 `complete|partial|failed`：全部 eligible account ready 为 `complete`，至少一个 eligible account partial/unavailable 且仍有可用 eligible account/requirement 为 `partial`，全部 eligible account unavailable 为 `failed`；没有 eligible account 时是合法 `complete` empty plan；
- account key 按 canonical account 排序，requirements 按 `requirement_id` 排序，planning errors 按 `(reason, lot_id, quote_key)` 排序；
- `content_sha256` 对移除 `content_sha256` 字段后的 canonical JSON 计算；manifest 中的 plan SHA-256 再对包含该字段的最终文件 bytes 计算；
- barrier 的 clock 作为显式输入捕获一次；determinism tests 对 account order 使用相同 run id / `run_started_at_utc`。

`required_data_snapshot_manifest.v1` 增加向后兼容的可选绑定：

```json
{
  "close_advice_required_data_plan_relpath": "close_advice_required_data_plan.json",
  "close_advice_required_data_plan_sha256": "<sha256>"
}
```

frozen Close Advice 必须同时验证 manifest 和该 plan；legacy manifest 没有此字段时，仅允许 candidate consumer，Close Advice fail closed 为 `close_advice_plan_unavailable`，不得恢复 post-seal fetch。

若 new manifest 声明了 plan binding，但 artifact 缺失、path 越界、schema/run/content/file hash mismatch，则不是普通 coverage gap，而是 `required_data_snapshot_integrity_failed`，必须走 §8.3 account-level authority failure。

## 6. Seal 前 requirements 构造

在 `tick_account_execution.run_tick_account_execution()` 中，prepared portfolio context 完成后、`build_cross_account_prefetch_config()` 之前：

1. 在 barrier 开始时捕获一次 `run_started_at_utc`，并用 `expiration_business_today(run_started_at_utc)` 生成唯一 `business_date`；
2. 从每个 scanning account 的 effective runtime config 计算 `close_advice_eligible_accounts = {account | close_advice.enabled is true}`；
3. 所有 scanning accounts 写入 artifact；非 eligible account 直接记 `not_applicable`，后续步骤不处理；
4. 仅对 eligible accounts 解析 canonical `portfolio.data_config` 和 broker/account scope；
5. 同一 data-config ledger 只读一次；
6. 复用 `list_position_lot_snapshots()` 和现有 position context canonicalization，避免直接猜测 raw event 状态；
7. 用本轮唯一 `business_date` 解析 lifecycle；
8. 过滤本轮 market，并生成 eligible account requirements；
9. 从 resolved account/base watchlist 构造 canonical fetch binding，解析每条 requirement 的 source/host/port；
10. 在 candidate demand 不变的前提下检查 position binding conflict，形成 typed planning errors；
11. 原子发布 `close_advice_required_data_plan.v1`；
12. 把 ready requirements 交给 cross-account required-data planning。

失败语义：

| Requirement planning | Candidate prefetch | Close Advice |
|---|---|---|
| account not applicable | 原 candidate plan 不变 | 不运行 Close Advice |
| account ready | 合入该账户精确合约 | 只读 frozen quote |
| one account unavailable | 其它 requirements + candidate plan 正常执行 | 该账户 active positions 全部 typed `plan_unavailable` |
| all accounts unavailable | candidate plan 仍正常执行 | 所有 Close Advice positions typed unavailable |
| candidate plan 也为空/失败 | 沿用 snapshot terminal failure | 不启动 normal account pipeline |

Close Advice requirements 失败不能移除 Sell Put / Covered Call / Combo Yield 的原有 market demand，也不能让另一个账户重新 prefetch。

### 6.1 Re-entry / recovery

`request.prefetch_done=true` 时不得重读 live ledger、重建 requirements 或重写 artifact：

1. 先按现有路径加载 `required_data_snapshot_manifest.json`；
2. 只从 manifest 的 `close_advice_required_data_plan_relpath` 定位 requirements artifact；
3. 拒绝 absolute path、`..`、越出 run state directory 的 relpath；
4. 校验 artifact 文件 SHA-256、canonical `content_sha256`、schema、run id 和 manifest binding；
5. 恢复同一 plan path/hash/business date，并传给 account runner；
6. legacy manifest 没有 binding 时标记 position-scoped `close_advice_plan_unavailable`；
7. new manifest 已声明 binding但 artifact 缺失、path 越界、schema/run/hash mismatch 时标记 `required_data_snapshot_integrity_failed`，不得重建、重新 prefetch 或回退到 post-seal fetch，并按 §8.3 使该账户退出 reliable pipeline。

snapshot manifest 自身不可用仍沿用现有 terminal barrier；仅 Close Advice plan 不可用时，candidate frozen consumer 可以继续使用合法 snapshot，但该账户 Close Advice positions 全部 typed unavailable。

## 7. Fetch plan 合并

### 7.1 Canonical symbol routing

ledger 只拥有 position identity，不拥有 market-data endpoint。binding resolver 的唯一输入是本轮已加载并完成 profile resolution 的 runtime config：

1. 先在该 account 的 resolved watchlist 中按 canonical symbol 查找；
2. 未命中时在 base resolved watchlist 中查找；
3. 命中后使用该 resolved item 的 normalized `fetch.source`、显式 `fetch.host`、显式 `fetch.port`；
4. source 不受 required-data runtime 支持时写 `required_data_symbol_source_unsupported`；
5. 两级均未命中时写 `required_data_symbol_config_missing`；
6. 禁止用 `resolve_symbol_fetch_source()` 的 default source、`127.0.0.1` 或 `11111` 补全缺失 binding。

先按现有逻辑构造完整 candidate source items，再把 candidate binding set 当作不可被 Close Advice requirements 降级的既有 authority。因为共享 snapshot 的 raw/CSV/receipt 路径以 symbol 为 identity，position requirements 按以下 precedence 合并：

1. **candidate binding set 恰好一个**：
   - position binding 相同：把 exact contracts 合入该 candidate fetch；
   - position binding 不同：只拒绝冲突 requirement，标为 `required_data_route_conflict` / not-evaluable；candidate fetch 保持一次且不改 route。
2. **没有 candidate demand**：
   - 所有 position requirements binding 相同：创建一个 position-only symbol fetch；
   - position bindings 不同：所有冲突 requirements 标为 `required_data_route_conflict`，该 position-only symbol 不 fetch。
3. **candidate binding set 多于一个**：
   - 这是本 work unit 之前已存在的 candidate config ambiguity；本修复不选择、合并或删除其中任何 candidate item；
   - 该 symbol 的 position requirements 全部标为 `required_data_route_conflict`，不向任何 candidate item 注入 exact contracts；
   - diagnostics 记录 `candidate_route_ambiguous=true`，留给独立 config-validation issue；不能由 Close Advice 改变既有 candidate 行为。

global plan/requirements diagnostics 必须记录 preserved candidate binding、rejected requirement ids 和 position-only conflict。任何 Close Advice routing failure都不能把既有 candidate fetch count 从非零改为零。

该 resolver 属于 `required_data_prefetch_planning.py` 的 planning boundary；不读取 ledger，不调用网络，不修改 config，也不新增 broker routing registry。

### 7.2 显式 requirement 输入

扩展：

- `build_cross_account_prefetch_config(...)`
- `build_prefetch_symbol_plan(...)`
- `build_required_data_fetch_plan(...)`

使每个 symbol 可带内部 planning-only 字段：

```text
_close_advice_position_requirements
```

不要把持仓伪装成临时 Sell Put / Sell Call strategy config。planning 层应显式生成 `OptionSideFetchPlan`：

- `explicit_expirations`：同 symbol/side 的 required expirations 并集；
- `min_strike/max_strike`：该 side required strikes 的 min/max；
- `include_realized_volatility`：任一 requirement 需要时为 true；
- `source_fields`：包含 `close_advice.position_requirements`；
- `planning_reason`：`cover active Close Advice position contracts`。

该 side plan 与现有 candidate side plans 通过 `_merge_same_side_plans()` 合并，继续由同一次 `fetch_symbol()` 获取，不建立第二条 fetch path。

### 7.3 Plan identity

`global_required_data_plan.symbols[].fetch_plan` 必须显示合并后的 exact expirations/strike windows、binding id 和 requirement-plan hash。现有 `plan_id` 继续基于 canonical symbols payload，因此任何持仓需求、business date 或 routing 变化都会改变 plan ID。

验收时必须证明：

- `9992.HK`、`3690.HK`、`0700.HK` 临近到期持仓 expirations 在 seal 前已进入 plan；
- account 顺序变化不改变 requirements content hash、fetch payload 或 plan ID；
- 同一合约多账户/多 lot 且 binding 相同时只增加审计映射，不重复 OpenD chain call；
- watchlist 外持仓、unsupported source 和 route conflict 均产生 typed planning error，不触发隐式默认 endpoint；
- candidate route 与 position route 冲突时保留 candidate fetch，只拒绝冲突 position requirement；
- disabled Close Advice account 不产生 requirements，也不改变 fetch payload/plan ID。

## 8. Frozen Close Advice consumer

扩展 `run_close_advice()`：

```text
required_data_snapshot_manifest: Path | None
required_data_snapshot_run_id: str | None
close_advice_required_data_plan: Path | None
account: str | None
```

`account_run.py` 已持有 snapshot manifest/run/account，只需继续向下传递绑定的 requirements plan。

不新增独立 `business_date` 参数：frozen mode 必须从已验证 requirements plan 读取 `business_date`，避免 plan path 与另一个日期参数形成双重 authority；legacy mode 才调用当前 `expiration_business_today()`。

### 8.1 Frozen mode

传入 manifest 时：

1. 验证 manifest run/root/hash；
2. 验证 requirements plan schema/run/hash、manifest binding 和 safe relpath；
3. 从 verified plan 读取唯一 `business_date`，用它而不是 wall clock 对本账户 runtime positions 做 lifecycle 分类；
4. 对本账户每个 active position：
   - 验证 requirement identity；
   - 调用 `resolve_frozen_required_data()` 校验 symbol receipt 和 exact bytes；
   - 只从已验证 CSV 加载 canonical quote key；
5. freshness/expiry 只使用 verified receipt 的 `source_observed_at` / `expires_at`，不调用 `validate_quote_cache_metadata()`；
6. 不调用 `_ensure_required_data_coverage_for_positions()`；
7. 不调用 `_fetch_missing_quotes_via_opend()`；
8. 不调用任何 required-data writer；
9. 报告写入前再次校验本次实际消费的 symbol entries，防止评估期间外部改写；
10. diagnostics 固定记录：
   - `quote_mode=frozen_snapshot`
   - `network_fetch_attempts=0`
   - `required_data_write_attempts=0`
   - manifest/plan hash、business date、binding ids
   - validated/missing requirement counts。

### 8.2 Typed gap

缺失情况保持 position-scoped：

| 条件 | reason |
|---|---|
| legacy manifest 未绑定 requirements plan | `close_advice_plan_unavailable` |
| 已绑定 plan 缺失/path/schema/run/hash mismatch | `required_data_snapshot_integrity_failed` |
| position 未出现在 seal 前 plan | `required_data_position_not_planned` |
| position 无 resolved runtime symbol config | `required_data_symbol_config_missing` |
| symbol source 不受 runtime 支持 | `required_data_symbol_source_unsupported` |
| 同 symbol 存在不同 fetch binding | `required_data_route_conflict` |
| manifest 无该 symbol entry | `required_data_symbol_not_planned` |
| symbol fetch failed | `required_data_snapshot_unavailable` |
| receipt/bytes mismatch | `required_data_snapshot_integrity_failed` |
| exact expiration/strike 不在 CSV | `required_data_missing_contract` |
| quote 无可用 bid/ask | `required_data_quote_unusable` |

对应行必须：

- `evaluation_status=coverage_missing|quote_unusable`；
- `tier=not_evaluable`；
- `notify_rows` 不包含该行；
- 保留 position lot id、quote key、manifest/plan provenance；
- 不把缺失值当 0，也不产生平仓动作。

### 8.3 Integrity failure projection

若检测到 manifest/plan/receipt identity、hash、safe path、expiry 或实际 bytes 任一 `required_data_snapshot_integrity_failed`：

1. frozen invocation 开始时先原子写 `close_advice.manifest.json` 的 `status=pending`，使同一 run 的旧 success marker 立即失效；
2. CSV/text 写同一 account run directory 下的 attempt-scoped 临时路径，完成发布前重验后再原子 promote；最后写 `status=success` 的 manifest 作为成功 report commit marker；
3. integrity failure 时中止 CSV/text promote、原子写 `status=failed` manifest，且不得留下可被 validator 接受的 success marker；
4. `run_close_advice()` 返回 `status=snapshot_integrity_failed`、`snapshot_authority=invalid` 和 typed symbol/receipt evidence；
5. `account_run.py` 写 audit/runlog/metrics，但不吞掉为普通 degraded；
6. 返回 `AccountRunOutcome.ran_pipeline=False`，`AccountResult.ran_scan=True`、`should_notify=False`；
7. 由现有 `ran_pipeline_accounts -> pipeline_succeeded -> reliable` 链写 Daily Brief failure artifact并阻断 normal envelope/provider；
8. 不在 Close Advice 内调用 provider，不新建 notification authority。

`close_advice_report.v1` 只增加向后兼容的可选 `status`、run/manifest/plan hash 和 `quote_mode` 字段：legacy manifest 缺 `status` 时仍按既有 hash contract 视为 success；新 validator 只接受 `status` 缺失或 `status=success` 且文件 hash 完整的 manifest，拒绝 pending/failed。pending/failed manifest 不携带可被旧 validator 接受的 CSV hash，因此 mixed-version reader 也会 fail closed。

该规则与执行顺序无关：candidate consumer 先成功、Close Advice 后发现篡改时同样阻断账户；另一个未受影响账户仍可保持 reliable。

### 8.4 Legacy mode

未传 snapshot manifest 的 manual / agent-tool 路径保持现有行为：

- `_ensure_required_data_coverage_for_positions()` 可补齐持仓覆盖；
- `_fetch_missing_quotes_via_opend()` 可做 in-memory quote refresh；
- `validate_quote_cache_metadata()` 继续作为 legacy freshness authority；
- 原有 gateway/receipt/freshness tests 保持通过。

这两个模式必须由显式 manifest 参数区分，不能通过目录路径或运行环境猜测。

## 9. 实施切片

### S1 — Characterization + post-seal write guard

文件：

- `src/application/account_run.py`
- `src/application/close_advice_runner.py`
- `tests/test_account_run.py`
- `tests/test_close_advice_runner.py`

工作：

- 把已有 snapshot manifest/run/account 传入 Close Advice；
- frozen mode 立即禁止 coverage fetch、fallback fetch 和 `save_outputs`；
- 增加网络/write spies；
- missing quote 先按 typed not-evaluable 处理。

退出条件：

- 原生产顺序回归中，`lx` Close Advice 后 CSV/receipt bytes 不变；
- `sy` 不再出现由 `lx` 引起的 receipt mismatch；
- legacy mode 行为不变。

S1 是安全止血，但只有 S2/S3 完成后才算完整修复。

Release gate：S1 可以单独实现、测试、commit 供 review，但不得单独 release 或 upgrade。任何正式发布必须同时满足 S1-S5 的退出条件和 §10 全部验收；否则 active Close Advice positions 可能因禁止补拉而失去原有覆盖。

### S2 — Canonical Close Advice requirements plan

文件：

- `src/application/close_advice_required_data.py`（新增）
- `src/application/close_advice_runner.py`
- `src/application/tick_account_execution.py`
- `src/application/positions/context_builder.py`（仅复用或提取纯 canonical helper）
- focused tests

工作：

- 提取 runner 与 planning 共用的 quote requirement/lifecycle 纯语义；
- 从 effective account config 计算 `close_advice_eligible_accounts`，disabled account 记录 `not_applicable`；
- 从 canonical ledger snapshot 构造跨账户 plan；
- 冻结 `run_started_at_utc` / `business_date`，consumer 不再重新读取 wall clock；
- 从 resolved account/base watchlist 解析 canonical fetch binding；
- 对 missing config、unsupported source、same-symbol route conflict 产生 typed planning error；
- 原子写 schema/hash/status；
- 保持单账户失败隔离。

退出条件：

- active/expiry-day/expired/unknown position 分类与 Close Advice 当前语义一致；
- disabled account 不读取 ledger、不生成 requirements、不参与 status denominator；
- 跨 Asia/Shanghai 午夜时 producer/consumer 仍使用同一 business date；
- account/market filtering 正确；
- reversed account order 生成相同 artifact bytes；
- 读取/route resolution 失败形成 typed unavailable，不抹掉 candidate requirements；
- 无 binding 时不使用默认 source/host/port。

### S3 — Merge requirements into the single prefetch

文件：

- `src/application/required_data_prefetch_planning.py`
- `src/application/required_data_planning.py`
- `src/application/multi_tick/required_data_prefetch.py`
- `src/application/required_data_snapshot.py`
- `src/application/tick_account_execution.py`
- focused tests

工作：

- 把 exact position side plans 合入现有 strategy side plans；
- plan/manifest 绑定 requirements artifact；
- global plan/plan id 显式绑定 canonical fetch binding；
- candidate binding 优先；冲突只拒绝 position requirement，不撤销既有 candidate fetch；
- position-only same-symbol route conflict 在 fetch 前 fail closed；
- 保持一次 prefetch、一次 multiplier enrichment、一次 receipt publication；
- 更新 budget estimation 使用去重后的 exact expirations。

退出条件：

- candidate + position requirements 都存在于同一 global plan；
- prefetch invocation count 始终为 1；
- plan seal 早于任何 account future；
- close positions 不触发第二次 OpenD fetch；
- 不会对同一 symbol 的冲突 endpoint 写同一 shared output path；
- Close Advice requirements 不会减少任何既有 candidate fetch。

### S4 — Frozen consumer provenance and failure projection

文件：

- `src/application/close_advice_runner.py`
- `src/application/account_run.py`
- `src/application/close_advice_report_manifest.py`
- `src/application/required_data_snapshot.py`
- Close Advice / Daily Brief focused tests

工作：

- frozen Close Advice 逐 symbol 验证 receipt/bytes；
- frozen freshness 只使用 receipt `source_observed_at` / `expires_at`，跳过 mutable quote-cache metadata；
- report manifest 记录 snapshot/plan hash 和 quote mode；
- position-scoped typed gaps；
- integrity failure 不发布成功 Close Advice report，并通过 `AccountRunOutcome.ran_pipeline=False` 阻断 normal Daily Brief。

退出条件：

- 每条 priced row 可追溯到本 run manifest + requirements plan；
- unavailable row 不生成 action；
- account 顺序/并发不改变 quote facts；
- valid receipt 不会被 stale/missing `.meta.json` 推翻；
- invalid receipt 不会被 fresh `.meta.json` 恢复；
- candidate 先成功、Close Advice 后检出篡改时，该账户仍不进入 `ran_pipeline_accounts`。
- 同一 run 先有 success report、随后 frozen re-entry 检出 integrity failure 时，pending/failed marker 会使旧 success report 不再可读。

### S5 — Regression、文档和 no-send 验证

文件：

- `docs/AGENT_WIKI.md`（仅补内部诊断契约）
- tests listed below

工作：

- focused tests；
- broader tick/notification tests；
- `prefetch_done=true` artifact recovery/path traversal/hash mismatch tests；
- synthetic two-account no-send；
- 记录 release/upgrade 前后验证方法。

## 10. 验收矩阵

### 10.1 原始故障回归

构造：

- `lx` 有需要 Close Advice 补覆盖的 `9992.HK`、`3690.HK`、`0700.HK` active positions；
- `sy` 在 `lx` 之后执行 candidate scan；
- frozen CSV 在 seal 时已有 receipt。

断言：

- position expirations/strikes 在 prefetch plan 中；
- `prefetch_required_data` 只调用一次；
- seal 后 `fetch_symbol/save_outputs/multiplier writer` 调用数为 0；
- `lx` 前后 raw/CSV/receipt bytes、SHA-256、mtime 不变；
- `sy` snapshot validation 通过；
- 两账户都生成可靠 Daily Brief delivery envelope；
- no-send 测试不调用真实 provider。

### 10.2 顺序与并发

参数化：

- account order `lx,sy` / `sy,lx`；
- `account_workers=1/2`；
- positions only in lx / only in sy / both；
- same contract multiple lots/accounts。

统一断言：

- requirements hash、plan ID、fetch request 和 snapshot hash 相同；
- prefetch count=1；
- 没有 post-seal write；
- 账户级资金/持仓建议可以不同，但 market quote facts 相同。

### 10.3 Partial / failure

- 一个 account ledger requirements unavailable：
  - candidate scan 继续；
  - 另一个账户 Close Advice 正常；
  - 失败账户 positions 为 typed unavailable；
- exact expiration OpenD 不存在：
  - snapshot 可为 partial；
  - 该 position not-evaluable；
  - 其它 symbol/family 正常；
- seal 后新增 position：
  - `required_data_position_not_planned`；
  - 无网络补拉；
- 修改 CSV 一个字节：
  - integrity failure；
  - 不发布成功 Close Advice report；
  - `AccountRunOutcome.ran_pipeline=false`；
  - Daily Brief 写 failure artifact，不生成 normal delivery envelope，不调用 provider；
  - 不放宽 receipt 校验；
- candidate consumer 已先成功、Close Advice 发布前才修改 CSV：
  - 仍按 account-level integrity failure 阻断；
  - 未受影响账户仍可 reliable；
  - `lx,sy` / `sy,lx` 结果一致；
- 同一 run Close Advice 首次 success，re-entry 前再篡改 CSV：
  - invocation 开始后旧 success manifest 不再可验证；
  - 最终 manifest 为 failed；
  - 旧 report 不进入 Daily Brief。

### 10.4 Routing、re-entry 与 business date

- active position 在 account watchlist：
  - 使用 account resolved fetch binding；
- account watchlist 未命中、base watchlist 命中：
  - 使用 base resolved fetch binding；
- 两级 watchlist 都未命中：
  - `required_data_symbol_config_missing`；
  - 不出现默认 `opend/127.0.0.1:11111` fetch；
- configured source 不受 required-data runtime 支持：
  - `required_data_symbol_source_unsupported`；
- candidate/position 或不同账户对同一 symbol 解析出不同 binding：
  - 有单一 candidate binding 时，candidate fetch count 保持 1，冲突 position `required_data_route_conflict`；
  - 无 candidate demand 且 position bindings 冲突时，该 position-only symbol fetch count=0；
  - candidate binding 本身已多值时，candidate items 保持现状，positions typed conflict，不注入 exact contracts；
  - 所有分支都不产生新的 shared output overwrite；
- `close_advice.enabled=false` scanning account 有 active positions、missing binding 或 divergent route：
  - artifact 记录 `status=not_applicable`；
  - ledger read count=0；
  - requirements/fetch payload/plan ID 与删除这些 positions 时相同；
  - enabled account candidate/Close Advice 不受影响；
- `prefetch_done=true`：
  - 从 manifest safe relpath 恢复相同 plan path/hash/business date；
  - 不重读 ledger，不重建或改写 requirements，不增加 prefetch invocation；
- plan artifact missing、path traversal、run/hash mismatch：
  - new manifest 已绑定 plan 时为 account-level `required_data_snapshot_integrity_failed`；
  - legacy manifest 未绑定 plan 时为 position-scoped `close_advice_plan_unavailable`；
  - 两者都无 fetch/write fallback；
- barrier 与 account execution 跨 Asia/Shanghai 午夜：
  - planning、runtime lifecycle、requirements identity 和两个账户输出均使用 artifact business date。

### 10.5 Freshness authority

- valid manifest/receipt + missing/stale/tampered `.meta.json`：
  - frozen quote 仍按 receipt facts 验证；
- invalid/expired receipt + fresh `.meta.json`：
  - frozen quote account-level fail closed；
- legacy/manual mode：
  - 仍执行现有 quote-cache metadata/max-age 检查。

### 10.6 Compatibility

- 非 frozen `run_close_advice()` 仍通过现有 coverage/fallback tests；
- agent tools 不传 manifest 时行为不变；
- candidate frozen consumer、Position Advice receipts、event prefetch 行为不变；
- `required_data_snapshot_manifest.v1` 老 reader 忽略新增可选字段；
- 历史 manifest 不被改写；
- S1 单独测试可以通过，但 release checklist 必须拒绝未完成 S2-S5 的版本。

## 11. 验证命令

先执行 focused tests：

```bash
./.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_close_advice_runner.py \
  tests/test_close_advice_runner_gateway_reuse.py \
  tests/test_account_run.py \
  tests/test_required_data_fetch_planning.py \
  tests/test_required_data_prefetch_inprocess.py \
  tests/test_required_data_snapshot.py \
  tests/test_tick_account_execution_barrier.py
```

再执行 tick / notification 范围：

```bash
./.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_multi_tick_*.py \
  tests/test_unified_tick_entrypoint.py \
  tests/test_daily_decision_brief_service.py \
  tests/test_daily_decision_brief_notification_flow.py
```

静态检查只覆盖本 work unit 变更文件；若 import graph 变化，生成并检查 dependency graph。最后在资源允许时执行全仓 pytest，并把既有基线噪声与新增失败分开报告。

synthetic runtime 验证必须使用 `--no-send` / no-send harness；不得为了验证本修复直接运行生产 tick。

## 12. 上线与生产验证边界

实施完成后的阶段严格分开：

1. **实现与测试**：只改代码/测试/文档；
2. **slice review**：S1 可独立 commit 供 review，但禁止单独 release/upgrade；
3. **commit and push**：需要用户另行授权，不修改 VERSION；
4. **release gate**：必须证明 S1-S5、§10 全部验收和 no-send 双账户 artifact；不满足时停止；
5. **release**：需要用户另行授权，按 VERSION release flow；
6. **upgrade**：需要用户明确指定远端环境并授权；
7. **生产验证**：升级后的下一次自然 scheduled run 只读核对，不主动补跑/补发。

生产验收信号：

- `prefetch_invocation_count=1`；
- manifest 绑定 requirements plan hash；
- `close_advice.quote_mode=frozen_snapshot`；
- `network_fetch_attempts=0`、`required_data_write_attempts=0`；
- 每个 priced symbol 的 binding id、receipt `source_observed_at` / `expires_at` 可审计；
- `lx`、`sy` 均无 `receipt_or_payload_mismatch`；
- 两账户 pipeline reliable，delivery key 非空，并各自有 provider confirmation；
- snapshot raw/CSV/receipt 在 seal 后 hash/mtime 不变。

若下一次自然 run 仍缺消息，继续按 scheduler → pipeline → Daily Brief authority → delivery envelope → provider confirmation 分层诊断；不得把 systemd success 当作投递证明。

## 13. 风险与停止条件

- 如果 canonical ledger snapshot 无法在 account futures 前安全只读，停止实施，不使用旧 account cache 猜测 requirements；
- 如果 active position 找不到 resolved account/base symbol config，保留 `required_data_symbol_config_missing`，不使用 fetch 默认值；
- 如果同一 symbol 的 candidate/position demands 解析出不同 binding，保留 candidate binding，只将冲突 position 标为 `required_data_route_conflict`；
- 如果 position-only symbol 存在多个 binding，所有冲突 positions typed unavailable，不允许两个 endpoint 写同一路径；
- 如果 candidate config 自身对同一 symbol 已存在多个 binding，本 work unit 不改变 candidate items；positions typed conflict，并将 `candidate_route_ambiguous` 记入后续 config-validation tracking；
- 如果 active position 的精确 expiration 超出 OpenD 当前可发现范围，保留 typed gap，不扩大成“拉全部到期日”；
- 如果 requirements 合并导致 rate-limit budget 超标，按现有 prefetch wave/budget 切分同一次 invocation，不能退回 account-owned fetch；
- 如果 `prefetch_done=true` 无法从 manifest 安全恢复原 requirements artifact，停止 Close Advice frozen consumer，不重建 live plan；
- 如果当前 dirty worktree 中出现与上述核心文件重叠的未归属改动，先确认所有权或切到独立 clean worktree，不覆盖用户工作；
- 不采用 account-local supplemental quote cache，除非实证表明 seal 前无法确定 position requirements；该备选需要独立 receipt/provenance 设计，不能作为隐式 fallback。

## 14. 完成报告要求

实现完成时必须交付：

1. 修改文件和 contract 变化；
2. focused / broader / full tests；
3. 原始 `lx -> Close Advice mutation -> sy failure` 的确定性回归证据；
4. no-send two-account artifact 路径及 manifest/requirements hashes；
5. seal 前后 bytes/hash/mtime 不变证据；
6. routing missing/conflict、re-entry、midnight、freshness authority 和 integrity-to-account-block 回归证据；
7. 明确说明是否未执行 commit、release、upgrade、真实通知或历史补发。
