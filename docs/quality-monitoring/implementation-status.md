# 运行与数据质量监控 — 实施状态与完成证据

- **目标**：严格实施 [implementation-plan.md](implementation-plan.md) 的 Phase 0–5
- **状态**：实施中
- **开始日期**：2026-07-26
- **完成标准**：所有 phase exit gate 有当前代码、测试和运行证据；生产阶段完成真实只读基线与回滚验证

本文只记录已由当前证据证明的状态。未验证、仅设计完成或尚未上线的项目一律保持未完成。

## 仓库

| 仓库 | 分支 | 当前用途 |
|---|---|---|
| `options-monitor` | `feat/quality-monitoring` | OM producer、本地门禁、设计与完成证据 |
| `portfolio-management` | `feat/quality-monitoring` | PM 前置修复、producer、本地门禁 |
| `investment-quality` | `main` | canonical contract、Hub、incident/outbox/watchdog |

## Phase 0 — 契约与测试工具包

状态：**完成**

完成证据：

- canonical Schema：`investment-quality/contracts/quality_status.v1.schema.json`
- contract release：`contract-v1`
- canonical contract commit：`36c9910bc67c941c04f0768f201aadab32f8749a`
- SHA-256：`8635a4b5b134fc911b4b5f68beb36cbe87f43e0ef4d6ca31c44e98c9bfd43338`
- OM/PM/Hub 三类公开 example 均通过同一 Draft 2020-12 Schema
- canonical 与 OM、PM vendor copy 的 SHA-256 完全一致
- OM 和 PM vendor manifest 均固定 upstream release、commit 与 SHA-256
- Hub 20 项 contract tests 通过
- OM 3 项 vendor contract tests 通过
- PM 3 项 vendor contract tests 通过
- 三个仓库均配置 contract CI
- public examples 不包含完整 broker account ID
- `trusted` example 必须同时具备 `required_evidence_complete=true` 和非空 evidence reference

Exit gate 判定：

| Gate | 状态 | 证据 |
|---|---|---|
| 三类 producer example 通过 Schema | pass | Hub contract test parameterization |
| 不存在无证据的 trusted example | pass | semantic validator + negative tests |
| 契约与架构、矩阵一致 | pass | 相同 V1 Schema SHA；enum/字段边界测试 |
| version/compatibility policy 写入 README | pass | `investment-quality/README.md` |

## Phase 1 — investment-quality Hub

状态：**完成**

完成证据：

- Hub 在无 producer 情况下稳定返回 `not_onboarded`，不创建 incident 或通知
- SQLite migrations 可重复执行
- restart 后 incident/outbox 不丢失
- fingerprint 去重，acknowledgement 在重复拉取后保持，recovery 正确转换
- notification ID 稳定，recovery supersede 尚未发送的 failure notification
- acknowledge 和 maintenance window 幂等、鉴权且有 audit event
- maintenance 只抑制 scope 内通知，不修改 incident 或 gate
- producer client 对 V1、未知版本、auth、timeout 使用稳定 reason code
- dead-man payload 不包含业务数据，错误不泄露 token/上游详情
- API 具备 read/operator 权限分离、安全错误 envelope、ETag 和 no-store
- scheduler 真实运行状态可由 `/health` 查询
- listener 默认 loopback，非 loopback 配置 fail closed
- `investment-quality` 共 38 项测试和 Ruff 通过

Exit gate 判定：

| Gate | 状态 | 证据 |
|---|---|---|
| 无 producer 稳定运行 | pass | Hub API + scheduler tests |
| 不发送 not_onboarded 告警 | pass | empty scheduler/incident test |
| service/DB/scheduler/outbox health 可查询 | pass | lifespan health test |
| restart 不丢 incident/outbox | pass | SQLite restart test |
| 所有 API 默认 loopback | pass | Settings fail-closed tests |

## Phase 2 — PM

状态：**未完成**

所需证据：

- 显式账户映射和 OpenD source contract
- cash/MMF 正确语义与 receipt
- 写后立即与 30 秒只读复查
- price/FX/NAV quality gate
- PM Quality API/artifact

## Phase 3 — OM

状态：**未完成**

所需证据：

- evidence/check facade
- trade intake/full replay checks
- OpenD option-position convergence
- lifecycle policy 与 11 条 stale 回归
- OM Quality API/artifact 与本地门禁

## Phase 4 — 集成、依赖、告警

状态：**未完成**

所需证据：

- Hub clients、dependency engine、incident/outbox
- 使用同一个飞书机器人但独立 notification type
- Host Watchdog 和 external dead-man
- 重启、去重、恢复通知与敏感信息检查

## Phase 5 — 生产上线与基线

状态：**未完成**

所需证据：

- 按顺序部署 Hub、PM producer、OM producer、集成能力
- read-only baseline 分类并保存 evidence
- 真实门禁、告警和恢复路径验证
- rollback 验证
- 最终逐项完成审计
