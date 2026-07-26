# 运行与数据质量监控 — 实施状态与完成证据

- **目标**：严格实施 [implementation-plan.md](implementation-plan.md) 的 Phase 0–5
- **状态**：Phase 0–4 本地完成；Phase 5 生产上线待授权
- **开始日期**：2026-07-26
- **完成标准**：所有 phase exit gate 有当前代码、测试和运行证据；生产阶段完成真实只读基线与回滚验证

本文只记录已由当前证据证明的状态。未验证、仅设计完成或尚未上线的项目一律保持未完成。

## 仓库

| 仓库 | 分支 | 当前用途 |
|---|---|---|
| `options-monitor` | `main`（合并进行中） | OM producer、本地门禁、设计与完成证据 |
| `portfolio-management` | `main@c66422a` | PM 前置修复、producer、本地门禁 |
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

- 完整实现映射：[hub-check-implementation.md](hub-check-implementation.md)
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
- `investment-quality` 共 66 项测试和 Ruff 通过

Exit gate 判定：

| Gate | 状态 | 证据 |
|---|---|---|
| Hub 检查矩阵有实现/测试映射 | pass | `hub-check-implementation.md` |
| 无 producer 稳定运行 | pass | Hub API + scheduler tests |
| 不发送 not_onboarded 告警 | pass | empty scheduler/incident test |
| service/DB/scheduler/outbox health 可查询 | pass | lifespan health test |
| restart 不丢 incident/outbox | pass | SQLite restart test |
| 所有 API 默认 loopback | pass | Settings fail-closed tests |

## Phase 2 — PM

状态：**代码与本地验证完成；生产 canary/onboarding 待 Phase 5**

完成证据：

- 完整实现映射：[pm-check-implementation.md](pm-check-implementation.md)
- PM commit：`c67ccf4`（叠加前置质量 commits，目标版本 `0.1.27`）
- `lx`/`sy` 使用显式账户级 `acc_id`、`REAL`、market、CNH source contract；不回退全局 `acc_id` 或 `acc_index`
- `accinfo.cash` 与 `fund_assets` 分别成为 securities cash/MMF 唯一权威字段；0 与 missing 分离
- positions/cash/MMF 使用同一 source snapshot，保存 durable latest/history receipt 和 dataset-scoped partial-write 状态
- OpenD source 证据要求 forced refresh、账户验证、完整 position snapshot/分页和脱敏 payload digest；balance-only 回执不能冒充完整 portfolio sync
- PM producer 产出 `RT-PM-002/003`；按早晚调度 deadline+15 分钟判断回执，过期后相关 replica 全部 unavailable
- OpenD 查询、来源验证或 position diff 在写前失败时也保存脱敏失败尝试，避免旧 success latest 掩盖明确故障
- 写后立即对账；不一致时 30 秒后只读重查，不重复写
- quantity 精确比较、cost basis 按 PM 存储精度比较；cost mismatch 不单独阻断 NAV
- NAV 写入边界复用当前 valuation evidence 和最近一次 durable reconciliation receipt；producer onboarding 后 fail closed，且不依赖 Hub 在线
- price evidence 保留 source、quote time、fallback、missing；非 CNY FX 缺少 fact time 时 unavailable，不用当前汇率补历史证据
- producer 覆盖 `PM-ACC-001` 至 `PM-NAV-002` 的 17 个 PM 检查 ID
- artifact 原子发布；`GET /quality/status` 只读已发布 artifact，独立 bearer token、ETag、`no-store` 和安全错误 envelope
- `pm quality status --json` 与 HTTP 使用同一 application payload；`pm quality refresh` 只发布控制面 artifact
- PM focused 46 项、完整 765 项测试通过；触及文件 Ruff 通过（仓库全量 Ruff 仍有既存基线问题，不属于本 work unit）

Exit gate 判定：

| Gate | 状态 | 证据 |
|---|---|---|
| PM 检查矩阵有实现/测试映射 | pass | `pm-check-implementation.md` |
| partial-write 数据集级 untrusted | pass | cash success + MMF failure receipt/status 回归 |
| `/health` 与 `/quality/status` 分离 | pass | HTTP auth/ETag/503/只读 artifact tests |
| producer payload 通过 canonical Schema | pass | `PMQualityService` Draft 2020-12 validation |
| 生产只读 canary 不写 Feishu | pending Phase 5 | 需要目标实例当前配置与只读执行批准 |
| Hub onboard 后真实告警和本地门禁 | pending Phase 5 | 需要生产 token/onboarded 配置与真实恢复测试 |

## Phase 3 — OM

状态：**代码与本地验证完成；生产 canary/onboarding 待 Phase 5**

完成证据：

- 完整实现映射：[om-check-implementation.md](om-check-implementation.md)
- producer 覆盖 `RT-OM-001` 至 `RT-OM-004` 及全部 11 个 OM 数据检查 ID
- trade intake 复用现有 runtime/checkpoint/audit/reconciliation evidence，不建立平行 intake 状态
- ledger full replay 只经 `src.application.ledger.api` 公共边界读取，并按账户比较 materialized projection
- duplicate broker identity、economic conflict 和 projection conservation 分账户判定，不跨账户污染
- OpenD option snapshot 要求显式账户、`REAL`、`refresh_cache=True`、完整 snapshot；normalized identity 包含方向、数量和 multiplier
- position divergence 执行首次、+1 分钟、+5 分钟状态机；transient 不阻断，persistent 才 untrusted
- lifecycle deadline 使用市场交易日及首次 deep reconcile +2 小时；11 条 overdue case 固定回归全部判定 stale
- external adjustment、legacy evidence gap 与实时 lifecycle pending 分开建模
- artifact 原子发布；HTTP 只读已发布 artifact，独立 bearer token、ETag、`no-store` 和安全错误 envelope
- 本地 gate 在 onboarding 前不生效；onboarding 后按 account/market/consumer fail closed，且不依赖 Hub 在线
- 普通候选扫描不受无关持仓异常影响
- OM 完整 pytest：`3238 passed, 10 skipped`；touched Ruff 通过；production module cycles 为 0

Exit gate 判定：

| Gate | 状态 | 证据 |
|---|---|---|
| OM 检查矩阵有实现/测试映射 | pass | `om-check-implementation.md` |
| 11 条 lifecycle stale 回归 | pass | `test_regression_eleven_overdue_lifecycle_cases_are_classified_stale` |
| full replay/duplicate identity/convergence 回归 | pass | `tests/quality/test_om_quality_checks.py` |
| `/health` 与 `/quality/status` 分离 | pass | HTTP auth/ETag/只读 artifact tests |
| producer payload 通过 canonical Schema | pass | `OMQualityService` Draft 2020-12 validation |
| 本地门禁不依赖 Hub 且按 scope 隔离 | pass | quality gate tests |
| 生产只读 canary | pending Phase 5 | 需要目标实例当前配置与只读执行批准 |
| Hub onboard 后真实告警和本地门禁 | pending Phase 5 | 需要生产 token/onboarded 配置与真实恢复测试 |

## Phase 4 — 集成、依赖、告警

状态：**代码与本地验证完成；生产 onboarding/真实投递待 Phase 5**

完成证据：

- Hub commit：`b7f2ca94735b5f5209eca1ab3f6d615d1f8826ab`
- Hub version：`0.2.0`
- OM/PM 使用独立 loopback base URL 和只读 token；配置缺失、token/endpoint 复用或非法 boolean 均 fail closed
- producer client 区分 timeout、transport、auth、HTTP、Schema、identity、stale 和 clock skew；支持 ETag/304，重启后 304 无缓存时不会猜测
- 最近 valid snapshot 与最近 poll result 分开持久化；拉取失败保留旧值用于诊断，但 component 和正式依赖保持 unavailable
- 固定依赖注册表逐项验证完整性；必要 dataset 缺失输出 `DEPENDENCY_DATASET_MISSING`，不能用 trusted 子集误判消费者可信
- dependency 以 consumer/account/market 分组；PM 故障不污染无关 OM consumer，`lx` 不污染 `sy`
- incident fingerprint 固定，状态覆盖 new/persistent/acknowledged/recovered；只有成功重验证可以 recovery
- pull、producer 和 Watchdog incident 使用独立所有权；`RT-OM-*`/`RT-PM-*` 归目标服务，Watchdog artifact 不可用时不误恢复旧事件
- blocking 首次、2 小时提醒/每日最多 3 次、warning 每日摘要和 recovery 均使用稳定 outbox ID
- 飞书使用同一机器人身份与收件人配置，但 notification type 独立；1/5/15 分钟重试，连续三次失败令 `RT-HUB-002` unhealthy，recovery supersede 未发送故障
- scheduler/dispatcher/Watchdog 状态持久化并检查新鲜度；新 CLI 进程不再用内存 `starting` 虚报健康，`iq check` degraded 时退出码为 1
- incident API 已实现 service/account/dataset 过滤和 opaque cursor；status projection 不混入未请求的数据集
- maintenance 禁止空范围全局静默，只抑制范围内通知，不改变 incident/gate
- Host Watchdog 只读取 systemd unit/timer 状态和 artifact mtime；目标、权限、路径非法时 fail closed，公开结果不含路径/命令输出
- dead-man heartbeat 仅包含 `service/status`；支持 secret ping URL 和可选
  Bearer token，token 无 endpoint 时 fail closed，endpoint/secret 不保存
  到状态或业务数据
- normal snapshot 30 天、blocking/control evidence 400 天；active incident 和未发送/失败通知不被 retention 删除
- systemd renderer 只生成不安装；使用专用 `investment-quality` 用户/组、`0077` umask 和 `0700` StateDirectory
- canonical Schema 四份副本 SHA-256 仍为 `8635a4b5b134fc911b4b5f68beb36cbe87f43e0ef4d6ca31c44e98c9bfd43338`
- Hub 完整 pytest：66 项通过；Ruff、compileall、`git diff --check` 通过
- `investment_quality-0.2.0` wheel 从已提交源码以
  `SOURCE_DATE_EPOCH=1785064563` 连续隔离构建两次，SHA 一致；全新 venv
  安装/import 及 packaged Schema 验证成功；候选 wheel SHA-256：
  `aac156209f8ad40434603be4a6a732e436d94af77e08f125d82bee86cde1ba35`

Exit gate 判定：

| Gate | 状态 | 证据 |
|---|---|---|
| 两 producer unavailable 不互相污染 | pass | retained snapshot / PM-to-OM isolation tests |
| dependency propagation 与矩阵一致 | pass | required-completeness + account scope tests |
| notification dedup/retry/recovery | pass | stable ID、1/5/15、supersession、same-bot envelope tests |
| Hub restart 恢复 incident/outbox/runtime | pass | SQLite migration 1→2、restart、persisted runtime tests |
| maintenance/ack audit | pass | API、idempotency、scope 和 audit event tests |
| external heartbeat payload 不泄露数据 | pass（本地 adapter canary） | exact payload + safe failure tests |
| 真实飞书 incident/recovery | pending Phase 5 | 需要生产机器人配置和受控真实状态转换批准 |
| 真实 external missed-heartbeat | pending Phase 5 | 需要选定 provider secret ping URL 或 endpoint/可选 Bearer token，并批准上线 |

## Phase 5 — 生产上线与基线

状态：**本地发布/部署准备进行中；生产未变更**

已完成的上线前准备：

- 已固化唯一跨仓库执行顺序、授权边界、证据规则和首次回滚隔离：
  [phase5-runbook.md](phase5-runbook.md)；
- OM 非深度刷新会保留仍有效的权威 OpenD 证据，不再把它覆盖为
  `unavailable`；
- OM 只在首次 baseline、本地 `position_lots` revision 变化、差异复查到期、
  日终 deadline 或人工强制时访问 OpenD；
- OM 单市场日终刷新保留另一市场已发布数据集；
- OM systemd renderer 以 opt-in 方式生成质量 HTTP、15 分钟常规刷新、
  1 分钟轻量到期探测及 US/HK 日终深度对账单元；
- PM Linux installer 以独立 opt-in `--enable-quality-timer` 生成并启用
  `portfolio-quality-refresh.timer`，默认 15 分钟；
- PM production `portfolio-futu-evening.service` 的
  `DatetimeFieldConvFail` 根因已在 `feat/quality-monitoring` 修复并覆盖所有
  holdings 时间字段写路径；生产尚未升级，失败同步尚未重跑；
- PM 提供只读 `pm futu accounts --market ... --json`，仅返回显式映射所需
  authority 字段，空/不完整/重复列表 fail closed，不读取余额/持仓或写业务数据；
- PM 完整 pytest：765 项通过；变更文件 Ruff 与 diff check 通过。全仓 Ruff
  仍有 52 个既有未使用导入告警，不属于本 work unit。
- PM 已 fast-forward 合入远端 `main@c66422a`，目标版本 `0.1.27` 尚未发布。
- OM 完整 pytest：3238 项通过、10 项跳过；变更文件 Ruff、dependency graph
  `--check` 与 diff check 通过。
- OM 质量代码合入 main 时保持当前已发布版本 `1.4.30`，变更记录位于
  `Unreleased`；执行 `AUTH-REL` 时才生成 `1.4.31` 发布元数据，并记录
  最终 release-candidate head。

所需证据：

- 按顺序部署 Hub、PM producer、OM producer、集成能力
- read-only baseline 分类并保存 evidence
- 真实门禁、告警和恢复路径验证
- rollback 验证
- 最终逐项完成审计
