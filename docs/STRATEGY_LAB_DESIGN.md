# Strategy Lab 当前实现清单

- **状态**：Phase 1 源码已实现，待自然运行取证；完整产品仍待重建
- **更新时间**：2026-08-30
- **目标产品合同**：[Strategy Lab PRD](STRATEGY_LAB_EXPERIMENT_PLATFORM_PRD.md)
- **目标技术设计**：[Strategy Lab 系统设计](STRATEGY_LAB_EXPERIMENT_PLATFORM_SYSTEM_DESIGN.md)

本文描述当前源码边界，防止把 Phase 1 基础能力或旧产品壳误认为完整产品。当前代码不是已发布且
需要兼容的产品，也没有通过新 PRD 的 20 日研究与 10 日隐藏验证验收。

## 当前结论

源码仍保留两条待删除旧入口，同时新增了 Phase 1 的最小 owner 入口：

| 入口 | 当前用途 | 与目标的关系 |
|---|---|---|
| `./om research strategy-lab top1-loop ...` | 旧 Sell Put Top1 生命周期、readiness、研究、验证和回执 | 删除，由根级 `./om strategy-lab` 替代 |
| `./om research strategy-lab update ...` | 包装 Shadow Replay dataset、mark 和 settle | 删除包装；Shadow Replay 自有入口继续保留 |
| `./om research corpus-calendar refresh ...` | Research Archive 日历刷新 | 已迁出，不依赖旧 Top1 profile |
| `./om strategy-lab readiness refresh-history-k ...` | 定向 history-K 权限、quota 和样本 PoC | Phase 1 运维入口，不是实验生命周期入口 |

当前实现把 Top1 当成产品子平台，目录位于 `src/application/strategy_lab/top1/`；旧
`ExperimentStore` 约 13 张表，包含四代 schema migration、feature、generation、capability、corpus
和 HK / Sell Put 专用状态。它们服务于未完成的旧设计，不迁移到新 Store。

代码中已有部分原语仍有价值：Candidate Engine 排序、期权持仓市值集中度、正式点和 Research
Archive、OpenD adapter、FX、费用、私有存储与锁。重建时复用这些 owner，不复用旧产品壳。

旧壳原有的 market-calendar refresh、账户 fee-plan loader 和 runtime context 已分别迁到 Research
Archive、通用 performance owner 和 `strategy_lab/service.py`。旧壳当前仍被旧生命周期调用，不在
Phase 1 提前删除；Phase 2 切换调用方后整块删除，不保留兼容层。

## 与新 PRD 的主要偏差

| 维度 | 当前源码 | 新 PRD |
|---|---|---|
| 产品入口 | `research strategy-lab top1-loop` 与 `update` | 根级 `strategy-lab` 单一入口 |
| Recipe 名称 | Sell Put Top1 | `sell_put_option_position_concentration` |
| Top1 定位 | 目录、状态机和产品命令名 | 可复用的单推荐替换评价口径 |
| 历史成交 | `t0_sell_limit` 假设成交 | 推荐后完整期权 1 分钟 K crossing |
| 评价 | Student-t、最差尾部和多项硬条件 | 日等权；收益率改善且 CNY PnL 不下降 |
| 存储 | 多代 schema、迁移和专用表 | 三张表，新建且不迁移 |
| 运行任务 | Top1 advance 加 recorder build/sample/settle | 一个独立 Strategy Lab advance |
| Tick 证据 | scheduled HK/US 为 Strategy Lab 同步刷新整仓 option marks | 只用 Tick 已有 provider 调用封存通用事实；缺失则不可评价 |
| OpenD 协调 | Strategy Lab 可与 Tick 共享 limiter / market lock | 不持有 Tick lock；只用低优先级零等待配额 |
| 版本 | 多个 Strategy Lab schema/contract 后缀 | 首次完成前不建立版本体系 |
| 验收 | 代码路径存在 | 真实 20 日研究和未来 10 日验证完成 |

因此不得继续使用旧链路积累“兼容价值”，也不得把旧状态或旧回执导入新实验。

## 当前可复用 owner

| 能力 | 当前 owner | 重建处理 |
|---|---|---|
| 生产候选和排序 | `domain/domain/engine/candidate_engine.py` | 原样复用 |
| 期权持仓市值集中度 | `domain/domain/short_vol_assessment.py` | 原样复用 |
| recommendation point 与正式点 | `src/application/recommendation_point.py`、`src/application/tick_notification_flow.py`、`src/application/research/formal_corpus.py` | 修改绑定 owner 和调用顺序，Formal Corpus 从 owner artifacts 重建后封存 |
| opening snapshot | `src/application/opening_candidate_snapshot.py` | 复用紧凑编码后的 loader 和语义 hash |
| option mark 规范化 | `src/application/performance/evidence_collection.py` | 提升现有合约行匹配、midpoint / Last fallback 和 `ValuationMarkFact` 构造为共享函数，不建新层 |
| OpenD snapshot | `src/application/opend_market_snapshot_fetching.py` | 隐藏观察复用 |
| OpenD 历史 K | `src/infrastructure/futu_gateway.py` | 研究与到期按需复用 |
| FX 和费用 | performance models、fee calculator | 原样复用 |
| 私有文件、SQLite 和锁 | `src/infrastructure/private_storage.py` | 原样复用 |

当前 `request_history_kline()` 只返回单页数据和 `page_req_key`，新 Evidence owner 必须循环到 key 为空；
Phase 1 在现有 `rate_limited_opend_call()` 的同一状态文件上增加最小低优先级零等待入口；生产 RV 的
history-K 调用也必须走该 coordinator，不能形成绕过生产预留的第二个计数面。
真实 HK 期权分钟 K 权限、过期合约覆盖和零量 bar 语义已由本机 history-K PoC 证明；远端 systemd 与
生产 Tick 的自然并发仍需取证。当前 snapshot 已保留原始
`bid_vol`；目标设计只把有限正值作为最优买价存在非零挂量的证据，不换算为合约张数，也不新增
snapshot-volume readiness 子流程。

Shadow Replay、required-data、ledger、performance evidence、Tick 和普通通知都有 Strategy Lab 外的独立
用途，不能因为删除旧实验链路而删除。

## 当前待删除 owner

| 目标 | 处理 |
|---|---|
| `src/application/strategy_lab/top1/` | 整目录删除，新产品层重新实现 |
| `src/application/strategy_lab/update.py` | 删除 recorder 包装 |
| `src/interfaces/cli/strategy_lab_top1.py` | 删除旧命令和 capability/calendar probe |
| `src/infrastructure/strategy_lab/experiment_store.py` | 全量替换，不保留 migration |
| service deploy 中 recorder 和 Top1 units | 删除，换成唯一 advance unit |
| 只验证旧入口、旧表或旧合同的测试 | 随 owner 删除 |

删除前必须用引用搜索确认边界；不能顺带删除上节列出的生产或通用研究能力。

Phase 1 已完成 calendar、fee-plan、runtime context 迁出，并让 recommendation point 从同 run
required-data / opening artifact 绑定 position / mark / FX；Formal Corpus 会从 owner artifact 重建并精确
比较该 binding。旧 `mark_evidence_accounts -> refresh_quotes=True` 已删除，不保留双写。进入 Phase 2 前
仍须用自然 Tick 证明 OpenD 调用数、snapshot 批次数和 deadline 不变差；通过后再切换旧生命周期调用方，
删除 Top1 目录和旧 CLI。

## 当前数据的处理原则

Research Archive 中的通用正式事实继续保留，因为它可以服务集中度、DTE、Delta 和其他 Recipe。
推荐时刻集中度只能读取同一 Formal Point 绑定的 position / mark / FX refs；不能用后来的 performance
evidence 回填。无法在不增加 Tick provider 调用数的条件下封存完整 mark 时，该点明确不可评价。
旧 ExperimentStore 的实验状态、projection、generation、capability 和旧回执不迁移；它们不能满足新
成交与评价合同。

新 Store 上线时不得由应用静默覆盖旧 SQLite。实施部署步骤应明确停止旧服务、确认具体旧库路径，
再由操作员选择隔离备份或删除。Research Archive 与旧 Store 是不同数据边界。

## 当前运行安全边界

在重建完成前：

- 不把旧代码路径视为新 PRD 的验收实现；
- 不用旧 `t0_sell_limit` 结果决定生产配置；
- 不因为 Strategy Lab 失败而影响 Tick、通知、持仓或交易；
- 不自动迁移、删除或补造生产 runtime artifact；
- 不发布 Strategy Lab 已完成或已提高线上收益的结论。

是否安装了旧 timer、远端是否仍有旧 SQLite、当前 Research Archive 是否健康，均属于运行事实，必须从
目标环境的 service、artifact 和健康回执只读核验，不能由本文推断。
