# 业务入口取数复用与离线耗时基线

这是指定源码和合成输入的基线，不代表生产状态。结论是先补齐实际慢在哪里的证据，再决定架构：现有入口已经共用账户 portfolio 读取器；已合格快照可以跨 scan、assignment、Wheel 复用。独立目录、严格只读、run 隔离、资格失效仍会触发刷新。当前证据不足以支持拆服务。

## 版本、复现与证据范围

交付时整合并发 main 更新至 `e3756a5cc938e6a3caed76121325087cd8557092`，变动为 service deploy、CI 门禁和既有测试；本基线涉及的取数 owner、脚本、fixture 与依赖未变化。下述测量仍绑定原审计源码和脚本摘要；最终集成门禁单独针对更新后的 base 验证。

审计基线：`1bd339cc96f77564d7879024a36f5d384da337fb`。测量 checkout 的 HEAD 是 accepted-plan checkpoint `0aa921b88d4a8988083938c48d0060973662727c`，当时脚本与测试待提交；脚本内容摘要见下方。最终 PR 中的脚本和测试是可复现来源，checkpoint 的 Git tree 本身不包含新脚本。

```sh
./.venv/bin/python scripts/benchmark_business_fetch.py --samples 7
```

默认七次，接受 1–50 次；只输出 JSON，不接收生产配置或 runtime 路径。每个案例、每轮都使用新的临时目录；fixture 预写、配置发布、模块导入不计时，业务调用内缓存与 prepared 文件读写计时。业务时钟固定，耗时用真实 `perf_counter`。不注入网络延迟、不设性能门槛。测试用例、逐轮 trace/count/outcome 和 min/median/max 均由脚本输出；不同路径或结果的样本不混合汇总。

证据分级：

- **实测 owner/adapter**：真实 shared reader、Futu provider 归一化、真实 gateway/client 参数与响应映射、现金应用及 Tool adapter、pipeline context、prepared worker/promotion/load、Wheel CLI/Tool capacity adapter、assignment collect/project。
- **替身边界计数**：ready factory、SDK context 的 `accinfo_query` / `position_list_query`、FX、PM、quote、账本读取、决策快照、option context、source persistence、position sizing。它们的次数属于调用边界证据，耗时包含轻量替身开销，真实 I/O 耗时未测。
- **静态追踪**：公开命令 dispatch、完整扫描/候选链、Wheel preview/confirm/receipt 写入链、assignment CLI/Tool 外壳。本次没有执行整条运行命令，没有证明线上请求频率或通知/持久化成功。

socket connect/connect_ex、create_connection、urllib 入口有网络阻断；预期案例的 blocked-attempt 必须为零，被 owner 捕获的阻断也会使样本失败。所有应用配置、数据与缓存路径是临时目录；真实 SDK 用模块替身避免 import 日志副作用。prepared 使用同步 Popen 替身调用真实 `run_worker`，没有启动子进程。仅适用于这个离线脚本，不能当作生产命令的隔离承诺。

## 真实入口与所有者审计

| 入口 | 实际调用链与复用边界 | 本次证据 |
|---|---|---|
| 公开 scan / Tool scan | CLI `scan` → operator handler → `run_scan` → `scan_opportunities` → materialization adapter → watchlist pipeline → `build_pipeline_context` → shared reader → Futu；默认 Tool 输出根下的 state，允许写缓存 | context 装配实测，显式账户 state；公开/Tool 外壳和完整扫描静态 |
| scan-pipeline 入口 | CLI `scan-pipeline` → pipeline runtime → 同一 watchlist pipeline/context；state 由该入口提供，不另写 portfolio 资格规则 | 静态；context owner 已测 |
| scheduled scan | tick account execution 发布账户配置 → `prepare_portfolio_contexts` → worker/shared reader → run 账户 state 的 manifest/payload → pipeline 验证并消费 | real worker/promotion/load 实测；tick 调度、并发、子进程和 prepared option/封存 FX 仅静态 |
| cash CLI | scheduler ops → `query_sell_put_cash` → shared reader；默认 `output_shared/state`，允许写缓存 | 应用 facade 两次实测，使用显式账户 state 覆盖；默认路径/CLI 外壳静态 |
| cash Tool | materialization → `query_cash_headroom_tool` → 同一 cash facade/shared reader；`write_cache=False`，默认 `output_shared/agent_tools/query_cash_headroom`，可由输出目录覆盖 | Tool adapter 实测，临时输出目录；默认 resolver 静态 |
| Wheel CLI/Tool cash/coverage | 各自薄 adapter → `load_shared_cash_capacity_fact` / `load_shared_coverage_fact` → frozen context → shared reader；账户 `output_accounts/<account>/state`，只读 | 两种 adapter 实测；真实资格检查保留，锁/账本/决策/option context 替身 |
| Wheel Call intent preview/confirm | CLI/Tool 加载候选 snapshot、当前 policy 与 coverage → workflows 创建 intent；writer 在新 intent 前核对新鲜度，事务内重新读取决策与占用 | 静态，未测完整 mutation；不能把 capacity adapter 耗时当 intent 耗时 |
| Wheel Put intent preview/confirm | 预览候选封存 portfolio/ledger 依赖 → `load_wheel_candidate_cash_fact` 校验并加载现金事实 → workflows；新 intent 与 receipt replay 的资格顺序不同 | 静态，sealed fact 无在线 portfolio 刷新；不据此复用未封存当前现金 |
| Wheel cancel / replay | Put cancel 入口取当前 cash capacity；Tool Call 路由也先取 coverage。writer 内 receipt 先于新 intent 资格判定；入口层仍可能已产生读取 | 静态；重复 receipt 路径的实际成本待测，不能声称整个 replay 零取数 |
| assignment CLI/Tool | portfolio ops / portfolio tool → `query_portfolio_assignment_scenario` → ledger + FX → shared reader（每账户）→ collect evidence → projection | 实测应用/collect/project；外壳静态 |
| assignment 非 Futu holdings / quotes | 启用 holdings 才读 PM non-Futu valuation；Futu quotes 独立补价。PM 失败时保留 partial，不能冒充完整资产 | 一个 PM failure fixture 边界和 quote fixture 实测；PM 完整协议/真实报价/多账户聚合未测 |

主要源文件：[API Index](API_INDEX.md)、[shared reader](../src/application/portfolio_context_service.py)、[Futu provider](../src/application/futu_portfolio_context.py)、[gateway](../src/infrastructure/futu_gateway.py)、[pipeline context](../src/application/pipeline_context.py)、[公开 scan facade](../src/application/scan_pipeline.py)、[watchlist pipeline](../src/application/pipeline_watchlist.py)、[prepared owner](../src/application/prepared_portfolio_context.py)、[tick preparation](../src/application/tick_account_execution.py)、[cash facade](../src/application/cash_headroom_query.py)、[cash Tool adapter](../src/application/agent_tools/materialization_impl.py)、[cash CLI](../src/interfaces/cli/scheduler_ops.py)、[Wheel capacity](../src/application/wheel/capacity.py)、[Wheel CLI](../src/interfaces/cli/wheel.py)、[Wheel Tool](../src/application/agent_tools/positions.py)、[Wheel writer](../src/application/wheel/workflows.py)、[sealed cash fact](../src/application/wheel/candidate_snapshot.py)、[assignment owner](../src/application/portfolio_assignment_scenario.py)、[assignment CLI](../src/interfaces/cli/portfolio_ops.py)、[assignment Tool](../src/application/agent_tools/portfolio.py)、[output resolver](../src/application/agent_tool_config.py)。

shared reader 按逻辑账户、物理账户、环境、市场/asset scope、完整性、时间与 cash authority 决定资格。普通 stock 窗口 300 秒，stock+option 严格窗口 60 秒；持仓刷新失败时仅保留独立合格现金。跨账户或降低资格标准来消除调用违反共享契约。

## 实测结果

每行是一个案例的完整调用序列，不是一次线上请求。SDK 列是替身查询次数，不包含连接、握手、账户列表、序列化、协议、retry 内部、option enrichment。单条 NVDA long stock，零 option，空可信账本；没有代表真实账户规模。

脚本 SHA256：`bbfcab4b3b6e0023734accac926d17374b01c525f2944ee5430b027572bda8dd`。测量环境：Python 3.12.13 / macOS-27.0.1-arm64-arm-64bit；固定业务时间 2026-10-09T04:00:00+00:00。

| 案例/序列 | shared reader | portfolio 刷新 | SDK cash / positions | median ms | min–max ms |
|---|---:|---:|---:|---:|---:|
| `scan_cold` | 2 | 1 | 1 / 1 | 3.656 | 1.762–8.682 |
| `scan_warm` | 2 | 0 | 0 / 0 | 0.994 | 0.938–1.231 |
| `cash_cli_cold` | 2 | 1 | 1 / 1 | 1.878 | 1.759–2.353 |
| `cash_tool_cold` | 2 | 2 | 2 / 2 | 1.591 | 1.523–2.477 |
| `cash_tool_warm` | 2 | 0 | 0 / 0 | 1.084 | 0.984–1.253 |
| `cross_entry` | 5 | 2 | 2 / 2 | 4.135 | 3.877–4.420 |
| `prepared` | 1 | 1 | 1 / 1 | 6.873 | 6.304–8.685 |
| `prepared_new_run` | 2 | 2 | 2 / 2 | 8.201 | 7.948–8.746 |
| `prepared_bad_hash` | 1 | 1 | 1 / 1 | 4.195 | 3.973–4.638 |
| `prepared_bad_account` | 1 | 1 | 1 / 1 | 4.565 | 3.780–5.476 |
| `account_isolation` | 2 | 2 | 2 / 2 | 2.774 | 2.281–3.252 |
| `physical_account_isolation` | 2 | 2 | 2 / 2 | 2.457 | 2.375–3.229 |
| `path_isolation` | 2 | 2 | 2 / 2 | 2.098 | 1.995–2.501 |
| `stock_301_cash_fresh` | 2 | 1 | 1 / 1 | 1.618 | 1.429–12.422 |
| `stock_option_61` | 2 | 1 | 1 / 1 | 4.057 | 1.653–12.104 |
| `partial_stock` | 2 | 2 | 2 / 2 | 3.213 | 2.595–6.064 |
| `positions_failure` | 2 | 2 | 2 / 2 | 2.765 | 2.475–3.622 |
| `balance_failure` | 2 | 2 | 2 / 0 | 0.866 | 0.793–1.279 |
| `timeout` | 2 | 2 | 0 / 0 | 0.442 | 0.432–0.503 |
| `cancel` | 2 | 2 | 0 / 0 | 0.518 | 0.461–3.431 |
| `assignment_pm_unavailable` | 1 | 1 | 1 / 1 | 0.895 | 0.814–1.136 |
| `wheel_cli_cold` | 2 | 2 | 2 / 2 | 1.350 | 1.308–1.696 |
| `wheel_tool_cold` | 2 | 2 | 2 / 2 | 1.490 | 1.425–1.662 |

prepared 分阶段（每阶段也是七轮）：

| 阶段 | portfolio 刷新 | median ms | min–max ms |
|---|---:|---:|---:|
| prepare_run-a | 1 | 4.558 | 3.759–5.264 |
| consume | 0 | 0.539 | 0.495–0.609 |
| consume | 0 | 0.473 | 0.408–0.639 |
| adopt | 0 | 1.395 | 1.062–2.367 |

案例解释与结果资格：

- scan/cash 两次调用：冷可写路径刷新一次，合格 warm 路径零次。cash Tool 冷只读路径两次，预先已有合格缓存时零次，冷路径不会写 portfolio 缓存。
- cross_entry：显式同账户 state 的 scan context → assignment → Wheel coverage → Wheel cash 在同一账户目录，仅首次刷新；随后 cash Tool 的独立目录再刷新一次。assignment 完整、Wheel cash/coverage 可用。这里没有把 CLI 默认共享目录等同于账户目录。
- prepared：创建 ready → 消费两次 → 复用已存在 manifest，仅创建刷新一次；新 run 私有 state 再刷新。bad hash/account 两例包含一次有效创建，随后拒绝消费，没有退回直接 provider。
- account/physical account/path isolation：两次请求分别换逻辑账户、物理账户、目录，均刷新两次；不能作为可以安全消除的重复。
- stock_301_cash_fresh：先现金请求复用，再 stock 请求刷新；stock_option_61：严格范围首次刷新，随后复用。零 option fixture 未验证真实 option term enrichment。
- partial_stock：真实 provider 返回后注入 partial completeness，两次 stock 请求均刷新，现金 fresh、持仓不合格。positions_failure 在 SDK context 注入失败，仍现金 fresh、持仓不合格；balance_failure 为 unavailable/unknown，未继续查 positions。
- timeout/cancel 在 shared reader 注入的 fetch 边界传播；这是该层传播证据，不代表 gateway wire timeout 行为。失败路径耗时是错误处理成本，不属于成功查询性能。
- assignment_pm_unavailable 保持 partial，PM 与 quote 各调用一次。Wheel cold 两种 adapter 均返回可用 cash/coverage，各自两次只读刷新；这是两种事实连续读取，不代表某个完整业务命令必然同时读取两者。

prepared 消费阶段还各触达一次 FX fixture，因为本例没有 prepared option manifest，实际 context 仍调用其 FX 装配分支。真实 scheduled 同时传入 prepared option 与封存 FX 的路径只做静态审计；不能把这个次数当成生产重复 FX 下载。

本地可写现金路径虽然刷新次数少，却可能比冷只读路径稍慢，因为测的是本地写文件和替身处理；这正说明不能从取数次数推出实际延迟改善，也不能用这些毫秒数字估算拆服务收益。

## 重复取数裁决与下一步

| 分类 | 证据与裁决 | 后续责任 |
|---|---|---|
| 必须保留 | 跨逻辑/物理账户、目录绑定、run 隔离、stock/option 资格失效、partial/失败后的重新观察 | shared reader / prepared owner；本次保持契约 |
| 已实现复用 | 合格缓存、同账户同目录跨入口、同 run prepared 消费/manifest adoption | 无需再建相同读取器 |
| 可能可减少 | 冷只读 Tool/Wheel 连续请求不能自行留下缓存；不同默认目录不会命中另一个入口的缓存 | 后续 work unit：materialization/output owner、Wheel capacity owner；先确定真实频率及请求内事实复用可行性 |
| 需区分事实 | assignment quote 与 PM 非 Futu valuation 不是已有 Futu cash/stock 的同一事实；事务内 ledger 重读用于当前占用，sealed Put fact 用于预览一致性 | assignment / ledger / Wheel owner；不能合并掉所需来源与时间边界 |
| 静态线索 | Wheel adapter 在 writer receipt replay 之前取得某些 capacity；writer 的幂等分支不会抹掉入口成本 | 后续 work unit：Wheel adapters/workflows；先测请求链，未在本次认定为新缺陷或修改顺序 |

**优先生产测量选择：先读取现有 tick/run artifacts，再在证据缺口处设计少量观测。此文仅提出方案，没有执行授权。**

1. 现有证据优先：明确 host、effective runtime root、版本/commit、config hash、market、账户与观察时间后，只读最近至多 10 个完整 tick/run 的现存 `prepared_context_metrics`、manifest 时间戳、stage timings 与错误记录。分 ready/unavailable/timeout，不把缺行当零调用。限制至多 2 个明确账户、最多 20 份账户证据、10 分钟；不重新触发 scan。先确认当前产物是否足以定位慢段。现存指标能证明阶段耗时，不能证明 SDK 每次调用数量。
2. 仍缺调用数时：另行授权一个最小观测 work unit。记录现有真实请求的 entry/run/account span、cache source/qualification reason、portfolio refresh count，及 readiness、balance、positions、quote、PM、FX 分段耗时/状态。只收逻辑绑定和脱敏物理绑定摘要，不收余额、持仓行、凭据。SDK 查询与连接/readiness 另计，调用次数按同一 span 关联，不能靠日志行数猜次数。首选一轮正常 scheduled run 与至多两次已有只读 Tool 请求的被动观测；总窗口 ≤15 分钟，先给出 preview、启停与日志去向。无授权不改服务、不额外发起查询。
3. 只有发现错误重复或查询是主要慢段后，才针对原 owner 评估请求内复用/读取 prepared fact；保留账户、scope、时间、封存依赖与事务重读要求。需要跨进程共同服务数据且进程隔离、独立扩缩容、故障恢复收益可验证时，再评估服务拆分。当前离线结果不能给出这种结论。

停止条件：host/version/config/account 绑定不一致、出现账户混用或脱敏失败、现存产物不全/时间不可信、额外 SDK 调用超过批准预算、provider 限流/timeout 增加、测量改变正常结果或写入范围。停止收集并保留证据，不用重试扫描补齐样本，不自动升级预算。生产测量 owner 是操作者与后续明确授权的 work unit。

## 验证与限制

离线基线回归测试检查所有案例调用数、现金与持仓资格、prepared 拒绝后无 fallback、PM partial、网络阻断及替身恢复；相关 cash/pipeline/prepared/holdings/assignment/Wheel 消费者也做定向回归。完整 pytest、Ruff、Guardrails、dependency graph 的具体结果绑定到本次 PR 的交付记录与 CI。

未覆盖：真实 OpenD/PM/FX/报价、网络/握手与 retry、SDK 编码、真实账户/option 数量、完整 scan/通知、真实账本与 writer 锁、并发/队列/子进程启动、prepared option 与封存 FX 的端到端测量、完整 Wheel mutation/receipt 重放。以上归属后续测量 work unit，不作为本基线的已验证结论。脚本不修改任何生产 owner、业务策略、资格、TTL、配置或服务。
