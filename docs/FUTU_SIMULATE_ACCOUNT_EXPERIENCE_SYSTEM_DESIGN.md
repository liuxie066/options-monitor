# 富途模拟账户体验扫描实施设计

- **状态**：已实现，待线上 OpenD PoC 验收
- **日期**：2026-08-27
- **产品合同**：[富途模拟账户体验扫描 PRD](FUTU_SIMULATE_ACCOUNT_EXPERIENCE_PRD.md)
- **实施范围**：手动 `run tick` 的 Cash-Secured Put (CSP)、Covered Call (CC)、Combo Yield 体验链路
- **明确排除**：Wheel、通知、交易、正式账户资产读取和权威金融状态写入

本文只定义实现边界、代码 owner、数据合同和验收证据。产品目标、用户场景和文案以 PRD 为准。

## 1. 设计结论

采用现有扫描链路上的显式体验上下文，不建立模拟账户、模拟持仓或第二套扫描器：

```text
run tick --experience --no-send
  -> 入口合同校验
  -> OpenD 账户元数据读取
  -> TickAccountExecutionRequest.experience
  -> AccountRunRequest.experience
  -> scan-pipeline --experience
  -> pipeline_runtime / pipeline_watchlist / symbol_monitoring
  -> 现有 required-data 行情预取
  -> 现有容量 owner 注入单候选演示输入
  -> 现有 Candidate Engine 与 Combo owner
  -> 现有 snapshot / manifest / 本地报告
```

五项不可妥协的边界：

1. 体验模式不是账户读取失败后的 fallback，只能由本次手动命令显式开启。
2. 不构造假的 portfolio、ledger、FX 或 physical-account authority。
3. Candidate Engine、Combo 组合经济计算和正式 bid/ask 规则不增加体验分支。
4. `executable=false` 是结果合同，不依赖 `--no-send` 间接推断。
5. 体验标识必须显式穿过扫描子进程，不能只停留在外层 orchestrator。

## 2. 当前实现约束

当前调用链的关键事实如下：

| 现状 | 代码 owner | 对本需求的约束 |
|---|---|---|
| `run tick` 与 `tick-cron` 是两个独立公开入口 | `src/interfaces/cli/run_ops.py` | `--experience` 只增加到 `run tick`，不得由 `tick-cron` 转发 |
| runtime config、账户范围和触发类型在 workspace 创建前可确定 | `src/application/multi_account_tick.py` | 入口拒绝必须发生在 `prepare_tick_run_workspace()` 前 |
| 正常账户执行会先准备 portfolio、option positions、Wheel 和 close advice | `src/application/tick_account_execution.py` | 体验模式必须在调用这些 owner 前旁路，不能事后丢弃 |
| 实际策略扫描由独立 `scan-pipeline` 子进程执行 | `src/application/account_run.py`、`src/infrastructure/external_services.py`、`src/application/pipeline_runtime.py` | `experience` 必须显式传入子进程，外层 request 不会自动生效 |
| 子进程仍会自行构建 pipeline context | `src/application/pipeline_watchlist.py`、`src/application/pipeline_context.py` | 即使外层没有 prepared manifest，也必须禁止子进程回退查询账户资产 |
| broker readiness 会建立 physical account authority，portfolio fetch 会继续查询资产 | `src/infrastructure/futu_gateway.py`、`src/application/futu_portfolio_context.py` | 体验模式不能复用完整 readiness/portfolio 入口 |
| CC 会在父进程预取和子进程 symbol monitoring 中两次经过正式持仓 prefilter | `src/application/prefilters.py`、`src/application/required_data_prefetch_planning.py`、`src/application/pipeline_watchlist.py`、`src/application/symbol_monitoring.py` | 两处都需识别 demo capacity，不能只放行父进程预取 |
| CSP 和 CC 已有各自容量 owner | `src/application/sell_put_cash.py`、`src/application/scan_sell_call.py` | 演示容量只进入这些逐候选、逐合约入口 |
| Combo 已复用 CSP / CC 容量并自行计算组合经济指标 | `src/application/combo_yield_steps.py`、`src/application/cc_lp_steps.py` | 只替换容量输入，不复制 Combo 公式或报价规则 |
| 开仓快照当前要求 physical account 与五类依赖 | `src/application/opening_candidate_snapshot.py`、`src/application/candidate_snapshot_contract.py` | 体验快照需有明确的非 physical 合同，不能伪造依赖 |
| pipeline runtime 会生成 alert、notification compatibility bundle，并可能追加 cash footer | `src/application/pipeline_runtime.py` | `--no-send` 不等于这些内部步骤零调用，体验模式必须显式跳过 |
| 候选由 run-scoped manifest 提交并被只读工具和正式研究链路共同消费 | `src/application/candidate_snapshot_manifest.py`、`src/application/candidate_evidence_history.py`、`src/application/shadow_replay/` | 体验结果可供本地解释，但不得贡献正式 replay、Combo capture 或 recommendation evidence |

现有美股开盘前 option-chain warmup 只服务 scheduled trigger。体验模式仅允许手动入口，因此不进入
该 warmup，也不修改其行为。

## 3. 运行合同

### 3.1 入口校验

公开命令保持为：

```bash
./om run tick --config <runtime-config> --accounts <account> --experience --no-send
```

实现顺序：

1. `run_ops.py` 解析 `--experience`，只转发给 `run tick`。
2. `multi_account_tick.py` 读取并验证现有 runtime config，解析所选账户。
3. 对本次所选账户执行全量预检：
   - 每个账户的 `trd_env` 都必须是 `SIMULATE`；
   - `--no-send` 必须存在；
   - trigger 必须是直接手动运行，不能是 scheduled / cron；
   - `--smoke` 与 `--experience` 不得同时使用。
4. 任一账户不满足时，整次请求以 `invalid_request` 拒绝；不得创建 candidate workspace、状态索引、
   候选快照或体验报告。
5. 全部通过后才创建现有 run workspace，并把 `experience=True` 传入账户执行请求。

多账户采用全有或全无，而不是先运行部分账户。这避免同一 run 中混合正式和体验 authority，也保证
“扫描前拒绝”可以验证。

### 3.2 体验上下文

体验上下文是现有 request/result 上的一组字段，不新增配置项、数据库表或独立上下文文件：

```json
{
  "scan_mode": "experience",
  "capacity_source": "demo_scenario",
  "account_display_name": "美股模拟期权账户",
  "executable": false
}
```

约束：

- 上述四个字段从账户执行开始保持不变，写入候选 owner 快照、terminal manifest 和本地报告；
- 内部只新增一个显式 `experience: bool` 传播参数，不新增 `ExperienceContext` 类或新的环境抽象；
- 传播链必须覆盖 `TickAccountExecutionRequest`、`AccountRunRequest`、`run_pipeline_script()`、
  `pipeline_runtime`、`pipeline_watchlist`、`SymbolMonitoringInputs` 和 snapshot sealer；
- 子进程使用独立的内部 `--experience` 参数；现有 `scan-pipeline --mode scheduled` 是 pipeline
  运行方式，不代表用户从 scheduler 触发，不得复用它判定体验入口是否合法；
- `account_display_name` 只能来自第 4 节的组合规则，不得使用内部 account label；
- 内部 account label 继续用于配置选择和本地路径，不成为展示字段；
- 普通 REAL / SIMULATE 运行不创建体验上下文，现有行为不变。

## 4. OpenD 元数据读取

### 4.1 最小读取面

在现有 `src/infrastructure/futu_gateway.py` 增加一个只返回脱敏账户元数据的读取函数，复用现有 OpenD
连接参数和返回值解析，不增加新的 gateway/interface 层。该函数只允许调用 `get_acc_list()`。

允许返回给 application 的字段：

- 是否精确匹配；
- 市场权限 `trdmarket_auth`；
- `sim_acc_type`；
- 仅在多匹配展示时使用的 account ID 尾四位。

不得返回到用户报告的字段：完整 account ID、综合账户号码、交易账户号码、内部 account label。

### 4.2 匹配与降级

1. 使用配置中的 account ID、`SIMULATE` 环境和目标市场精确匹配。
2. 精确匹配后，根据市场与 `sim_acc_type` 生成组合显示名。
3. 只有存在多个同类可匹配账户时才附加脱敏尾号。
4. 无匹配、异常、超时或响应不可解析时返回 `模拟账户名称不可用`，记录
   `account_metadata_unavailable`，继续行情与候选扫描。
5. 不得任意选择第一行，不得回退查询现金、持仓、订单或成交。

元数据状态只影响显示和审计，不参与 `opening_status` 计算。

## 5. 账户型准备步骤旁路

`run_tick_account_execution()` 在体验模式下必须在调用正式账户 owner 前选择现有扫描链路的体验分支：

| 正常步骤 | 体验模式行为 |
|---|---|
| `prepare_portfolio_contexts` | 不调用 |
| `prepare_option_positions_contexts` | 不调用 |
| cash / holdings / sellable 查询 | 不调用 |
| option occupancy 与 FX evidence | 不读取、不持久化 |
| 子进程 `build_pipeline_context` | 不调用；不得回退读取 portfolio 或 option context |
| Wheel requirements merge | 不调用；Wheel scope 不进入 expected scopes |
| close-advice barrier / close scan | 不调用 |
| account cash footer | 不生成 |
| alert / notification compatibility bundle | 不生成 |
| notification eligibility 与外层通知 flow | 不进入 |
| runtime portfolio shadow sidecar | 不生成 |
| required-data、候选、snapshot、manifest、本地报告 | 保留 |

不能只把 `allow_mutations=False` 或 `no_send=True` 当作上述边界，因为它们不能证明账户查询与正式
准备函数的调用次数为零。父进程和 `scan-pipeline` 子进程必须分别具有显式门禁。

## 6. 行情预取

`build_cross_account_prefetch_config()` 继续作为跨账户 required-data 需求 owner，`pipeline_watchlist` /
`symbol_monitoring` 继续作为账户内实际扫描 owner。体验模式只改变两层 prefilter 的容量来源输入：

- CSP 与 `sp_lc` 按现有配置产生 Put/Call leg 行情需求；
- CC 与 `cc_lp` 标的不再因缺少正式 holdings authority 在预取前被裁掉；
- 子进程内的 `apply_prefilters()` 同样保留配置中的 CC 与 `cc_lp` scope；
- 该放行只表示“需要获取行情以生成演示覆盖”，不得产生持仓事实；
- 股票现价、期权链、bid/ask、事件和 RV 继续走现有 required-data owner；
- Combo 任一必要腿 bid/ask 缺失或非正数时继续 fail closed，不增加零价、中间价或模型价 fallback。

实现可在现有 prefilter 增加默认关闭的 demo-capacity 参数，由两个既有 caller 显式传入；不增加体验
专用 prefetcher，也不修改正式 prefilter 的默认行为。

## 7. 容量与策略执行

`experience` 通过 `SymbolMonitoringInputs` 到达策略 orchestration，再由现有 SP、CC 和 Combo owner
消费。不能在报告或 snapshot 层事后重写容量结果。

### 7.1 CSP

在 `sell_put_opening_capacity_inputs()` / `enrich_sell_put_candidates_with_cash()` 的逐候选容量边界传入：

```text
available_cash = strike * multiplier
occupied_cash = 0
max_new_contracts = 1
currency = 合约原币种
capacity_source = demo_scenario
```

这些值只服务当前候选，不能跨候选累计，也不表示账户余额、购买力或跨币种能力。

### 7.2 CC

在 `_resolve_sell_call_contract_capacity()` 的逐合约边界生成：

```text
shares = multiplier
sellable_shares = multiplier
locked_shares = 0
covered_contracts_available = 1
average_cost = 本轮 required-data 正股现价
capacity_source = demo_scenario
```

不能预先写死 100 股；`multiplier` 必须来自当前合约。正股现价缺失时按现有行情完整性 fail closed，
不得补造成本。

### 7.3 Combo Yield

- `sp_lc` 复用 CSP 的一组演示现金容量；
- `cc_lp` 复用 CC 的一组合约覆盖容量；
- Combo premium、`cash_required`、收益、风险和排名继续由现有 Combo owner 计算；
- 所有腿保持现有正式 bid/ask、同币种和 multiplier 一致性要求。

### 7.4 Candidate Engine 与 Wheel

`domain/domain/engine/candidate_engine.py` 不修改。体验输入仍需通过现金/覆盖、流动性、收益、事件、
风险和排名规则。Wheel 不进入预取 scope、候选 owner、snapshot owner 或生命周期链路。

## 8. 快照、manifest 与下游

### 8.1 兼容方式

继续使用现有：

```text
output_runs/<run_id>/accounts/<account>/state/
```

不新增体验目录或额外 run ID 规则，但不可在原 schema 内静默增加可选体验字段。旧消费者会忽略
`executable=false`，存在把演示容量摄取为正式证据的风险。采用同目录、同 owner、显式新版本：

| Artifact | 普通模式保持 | 体验模式拟新增 |
|---|---|---|
| opening snapshot | `opening_candidate_snapshot.v1` | `opening_candidate_snapshot.v2` |
| SP+LC snapshot | `combo_yield_candidate_snapshot.v2` | `combo_yield_candidate_snapshot.v3` |
| CC+LP snapshot | `cc_lp_candidate_snapshot.v2` | `cc_lp_candidate_snapshot.v3` |
| status index | `strategy_scan_status_index.v2.json` | `strategy_scan_status_index.v3.json` |
| terminal manifest | `candidate_snapshot_manifest.v1.json` | `candidate_snapshot_manifest.v2.json` |

版本合同：

- 体验 owner snapshot、status index 和 manifest 必须同时携带四个体验字段；
- `executable` 在体验版本中只能为 `false`；
- 体验 manifest 发布前校验所有 owner snapshot 的版本、模式和四个字段完全一致；
- 新读取面可显式识别普通版本与体验版本，不能把缺失 `scan_mode` 猜测成体验模式；
- 只识别 v1 manifest 的旧读取面不得回退摄取体验 artifact；遇到 v2 manifest 或未知 owner schema
  时必须按严格校验 fail closed；
- content hash、文件 hash、write-once 和 manifest owner binding 继续覆盖新增字段；
- 普通 REAL / SIMULATE writer 与现有历史读取合同不变。

### 8.2 Authority 与依赖

普通模式保持当前 physical account 校验和依赖集合不变。体验模式：

- `trade_env` 必须为 `SIMULATE`；
- 配置 account ID 只作为内部候选 identity 和 OpenD 元数据匹配键，不作为资产 authority；
- 不要求或生成 physical-account authority；
- snapshot 依赖集合只包含本轮实际使用的 `required_data`、市场汇率 `fx` 与 `earnings_rv`；
- `fx` 只绑定行情计算实际使用的市场汇率缓存，不表示账户换汇或跨币种购买力；
- 不添加假的 `portfolio` 或 `ledger` dependency。

共享 dependency validator 增加“由 scan mode 指定允许集合”的最小参数；默认值仍是当前正式五类依赖，
Wheel 继续使用默认值。

### 8.3 读取与执行门禁

以下只读面继续允许读取体验 manifest，并原样返回四个体验字段：

- latest-run candidate bundle；
- candidate filter / rank explain；
- 本地体验简报 `experience_report.md`；
- research archive 的文件归档与分类展示。

本地体验简报顶部固定显示：

```text
体验模式｜演示账户假设｜未读取账户现金与持仓｜不可作为可执行建议
```

体验简报不接入正式 Daily Decision Brief，不包含账户现金 footer、close advice 或可通知声明。

正式证据与执行 consumer 必须具有以下门禁：

| Consumer | 体验结果行为 |
|---|---|
| `candidate_evidence_history` | 分类为拟新增的 `non_contributing_experience`，`contributes_evidence=false` |
| Shadow Replay candidate / rank capture | 不摄取候选、决策或排名事实 |
| Combo Funding Put capture | fail closed |
| scheduled Recommendation Point | fail closed |
| research archive | 识别体验版 artifact 后可以归档，但 classification 必须显示非正式、不可贡献 evidence |
| trade intent、ledger/lifecycle、broker consumer | 在任何副作用前 fail closed |

所有 fail-closed consumer 使用稳定原因 `experience_candidate_not_executable`。不得用权重为零、调用方
约定或“通常不会从手动入口调用”代替硬门禁。

## 9. 状态与错误投影

不新增 opening status：

| 条件 | 结果 |
|---|---|
| REAL、缺少 `--no-send`、scheduled/cron、与 smoke 冲突 | `invalid_request`；workspace 前拒绝 |
| 元数据不可用 | 扫描继续；显示名称不可用；审计 `account_metadata_unavailable` |
| 行情完整且有候选 | `candidates_found` |
| 行情完整且正式规则淘汰全部候选 | `no_candidate`，保留 reject reason 摘要 |
| 部分 scope 完成、部分行情证据不完整 | `partial_data`，同时展示完成范围摘要和缺口 |
| 必要行情证据不可用 | `data_unavailable` |
| 市场关闭 | `market_closed` |

现金、持仓、可卖数量、已有期权占用和 physical account authority 缺失是体验模式主动省略的输入，
不得进入缺口列表或改变上述状态。

## 10. 副作用门禁

| 操作 | 体验模式 |
|---|---|
| OpenD 行情读取 | 允许 |
| `get_acc_list()` | 允许 |
| 现金、持仓、订单、成交查询 | 禁止 |
| 本地 run / cache / status / snapshot / manifest / report / audit | 允许 |
| alert / notification compatibility bundle、cash footer | 禁止 |
| 飞书、邮件、Webhook 等通知 | 禁止 |
| broker 下单或其他写入 | 禁止 |
| trade intake | 禁止 |
| 正式 ledger、position、lifecycle、external holdings 写入 | 禁止 |

实现必须以调用链旁路和 fake/spy 断言证明禁区为零调用，不能仅依赖配置约定。

## 11. 代码改动 owner

| Owner | 最小改动 |
|---|---|
| `src/interfaces/cli/run_ops.py` | 只为 `run tick` 增加并转发 `--experience` |
| `src/application/multi_account_tick.py` | workspace 前校验；传递体验标识；跳过 notification flow 与 portfolio sidecar |
| `src/infrastructure/futu_gateway.py` | 增加脱敏 `get_acc_list()` 元数据读取与纯显示名组合 |
| `src/application/tick_account_execution.py` | 旁路账户型准备、Wheel、close advice；保留 required-data 和候选链路 |
| `src/application/account_run.py`、`src/infrastructure/external_services.py` | 将 `experience` 显式传入 `scan-pipeline` 子进程 |
| `src/application/pipeline_runtime.py` | 解析内部 flag；跳过 alert、notification bundle 和 cash footer |
| `src/application/required_data_prefetch_planning.py` | 让体验 Call / `cc_lp` scope 进入现有预取 |
| `src/application/pipeline_watchlist.py`、`prefilters.py`、`symbol_monitoring.py` | 跳过账户 context；保留体验 scope；把 demo capacity 送到现有策略 owner |
| `src/application/sell_put_cash.py` | 在现有逐候选入口接受 demo capacity source |
| `src/application/scan_sell_call.py`、`sell_call_steps.py` | 按合约 multiplier 与本轮 spot 生成一张覆盖 |
| `src/application/combo_yield_steps.py`、`cc_lp_steps.py` | 传递并复用对应短腿 demo capacity，不改组合公式 |
| candidate snapshot、manifest、status owner | 发布显式体验版本、体验依赖集合与跨 owner 一致性验证 |
| candidate explain、rank、brief 读取面 | 读取并展示体验版本与四个字段 |
| `candidate_evidence_history.py`、`shadow_replay/`、`recommendation_point.py` | 隔离正式 evidence；要求可执行结果的入口 fail closed |
| `research/archive.py` | 识别体验版 manifest / status artifact 并归档，但保持非正式分类 |

不新增 `ExperienceContext` 类、domain entity、数据库 migration、配置 schema、provider adapter、扫描器、
通知类型或体验专用输出目录。

## 12. 实施顺序

1. 入口参数、全量预检、元数据显示名和外层副作用旁路。
2. 将内部 flag 传过 `account_run`、`run_pipeline_script`、`pipeline_runtime`、`pipeline_watchlist` 和
   `SymbolMonitoringInputs`。
3. 在父、子两层 prefilter 保留体验 scope，并接入 SP/CC demo capacity、Combo 复用和 Wheel 排除。
4. 发布版本化 owner snapshot、status index 和 terminal manifest。
5. 更新只读展示面与正式 evidence / execution consumer 门禁。
6. 运行公共入口、跨进程传播、零副作用、证据隔离和普通模式回归测试。

每一步都应保持普通 REAL 与 SIMULATE 默认路径可运行；不先加入通用 bypass 开关再逐步补安全边界。

## 13. 验收映射

| PRD AC | 最小自动化证据 |
|---|---|
| AC-01 | 扩展 `tests/test_unified_tick_entrypoint.py`，使用 fake required-data 跑通 SP、CC、`sp_lc`、`cc_lp`；断言 `experience` 穿过子进程边界并形成 v2 manifest 与本地报告 |
| AC-02 | 完整行情但全部被正式规则淘汰，断言零候选、`no_candidate` 和 reject reason 摘要 |
| AC-03 | 覆盖部分证据、必要证据缺失、Combo 非正/缺失 bid/ask、休市；断言状态不混淆且无报价 fallback |
| AC-04 | 表驱动覆盖 REAL、缺少 `--no-send`、scheduled/cron、smoke 冲突；断言 workspace 与 candidate artifact 不存在 |
| AC-05 | broker spy 只允许 `get_acc_list()`；父、子进程的 portfolio、option positions、alert、notification bundle、cash footer、trade intake、ledger、lifecycle、broker write 均为零调用；覆盖显示名与元数据降级 |
| AC-06 | 体验 manifest 对 Candidate Evidence、Shadow Replay、Combo Funding Put 和 Recommendation Point 均不贡献正式事实；未指定 `--experience` 的现有 REAL/SIMULATE 测试保持原断言；Wheel owner 未进入 manifest |

同时扩展现有快照和读取面测试：

- `tests/test_opening_candidate_snapshot.py`；
- `tests/test_combo_yield_candidate_snapshot.py`；
- `tests/test_cc_lp_candidate_snapshot.py`；
- `tests/test_candidate_snapshot_manifest.py`；
- `tests/test_candidate_filter_trace.py` 与 candidate rank 相关测试；
- `tests/test_daily_decision_brief_service.py`；
- `tests/test_account_run.py`、`tests/test_pipeline_runtime_paths.py`；
- `tests/test_candidate_evidence_history.py`、`tests/test_shadow_replay.py`；
- `tests/test_recommendation_point.py`、`tests/test_combo_yield_research.py`。

文档完成不授权代码、配置、通知、交易、发布或远端升级。进入实施前应先确认本设计，再把第 12 节
转换为可执行开发计划。
