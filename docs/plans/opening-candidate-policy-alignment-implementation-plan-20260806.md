# Sell Put / Covered Call 开仓候选策略对齐实施方案

> 状态：已批准，Phase 1 实施中（2026-08-06）
>
> 日期：2026-08-06
>
> 工作分支：`feat/opening-candidate-policy-alignment`
>
> 基线提交：`b4bcf2ad docs: define opening candidate policy contract`
>
> 唯一策略合同：[`docs/candidate_strategy.md`](../candidate_strategy.md)

## 1. 目标与完成定义

本 work unit 把已经确认的 Sell Put / Covered Call 召回、数据、筛选、容量、排序和候选快照合同落到同一条正式运行路径。完成后：

1. OpenD 是开仓候选所需行情、合约、交易日历、QFQ 日线、财报日历和汇率的唯一正式外部数据源；
2. Candidate Engine 是 Sell Put / Covered Call 正式过滤和排序的唯一策略所有者；
3. 资金、持仓和 SQLite option-position ledger 按物理 Futu 账户冻结并参与容量计算；
4. 每个账户/run 即使没有候选也会封存一份不可变、可校验的 opening candidate snapshot；
5. Agent、通知和 Position Advice 只读取已封存快照，不从 CSV、任意路径或旧 artifact 重算候选；
6. yfinance、旧评分、旧字段和重复运行 artifact 退出当前开仓路径；
7. focused tests、全量测试和 shadow validation 都通过，且旧/新差异能按规则解释。

这是一条目标合同、一个分支，但按下列阶段逐步实施和验证。任何阶段失败都停在该阶段修复，不通过临时 fallback 绕过合同。

## 2. 明确不在本 work unit 内

- 不修改生产 `config.yaml`、`config.us.json`、`config.hk.json`；必要配置迁移只改 schema、默认值、示例和校验，生产值留到单独授权的发布/升级阶段。
- 不发布版本、不创建 tag/Release、不升级远端、不重启服务。
- 不发送真实通知，不写 Feishu，不写交易或券商数据，不自动下单或自动换汇。
- 不改变 Close Advice 的历史 `short_vol` thesis 兼容读取，不重写历史 artifact。
- 不重构 Combo Yield 自身策略；它只能共享规范化 OpenD 证据，不能复用 Sell Put / Covered Call 的策略结论或 opening snapshot。
- 不把候选容量解释成多候选可同时执行的资金分配方案。
- 不为人工导出保留新的 CSV/JSONL 公共合同。

## 3. 当前代码事实与主要差距

| 边界 | 当前事实 | 与目标的差距 |
|---|---|---|
| 排序 | `candidate_engine.py` 已有 Sell Put 的 `0.002` 收益带和部分 tie-break；Covered Call 仍由年化收益/综合 score 路径排序 | 两个策略都要使用持有周期收益带，且删除正式 `strategy_score` / `premium_edge_score` 语义 |
| 策略后处理 | `candidate_scanning.py`、`scan_sell_put.py`、`scan_sell_call.py` 和 underwriting wrapper 仍装配兼容评分与二次处理 | 正式 hard gate、计算和排序必须收敛到 Candidate Engine，application 只装配事实 |
| RV | `short_vol_metrics.py` 计算 RV20/60/120，并按 DTE 加权为 `realized_volatility_estimate` | 改为基于剩余 OpenD 交易 session 的唯一 `term_matched_RV`；RV20/60/120 仅诊断 |
| 财报 | `events/` 同时有 yfinance、Futu 财报价格历史、fallback、store、probe、除息/拆股 | 改为每市场/run 一份 OpenD `get_earnings_calendar` 覆盖；删除通用事件 resolver 和 yfinance |
| SDK | gateway 只有 `get_financials_earnings_price_history`，当前本地 SDK 不具备财报日历方法 | 先建立 SDK/OpenD capability preflight，再接入正式接口；不支持时 typed unavailable |
| 汇率 | `exchange_rates.py` 仍使用新浪、stale fallback；Sell Put 跨币种现金仍有 `0.95` haircut | 只认 OpenD observation；最长 24 小时；同币种优先，必要外币 stale 时才阻断 |
| 账户能力 | portfolio context 已读取分币种 cash、`fund_assets`、OpenD 持仓；ledger 已计算 short put/call 锁定 | 移除逻辑账户聚合和 haircut，统一物理账户 identity/hash，并证明最小失败范围 |
| 候选 artifact | required-data 已有 run manifest；候选仍写 CSV/trace JSONL，并为 Position Advice 生成二次 candidate source | 新建 opening-domain-owned account/run sealed snapshot，消费者切换后删除重复正式 artifact |
| Agent | candidate rank 工具可接收路径、读取 CSV、接受旧 score weights | 必须显式 account，只解析指定 run 或最新有效 sealed snapshot，不接受任意文件路径/旧权重 |

## 4. 目标所有权与数据流

```text
OpenD quote/broker capabilities
  -> run-scoped normalized evidence
     - market / spot / option chain / option snapshot
     - trading calendar / QFQ history / term-matched RV
     - earnings calendar coverage
     - FX observations
  -> account-scoped frozen facts
     - physical Futu account cash + fund_assets
     - physical Futu account holdings
     - SQLite open short put/call locks
  -> Candidate Engine
     - recall bounds
     - quote/tick/fee/return calculations
     - hard gates and minimal failure scopes
     - Sell Put / Covered Call return-band ranking
  -> opening_candidate_snapshot.v1 (seal + hash)
     -> Candidate Agent tools
     -> Daily Brief / notification renderer
     -> Position Advice input builder
```

所有权固定为：

- `src/infrastructure/futu_gateway.py`：只包装 OpenD/Futu SDK 能力与 provider error；不包含策略判断。
- required-data/application adapters：冻结和规范化外部证据、记录覆盖与 freshness；不排名。
- `domain/domain/engine/candidate_engine.py`：唯一正式过滤、计算约束和排序 owner；不导入 `src/`。
- account context/ledger application modules：只形成物理账户 capacity facts；不选择候选。
- 新的 opening candidate snapshot application module：拥有 payload、seal、hash、状态和只读 repository；不重新计算策略。
- Position Advice：读取 opening snapshot 后自行计算 `hold / replace / reallocate / manual-review`，候选 producer 不预写 replacement 结论。

## 5. 分阶段实施

### Phase 1 — OpenD 数据合同与运行前置条件

#### 1A. SDK capability 与 gateway

主要文件：

- `requirements/runtime.txt`
- `src/application/setup/check.py`
- `src/infrastructure/futu_gateway.py`
- `tests/test_futu_gateway_minimal.py`
- 相关 setup/runtime health tests

实施内容：

1. 把仓库要求的 `futu-api` 最低版本锁到实际支持 `get_earnings_calendar` 的版本，并增加 capability preflight；版本号本身不是成功证据，方法存在且可调用才是。
2. gateway 增加窄的 `get_earnings_calendar` 包装，保留市场、开始/结束日期、分页或返回覆盖所需原始字段。
3. gateway contract 明确 quote client 与 broker client 的能力边界；行情/日历调用不隐式要求交易连接。
4. SDK 或 OpenD 缺能力时返回稳定 reason code，例如 `opend_earnings_calendar_unsupported`，不得回退财报价格历史或 yfinance。

退出门：gateway contract tests 覆盖成功、空结果、SDK 缺方法、OpenD error 和分页/区间绑定。

#### 1B. 行情、合约和报价规范化

主要文件：

- `src/application/opend_market_snapshot_fetching.py`
- `src/application/option_chain_fetching.py`
- `src/application/opend_symbol_fetching.py`
- `src/application/opend_symbol_outputs.py`
- `src/application/required_data_fetching.py`
- `src/application/required_data_planning.py`
- `src/application/multi_tick/required_data_prefetch.py`
- 对应 `tests/test_required_data_*`、`tests/test_market_snapshot_fetching.py`

实施内容：

1. 定义并验证同一 run 的 underlier observation：`last_price`、`update_time`、`market_state`、`sec_status`。
2. 期权 observation 必须保留 bid/ask、各自时间证据、`price_spread`、IV、delta、OI、volume、合约状态、`option_standard_type`、`stock_owner`、currency 和 multiplier。
3. 适配层固定把 OpenD IV 百分号前数值除以 `100`；delta 不缩放。
4. chain 与 snapshot 的 multiplier 必须按 contract identity 一致；缺失/冲突只使该合约不可用，不默认 `100`。
5. 连续交易时段内 spot 与 bid/ask 最长 5 分钟；闭市单独投影为 `market_closed`，缺 timestamp/状态为最小范围 unavailable。
6. 保留 raw quote 完整性和 required-data receipt，但停止把 CSV round-trip 当作未来 opening 候选权威合同。

退出门：US/HK provider-shaped fixtures 都能区分 `0` 与 missing，能证明 stale、非标准合约、owner mismatch、suspended、tick/multiplier 冲突的最小失败范围。

#### 1C. 唯一期限匹配 RV

主要文件：

- `src/application/short_vol_metrics.py`
- `src/application/opend_symbol_fetching.py`
- `src/application/opend_symbol_outputs.py`
- `src/application/required_data_coverage.py`
- `src/application/multi_tick/required_data_prefetch.py`
- 对应 RV/required-data tests

实施内容：

1. 使用 OpenD QFQ 已完成日线和 OpenD trading calendar 计算到每个 expiry 的剩余 session 数。
2. `lookback=max(20, remaining_sessions)`，只用已完成收盘价的 log return，样本标准差乘 `sqrt(252)`。
3. 正式字段改为 `term_matched_rv`；RV20/60/120 可继续计算和展示，但不得加权或回退为正式值。
4. 持久化缓存 identity 为 `market + canonical symbol + QFQ`；每次只增量补新 session，并回抓最后 5 个 session。
5. 比较回抓区间的日期/收盘价；发现 QFQ 修订时刷新本轮所需完整 horizon。每个 expiry 保存 input start/end、session count 和 input hash。
6. 历史缺口、calendar 缺口或更新失败只阻断依赖该区间的 expiry。
7. 在晋升前同时产出旧加权值和新值的 shadow comparison；旧值只做对照，不能参与新正式 decision。

退出门：确定性价格序列覆盖 20、短于/长于 DTE、节假日、当日未收盘、QFQ 修订、缓存增量、缺口和 US/HK 日历差异。

#### 1D. 市场级财报日历

主要文件：

- `src/application/events/`（先切换消费者，Phase 5 再删除旧模块）
- 建议新增一个窄的 `src/application/earnings_calendar.py` 作为正式 owner
- `src/application/multi_tick/required_data_prefetch.py`
- 对应 earnings/event tests

实施内容：

1. 每个 market/run 从扫描时点到最远 expiry 按不重叠、最多 7 天区间查询一次 OpenD 财报日历，并供所有账户共享。
2. 保存每个区间的 start/end、调用状态、observed_at、结果 hash；只有所有相关区间成功，absence 才表示 OpenD 当前无已知财报安排。
3. 把覆盖映射到 expiry：任一区间失败只阻断需要该区间证明安全的 expiry。
4. expiry 当天财报仍属于持有期；扫描当天优先用可靠 timestamp 判断已发布/未发布，只有日期时返回 unavailable。
5. 本阶段不再从 `get_financials_earnings_price_history`、除息、拆股或预测值生成正式 decision。

退出门：覆盖完整空结果、区间中断、expiry 当天、扫描当天 timestamp 前后、缺 timestamp、SDK unsupported 和多账户单次 fetch。

### Phase 2 — Candidate Engine 计算、硬筛与排序统一

主要文件：

- `domain/domain/engine/candidate_engine.py`
- `domain/domain/engine/candidate_strategy.py`
- `domain/domain/insurance_underwriting.py`
- `domain/domain/fee_calc.py`
- `src/application/candidate_models.py`
- `src/application/candidate_scanning.py`
- `src/application/scan_sell_put.py`
- `src/application/scan_sell_call.py`
- `src/application/sell_put_strategy_risk.py`
- `src/application/covered_call_strategy_risk.py`
- 对应 candidate/sell put/covered call tests

实施内容：

1. 建立 Candidate Engine 输入 contract，只接受已规范化且带 provenance 的 quote、contract、RV、earnings、fee 和 account capacity facts。
2. Candidate Engine 计算 `raw_mid`、raw spread、tick-rounded `sell_limit`、版本化完整卖出费用、net premium、CNY net premium 和两类正式收益。
   `sell_limit` 是正式等待价格，不生成追价、市价成交或自动下单意图。
3. Sell Put：
   - recall upper/lower 按 `min(config max, spot)` 和向下 20% 计算；
   - 保持现有市场/symbol DTE 召回和过滤窗口，不新增 DTE 加分；
   - period return 使用扣除净权利金后的 net cash basis；
   - capacity 仍使用 gross assignment notional；
   - 年化 10%、CNY 50、IV/RV ratio 1.10、spread 0.05、raw quote spread 40% 为硬门槛；
   - 0.002 收益带内按合同规定的同 symbol/cross symbol tie-break 排序。
4. Covered Call：
   - cost floor、spot 和配置上限组成 recall window；
   - 保持现有市场/symbol DTE 召回和过滤窗口，不新增 DTE 加分；
   - period net premium return 分母为当前 spot market value；
   - 年化只作硬门槛；
   - 0.002 收益带内先选更高 strike，再按 spread/OI/net premium；跨 symbol 再比较剩余 concentration。
5. OI 只参与收益接近时 tie-break；OI `0` 是可靠值，missing 排在其后。volume、delta 只展示。
6. 两个策略都先在每个 symbol 内选出一张代表合约，再用相同锚定收益带规则做跨 symbol 排序；不得使用两两比较形成非传递“接近”关系。
7. stress、path risk、delta band、gamma、vega、gap up/down 和 concentration 不再成为开仓硬门槛；concentration 只保留合同指定的 cross-symbol tie-break。
8. `insurance_underwriting` 可以保留为当前策略 profile 名称，但不得再拥有平行评分/排序；application wrapper 只调用 Candidate Engine 并投影理由。
9. 费用计算取消 candidate path 的默认 multiplier 参数；费用表带明确 `fee_schedule_version` 和 basis，实际成交绩效仍读取 broker actual fees。
10. US/HK 共享同一 Candidate Engine 公式和状态机，只由市场事实注入各自 DTE/strike 配置、时区、交易日历和费用表。

退出门：一组相同输入从 direct domain test、Sell Put/CC scan adapter 和 Agent explanation 得到相同 accept/reject、计算值和排序；不存在 application 二次排序。

### Phase 3 — 物理账户现金、汇率、持仓与覆盖能力

主要文件：

- `src/application/futu_portfolio_context.py`
- `src/application/prepared_portfolio_context.py`
- `src/application/prepared_option_positions_context.py`
- `src/application/sell_put_cash.py`
- `src/application/exchange_rate_loader.py`
- `src/infrastructure/exchange_rates.py`
- `src/application/positions/context_builder.py`
- `domain/domain/cash_secured_utils.py`
- ledger public API 与相关 tests（只在需要的 facade 范围内）

实施内容：

1. 每份 capacity fact 绑定逻辑 account、物理 `futu_account_id`、trading environment、market、currency、source observation 和 identity hash；不同物理账户不得聚合。
2. Sell Put cash pool 保留明确分币种 cash 和 OpenD `fund_assets`；已有 open Short Put 按 gross `strike * multiplier` 扣除。
3. 同币种资金先按 100% 使用；不足时再逐币种按 OpenD FX observation 折算，删除 `0.95` haircut。
4. FX 最长有效 24 小时，以 provider `observed_at` 计算；timestamp 缺失等同 stale。stale/missing 外币不参与，但同币种资金继续有效；仅当必须用该外币才能达到一张时返回 capacity unavailable。
5. 删除新浪 FX 获取、stale fallback 和把本地缓存 timestamp 刷新成“新 observation”的可能；缓存只保存 OpenD 原始 observation。
6. Sell Put `max_new_contracts=floor(effective_free_cash/assignment_notional)`；候选共享现金池，snapshot 显式声明容量不可相加。
7. Sell Put 不读取或扣除 pending order、`frozen_cash`；候选按一张合约独立计算收益和门槛，容量只表达当前最多整数张数。
8. Covered Call 使用同一物理账户 OpenD `qty`、`can_sell_qty`、`avg_cost`、currency，并扣除 SQLite open Short Call 锁定；不读取 pending order。
9. Covered Call `max_new_contracts=floor((min(qty, can_sell_qty)-locked_shares)/multiplier)`，只接受普通股票交割的标准合约，结果不足一张时不得产生候选。
10. OpenD 与 ledger 无法一致解释、locked shares 超过持仓、multiplier 缺失等情况按 symbol/contract fail closed。
11. 普通持股与 Sell Put 指派股混合时可按账户总量判断覆盖，但没有显式 lot/FIFO 证据就保持 unallocated，不产生批次级 Wheel 收益。
12. concentration 延续当前 NAV 口径：当前市值计量，货币基金计入；只提供 tie-break fact，不做 hard gate。

退出门：测试覆盖同币种足够、需要新鲜外币、外币 stale 但同币种足够、外币 stale 且确实需要、多个物理账户隔离、open short put/call 锁定、混合持股未分配和 US/HK 一致算法。

### Phase 4 — 不可变 opening snapshot 与消费者切换

建议新增 owner：

- `src/application/opening_candidate_snapshot.py`
- 如只读解析足够复杂，再拆一个窄的 repository；不先建通用 snapshot framework

主要接入文件：

- `src/application/pipeline_watchlist.py`
- `src/application/symbol_monitoring.py`
- `src/application/position_advice_account_sources.py`
- `src/application/position_advice_input_builder.py`
- `src/application/daily_decision_brief_service.py`
- `src/application/agent_tools/candidate_filter_impl.py`
- `src/application/agent_tools/candidate_rank_impl.py`
- Agent tool metadata/contract tests

#### 4A. Snapshot contract

正式路径固定为：

```text
output_runs/<run_id>/accounts/<account>/state/opening_candidate_snapshot.json
```

`opening_candidate_snapshot.v1` 至少包含：

- `schema_version`、`run_id`、logical `account`、physical `futu_account_id`、trade env、market；
- `strategy_modes`、account config hash、strategy policy hash、required-data manifest hash；
- portfolio/ledger/FX/earnings/RV dependency receipt 或 content hash；
- `sealed_at_utc`、`content_sha256`；
- account/run `opening_status`：`candidates_found | no_candidate | data_unavailable | partial_data | market_closed`；
- 每个 strategy 的 `strategy_status` 与 `capacity_status`；
- `scope_results`，scope 至少可定位 account、symbol、expiry、contract，并带稳定 reason code；
- 全部通过 hard gate 的 ranked candidates，以及为 Agent 解释所需的 normalized facts、计算值、排序 key/provenance；
- 每个 `candidate_id` 都绑定 logical account、physical `futu_account_id`、market、strategy 和 contract identity；
- 即使候选为零也完整封存，空结果不能由“文件不存在”表达。

seal 是唯一 commit marker：先在内存完成所有 scope，再校验依赖/hash，最后原子写入。已存在的 terminal snapshot 只能 exact adopt；不同内容不得覆盖。

`partial_data` 只表示必要 scope 的数据不完整且仍有其他 scope 可用；OI、volume、delta 等可选字段缺失不产生 partial。

#### 4B. 消费者切换

1. Candidate Agent 工具必须提供 account；run 可选。省略 run 时只通过受控 repository 解析该账户最新、sealed、hash-valid snapshot。
2. 删除 `candidate_path`、`candidate_paths`、`report_dir`、`run_dir` 等任意文件路径输入，以及 `score_weights` 旧输入。
3. Agent 的 filter/rank explanation 只解释 snapshot 已记录的 Candidate Engine decision/provenance，不重跑 filter/rank。
4. Daily Brief 和 renderer 只读取 snapshot 的状态和候选；不得以缺 CSV 推导 no candidate。
5. Position Advice 直接把 opening snapshot 作为 candidate source，复用现有 receipt/dependency 校验基础设施；删除 `position_advice_candidate_all_decisions.raw.json -> candidate_decisions source` 的二次事实复制。
6. Position Advice 只消费开仓候选和 capacity facts，replacement eligibility 仍由自己的 domain/application policy计算。
7. notification、Agent、Position Advice 对同一 run/account 必须显示相同候选顺序、状态和 reason code。

退出门：tampered hash、wrong account/run、unsealed、missing dependency、empty snapshot、partial scopes 和 latest resolution 都有 fail-closed tests；三个消费者对同一 fixture 一致。

### Phase 5 — 旧链路、字段和依赖删除

删除只在 Phase 4 所有正式消费者切换完成后进行。

#### 5A. 必须从当前开仓运行路径删除

- `requirements/runtime.txt` 的 yfinance；`src/application/setup/check.py` 的 yfinance runtime import。
- `src/application/events/source_yfinance.py`。
- yfinance/futu 多 provider resolver、fallback、probe、stale event store、ex-dividend/split 正式事件逻辑，以及对应 config defaults/validator/examples。
- `get_financials_earnings_price_history`、dividend/split gateway 包装在无其他正式消费者后删除；若仍有非开仓消费者，先显式迁移或记录为独立 work unit，不允许候选路径继续使用。
- `strategy_score`、`premium_edge_score`、score weights、risk/path/delta/gamma/vega/gap 开仓评分与 Agent 参数。
- application 二次过滤/排序和只为其服务的兼容字段。
- candidate path 的 multiplier `100` 默认和 spot/last/CSV fallback。
- runtime Sell Put/CC candidate CSV、labeled CSV、candidate trace JSONL 作为事实源的写入与读取。
- Position Advice 重复 candidate capture/source artifact。

#### 5B. 明确保留的兼容边界

- 历史 artifact 可以被 research/archive/shadow tooling 只读解析，但不能被当前 Agent、通知、Position Advice 或扫描选作最新事实。
- Close Advice 历史 `short_vol` thesis 字段继续由 `domain/domain/short_vol_assessment.py` 解释。
- Shadow Replay 可以读取旧 score/RV 字段以生成对比样本，但新 capture 必须使用 opening snapshot；旧字段不得回写新 snapshot。
- ledger 中历史 multiplier/position 兼容读取不因 candidate path 禁止默认 `100` 而全局删除；只在能证明历史 migration 安全时另行收敛。
- Combo Yield 仅保留其自身仍在使用的字段和 artifact；不得以“同名旧字段”理由自动删除。

#### 5C. 删除验证

使用代码搜索和 import tests 证明：

- runtime dependency/import/config 中无 yfinance；
- current opening path 无 score weights、旧 RV estimate、Sina FX、candidate CSV path、Position Advice duplicate candidate artifact；
- 保留的旧字段只出现在明确的 historical-read adapter/test fixture 中；
- 没有新的兼容 alias 把已删除路径重新接回当前开仓。

### Phase 6 — 验证、Shadow 与晋升

#### 6A. Focused tests

按阶段运行并扩充：

- OpenD/gateway/required data：`tests/test_futu_gateway_minimal.py`、`tests/test_market_snapshot_fetching.py`、`tests/test_required_data_*.py`；
- RV/earnings/FX：现有 short-vol/event/exchange-rate tests 迁移为新 contract tests；
- 策略：`tests/test_candidate_engine_contract.py`、`tests/test_candidate_engine_parity.py`、`tests/test_option_candidate_strategy.py`、Sell Put/CC strategy/capacity tests；
- snapshot/consumer：新增 opening snapshot tests，并覆盖 Agent、Daily Brief、Position Advice source tests；
- import/config：Agent contract/smoke、setup、config validator/defaults tests。

每个 phase 的 focused suite 通过后才进入下一个 phase。

#### 6B. 全量静态与测试基线

1. 对改动 Python 文件运行项目既有 analyze/static checks；
2. 运行完整 `pytest`；
3. 运行 `./om config validate/build --dry-run` 的 US/HK example checks；
4. 运行 Agent plugin contract/smoke；
5. `git diff --check`，并审计依赖、配置、公共 tool payload 和文档一致性。

#### 6C. 离线 Shadow validation

使用只读、无通知、无生产写入的 research evidence：

1. 对同一组 frozen OpenD/account evidence 同时计算旧/新 RV 和旧/新候选结果；
2. 分类差异：数据不可用范围、召回边界、费用/tick、收益口径、RV、财报、容量、排序；
3. 检查新算法稳定性：相同 snapshot 重放必须产生相同 hash、候选和顺序；账户执行顺序不得改变 market facts；
4. 检查 US/HK、Sell Put/CC、空候选、partial、market closed 的代表样本；
5. 晋升门槛不是“与旧结果相同”，而是所有差异都能由 `candidate_strategy.md` 的已确认规则解释，且不存在未分类回归。

#### 6D. 晋升与回滚边界

- 本分支完成并通过评审后才合入 main；发布、远端升级和生产配置迁移分别取得后续授权。
- 代码层回滚以 commit/phase 为单位；不通过同时保留两个正式策略开关实现长期双轨。
- shadow 对照代码在晋升后只保留必要的离线 research adapter；运行时旧策略不得作为 fallback。
- 远端升级后的验证另行制定，只读检查 runtime SDK capability、sealed snapshot、Agent read consistency 和无 yfinance import；不得通过触发真实通知证明成功。

## 6. 建议提交切片

在同一分支按可独立审阅的责任边界提交：

1. `feat: establish OpenD opening evidence contract`
2. `feat: add term-matched RV and earnings coverage`
3. `feat: align candidate engine policy and ranking`
4. `feat: align physical-account opening capacity`
5. `feat: seal opening candidate snapshots`
6. `refactor: switch opening candidate consumers`
7. `refactor: remove legacy opening candidate paths`
8. `test: add opening policy shadow and regression coverage`

实际提交数可在不混合责任边界的前提下合并，但不能把大范围删除与首次 consumer cutover 混在同一不可审阅提交中。

## 7. 合同逐项覆盖审计

下表用于证明策略合同中的 67 项确认结论都有实施 owner 和退出证据；详细规则仍只以策略合同正文为准。

| 决策 ID | 实施位置 | 退出证据 |
|---|---|---|
| S01-S04 | Phase 2 | 两个策略的意图、mid 等待、现有 DTE window 和无 DTE 加分 contract tests |
| S05-S10 | Phase 2 | period/annualized、两类分母、CNY 50、IV/RV、spread/OI/volume/delta fixtures |
| S11-S15 | Phase 2 | 锚定 `0.002` 收益带、每 symbol 代表合约、两层 tie-break 和 concentration 非 hard gate tests |
| S16 | Phase 2、5 | 正式 decision 无 stress/gap/path/delta/gamma/vega/旧评分；删除搜索审计 |
| C01-C04 | Phase 2 | Sell Put/CC recall boundary table tests，含无窗口和 spot/avg_cost unavailable |
| C05-C11 | Phase 3 | 物理账户隔离、同币种优先、无 haircut、fund_assets、gross locks、无 pending/frozen、整数容量 tests |
| C12-C15 | Phase 3 | OpenD holdings + SQLite call locks、mixed lot unallocated、普通股票交割、NAV concentration tests |
| D01-D09 | Phase 1B、2、4 | OpenD-only、market state、5 分钟、tick/mid、standard/owner、multiplier、IV/delta、optional missing tests |
| D10-D11 | Phase 1C | term-matched RV、持久缓存、增量、5-session 回抓、QFQ revision tests |
| D12-D15 | Phase 1-4 | US/HK parity、versioned fees/actual fee boundary、OpenD FX 24h、最小失败 scope tests |
| E01-E09 | Phase 1A、1D、2、5 | 两策略 earnings hard risk、唯一日历源、分段完整性、当天/expiry、unsupported 和无 fallback tests |
| A01-A06 | Phase 4 | 五状态、scope_results、empty immutable seal、single engine、candidate identity、Agent account/latest tests |
| A07-A12 | Phase 4、5 与 non-goals | Position Advice 自算 replacement、Combo 独立、Agent-only 正式入口、旧 artifact 删除、无自动动作、target/runtime 状态标识 |

审计结果：全部 67 项均有实施阶段和验收证据；没有把历史只读兼容误当成当前开仓 fallback。

## 8. 验收矩阵

| 合同范围 | 验收证据 |
|---|---|
| OpenD quote/contract/SDK | provider-shaped contract tests；5 分钟、market state、tick、standard/owner、multiplier 冲突均按最小范围解释 |
| Earnings | 每 market/run 单次分段 fetch；完整空结果可通过；缺段/当天时间不可靠 fail closed；无 fallback |
| RV | trading-session matched deterministic tests；增量缓存、5-session 回抓、QFQ revision 和 input hash |
| Sell Put | recall、net cash basis period return、gross capacity、硬门槛和 0.002 两层排序 fixtures |
| Covered Call | cost/spot recall、current market value return、higher-strike tie-break、coverage/remaining concentration fixtures |
| Cash/FX | 物理账户隔离、同币种优先、OpenD FX 24h、无 0.95、fund_assets 和 open put locks |
| Holdings | OpenD qty/can_sell_qty/avg_cost + SQLite call locks；无 naked call；mixed lot 未分配 |
| Snapshot/status | 五种 opening status、三层 status、scope_results、empty seal、immutable exact-adopt、hash tamper rejection |
| Consumers | Agent/Brief/Position Advice 对同一 snapshot 一致；Agent 无路径参数或 score weights |
| 删除 | runtime 无 yfinance/Sina/旧 event resolver/旧 scoring/重复 candidate artifact；历史只读边界仍可解释 |
| 整体质量 | focused suites、完整 pytest、US/HK dry-run config checks、离线 shadow 差异全部分类 |

## 9. 计划审阅检查点

本方案已获批准，现从 Phase 1 开始实施。策略数值、FX 24 小时上限和排序口径均以已提交的 `docs/candidate_strategy.md` 为准，不在实现过程中重新发明。

方案获批不包含 commit/push、合并 main、发布或远端升级授权；这些仍是独立边界。
