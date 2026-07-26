# 投资系统运行与数据质量监控 — 正式设计

- **状态**：已确认；Phase 0–4 已本地实现，生产上线待 Phase 5
- **日期**：2026-07-26
- **适用系统**：options-monitor（OM）、portfolio-management（PM）、investment-quality（Quality Hub）
- **HTTP/API 契约**：[api-contract.md](api-contract.md)
- **状态 Payload Schema**：[quality_status.v1.schema.json](quality_status.v1.schema.json)
- **检查矩阵**：[check-matrix.md](check-matrix.md)
- **实施计划**：[implementation-plan.md](implementation-plan.md)

## 1. 决策摘要

采用“服务内质量生产者 + 独立只读质量中心”的联邦式架构：

```text
OM 业务能力
  -> OM Quality Producer
  -> investment.quality_status.v1 ─┐
                                   ├-> investment-quality Quality Hub
PM 业务能力                        │     -> incident / dependency / Feishu
  -> PM Quality Producer           │     -> unified read API / CLI
  -> investment.quality_status.v1 ─┘

liuxie-incus Host Watchdog
  -> service / timer / artifact freshness

investment-quality
  -> outbound dead-man heartbeat
  -> external missed-heartbeat alert
```

核心边界：

1. OM 和 PM 分别拥有本系统的业务事实、检查和门禁。
2. Quality Hub 只拉取标准质量状态，不连接 OpenD，不读取 OM/PM 业务数据库。
3. Quality Hub 不计算持仓、成本、价格、汇率、NAV 或期权生命周期。
4. 质量模块不自动修复业务数据、修改配置或重启服务。
5. 数据是否可信必须按服务、账户、市场、数据集和截止时间回答，不能退化为一个全局布尔值。

## 2. 目标与成功标准

### 2.1 目标

系统应能确定性回答：

- 哪个服务当前运行异常；
- 哪个账户的哪个数据集受影响；
- 数据截至什么时间可信；
- 判断所依赖的证据是什么；
- 哪些 NAV、报告或建议必须阻断；
- 异常何时开始、是否已知悉、何时恢复。

### 2.2 成功标准

1. PM API 进程存活但定时同步失败时，系统不得宣称 PM 整体健康。
2. PM OpenD 源数据正确但本地发生部分写入时，源状态与本地副本状态必须分开表达。
3. OM 出现交易消息积压、账本重放失败、持仓持续不一致或生命周期 stale 时，必须定位到受影响账户和数据集。
4. `untrusted` 与 `unavailable` 均不得被包装成“正常但数据较旧”。
5. 每个阻断结论都必须有证据 ID、原因码、时间和 `blocked_consumers`。
6. 恢复只能由重新验证通过产生；人工 acknowledge 不能修改质量结论。

## 3. 范围与非目标

### 3.1 V1 范围

OM：

- 服务、listener、timer、OpenD 可用性；
- 成交 inbox、checkpoint、pending/failed/unresolved；
- `trade_events -> projection -> position_lots` 全量重放；
- 本地期权持仓与 OpenD 当前持仓对账；
- 到期、行权、指派等生命周期证据与 stale 判定；
- 与 PM 正股刷新意图相关的运行证据。

PM：

- 服务、timer、OpenD 可用性；
- OpenD 正股/ETF 持仓数量与平均成本；
- 证券账户 `cash`；
- 基金账户 `fund_assets`，在 PM 中映射为汇总 MMF；
- PM 写入阶段、部分写入和写后对账；
- 价格、事实时间汇率、NAV 估值证据和 finality。

Quality Hub：

- 服务状态拉取；
- 统一契约校验；
- 跨服务依赖传播；
- incident 生命周期；
- 飞书告警、投递 outbox、acknowledge；
- 统一只读 API/CLI；
- external dead-man heartbeat。

### 3.2 非目标

V1 不做：

- PM 正股逐笔交易账本；
- 在 Quality Hub 中重新计算 PM 持仓、平均成本、NAV、价格或汇率；
- 在 Quality Hub 中重新计算 OM 期权生命周期或持仓；
- 用 OpenD 差异自动覆盖本地数据；
- 自动补写成交、生命周期或历史证据；
- 自动修改配置、重启服务或执行修复命令；
- 消息队列、Kafka、通用插件系统、Prometheus 平台或新前端；
- 将 Quality Hub 作为 PM/OM 业务 API 的代理。

未来需要逐笔成交、手续费、已实现盈亏或任意历史时点重建时，应单独设计 PM 正股交易账本，不得把该能力隐藏在质量监控中。

## 4. 权威边界

### 4.1 PM

OpenD 是 PM 当前正股持仓、平均成本、证券现金和基金账户 MMF 的唯一外部权威。

PM 本地/飞书数据是派生副本。PM 对账的目的不是验证 OpenD 是否正确，而是证明：

```text
OpenD 查询完整
  -> PM 字段转换正确
  -> PM 写入完整
  -> PM 写后读取与同一 OpenD 快照一致
```

PM 正股成交消息只用于触发 OpenD 全量刷新和监控刷新时效，不作为当前持仓的第二权威。

### 4.2 OM

OM 同时维护两类权威事实：

- OpenD 当前持仓：经纪商当前状态事实；
- OM ledger：成交接收、生命周期和历史审计事实。

OM 采用三段验证：

```text
交易消息完整性
  -> 全量事件重放是否等于本地投影
  -> 本地投影是否收敛到 OpenD 当前持仓
```

任何一段失败都不能通过直接覆盖 `position_lots` 消除。

### 4.3 Quality Hub

Quality Hub 仅拥有：

- 标准契约；
- 跨服务状态汇总；
- dependency propagation；
- incident、acknowledgement、notification outbox；
- 对外脱敏视图。

它不拥有任何金融业务事实。

## 5. 组件职责

### 5.1 OM Quality Producer

部署在 OM 仓库和运行环境内，调用已有 application facade：

- runtime status、healthcheck、scheduler status；
- trade intake state reconciliation；
- ledger projection verification；
- option position read/projection；
- OpenD 只读持仓 adapter；
- lifecycle evidence evaluator。

不得：

- 绕过 `src/application/ledger/api.py` 直接使用 ledger internals；
- 在 Quality 模块中复制 `domain/domain/ledger/projection.py` 的计算；
- 让 Hub、浏览器或外部服务读取 OM SQLite。

### 5.2 PM Quality Producer

部署在 PM 仓库和运行环境内，调用 PM service/application facade：

- Futu/OpenD snapshot provider；
- holdings/cash/MMF repository read；
- sync receipt、write stage 和 post-write reconciliation；
- pricing/FX evidence；
- NAV finality、accuracy 和 provenance。

PM producer 必须保存同一同步运行的：

- `sync_run_id`；
- `source_snapshot_id`；
- OpenD 账户、环境、市场、源币种指纹；
- positions、securities_cash、fund_mmf 分阶段结果；
- 写后读取证据。

### 5.3 investment-quality Quality Hub

独立控制面，建议技术栈：

- Python 3.12；
- FastAPI；
- Pydantic；
- SQLite；
- httpx；
- systemd service + timer；
- CLI：`iq status`、`iq incidents`、`iq check`。

Hub 每 5 分钟拉取 OM/PM，验证 Schema，更新 incident 和告警状态。V1 不建设前端；未来投资控制台只读调用 Hub API。

### 5.4 Host Watchdog

每 5 分钟从进程外检查：

- systemd service/timer；
- 最近任务 exit status；
- OM/PM/Hub 状态文件更新时间；
- Hub SQLite/outbox 基本可用性。

服务自身无法证明“自己已经完全停止”，因此 Host Watchdog 与服务内 health 不可合并。

### 5.5 External dead-man monitor

Hub 每 5 分钟发送无业务数据的出站 heartbeat。连续 15 分钟未收到 heartbeat 时，外部服务发送 `[基础设施失联]` 告警。

适配器同时支持 provider 生成的 secret ping URL，以及显式要求 Bearer
鉴权的 HTTPS endpoint；后者才配置独立 bearer token。请求正文只包含静态
`service=investment-quality` 和 `status=alive`，不接收账户、持仓、金额或
质量明细。endpoint 本身也按 secret 管理，不进入状态、日志或告警。具体
provider 在生产集成前选择，不阻碍核心开发。

## 6. 服务内部分层

OM 和 PM producer 使用相同结构，但不共享重量级运行时实现：

```text
Collectors
  -> Evidence
  -> Checks
  -> Policy
  -> Status Publisher
  -> Local Gate / API / CLI
```

### 6.1 Collectors

只负责 I/O：

- OpenD；
- 本地 repository；
- state/audit artifact；
- systemd；
- timer/calendar/config。

Collector 不做可信度汇总。

### 6.2 Evidence

将 OpenD DataFrame、本地行和任务回执转换为不可变、可哈希证据：

- scope；
- observed time；
- source snapshot ID；
- watermark；
- completeness；
- normalized payload hash；
- opaque evidence reference。

缺失与零值必须区分，尤其是 `fund_assets=0` 与 `fund_assets` 缺失。

### 6.3 Checks

检查是只读纯函数：

```text
Check(EvidenceBundle) -> CheckResult
```

每个检查只回答一个问题，不发通知、不写 incident、不执行修复。

### 6.4 Policy

Policy 将检查结果确定性汇总为运行或数据状态。业务阈值由所属服务维护，Hub 不复制 OM/PM 交易日历、生命周期或同步规则。

每份状态必须记录 `policy_version` 和阈值摘要。

### 6.5 Status Publisher

服务侧写入：

```text
quality/latest.json
quality/history-YYYY-MM.jsonl
quality/evidence/<evidence_id>.json
```

`latest.json` 原子替换；history/evidence 只追加。

### 6.6 Local Gate

关键业务入口使用同一套检查器读取新鲜证据，不只依赖可能过期的 `latest.json`：

- PM NAV 正式发布；
- PM 正式报告发布；
- OM 持仓型报告；
- OM 生命周期/平仓建议；
- 其他明确声明依赖的消费者。

数据采集、同步、只读对账和修复流程永不被质量门禁阻断。

## 7. 状态与严重度

### 7.1 检查状态

```text
pass / warn / fail / unknown
```

### 7.2 运行状态

```text
healthy / degraded / unhealthy / unknown
```

### 7.3 数据状态

```text
trusted / partial / untrusted / unavailable
```

定义：

- `trusted`：所有必要证据完整且检查通过；
- `partial`：非核心证据异常，只允许明确列出的有限用途；
- `untrusted`：已有证据证明不一致、损坏或部分写入；
- `unavailable`：缺少必要证据，无法判断。

正式消费者对 `untrusted` 和 `unavailable` 都 fail closed。

### 7.4 严重度

只使用三档：

- `info`：恢复或已经自行收敛的短暂差异；
- `warning`：运行降级或非核心证据异常，不阻断；
- `blocking`：必要证据不可用或已证明数据不可信，执行相应门禁。

影响范围由 `scope` 和 `blocked_consumers` 表达，不增加平行的 P0/P1/critical 等级。

## 8. PM 数据模型

### 8.1 持仓数量与平均成本

- 账户、环境、市场、代码、证券类型构成持仓身份；
- 数量标准化后必须精确一致；
- OpenD 平均成本是唯一权威；
- PM 不重新计算平均成本；
- 平均成本使用 Decimal 按 PM 存储精度规范化后比较；
- 成本不可信只阻断成本/盈亏消费者；
- 数量仍可信时，不阻断只依赖数量和市场价格的 NAV。

### 8.2 证券现金和基金账户 MMF

OpenD API 在同一 `accinfo` 记录中返回两个业务域：

```text
accinfo.cash        -> pm.securities_cash -> CNY-CASH
accinfo.fund_assets -> pm.fund_mmf        -> CNY-MMF
```

规则：

- 两项独立同步、独立写后对账、独立定级；
- `fund_assets=0` 是可信零值，字段缺失是 `unavailable`；
- `available_funds`、`withdraw_cash`、`power` 不得替代 `cash`；
- V1 使用 `fund_assets` 汇总值，不追踪 MMF 内部基金份额；
- `pm.cash_like_assets` 只有两项都可信时才为 `trusted`。

### 8.3 CNH/CNY

- OpenD 请求使用 `CNH`；
- PM 将人民币金额归一为 CNY 业务资产；
- 证据必须保留 `source_currency=CNH`；
- OpenD 返回币种与配置不一致时 fail closed；
- USD/HKD 必须使用独立现金资产和事实时间汇率，不得混入 `CNY-CASH`。

### 8.4 账户映射

`lx`、`sy` 必须显式配置唯一 OpenD `acc_id`：

- ID 存在且唯一；
- 账户之间不得重复；
- `trd_env=REAL`；
- 市场和币种与配置一致；
- 公共状态只输出掩码或指纹。

当前 `lx` 缺少显式 `acc_id` 是上线前必须处理的生产配置前置项。

### 8.5 写后对账

PM 同步：

1. OpenD 快照不完整时不写入；
2. 明确写失败或可能部分写入时立即 `untrusted`；
3. 写成功后立即重读 PM；
4. 首次不一致为 `transient_divergence`；
5. 只重试读取，不重复写入；
6. 30 秒后仍不一致升级为 `persistent_divergence/blocking`。

positions、securities_cash、fund_mmf 必须保留同一 `sync_run_id` 和 `source_snapshot_id`，但分别记录写入及可信状态。

## 9. OM 双路验证

### 9.1 消息路径

```text
OpenD Deal Push / history backfill
  -> inbox
  -> normalized trade event
  -> trade_events
  -> deterministic projection
  -> position_lots
```

### 9.2 快照路径

```text
OpenD position_list_query(refresh_cache=True)
  -> normalized broker position snapshot
```

### 9.3 交叉对账

```text
inbox completeness
  -> full replay == materialized position_lots
  -> materialized position_lots == OpenD current positions
```

诊断：

- inbox pending/failed：消息接收或入账问题；
- full replay 与 materialized projection 不同：投影代码或本地物化状态问题；
- replay 与本地一致但与 OpenD 不同：漏消息、外部调整或未识别生命周期事实；
- 三者一致：相关持仓可标记 `trusted`。

### 9.4 收敛宽限

盘中：

- 首次差异：`transient_divergence`；
- 第 1 分钟复查；
- 第 5 分钟复查；
- 5 分钟内恢复：记录 info，不告警、不阻断；
- 持续 5 分钟或连续三次不一致：`persistent_divergence/blocking`。

日终：

- inbox 已清空且 OpenD 完整刷新后仍不一致，直接阻断。

### 9.5 生命周期期限

- 普通开仓/平仓：5 分钟消息收敛期限；
- 到期、行权、指派：等待下一个对应市场交易日第一次成功 OpenD 深度对账，再增加 2 小时宽限；
- 宽限内：`lifecycle_pending`；
- 超期：`lifecycle_stale`；
- 公司行动、转仓、人工调整：`external_adjustment_pending_review`；
- 上线前历史缺口：`legacy_evidence_gap`，与实时故障分开统计。

此前出现的 11 条 stale 生命周期状态必须成为固定回归场景。

## 10. 数据依赖和门禁

### 10.1 PM

```text
holdings_quantity ─┐
securities_cash ───┤
fund_mmf ──────────┼-> nav -> 正式日报/业绩报告
prices ────────────┤
fx ────────────────┘

cost_basis -> 成本/盈亏类报告
```

- 正式 NAV 的所有必要数据必须为 `trusted`；
- 价格证据包含来源、时间、新鲜度和 fallback；
- 汇率必须是事实时间证据，不得使用当前汇率补历史证据；
- NAV 继续由 PM 计算，Quality 只验证 valuation evidence 和 finality。

### 10.2 OM

```text
trade_intake
  -> ledger_projection
  -> option_positions <-> OpenD option positions
  -> lifecycle / close advice / 持仓报告
```

普通候选扫描如果不依赖现有持仓可以继续运行；账户持仓型建议必须被门禁。

### 10.3 跨服务

PM 异常不得污染无关 OM 状态。只有显式声明依赖 PM 持仓或 NAV 的组合级报告传播阻断，并输出 `blocked_by`。

## 11. 调度与新鲜度

正常频率：

- OM/PM 常规质量扫描：每 15 分钟；
- Quality Hub 拉取：每 5 分钟；
- Host Watchdog：每 5 分钟；
- 关键任务结束：立即刷新服务本地状态；
- PM OpenD 权威查询：跟随现有早晚同步；
- OM OpenD 深度对账：市场日终一次；
- 不执行固定分钟级 OpenD 轮询。

运行阈值：

- listener 心跳不超过配置间隔 2 倍：healthy；
- 2–5 倍：degraded；
- 超过 5 倍：unhealthy；
- timer 到期后允许 15 分钟执行宽限；
- 任务明确失败：立即 unhealthy；
- 交易休市按所属服务交易日历处理。

OpenD 查询：

- 权威对账使用 `refresh_cache=True` 并记录刷新证据；
- 按账户串行、有限超时并遵守限频；
- 强制刷新失败时为 `unavailable`；
- 不允许退回旧缓存冒充最新数据。

## 12. Incident 与告警

### 12.1 生命周期

```text
new -> persistent -> acknowledged -> recovered
```

`acknowledged` 只暂停重复提醒，状态、门禁和阻断消费者不变。恢复必须由重新验证产生。

### 12.2 频率

- transient：不发送；
- warning/partial：每日摘要；
- blocking/unhealthy：首次立即发送；
- 同一指纹不重复发送；
- 阻断持续 2 小时提醒一次，每日最多 3 次；
- 原因、范围或严重度变化时重新发送；
- 恢复立即通知。

### 12.3 飞书

使用现有飞书机器人和通知链路，统一前缀：

```text
[质量告警][PM][sy][fund_mmf][阻断]
[质量恢复][OM][lx][option_positions]
[基础设施失联][investment-quality]
```

只有 Hub dispatcher 发送跨系统质量通知，避免 OM、PM 重复发送。

### 12.4 Outbox

- 状态变化先写 Hub SQLite；
- 稳定 `notification_id`；
- 失败后 1、5、15 分钟重试；
- 三次失败后 `alert_delivery=unhealthy`，通知保留；
- 恢复后只发送仍有效的最新通知，避免刷屏。

仅使用一个飞书机器人意味着机器人自身失效时无法通过同一机器人通知，这是明确接受的残余风险。

### 12.5 维护窗口

维护窗口必须记录范围、开始/结束、原因和操作人。它只抑制范围内重复通知，不改变真实状态、incident 或门禁；不得无限期静默。

## 13. 存储、保留与安全

服务侧：

```text
/var/lib/options-monitor/quality/
/var/lib/portfolio-management/quality/
```

Hub：

```text
/var/lib/investment-quality/investment-quality.sqlite
```

保留：

- latest：持续；
- 状态、检查、incident：400 天；
- 阻断异常完整差异：400 天；
- 正常详细标准化快照：30 天；
- 按月轮转。

安全：

- 完整金额和持仓证据仅在受限服务目录；
- 飞书和普通 API 默认脱敏；
- 不保存 OpenD 原始响应；
- OM/PM `/quality/status` 仅监听 loopback/受控内网；
- Hub 使用 OM/PM 各自独立只读 token；
- 浏览器不得直接访问 OM/PM；
- 未知 Schema 版本 fail closed。

## 14. 契约与版本

规范名：

```text
investment.quality_status.v1
```

规则：

- `investment-quality` 仓库最终拥有 canonical JSON Schema；
- OM/PM 使用本地 DTO，不依赖共享 Python 业务包；
- CI 通过同一 Schema 做契约测试；
- V1 只允许新增可选字段；
- 删除字段、修改枚举或语义发布 V2；
- Hub 兼容当前和前一个版本；
- 每份状态包含 `schema_version`、`producer_version`、`policy_version`。

本仓库中的 Schema 是已确认设计基线；建立独立仓库时迁移为 canonical authority。

## 15. 首次上线和分批部署

不设置临时 `observe_mode`。生产上线后：

- 真实状态和真实告警立即启用；
- 确定性 blocking 检查立即执行门禁；
- transient/warning 不阻断。

首次上线执行只读基线：

- OM 全量重放、投影验证、OpenD 期权持仓、pending/stale；
- PM OpenD positions/cash/fund_assets 与本地副本对比；
- service/listener/timer/任务回执检查。

已有异常标记 `preexisting_incident`，但不豁免门禁；基线不写业务数据。

部署顺序：

1. Schema 与 Hub scaffold；
2. PM producer；
3. OM producer；
4. Hub 分别 onboard PM/OM 并开启真实告警；
5. 跨服务 dependency propagation。

任一组件可以独立升级。Hub 不可用不得中断数据采集；本地门禁继续有效。

## 16. 自动化权限

允许：

- 只读 OpenD 重试；
- 重新读取本地数据；
- 纯对账和投影验证；
- 质量状态、incident、告警更新；
- 对依赖消费者执行门禁。

禁止：

- 修改业务数据；
- 补写事件/生命周期；
- 自动重新同步并写 PM；
- 修改配置；
- 重启服务；
- 人工关闭未恢复 incident。

修复建议可以包含 dry-run 命令，但任何业务写入、配置或服务操作仍需单独批准。

## 17. 已知实施前置项

以下不是待确认策略，但必须在对应阶段处理：

1. 为 PM `lx` 确认并显式配置 OpenD `acc_id`。
2. 移除 PM 用 `available_funds/withdraw_cash/power` 替代 `cash` 的语义回退。
3. 修复或隔离 PM positions 与 cash/MMF 跨阶段部分写入风险。
4. 为 PM 保存可持续查询的同步 receipt、snapshot ID 和写后证据。
5. 为 OM 增加 application facade 之上的本地只读 HTTP quality adapter。
6. 选择 external dead-man provider。
7. 生成服务间只读凭证并建立证据目录权限。

这些生产配置、服务安装和真实通知变更均需在实施阶段按各仓库安全边界单独批准。
