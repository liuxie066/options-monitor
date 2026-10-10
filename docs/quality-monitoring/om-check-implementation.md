# OM 质量检查实现映射

- **规范来源**：本页检查矩阵与 [OM 本地质量文件契约](../../contracts/quality-monitoring/README.md)

本文把规范中的 OM 检查 ID 映射到当前实现入口、确定性测试和门禁范围，不改变检查矩阵的业务语义。

## 运行检查

| ID | 实现入口 | 当前证据 | 本地结论边界 |
|---|---|---|---|
| `RT-OM-001` | `src/application/quality/runtime_checks.py::build_runtime_checks` | `tests/quality/test_om_quality_service.py` | 复用 `runtime_status` service profile |
| `RT-OM-002` | `src/application/quality/runtime_checks.py::build_runtime_checks` | `tests/quality/test_om_quality_service.py` | 按 account/source 判断 listener heartbeat、stage、last error |
| `RT-OM-003` | `src/application/quality/runtime_checks.py::build_runtime_checks` | `tests/quality/test_om_quality_service.py` | 读取现有 timer/run receipt |
| `RT-OM-004` | `src/application/quality/position_checks.py::build_opend_runtime_check` | `tests/quality/test_om_quality_service.py`、`tests/quality/test_om_quality_checks.py` | 要求 REAL、显式账户、`refresh_cache=True`、snapshot complete |

## 数据检查

| ID | 实现入口 | 确定性回归证据 | `blocked_consumers` |
|---|---|---|---|
| `OM-INT-001` | `src/application/quality/intake_checks.py::build_trade_intake_datasets` | service fixture 覆盖 pending/heartbeat/checkpoint facade | `option_position_report`、`lifecycle`、`close_advice` |
| `OM-INT-002` | 同上 | service fixture 覆盖 failed/unresolved facade | 同上 |
| `OM-INT-003` | 同上 | audit 口径 reconciliation 与无 audit 运行预览的差异、缺证据 unknown、显式单源路径回归 | `option_position_report`、历史成交消费者 |
| `OM-LED-001` | `src/application/quality/ledger_checks.py::build_ledger_datasets` | `test_full_replay_mismatch_blocks_position_consumers` | `option_position_report`、`lifecycle`、`close_advice` |
| `OM-LED-002` | 同上 | `test_duplicate_broker_identity_with_economic_conflict_is_blocking` | 同上 |
| `OM-POS-001` | `src/application/quality/position_checks.py::build_position_dataset` | schema-valid service fixture；OpenD completeness 回归 | `option_position_report`、`lifecycle`、`close_advice` |
| `OM-POS-002` | 同上 | 持仓 code lineage 对比当前 snapshot 条款；换仓反例、lifecycle 优先级、Scheduled Tick account/market gate、transient、5 分钟 persistent 回归 | 同上 |
| `OM-LCY-001` | `src/application/quality/lifecycle_checks.py::build_lifecycle_datasets` | 周末/假日 deadline；11 条 stale 固定回归 | `lifecycle`、`close_advice` |
| `OM-LCY-002` | 同上 | external adjustment 与 legacy gap 分离回归 | 受影响的 `lifecycle`、`close_advice` |
| `OM-LCY-003` | 同上 | legacy history 独立 dataset 回归 | 受影响历史报告 |

质检读取的私有 `runtime_status.trade_intake` 保留不读取历史 audit 的原有 reconciliation 预览，
用于运行状态展示。每个分源 `summary.audit_reconciliation`（显式单源路径在顶层
`summary`）是使用该 source audit 路径的只读预览；`OM-INT-003` 只用这份
与处置命令同口径的 pending 和 delegated 结果。该字段缺失或预览不可用时，
检查为 `unknown`，不以无 audit 的数值判 `fail`，也不把未知当零。两次预览
不是同一个原子快照；坏 JSONL 审计行仍按现有共同解析器跳过，不能把
`available` 解读为审计文件完整性证明。

## 发布、读取和门禁边界

| 能力 | 实现 | 验证 |
|---|---|---|
| 原子 artifact | `src/infrastructure/quality/artifact_repository.py` | schema-valid service 发布测试 |
| 控制状态 | `src/infrastructure/quality/control_state_repository.py` | transient→persistent、首次 deep reconcile 测试 |
| OpenD 只读快照 | `src/application/quality/opend_position_adapter.py` | fake adapter、请求市场隔离与 position/lifecycle 测试 |
| CLI | `src/interfaces/quality/cli.py` | 复用同一 service/artifact |
| Agent tool | `src/application/agent_tools/quality.py` | agent contract/plugin smoke 全量回归 |
| 本地门禁 | `src/application/quality/gate.py` | onboarding 前无效；onboarding 后按 account/market/consumer 阻断；目标持仓数据集缺失/重复和 stale 均 fail closed |

## Quality refresh 账本读取效率合同

状态：只读查询优化已实现并有确定性回归。现有质量检查语义、artifact
schema 和 600 秒 systemd 上限均保持不变；生产耗时仍需另行授权部署后验证。

### 目标、非目标与成功信号

目标是消除常规 quality refresh 在交易录入对账期间对生命周期 evidence 的
重复全表读取和 JSON 解码。优化后，带 `case_id`、`account` 或 `symbol` 条件的
只读 evidence 查询应由 SQLite 过滤，仅解析匹配行。

成功信号为：

- `list_trade_lifecycle_evidence()` 的返回字段和过滤语义与 canonical
  SQLite repository 一致，并继续注入 `_ledger_created_at_ms`；带过滤条件时
  显式按 `created_at_ms, evidence_id` 升序返回，无过滤条件时保留当前无
  `ORDER BY` 的全表读取，避免新增无索引全局排序；
- 缺表仍返回空列表，损坏 JSON 仍按当前只读容错语义跳过；SQL/连接
  异常仍从 repository 传出，而 `_completed_lifecycle_cases_by_deal()` 目前会将单个
  case 的读取异常降级为空 evidence 并继续生成 preview；本次性能修复不改
  这一既有错误语义；
- trade-intake reconciliation preview 仍为只读，quality payload、检查 ID、
  blocking 结论和 OpenD 调用策略均不改变；
- 确定性回归通过 SQL trace 和 JSON 解码计数证明过滤查询不会解析无关
  evidence 行；本地计时分析只是可选补充，生产耗时只有在另行授权发布、升级
  并完成自然调度验证后才能确认。

本工作单元不新增缓存、线程、进程、配置键、数据库迁移、公开命令或状态；不调整
600 秒超时，不并行 OpenD，也不顺带优化 assistant audit 的最新记录查询。

### 当前事实与约束

常规 producer 依次读取 US、HK 两份 `runtime_status`。每份 runtime status 又按
trade-intake source 生成 reconciliation preview；这些 preview 指向同一个
`option_positions.sqlite3`。`_completed_lifecycle_cases_by_deal()` 对每个已完成
lifecycle case 调用一次带 `case_id` 的 evidence 查询。

canonical 可写 repository 已在 SQL 中组合 `case_id`、`account`、`symbol` 条件，
并按 `created_at_ms, evidence_id` 排序。修改前的只读 evidence adapter 先读取并解析
整张 `trade_lifecycle_evidence`，再在 Python 中过滤。表上已经存在以 `case_id`
开头的索引，因此修复不需要 schema 变更。

2026-09-04 的生产只读诊断中，约 63 MB 的 ledger 对应单次 refresh 超过 6 GB
逻辑读取量和约 540 MiB 内存峰值；一次 6 分 55 秒自然运行直到最后约 9 秒才连接
OpenD。该快照用于定位读取放大，不作为修复后的性能验收结果。

### 选定设计、数据流与失败语义

唯一实现 owner 是
`src/application/ledger/read_only_evidence.py::_ReadOnlyTradeReconciliationEvidenceRepository`。
其 `list_trade_lifecycle_evidence()` 复用 canonical repository 的查询形状：

```text
quality refresh
  -> runtime_status (US, HK)
  -> trade-intake reconciliation preview (lx, sy)
  -> read-only evidence repository
  -> SQLite WHERE case_id/account/symbol
  -> JSON decode matching rows only
  -> unchanged reconciliation and quality results
```

实现继续使用现有 `_connect()`、`PRAGMA query_only=ON`、`_table_exists()` 和
`_read_json_query_from_conn()`；不引入新的 repository、cache 或查询构建器。
过滤子句是 canonical repository 的三个独立可选等值条件，只以原始
参数的 truthiness 决定是否加入子句，再规范化绑定值：account 小写、symbol
大写、case ID 去除首尾空白。因此传入仅含空白的真值字符串时，仍生成
过滤子句并绑定空字符串，不得退化为无条件全表读取。

带任意过滤条件的查询沿用 canonical 的
`ORDER BY created_at_ms ASC, evidence_id ASC`；`case_id` 路径可复用现有索引。
无过滤条件时仍读取全部 evidence，保留当前无显式排序的行为，以避免在
没有对应全局索引时引入临时排序。过滤路径的显式时间顺序对下游
`entries[-1]` 选取最新证据有行为意义，因此必须有同时间戳和逆序插入的
回归证明，不依赖 SQLite 的物理行顺序。

若表不存在，方法在执行查询前返回空列表。若匹配行 JSON 损坏，沿用当前
`strict=False` 行级跳过语义。SQL/连接异常不在 repository 方法中吞掉；
现有 `_completed_lifecycle_cases_by_deal()` 的宽泛 `except Exception` 会把该 case 当作
无 evidence，这是已存在的正确性残余风险。本工作单元只修复读取放大，不顺带
改变 reconciliation 错误语义。

### 未选择的方案

- 不在 `_completed_lifecycle_cases_by_deal()` 一次性加载整表后分组：只能修复一个
  调用方，保留只读 repository 的低效过滤合同。
- 不在 quality service 跨 market/account 缓存 reconciliation：改动范围更大，
  需要额外定义快照一致性和失效规则；只有 SQL 下推验证后仍超过目标才重新评估。
- 不增加 assistant audit `(created_at, id)` 索引：它是独立次级热点，需要 schema
  变更，不属于本次最小修复。

### 实施与验证

1. 只读 evidence repository 复用 canonical 的三条可选子句、参数规范化和
   过滤排序；无过滤路径不加全局排序，并保持缺表、JSON 容错和
   `_ledger_created_at_ms` 合同。
2. `tests/test_ledger_current_decision_projection.py` 使用 SQLite trace 和该模块的
   `json.loads` 计数覆盖组合过滤、大小写规范化、空白真值、损坏行、匹配行
   解码、稳定过滤排序、无过滤全量读取和只读不写。
3. 验证包括该直接回归、trade-intake reconciliation、quality service、相关
   ledger projection 测试和全仓静态检查。不设机器耗时阈值。

主要风险是生产旧 schema 缺少过滤列，或 SQL 列与 `raw_json` 的规范化值发生
漂移。canonical repository 已以这些列作为过滤权威，但 SQL 下推会使漂移行成为
假阴性，Python 后过滤无法挽回没被 SQL 选中的行。因此后续获得部署授权时，
必须在升级前对生产 ledger 做一次只读兼容门禁：确认
`case_id/account/symbol` 列存在，并对可解码的 dict payload 计数列值与
`raw_json` 规范化值不一致的行。缺列或漂移计数非零时停止部署并保留旧版本；
本次不加运行时 fallback，也不自动迁移数据。

本地门禁已接入 `option_positions_read`、option performance、持仓物化/报告和 close-advice 读取/生成边界。消费者名称与 payload 中的 `blocked_consumers` 使用同一稳定标识。普通候选扫描不依赖持仓质量，不受无关异常影响。producer 与 gate 使用本地 V1 状态文件。

生产只读 canary、定时器运行状态和 rollback 必须以当前部署证据验证，不能由本地测试替代。

## 持仓质量与成交收口契约

当前源码已修正以下行为；2026-09-13 的临时调查文件和逐笔生产修复计划不再作为当前状态或授权。目标环境质量及历史记录须重新采证、预览并回读。

| 责任 | 当前 owner 与约束 |
| --- | --- |
| 持仓输入 | `src/application/futu_portfolio_context.py::build_futu_position_snapshot` 规范化 short 的有符号数量/可卖量；方向冲突、越界、非法数值仍拒绝。 |
| 采集后时钟 | `src/application/quality/service.py` 在 OpenD 采集后取得 scope 时钟，再验证快照及刷新时效；真实 future/stale、错账户与不完整输入仍失败。 |
| 合法拆分与重复 | `src/application/quality/ledger_checks.py` 复用 `domain/domain/trade_execution.py` 的完整 execution 证明；物理账户、环境、namespace、经济内容、分配目标/索引与数量必须完整一致，void 后重算有效事件。 |
| 完成状态收口 | `src/application/trades/state_reconcile.py` 使用 coherent lifecycle anchors、有效终态及完整 allocation 证明；裸 deal ID、case status 或部分分配不能关闭 pending。 |

收口在现有锁内比较观察到的 key、bucket 与完整 payload；并发改变保留最新状态，其他 key 不丢失。先按 inbox 观察版本条件收口，再更新 source；中断后可重跑，已完成为无操作。有效 claim、身份隔离、冲突或证据读取失败保持原待办。

本地收口与外部采集各有异常边界；OpenD 失败不妨碍用已有完整持久证据收口。收口失败明确报告，不伪报 checkpoint/seal 失败；不得重放成交、重复记账、新增可发送 receipt 或唤醒 PM 刷新。跨 SQLite 与文件的完成性通过既有锁、原子写及重试恢复，不声称跨文件原子事务。

### 验证入口

- 输入与质量：`tests/test_position_snapshot_input.py`、`tests/test_futu_portfolio_context.py`、`tests/quality/test_opend_position_adapter.py`、`tests/quality/test_om_quality_service.py`、`tests/quality/test_om_quality_checks.py`。
- 拆分与收口：`tests/test_trade_execution_input.py`、`tests/test_trades_state_reconcile.py` 及成交 listener/Inbox 集成测试。
- 负例包括真实重复、混合身份、缺腿/重复目标、金额冲突、void、状态变化、并发与中断恢复；预览无写，重试无新增 ledger/receipt/PM 效果。

生产历史修复按当前完整证据逐项处理。缺证券条款、结算或归属证据时保留未知；不能按旧清单批量删除 pending、按日期强制平仓或将缺口隐藏为 trusted。
