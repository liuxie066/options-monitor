# Sell Put Top1 实验平台模块化实施控制文档

## 0. 文档状态

- 产品合同：`docs/plans/sell-put-top1-optimization-loop-mvp-20260814.md`
- 产品合同 SHA-256：`ef30f378ff02215223456180ff03fb9a4e4e87dd048952fafc3ea9024f46326c`
- 技术方案：`docs/plans/sell-put-top1-modular-technical-implementation-plan-20260814.md`
- 技术方案 SHA-256：`fd9798381caa8e16e083d8013325d0bf84674ca06a84577f38de5b6a5e91fddc`
- 源码基线：`main@c1d759ae10352d2a5664739e2053bb396e698919`
- 当前实施状态：`W0A build_go`；`W0R runtime_no_go`；`W1A in_gateflow`；provider-dependent research/validation 与真实试点仍 blocked
- 本文用途：控制逐模块实施、验收和停止，不重新定义产品或技术架构
- 本文不单独授权实现或运行变更；当前 W1A 源码实现由 2026-08-15 用户确认的 Gateflow work unit 授权。生产配置、服务安装、真实实验、发布和部署仍是独立授权边界。

## 1. 首发交付结果

首发完成后，系统应能在默认关闭、可随时下线的实验功能内，完成一条 `HK/lx` Sell Put Top1 排序实验闭环：

1. 生产 tick best-effort 发布正式 scheduled recommendation point，不依赖实验平台成功；
2. 平台持续保存最小排序投影，并冻结一段完整、成熟的固定 40 交易日研究集；
3. 程序比较 baseline 与三个已确认排序 profile，确定唯一 research leader 或无胜者；
4. 人工确认 leader，并授权一段全新、不重叠的未来 20 交易日 hidden validation；
5. 定时推进已授权实验，收集推荐点、成交观察、到期条款与结果；
6. 程序生成确定性三态结论和封闭回执；
7. 外部 LLM 可提出假设、分析 research/final 回执并形成下一假设草案，但不能启动实验、锁定 challenger 或采纳结果。

首发只验证 `cross_symbol_concentration_priority` 排序假设。过滤参数、expanded candidate universe、GitHub Issue 自动同步、通用 capability registry 和第二种 hypothesis type 均不实施，也不预建空接口。

## 2. 实施原则

### 2.1 交付单位

- 模块用于划分代码责任；Work Unit 用于划分实际交付。
- W1A–W8 必须留下可单独运行的 focused test；存在已完成上游时，同时留下一个 seam test。W0A/W0R 只留下可复核的只读证据和分离的 build/runtime 结论。
- 每个 Work Unit 独立验收后才进入下一个；W9 只做最终总回归，不能成为第一次集成。
- 一个 Work Unit 不得顺手实现后续模块，也不得建立只为未来扩展服务的抽象。

### 2.2 不可改变的边界

- Candidate Engine 仍是 Sell Put 正式过滤和排序的唯一权威；application 不复制排序规则。
- 生产 tick 不读实验 SQLite；实验失败不能改变候选、通知、watermark 或 tick 结果。
- 40 日 research 使用 `t0_sell_limit` 反事实；20 日 validation 使用正式推荐频率下的 observed-cross 证据，二者不得混用。
- 20 日 hidden validation 本身就是 Shadow，不再叠加第二段 shadow。
- 一天多个正式推荐点先逐点计算 paired delta，再按交易日日均；统计只对日值进行。
- Student-t 临界值和最差尾部样本数必须随实际 `n` 计算，不写死 `1.729` 或固定 4 天。
- 程序计算事实和结论；LLM 只处理已封闭、已脱敏回执；人工拥有授权与采纳权。
- 不保存 raw option chain、完整候选表、日内报价序列、broker 原始响应、完整 Prompt、完整对话或思维链。

## 3. 实施顺序

```text
W0A build preflight
  -> W1A ranking/projection core
  -> W1B spec/economics/statistics
  -> W2 producer seam
  -> W3 ledger/lifecycle
  -> W4 corpus
W0R runtime capability remediation/readiness
  -> W5 40-day research
  -> W6 20-day validation
  -> W7 advance/service source delivery
  -> W8 LLM advisory
  -> W9 aggregate regression
```

W2 只依赖 W1A 的 projection contract，W3 还依赖 W1B 的 spec/behavior contract；首发默认按上表串行，减少同一工作区交叉修改。

## 4. Work Units

### W0A/W0R — Build and runtime capability preflight

**目标**

用当前源码、真实 provider/domain read receipt 和本地历史 artifact 分别判断源码可实施性和真实运行准入。

**允许改动**

- `docs/performance/` 下新增一份带时间、命令、源码 SHA 和证据引用的 preflight 产物。
- 不新增 Strategy Lab 模块、SQLite 表、服务或配置。

**必须检查**

- terminal scheduled run、每日正式 target、每 point accepted `U_rank` 的 p50/p95/max；
- 当前可形成的完整、成熟 40 日窗口和最早可用日期；
- validation 最大 active observation、terms shard 和 outcome-job cardinality；
- corpus/artifact/SQLite 当前字节基线；
- HK assignment/exercise/expiry 净费用事实；
- OpenD observation、交易日历、未复权历史日线、history K-line quota 和 expiry terms chain capacity；
- 根据真实最大 cardinality 推导未来 advance cadence/timeout 上界。

**退出门**

- W0A 只核对当前切片的真源、合同字段、依赖和纯计算 fixture 可实施性；它可在无真实 provider/corpus 的情况下给出 `build_go`。
- W0R 只有在费用、calendar/K-line/quota、observation 和 terms capacity 全部有真实证据且为 green 时才是 `runtime_go`。
- W0R 任一项 red/unknown 即 `runtime_no_go`，必须另立最小 capability remediation work unit；它阻止 provider-dependent research/validation 和真实试点，但不阻止 W1A–W4 中不读真实 provider 的源码实现。
- 当前产品方案预期 HK assignment/exercise/expiry fee 会先触发 `no-go`；不得用零费用、合成费用或换公式绕过。
- 缺少成熟 40 日 corpus 记为 `research_corpus_warming`，它阻止真实 research，但不单独阻止平台代码实施。

### W1A — Ranking and projection core（M1A）

**主要文件**

- `src/application/strategy_lab/top1/ranking.py`
- `domain/domain/engine/candidate_engine.py`：只增加三值 Sell Put ranking profile 和合同版本；默认生产行为不变

**交付**

- `sell_put_ranking_projection.v1` 构造与严格校验；
- 在同一 `U_rank` 上调用 Candidate Engine 的三 profile 重排；
- 投影缺字段、hash/ID 冲突和 baseline parity 偏差到稳定 reason code 的确定性映射。

**限制**

M1A 不读文件、SQLite、环境变量、系统时钟或 OpenD，不依赖 CLI、service、LLM、research 或 validation 状态。

**验收**

- baseline 默认 profile 与 producer 已有正式顺序完全 parity；
- variant 只能改变同一 accepted set 的顺序；
- projection required key 缺失时 fail closed；
- 从封存 snapshot 构造 projection 后，不再依赖 source 对象即可完成三 profile 重排；
- projection 的字段白名单、ID/排名、content hash 和 baseline parity 失败全部 fail closed。

### W1B — Spec, economics, and statistics core（M1B）

**主要文件**

- `src/application/strategy_lab/top1/contracts.py`
- `src/application/strategy_lab/top1/economics.py`
- `src/application/strategy_lab/top1/statistics.py`
- `requirements/runtime.txt`、`constraints/runtime.txt`、`constraints.txt`：沿现有安装路径加入同一精确版本的 SciPy，不新增依赖 profile

**交付**

- ExperimentSpec 白名单、canonical hash 和 behavior binding；
- 在费用合同已锁定的前提下实现到期经济 PnL、资金效率、point/day delta、样本标准差、动态 t 下界和最差尾部纯函数；
- 完整事实到稳定 reason code 的确定性映射。

**验收**

- 20/40 日手算 fixture、动态 t 值、零标准差和尾部计算通过；
- source/config 等无关 provenance 不改变 behavior hash，相关合同版本变化必须改变 hash。

### W2 — Production recommendation point seam（M2）

**主要文件**

- `src/application/recommendation_point.py`
- 现有 tick/notification flow 中一个最小 best-effort observer 接线

**交付**

- `recommendation_point.v1` builder、validator 和 run/account-scoped write-once publisher；
- scheduled target 的 canonical point identity；
- point 与 terminal manifest、opening snapshot path/hash 的绑定；
- maintainer availability gate 的纯读取。

**验收**

- 正式 scheduled point 可幂等发布；不同 bytes 冲突 fail closed；
- manual、force、smoke、replay 和缺 scheduled identity 的 run 不发布；
- availability 关闭时不发布；
- observer 异常只形成实验 gap，生产 tick、watermark 和通知结果不变。

**即时 seam**

用 W1A 校验 point 引用的 opening snapshot 和 accepted IDs；不等待 W4 再首次验证。

### W3 — Experiment ledger and lifecycle（M3）

**主要文件**

- `src/infrastructure/strategy_lab/experiment_store.py`
- `src/application/strategy_lab/top1/lifecycle.py`
- `src/application/strategy_lab/top1/terminal_projection.py`

**交付**

- 紧凑 SQLite schema/migration；
- account opt-in、experiment、两阶段 authorization、generation、event、hidden commitment 和 validation collection slot；
- phase/progress/terminal CAS；
- completed/aborted 唯一 terminal request、deterministic bytes outbox 和崩溃恢复；
- 非泄漏 status/receipt projection。

**限制**

- 只保留一个 SQLite store 以维护跨表原子性；不建立单实现 repository interface。
- store 只做 schema、查询、约束和原子命令，不计算 leader、指标或市场结果，也不反向 import application。
- 只创建本 Work Unit 所需表；corpus/validation 表由后续 migration 添加。

**验收**

- 空库和前版迁移通过；
- 默认关闭，maintainer availability 对 account opt-in 有最终否决权；
- 未授权、hash 不一致、hidden 日期重叠和迟到写全部 fail closed；
- day 19/day 20 slot、completed/aborted 竞争、重复请求和崩溃恢复通过；
- feature disable 先禁止新市场读取，再幂等封存 active experiment。

### W4 — Corpus capture and freeze（M4）

**主要文件**

- `src/application/strategy_lab/top1/corpus.py`
- `src/application/strategy_lab/top1/readiness.py` 的 corpus 部分
- M3 的下一 schema migration

**交付**

- 首个 target 前 write-once `corpus_day_expectation.v1`；
- M2 point → M1A 最小 ranking projection → content-addressed corpus；
- corpus 索引和 coverage status；
- 截止日前最新、连续、完整、成熟的固定 40 日 dataset freezer。

**验收**

- account opt-out/maintainer 关闭时不新增持久 corpus；
- 错过首个 target、日内 schedule drift、point/snapshot/projection gap 使整日不可评估；
- 同 point/hash 幂等，不同 hash 冲突；
- source `output_runs` 删除后仍可从 projection 精确重排；
- 最新 40 日有 gap 时不跳过坏日或挑选旧窗口。

**即时 seam**

完成 M2 point → M4 corpus projection 合同测试。

### W5 — 40-day research（M5）

**主要文件**

- `src/application/strategy_lab/top1/research.py`
- `src/infrastructure/futu_gateway.py` 的最小 history K-line/quota receipt 扩展
- 相关 OpenD endpoint limit/config validation

**交付**

- `evaluate_research()` 纯 evaluator；
- `run_research()` 薄编排；
- baseline/全部 levels 的 T0 `sell_limit` 反事实、exact-expiration close/fee 经济结果；
- 日级配对统计、唯一 leader/无胜者/证据不足回执；
- research terminal 交给 M3 封存。

**验收**

- 一份手算可复核的合成 40 日完整闭环；
- 多 level、平手、无胜者、缺证据、硬风控失败均为确定性结果；
- close/quota 请求按 `(stock_owner, expiration)` 去重；
- research 不创建 live fill observation 或 outcome job；
- provider 只读 receipt 可以独立 dry-run，缺失时不回退到近似价格。

**即时 seams**

- M4 dataset → M5 research receipt；
- M5 leader → M3 validation authorization，且仍需人工确认。

### W6 — 20-day hidden validation（M6）

**主要文件**

- `src/application/strategy_lab/top1/validation.py`
- `src/application/strategy_lab/top1/fill_observation.py`
- `src/application/strategy_lab/top1/outcome.py`
- M3 的下一 schema migration
- `src/infrastructure/futu_gateway.py` 的 exact-expiration terms receipt 扩展

**交付**

- 全新、不重叠的 20 日 hidden commitment；
- `scheduled_point_first_observed_cross.v1`；
- exact-expiration terms、due queue、close/fee outcome；
- append-only decision/outcome manifests；
- 隐藏期间不泄漏中间输赢；
- `candidate_for_adoption | keep_baseline | insufficient_evidence` 最终结论。

**验收**

- 一份手算可复核的合成 20 日 fixture 跨过 decision terminal 和最后 outcome terminal；
- observed fill、no observed fill、quote/terms/close 缺失、deadline 和提前终止通过；
- baseline/challenger 共用同一 point/accepted set，但各自保留独立成交与结果证据；
- 任一必需事实缺失均 fail closed，research 的 T0 fill assumption 不进入 validation。

**即时 seam**

完成 M4 point → M6 decision/fill/outcome 合同测试。

### W7 — Advance and Linux/systemd source delivery（M7）

**主要文件**

- `src/application/strategy_lab/top1/advance.py`
- `src/interfaces/cli/strategy_lab_top1.py`
- `src/interfaces/cli/research.py`：只注册并委派
- `src/application/service_deploy.py`
- `src/application/service_drift.py`
- `src/interfaces/cli/service_ops.py`

**交付**

- `advance --scheduled` 依次做 gate、corpus capture、active validation、due jobs 和 terminal recovery；
- 默认 false 的 Linux/systemd Top1 renderer intent；
- `service.profile.json` → expected bundle → drift 的无损往返；
- 只读 feature/status/readiness。

**限制**

- `advance.py` 不包含排序、PnL、统计、SQL、OpenD payload 解析或 Prompt。
- 只交付源码和 golden tests；不安装、enable 或 start unit，不修改生产 env/config，不实现 launchd。

**验收**

- 单实验失败不阻塞同账户其他实验合法推进；
- effective gate=false 时除终止/恢复外不读 point、market、OpenD 或 due queue；
- renderer 不带 flag 时完全无 Top1 unit；带 flag 时只接受 Linux + `HK/lx` + 唯一 OpenD binding + 非空 profile env file；
- profile round-trip no drift、unit 篡改检出和显式移除后的退休语义通过。

### W8 — Receipt and LLM advisory（M8）

**主要文件**

- `src/application/strategy_lab/top1/receipt.py`
- `src/application/strategy_lab/llm_context.py`
- `src/application/agent_tools/strategy_lab.py`
- `src/application/agent_tool_registry.py`

**交付**

- `sell_put_top1_llm_prompt.v1`；
- `propose_hypothesis | analyze_research | analyze_validation` 三种 mode；
- redacted context、严格输出 schema、Prompt/input/output hash；
- schema-valid advisory 和未授权下一假设草案；
- unsupported 草案的紧凑本地 capability gap receipt。

**验收**

- Prompt bytes/version golden、mode 隔离、redaction、注入边界、schema failure 和幂等 hash 通过；
- 模型失败不改变已封闭实验结果；
- timer 永不调用模型；
- Agent tool 不能创建/启动实验、授权、锁定 challenger、采纳结果或创建 GitHub Issue。

**即时 seam**

完成 M6 final → M8 redacted context 合同测试。

### W9 — Aggregate regression only

**交付**

- 只新增一份复用公开 API 的合成 `40-day research + 20-day validation` 闭环 fixture；
- 运行完整 Strategy Lab、Candidate Engine parity、Agent contract、Research、service deploy/drift 和 dependency graph 回归。

**禁止**

- W9 不新增业务逻辑；
- 不从外部读取模块私有表、调用下划线函数或 monkeypatch 业务计算；
- 如果 aggregate test 需要上述做法，返回对应模块修正边界。

## 5. 每个 Work Unit 的固定执行模板

开工前冻结：

```text
目标
输入 schema
输出 schema
允许依赖
禁止依赖
唯一写入数据/路径
幂等键与冲突语义
focused tests
上游 seam test
退出门
```

完成时必须留下：

1. 本 Work Unit 的精确改动文件；
2. focused test 实测结果；
3. 与上游模块的 seam test 实测结果；
4. W1A 起的 `tests/test_strategy_lab_top1_architecture.py` 与 dependency graph 结果；
5. 相关 Ruff、BasedPyright 和回归测试结果；
6. 对下一 Work Unit 开放的稳定合同；
7. 未解决且不属于本 Work Unit 的问题。

每个 Work Unit 至少形成一个独立、范围明确的提交。提交、推送、合并、发布、服务安装和真实实验仍是彼此独立的授权边界。

## 6. 测试推进规则

| 完成节点 | 必须立即增加的 seam test |
|---|---|
| W2 | point builder/publisher 与 W1A 合同校验 |
| W4 | M2 point → M4 corpus projection |
| W5 | M4 dataset → M5 research receipt；M5 leader → M3 authorization |
| W6 | M4 point → M6 decision/fill/outcome |
| W7 | profile render → drift no-op/篡改/retire |
| W8 | M6 final → M8 redacted context |
| W9 | 公开 API 的完整合成闭环 |

不存在“各模块全部写完后再联调”的阶段。模块完成时，它与已经完成模块之间的接口就必须可执行、可失败、可定位。

## 7. 全局停止条件

出现以下任一情况，停止当前 Work Unit，不由实现者猜测：

- W0A 当前切片的真源、合同字段或静态依赖无法确定；W0R red/unknown 只停止 provider-dependent 路径和真实试点；
- 需要新增第二个 Candidate Engine、第二份排序/指标实现或让生产 tick 依赖实验库；
- 需要改变硬风控、首发 hypothesis type、评价指标或 40+20 时间合同；
- 无法证明 hidden-data 隔离或 terminal append-only；
- 需要生产配置修改、真实通知、交易/账本写、服务安装、发布、部署或真实实验，但未获得对应明确授权；
- 需要 registry、event bus、DI container、通用 workflow engine、GitHub adapter 或其他未出现重复需求证据的扩展。

## 8. 第一项执行任务

已有 preflight 证明 W1A 的排序真源、snapshot 事实和纯计算依赖可实施，因此下一项只执行 W1A。W0R 保持 `runtime_no_go`，费用/OpenD 缺口由独立 remediation 关闭；在其 green 前不运行 provider-dependent research/validation 或真实试点。

不得一次性创建 W1A–W8 的目录、空文件、表或 CLI scaffolding。
