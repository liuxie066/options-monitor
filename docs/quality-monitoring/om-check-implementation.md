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
| `OM-INT-003` | 同上 | service fixture 覆盖 reconciliation preview/window completeness | `option_position_report`、历史成交消费者 |
| `OM-LED-001` | `src/application/quality/ledger_checks.py::build_ledger_datasets` | `test_full_replay_mismatch_blocks_position_consumers` | `option_position_report`、`lifecycle`、`close_advice` |
| `OM-LED-002` | 同上 | `test_duplicate_broker_identity_with_economic_conflict_is_blocking` | 同上 |
| `OM-POS-001` | `src/application/quality/position_checks.py::build_position_dataset` | schema-valid service fixture；OpenD completeness 回归 | `option_position_report`、`lifecycle`、`close_advice` |
| `OM-POS-002` | 同上 | 持仓 code lineage 对比当前 snapshot 条款；换仓反例、lifecycle 优先级、Scheduled Tick account/market gate、transient、5 分钟 persistent 回归 | 同上 |
| `OM-LCY-001` | `src/application/quality/lifecycle_checks.py::build_lifecycle_datasets` | 周末/假日 deadline；11 条 stale 固定回归 | `lifecycle`、`close_advice` |
| `OM-LCY-002` | 同上 | external adjustment 与 legacy gap 分离回归 | 受影响的 `lifecycle`、`close_advice` |
| `OM-LCY-003` | 同上 | legacy history 独立 dataset 回归 | 受影响历史报告 |

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

## 持仓质量与成交收口修复设计

状态：待实现；Devflow simple。此节是本次唯一设计与范围记录。
授权依据：用户要求「用 devflow 的简单模式修复所有 bug，然后再处理线上数据」。
研发按 Brainstorm → Save Design → Improve Design → Impl → Review 执行；
提交、发布和服务升级保持独立授权边界。

### 目标与成功信号

修复本次证据揭示的持仓输入、快照时间、拆分成交质量误报和成交完成状态
未收口问题，验收已存在的身份隔离可见性修复，然后逐笔处理对应历史数据。
成功要求正常空头快照可校验、正常采集不会被判未来、完整合法拆分通过质量
检查，而异常输入、真实重复、经济冲突、缺失身份与未完成结算继续阻断。
已完成成交的 pending 必须在真实入站运行路径可靠收口且可重试，不能仅改报表计数。
生产完成以目标记录的证据、修复预览、持久结果及回读为准，本地测试不替代生产验收。

非目标：不下单、不发送通知、不凭到期日强制平仓、不按端口猜账户、不删除合法
拆分事件、不跳过质量门禁、不启用迁移 cutover、不新增 schema、服务、依赖或配置。
不修改无关工作区内容。运行期读写必须绑定主机、runtime root、账户、市场与时间。

### 已核对事实与剩余证据

2026-09-13 晚间只读证据：/tmp/om-quality-now.json、
/tmp/om-quality-conflicts.json、/tmp/om-intake-crosscheck.json；这些是调查输入，
不是持久业务权威，实施线上修复前重取当前事实。

- OpenD 空头 qty 与 can_sell_qty 均可为负；适配器只规范化 quantity，
  导致标准非负 sellable_quantity 校验失败。方向冲突与非法数值仍须拒绝。
- quality service 在采集前取 now，却将其用于采集后快照校验；
  标准域拒绝负 age，正常采集因而可被误报来自未来。
- lx 一笔平仓 3 张分配 1+2，sy 一笔 2 张分配 1+1，目标 lot 各不相同；
  两者都有 broker_deal_completion 且当前完成性 helper 接受。
  ledger_checks 只比较单行 fingerprint，未区分完整拆分与重复。
- 19 条 source pending 中，16 条关联 case 已 ledger_written；仅凭状态尚不能
  证明每条 source 的最终事件均有效，需要 canonical anchor、allocation 与未 void
  的终态事件证明。state_reconcile 已有 preview/apply，不能再建第二套修账器。
  listener 已有每分钟 lifecycle due 路径，但没有在该路径执行 source state 收口。
- 身份隔离可见性修复已在独立提交 3bc68101；纳入完整 diff 验收而不重复实现。

### 选定设计与失败语义

1. 在 build_futu_position_snapshot 共同适配边界，将合法空头带符号可平量
   规范化到标准绝对数量，同时保留 source_row。仅对已证明 short 的负数做
   方向转换，不能用 abs 掩盖 long 的负数、非数值、无穷或数量越界。
   所有调用者沿用标准输入校验，域模型不放宽。
2. 在 quality service 完成采集后使用注入时钟的新读数校验该 scope；
   数据集观察时间、刷新到期时间与本次校验保持一致。不得用快照自己的时间
   冒充可信时钟；真正未来、过期、不完整、缓存、错账户输入仍然失败。
3. 在现有 deal_identity owner 复用并必要时收紧完整拆分判定，供 ledger_checks
   使用。证明同一 scoped broker execution、合约/方向/价格/乘数一致、每个
   target lot 唯一、索引集合完整、各分配等于事件实际数量且总量等于券商成交量。
   缺证据、混合身份、部分拆分、重复 index/target、经济冲突仍报警；
   void 之后重算 active 事件。不能仅因 completed helper 返回 ID 就豁免真实冲突。
4. 在 state_reconcile 复用 canonical lifecycle coherent facts，补齐已完成
   anchor 到 source key 的关联。只接受同账户/物理身份、唯一且有效的终态证据，
   不用 case.status 或裸 deal ID 单独证明完成；冲突、缺失、读取失败保留 pending。
   在 listener 现有受锁保护的维护路径调用共同收口逻辑，preview 不写，apply
   只更新证据覆盖的 source entries；共享 inbox 同样只按准确身份及完成证据收口。
   跨 SQLite 与文件不假设原子事务：沿用现有锁、原子写及重试，对账中断后可重跑，
   不重放券商成交、不重复写 ledger、不触发 PM 刷新或通知；并发新记录不得丢失。

不选择下游过滤负数、放宽未来时间容差、按 deal ID 直接去重、按日期 expire、
批量删除 pending 或引入新恢复队列。这些会隐藏源错误或丢失真实待办。

### 三个行为增量及验证

1. 正常持仓证据可用：覆盖 short/long/zero、负数与越界、NaN/布尔值、
   延迟采集及真实 future/stale、四个账户市场 scope 隔离；运行
   tests/test_position_snapshot_input.py、tests/test_futu_portfolio_context.py、
   tests/quality/test_opend_position_adapter.py 与 quality service/checks。
2. 拆分成交守恒：覆盖 1+1、1+2、缺腿、重复索引/目标、元数据与实际数量不同、
   错账户/环境、价格冲突及 void；运行 deal identity、state reconciliation、
   trade intake 与 quality checks 的相关现有测试。合法在线样本作脱敏离线回归。
3. 完成状态与历史修复：覆盖 canonical adopted lifecycle、终态被 void、
   跨账户裸 ID 碰撞、部分分配、证据读取失败、dry-run 无写、并发写保留、
   中断重试与第二次无操作；通过真实 listener/CLI 集成验证，而非只测 helper。
   验收身份隔离持续可见且不能进入普通自动认领；运行对应已有提交测试。

最终运行相关完整测试集、ruff、项目 guardrails 和 git diff --check，随后对
全部相对 main 的改动执行 Devflow Review；不以测试成功推断已发布或已修复生产。

### 线上处理范围与验收

研发验证后，针对 liuxie-incus 的 /var/lib/options-monitor 重新只读采证，
生成逐项预览（标识、当前值、依据、预期动作、幂等键、回读与恢复方式）。
有证据的残留按既有受控入口处理；缺证据的项目继续查原始成交/结算，不制造事件。
范围包括 16 条 completed lifecycle 残留、两条腾讯各 100 股 @440 指派腿、
4 条身份隔离、四个过期 open lot（合计 5 张）、3 个旧 PDD lifecycle 案例、
sy 腾讯 400 股卖出匹配、lx NVDA 指派绑定、sy 两条腾讯指派股数不一致。
这些集合可能重叠，按稳定记录身份去重；495 已闭合与两笔合法拆分无需改账。
涉及具体不可逆记账修复时遵循既有预览确认契约；所需发布/升级另行取得明确授权。

剩余风险：历史证券条款与结算证据可能不可得，不能承诺强制清零待办；
线上质量仍受未修数据影响。完成报告必须分别列出已修代码、已处理数据、
证据不足的待决策项及阻塞原因，不能将它们隐藏成 trusted。

### 实施约束补充

- 状态应用在现有文件锁内比较同一 key 的 bucket 与完整 payload；观察值改变时
  跳过该条，保留最新状态，applied_count 只计实际写入。其他 key 也必须保留。
- 本地收口与 lifecycle due/外部采集有独立异常边界；即使本轮 OpenD 失败，
  仍可用既有持久证据收口。收口失败不能伪报 checkpoint/seal 失败。
- 先按 inbox 观察版本条件收口，再更新 source；中断后从未完成 source 重新发现，
  已收口 inbox 为幂等无操作。有效 claim、payload/result 变化、冲突或身份隔离
  仍拒绝普通自动收口。历史身份隔离需独立人工证据修复路径。
- 收口不得新增可发送 receipt 或唤醒 portfolio_refresh_intent；应保留既有交付
  历史与抑制语义，验收必须跑收口后的 receipt/PM 恢复路径证明没有新副作用。
- 拆分元数据与实际事件数量均为有限、非布尔、精确正整数；禁止 int 截断。
  质量分组使用已证明的物理账户、环境、namespace 与 execution 身份，canonical
  身份和 legacy alias 不重复计数；缺少可证明身份时保留异常，不合并猜测。
