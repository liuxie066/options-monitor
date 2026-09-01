# Strategy Lab 当前实现清单

- **状态**：Phase 3 本地实现完成；远端自然 Tick 隔离门槛已通过；待真实 20 日 / 10 日验收
- **工程验通**：只读两日 Canary 已实现，待两个完整自然交易日线上验收
- **更新时间**：2026-09-01
- **产品合同**：[Strategy Lab PRD](STRATEGY_LAB_EXPERIMENT_PLATFORM_PRD.md)
- **技术设计**：[Strategy Lab 系统设计](STRATEGY_LAB_EXPERIMENT_PLATFORM_SYSTEM_DESIGN.md)

本文只描述当前源码。研究和隐藏验证链存在不代表已跑出真实 20 日 / 10 日结论；真实验收必须以具体
Research Receipt 和 Final Receipt 为准。

## 当前公开入口

Strategy Lab 当前有以下根级入口：

```text
./om strategy-lab recipes
./om strategy-lab canary
./om strategy-lab preview
./om strategy-lab confirm-research
./om strategy-lab preview-validation
./om strategy-lab confirm-validation
./om strategy-lab advance
./om strategy-lab status
./om strategy-lab research execute
./om strategy-lab receipt --kind research|final
./om strategy-lab readiness refresh-history-k
```

`recipes`、`canary` 和 `preview` 只读，不创建实验、写 Store 或调用 OpenD。readiness 入口预览或在
显式 hash、actor 和 `--write` 确认后发布 targeted history-K readiness receipt。
`confirm-research` 和 `confirm-validation` 分别只接受当次重建后仍可用且 hash 相同的 preview；
`research execute` 与 `advance` 每次最多执行一个 provider 逻辑证据单元。`status` 和 `receipt` 只读，只依赖
runtime / artifact / Store authority；status 会只读核对冻结 evaluator，变化时停止解释旧 spec，且不持久化
Tick/limiter 的瞬时阻塞。唯一 opt-in advance timer/service 只推进已确认的活动实验。

旧 nested Strategy Lab CLI、recorder maintenance 包装和 Top1 产品壳已删除。Shadow Replay
dataset、mark、outcome 和 candidate-impact 继续使用 `./om research shadow-replay ...`。

## 已完成的基础

| 能力 | 当前 owner | 边界 |
|---|---|---|
| Strategy Lab runtime context | `src/application/strategy_lab/service.py` | 从普通 service profile 解析固定 HK / `lx`、artifact、Store、OpenD 与 Tick 绑定；不读旧专用 profile |
| targeted history-K readiness | `src/application/strategy_lab/readiness.py` | 显式 PoC、低优先级零等待准入、不可变 receipt |
| Recipe 目录与实验 preview | `src/application/strategy_lab/recipe.py`、`src/application/strategy_lab/service.py` | 固定集中度 Recipe；只读冻结 20 日窗口、证据和 hash，不创建 provider gateway |
| 研究证据与单推荐结果 | `src/application/strategy_lab/evidence.py` | 分钟 K 模拟成交、到期结果、冻结费用/FX；query/artifact 绑定 OpenD source authority；artifact-first、低优先级、一次一个 provider 单元 |
| Top1 Comparison | `src/application/strategy_lab/comparison.py` | 可复用的单推荐替换比较；按日等权比较年化收益率和 CNY 收益金额 |
| Research Receipt | `src/application/strategy_lab/receipts.py` | provisional、write-once-or-verify；公共读取反查 Store ref/hash；明确模拟成交不是实际交易 |
| 10 日隐藏验证 | `src/application/strategy_lab/service.py`、`src/application/strategy_lab/evidence.py` | 冻结分钟网格；批量 Bid / Bid Volume；每批最多一次低优先级零等待 provider 调用；缺失证据 fail closed |
| Final Receipt | `src/application/strategy_lab/receipts.py` | 两次确认、锁定 leader、fill/outcome 引用、按日比较、安全状态和三态结论；write-once-or-verify |
| 独立 advance 调度 | `src/application/service_deploy.py` | opt-in 的唯一 timer/service；不进入 Tick 调用链，不持有 Tick lock |
| Experiment Store | `src/infrastructure/strategy_lab/experiment_store.py` | 只接受新建/空库或精确三表 schema；不迁移旧数据 |
| 最小合同 | `src/application/strategy_lab/contracts.py` | 固定 HK / `lx` / Cash-Secured Put (CSP) 范围、研究状态和 canonical hash |
| Formal Corpus | `src/application/research/formal_corpus.py` | expectation、formal point、calendar 和不可变证据 |
| 生产优先级 | `src/application/opend_call_coordinator.py`、`src/application/tick_cron.py` | Strategy Lab 只可低优先级零等待准入，不取得 Tick lock |

三表 Store 仅包含 `experiments`、`experiment_events` 和 `experiment_observations`。全局
最多一个非终态实验，确认、revision、observation 和 receipt 绑定必须幂等并 fail closed。

## 两日工程验通

两日工程验通用于尽快确认新链路能够消费真实正式点并执行集中度 Recipe。它是工程 canary，不能替代
20 日研究或 10 日隐藏验证，也不能产生实验结论。

公开只读入口为：

```bash
./om strategy-lab canary --profile-path <runtime>/service.profile.json
```

CLI 在一次调用内冻结当前 UTC 时间，并调用 `preview_engineering_canary()`。该服务只选择截至该时间的
最近两个市场交易日；不会因为最近一天缺点、冲突或尚未结束而回退到更早的完整日。每个选中日期必须：

1. 存在唯一且有效的 expectation；
2. 所有预期正式推荐点均已封存且早于调用时间；
3. 每个正式点都能复用 `build_concentration_arms()` 生成一个 baseline 和三个固定 challenger；
4. 不要求合约已经到期，不读取 terminal FX、费用计划或收益 outcome。

输出保持为一个小型只读摘要：

```json
{
  "authoritative": false,
  "status": "available",
  "observed_at_utc": "<UTC>",
  "selected_trading_dates": ["<day-1>", "<day-2>"],
  "blockers": [],
  "projection": {
    "recipe_id": "sell_put_option_position_concentration",
    "trading_day_count": 2,
    "recommendation_point_count": 24,
    "variant_ids": [
      "baseline",
      "challenger_0.002",
      "challenger_0.004",
      "challenger_0.006"
    ]
  },
  "unlocks": []
}
```

实际点数由两日 expectation 决定，示例中的 `24` 不是固定门槛。缺日、冲突、点未到时或 Recipe
证据不完整时返回 `blocked`、原 owner 的 reason code 和 `projection: null`；不得增加 Canary 专用的
reason-code 映射层。任何结果的 `authoritative` 都为 `false`，`unlocks` 都为空，也不包含 leader、
comparison、Research Receipt 或 Final Receipt 字段。`available` 只表示两日 Corpus 和 Recipe 投影可用，
不是实验通过或收益结论。

实现边界：

| 文件 | 当前实现 |
|---|---|
| `src/application/strategy_lab/recipe.py` | 给现有 `_load_window_day()` 增加一个默认开启的内部 maturity 开关，并新增固定两日的 `select_engineering_canary_window()`；正式 20 日选择保持原行为，Canary 不要求到期且不回退 |
| `src/application/strategy_lab/service.py` | 新增只读 `preview_engineering_canary()`，校验轻量 runtime context 并汇总覆盖和 Recipe 投影 |
| `src/interfaces/cli/strategy_lab_ops.py` | 新增 `canary` 子命令；只调用 `resolve_strategy_lab_runtime_context(profile, market="hk")` 并要求 profile 包含 `lx`，不解析 ledger、费用或 OpenD binding，不打开或创建 Experiment Store |
| `tests/test_strategy_lab_recipe.py`、`tests/test_strategy_lab_cli.py` | 增加一个未到期两日成功测试、一个最新日异常且不回退测试、一个 CLI 单次时钟及零文件/Store/OpenD 副作用测试；缺失和冲突的底层矩阵继续由现有 Formal Corpus 测试拥有 |

不修改 `RESEARCH_SESSIONS = 20`、`VALIDATION_SESSIONS = 10`、正式 preview、Research Receipt、
leader、隐藏验证或 Final Receipt。Canary 不增加表、状态、timer、配置项、可变 `--days` 参数或迁移。

history-K provider PoC 仍由现有 `strategy-lab readiness refresh-history-k` 显式执行。该入口按合同只接受
已到期合约；两日新候选通常尚未到期，因此 Canary 不生成伪装成可立即执行的 probe，也不把 provider
调用藏进只读命令。工程验通时另选一个已到期真实合约执行 PoC，并独立核对自然 Tick 的延迟和超时。

两日工程验通的完成信号是：Canary 对两个连续自然交易日返回 `available`、显式 history-K PoC 成功、
同一窗口内 Tick 没有新增 Strategy Lab 导致的超时。它仍不能证明收益、成交、Research Receipt、
research leader 或 Final Receipt。

## 尚未完成

- 两日工程 Canary 的真实线上验通；
- 真实 20 日研究、未来 10 日隐藏验证、合约到期等待和 Final Receipt 审计；
- MCP、Skill、飞书、并行实验、自动采用与生产配置写入。

因此，没有具体 Research Receipt 时不能宣称已有 research leader；没有具体 Final Receipt 时也不能宣称
challenger 已通过或已经实现线上收益。

## 保留的通用 owner

Candidate Engine、期权持仓市值集中度、Research Archive、opening snapshot、performance
evidence、FX、费用、OpenD gateway、required-data、ledger、Shadow Replay、Tick 和普通通知
均有 Strategy Lab 以外的用途。它们是待完成 Recipe 的复用边界，不是应删除的兼容壳。

Research Archive 中的通用正式事实继续保留，可服务集中度、DTE、Delta 和其他后续
Recipe。旧实验状态、旧回执和旧 Store schema 不迁移。

## 当前安全和交付门槛

- Strategy Lab 失败不得影响 Tick、通知、持仓或交易；
- 不自动迁移、删除或补造运行 artifact；
- 不将 readiness receipt 表述为实验成功；
- 不在产品流程完成前声称 Strategy Lab 可用或已提高线上收益；
- Phase 1 远端自然 Tick 隔离门槛已通过；Phase 4 的真实 20 日 / 10 日证据仍不能由本地测试替代。

远端是否仍安装旧 service/timer、是否存在旧 SQLite、Research Archive 是否健康，都是运行事实。
本次源码删除不会自动修改生产服务或删除运行数据。
