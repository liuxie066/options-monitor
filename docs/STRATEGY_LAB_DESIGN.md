# Strategy Lab 当前实现清单

- **状态**：Phase 3 本地实现完成；远端自然 Tick 隔离门槛已通过；待真实 20 日 / 10 日验收
- **更新时间**：2026-08-31
- **产品合同**：[Strategy Lab PRD](STRATEGY_LAB_EXPERIMENT_PLATFORM_PRD.md)
- **技术设计**：[Strategy Lab 系统设计](STRATEGY_LAB_EXPERIMENT_PLATFORM_SYSTEM_DESIGN.md)

本文只描述当前源码。研究和隐藏验证链存在不代表已跑出真实 20 日 / 10 日结论；真实验收必须以具体
Research Receipt 和 Final Receipt 为准。

## 当前公开入口

Strategy Lab 当前有以下根级入口：

```text
./om strategy-lab recipes
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

`recipes` 和 `preview` 只读，不创建实验、写 Store 或调用 OpenD。readiness 入口预览或在
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
| 最小合同 | `src/application/strategy_lab/contracts.py` | 固定 HK / `lx` / Sell Put 范围、研究状态和 canonical hash |
| Formal Corpus | `src/application/research/formal_corpus.py` | expectation、formal point、calendar 和不可变证据 |
| 生产优先级 | `src/application/opend_call_coordinator.py`、`src/application/tick_cron.py` | Strategy Lab 只可低优先级零等待准入，不取得 Tick lock |

三表 Store 仅包含 `experiments`、`experiment_events` 和 `experiment_observations`。全局
最多一个非终态实验，确认、revision、observation 和 receipt 绑定必须幂等并 fail closed。

## 尚未完成

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
