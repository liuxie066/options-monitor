# 运行与数据质量监控 — 分阶段实施计划

- **状态**：实施中；Phase 0–4 代码与本地验证完成，Phase 5 生产上线待授权
- **日期**：2026-07-26
- **涉及仓库**：options-monitor、portfolio-management、investment-quality
- **总设计**：[architecture.md](architecture.md)
- **检查矩阵**：[check-matrix.md](check-matrix.md)
- **HTTP/API 契约**：[api-contract.md](api-contract.md)
- **状态 Payload Schema**：[quality_status.v1.schema.json](quality_status.v1.schema.json)
- **实施状态与证据**：[implementation-status.md](implementation-status.md)

## 1. 交付目标

按可独立测试、发布和回滚的 work unit 建立：

1. 一个稳定的 `investment.quality_status.v1` 契约；
2. PM 服务内质量 producer 和确定性本地门禁；
3. OM 服务内质量 producer 和确定性本地门禁；
4. 独立 investment-quality Hub；
5. Host Watchdog、飞书 incident/outbox 和 external dead-man heartbeat；
6. 生产只读基线、真实告警和确定性门禁。

本计划不授权创建新仓库、修改生产配置、安装服务、发送真实测试通知或升级生产。进入相应阶段时仍需按仓库和生产安全边界单独执行。

## 2. 全局实施原则

### 2.1 每个 work unit 的完成条件

每个 work unit 必须：

- 先确认当前 source/config/runtime authority；
- 只修改所属仓库；
- 保持现有业务 facade；
- 先 focused tests，再运行风险相称的 broader suite；
- 提供机器可读契约测试；
- 记录 production read-only canary；
- 不把 release、production upgrade 与开发提交隐式合并。

### 2.2 禁止跨仓库捷径

- Hub 不直接读 OM SQLite、PM 飞书表或本地业务缓存；
- OM 不导入 PM 代码；
- PM 不导入 OM ledger；
- OM/PM 不在运行时依赖 Hub 的 Python 业务实现；
- PM/OM producer 只通过各自公开 application/service facade 读取事实；
- 跨服务只使用 HTTP `GET /quality/status` 和版本化 JSON。

### 2.3 无观察模式

不实现临时 `observe_mode`。producer 被 Hub 标记 onboarded 后：

- 状态和真实告警立即生效；
- 确定性 blocking 检查立即执行本地门禁；
- transient/warning 按既定规则不阻断。

## 3. Phase 0 — 契约与测试工具包

### 3.1 目标

冻结 V1 公共语义，使三个仓库可以独立开发和分批升级。

### 3.2 交付物

在新 `investment-quality` 仓库建立：

```text
contracts/
  quality_status.v1.schema.json
  examples/
    om.status.v1.json
    pm.status.v1.json
    hub.status.v1.json
src/investment_quality/contracts/
tests/contracts/
```

契约要求：

- Draft 2020-12 Schema；
- 当前仓库设计基线迁移为 canonical schema；
- OM/PM vendor 一份 pinned test copy，记录上游 release/commit 和 SHA-256；
- runtime DTO 仍由各仓库本地维护；
- CI 对 example、producer fixture 和 Hub parser 执行同一 Schema；
- Hub 支持 V1 和未来前一个兼容版本，不猜测未知版本。

### 3.3 测试

- Schema self-validation；
- 最小合法 payload；
- 每个 enum 的合法/非法边界；
- 未知顶层字段拒绝；
- `extensions` 允许 additive producer metadata；
- `unavailable`、`untrusted`、acknowledged、recovered 示例；
- 账户 scope 和 reason code 格式；
- 不允许完整 broker account ID 出现在公共 example。

### 3.4 Exit gate

- 三种 producer example 全部通过 Schema；
- 不存在无证据的 `trusted` 示例；
- 契约字段与架构、检查矩阵一致；
- contract version 和 compatibility policy 写入 README。

## 4. Phase 1 — investment-quality Hub 基础

### 4.1 目标

先部署能容忍 producer 尚未接入的 Hub 骨架，服务显示 `not_onboarded`，不制造故障告警。

### 4.2 建议结构

```text
src/investment_quality/
  application/
    aggregate_service.py
    dependency_service.py
    incident_service.py
    maintenance_service.py
    notification_service.py
  contracts/
  infrastructure/
    http_clients.py
    sqlite_repository.py
    feishu_dispatcher.py
    deadman_client.py
  interfaces/
    api.py
    cli.py
    scheduler.py
  config.py
scripts/
  install_linux.py
```

保持显式依赖，不建设动态 plugin framework。

### 4.3 SQLite 最小表

```text
service_snapshots
incidents
incident_events
notification_outbox
maintenance_windows
deadman_state
schema_migrations
```

数据库只保存质量控制面状态，不保存完整业务持仓或 OpenD 原始响应。

### 4.4 API/CLI

只读：

```text
GET /health
GET /quality/status
GET /quality/incidents
iq status
iq incidents
iq check
```

受控 Hub 状态写入：

```text
POST /quality/incidents/{incident_id}/acknowledge
POST /quality/maintenance-windows
DELETE /quality/maintenance-windows/{window_id}
```

这些操作只修改 Hub incident/notification 状态，必须鉴权并写 audit；不能修改 OM/PM。

### 4.5 行为

- 每 5 分钟 poll scheduler；
- producer 未 onboarded 时仅显示组件状态；
- schema incompatible、auth failure、timeout、stale status 分别使用稳定 reason code；
- dependency engine 只使用显式声明；
- outbox 具备稳定 notification ID 和 1/5/15 分钟重试；
- acknowledge 不改变 quality/gate；
- maintenance window 只抑制范围内提醒；
- dead-man client 是窄 adapter，provider 可替换。

### 4.6 测试

- SQLite migration/idempotency；
- incident state machine；
- fingerprint/dedup；
- notification supersession；
- acknowledge/maintenance audit；
- component not_onboarded；
- V1/unknown version；
- Hub restart 后 outbox 和 incident 恢复；
- API auth 与脱敏；
- dead-man timeout/failure 不泄露业务数据。

### 4.7 Exit gate

- Hub 可在无 producer 情况下稳定运行；
- 不发送 not_onboarded 故障告警；
- service/DB/scheduler/outbox health 可查询；
- restart 不丢 incident/outbox；
- 所有 API 默认 loopback。

## 5. Phase 2 — PM 正确性前置项与 Quality Producer

### 5.1 Work unit 2A：账户与 OpenD source contract

目标：

- `lx`、`sy` 显式唯一 `acc_id`；
- `REAL`、market、source currency fail closed；
- 公共输出只包含指纹。

代码工作：

- config doctor 增加 account mapping validation；
- OpenD provider 返回 source metadata/completeness；
- 账户列表只读检查；
- 多真实账户且未指定 acc_id 时拒绝同步。

生产前置：

- 只读确认 `lx` 目标 acc_id；
- 生产配置修改单独批准；
- 修改后先 config doctor 和 dry-run，再允许正式同步。

测试：

- acc_id missing/duplicate/not-found；
- sy/lx 冲突；
- REAL/SIMULATE；
- 新开/注销账户不因 acc_index 顺序改变而串账户。

### 5.2 Work unit 2B：cash/MMF 语义和同步 receipt

目标：

- `accinfo.cash` 是 securities_cash 唯一来源；
- `fund_assets` 是 fund_mmf 唯一汇总来源；
- 不允许 `available_funds/withdraw_cash/power` 替代 cash；
- 0 与 missing 分离。

代码工作：

- source adapter 返回字段级 presence/source field；
- snapshot 保留 `source_currency=CNH`；
- `sync_run_id`、`source_snapshot_id` 贯穿 positions/cash/MMF；
- 保存 durable latest/history receipt；
- 每阶段记录 started/succeeded/failed；
- `partial_write_possible` 精确到数据集。

测试：

- cash missing、fallback fields present；
- fund_assets 0/missing/invalid；
- CNH/CNY metadata；
- positions success + cash/MMF failure；
- cash success + MMF failure；
- receipt 序列化和 restart read。

### 5.3 Work unit 2C：写后对账

目标：

- write success 不再等于 replica trusted；
- 同一 OpenD snapshot 与 PM repository read 做确定性比较。

实现：

```text
OpenD snapshot complete
  -> validate complete diff
  -> write
  -> immediate repository read
  -> compare
  -> if mismatch, read-only retry after 30 seconds
  -> persistent verdict
```

约束：

- 重试只读，不重复写；
- quantity 精确；
- average cost Decimal 按存储精度；
- positions/cash/MMF 分别输出；
- 跨表无法原子时，通过 generation/receipt 证明或暴露部分状态，不能伪装原子事务。

测试：

- immediate match；
- first mismatch then recover；
- persistent mismatch；
- stale repository cache；
- partial write；
- duplicate symbol/unknown side/empty snapshot protections保持。

### 5.4 Work unit 2D：价格、FX、NAV 与本地门禁

目标：

- producer 输出 prices、fx、nav dataset status；
- 正式 NAV 使用同步新鲜质量检查；
- 不重算估值。

实现：

- 复用现有 valuation evidence/finality；
- price status 包含 source、quote time、fallback、missing；
- FX status 包含 source、fact time、evidence；
- 当前 FX 不得补历史 evidence；
- NAV 输出 `blocked_by`；
- cost_basis 与 holdings_quantity 分离。

测试：

- price missing/stale fallback；
- FX missing/fact-time mismatch；
- finality missing；
- cost mismatch 不单独阻断 NAV；
- cash/MMF 任一异常阻断正式 NAV；
- 内部展示保留 as_of 和原因。

### 5.5 Work unit 2E：PM Quality API/Artifact

建议结构：

```text
src/app/quality/
  evidence.py
  checks/
  policy.py
  service.py
src/feishu/repositories/ 或现有 storage facade
src/service/http.py
```

接口：

```text
GET /quality/status
pm quality status --json
```

安全：

- loopback/受控内网；
- 独立只读 token；
- artifact 原子写；
- evidence path 不进入公共 payload。

### 5.6 PM Exit gate

- PM 检查矩阵全部有实现/测试映射；
- 当前 partial-write 场景能得到 dataset-scoped untrusted；
- `/health` 与 `/quality/status` 语义分离；
- 生产只读 canary 不写 Feishu；
- producer payload 通过 canonical Schema；
- PM release/upgrade另行批准；
- Hub onboard PM 后真实告警和本地门禁生效。

## 6. Phase 3 — OM Quality Producer

### 6.1 Work unit 3A：统一 evidence/check facade

建议结构：

```text
src/application/quality/
  evidence.py
  checks/
  policy.py
  service.py
src/infrastructure/quality/
  artifact_repository.py
  opend_position_adapter.py
src/interfaces/quality/
  http.py
  cli.py
```

必须复用：

- `src/application/agent_tools/runtime_status_impl.py`；
- `src/application/trades/state_reconcile.py`；
- `src/application/ledger/api.py`；
- `domain/domain/ledger/projection.py` 的公开应用边界；
- 现有 lifecycle facts。

不得在 quality checks 中复制 ledger projection。

### 6.2 Work unit 3B：trade intake 与 full replay

实现：

- inbox/checkpoint/failed/unresolved evidence；
- broker deal terminal evidence；
- full replay vs materialized projection；
- duplicate/economic conflict；
- reason code、scope、evidence refs；
- 11 stale cases regression fixture。

测试：

- pending within grace；
- pending >5 minutes；
- failed/unresolved；
- broker has/local missing；
- replay mismatch；
- duplicate ID/economic mismatch；
- repair 后 recovered。

### 6.3 Work unit 3C：OpenD option position convergence

实现：

- 独立 `refresh_cache=True` snapshot；
- account/market/environment metadata；
- option identity normalization；
- local net open projection compare；
- transient state persisted across checks；
- 1 分钟/5 分钟只读复查；
- 日终严格模式。

测试：

- source incomplete；
- OpenD first/local later；
- local first/OpenD later；
- 5 分钟内 convergence；
- persistent mismatch；
- account/market mapping；
- contract multiplier/side/expiry/strike mismatch。

### 6.4 Work unit 3D：lifecycle policy

实现：

- `lifecycle_pending`；
- `lifecycle_stale`；
- `external_adjustment_pending_review`；
- `legacy_evidence_gap`；
- market calendar + first successful deep reconciliation +2 hours；
- status recovery evidence。

测试覆盖 Friday expiry/weekend、market holiday、assignment/exercise、历史 migration gap。

### 6.5 Work unit 3E：OM API、artifact 和本地门禁

接口：

```text
GET /quality/status
./om quality status --json
./om-agent run --tool quality_status --input-json ...
```

关键边界：

- HTTP adapter 只调用 application use case；
- 不按请求 spawn `./om`；
- 不暴露 OM SQLite 或 output 目录；
- local gate 只阻断依赖持仓/生命周期的消费者；
- ordinary candidate scan 不被无关 PM/持仓异常污染。

### 6.6 OM Exit gate

- 检查矩阵 OM 项全部映射；
- 11 stale、replay mismatch、persistent divergence 通过；
- existing runtime_status contract 不被破坏；
- focused tests + agent contract + tick regression 通过；
- producer payload 通过 canonical Schema；
- 生产只读 canary；
- OM release/upgrade另行批准；
- Hub onboard OM 后真实告警和本地门禁生效。

## 7. Phase 4 — Hub 集成、依赖和告警

### 7.1 Producer clients

- PM/OM 独立 base URL/token；
- 每 5 分钟拉取；
- timeout、auth、schema、freshness 分开 reason code；
- 保留最近 valid snapshot 供展示，但 producer unavailable 时正式消费者 fail closed；
- 不用旧 snapshot 冒充当前。

### 7.2 Dependency engine

固定依赖：

```text
PM nav <- holdings_quantity + securities_cash + fund_mmf + prices + fx
PM cost reports <- holdings_quantity + cost_basis + prices + fx
OM option_positions <- trade_intake + ledger_projection + OpenD convergence
OM close advice <- option_positions + lifecycle + required market evidence
```

跨服务依赖必须显式注册。输出：

- `usable_for`；
- `blocked_consumers`；
- `blocked_by`。

### 7.3 Incident/notification

- fingerprint 包含 producer/check/scope/reason；
- new/persistent/acknowledged/recovered；
- blocking 首次立即发送；
- 2 小时提醒，每日最多 3 次；
- warning 每日摘要；
- recovery 立即；
- 飞书使用同一机器人和统一前缀；
- outbox supersession 防止恢复后补发旧故障。

### 7.4 Host Watchdog 与 dead-man

- 5 分钟检查 systemd/timer/artifact；
- Hub 每 5 分钟 outbound heartbeat；
- 外部 15 分钟 missed-heartbeat；
- heartbeat payload 不含业务数据；
- dead-man provider 通过窄 adapter 和 secret env 配置。

### 7.5 Exit gate

- 两 producer 分别 unavailable 时不互相污染；
- dependency propagation 与矩阵一致；
- notification dedup/retry/recovery 通过；
- Hub restart 恢复 incident/outbox；
- maintenance window/ack 审计通过；
- external heartbeat canary 不泄露数据。

## 8. Phase 5 — 生产上线与基线

### 8.1 部署顺序

1. 发布并部署 Hub scaffold；
2. 部署 PM producer，先保持 Hub component not_onboarded；
3. 生产只读 PM baseline；
4. 处理 `lx` acc_id 和已知 PM correctness prerequisites；
5. Hub onboard PM，启用真实告警和本地确定性门禁；
6. 部署 OM producer；
7. 生产只读 OM baseline；
8. Hub onboard OM；
9. 启用跨服务依赖；
10. 启用 dead-man provider。

每一步的 release 和 production upgrade 单独验证。

### 8.2 只读基线

PM：

- acc_id/env/market/currency；
- OpenD positions/cash/fund_assets；
- PM replica read；
- prices/FX/NAV evidence；
- timer/last sync；
- 只生成差异，不同步写入。

OM：

- intake/checkpoint；
- full replay；
- materialized positions；
- OpenD option positions；
- lifecycle pending/stale/legacy gaps；
- listener/timer。

已有异常：

- `preexisting=true`；
- 保留 first-seen=baseline；
- 确定性 blocking 仍执行门禁；
- 不批量自动修复。

### 8.3 生产验证

- `/quality/status` producer + Hub；
- local CLI 与 API 同一结论；
- Hub poll watermark；
- 飞书使用受控的真实 incident transition 验证，不发送任意测试噪音；
- Host Watchdog；
- dead-man heartbeat；
- evidence 权限和 retention；
- 现有 tick、PM sync、NAV 性能无回退。

## 9. 回滚与故障隔离

### 9.1 Hub

Hub 回滚/停止：

- 不影响 OM/PM 数据采集；
- 不影响服务本地质量检查和门禁；
- 暂停统一视图和飞书 incident；
- external dead-man 应发现 Hub 失联。

### 9.2 Producer

producer release 回滚：

- Hub 将对应服务标记 incompatible/unavailable；
- 其他服务不受污染；
- 业务修复/同步入口仍可运行；
- 正式依赖消费者 fail closed。

### 9.3 门禁

不提供临时 observe toggle 或绕过按钮。若新门禁实现存在 bug：

- 回滚对应 producer release；
- 不通过 Hub 把状态强制改为 trusted；
- 保留 incident 和回滚证据。

## 10. 生产配置与权限清单

需要单独批准的变更：

- `lx` OpenD acc_id；
- PM/OM quality API token env；
- Hub service token env；
- 飞书 webhook/机器人配置；
- dead-man secret；
- systemd service/timer 安装或修改；
- `/var/lib/*/quality` 目录和权限；
- Hub SQLite backup/retention job；
- producer/Hub production release and upgrade。

不得在文档、Git、状态 JSON、日志或告警中保存 secret。

## 11. 最终完成定义

整个 V1 完成必须同时满足：

1. 四份设计产物保持一致；
2. 三仓库 contract tests 通过；
3. PM/OM 检查矩阵逐项有 owner、测试和 evidence；
4. Hub incident/outbox/dependency tests 通过；
5. 生产只读 baseline 完成；
6. `lx/sy` account mapping 明确；
7. 当前 PM 部分写入问题不再被 `/health=ok` 掩盖；
8. OM 11 stale 场景成为回归；
9. 真实状态、门禁、告警、恢复和 dead-man 均有生产证据；
10. 系统能够按账户/数据集/as_of/证据回答“现在是否健康、数据是否可信”。
