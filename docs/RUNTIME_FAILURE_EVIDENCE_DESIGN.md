# 运行失败证据与保留设计

> Devflow 设计稿。2026-09-23 事故时间线是用户提供的历史输入；本稿的“当前实现”以本地 `origin/main` 源码为准。源码完成、提交、发布、远端升级和生产清理是不同边界。

## 目标、范围与成功信号

目标：计划 Tick 在 OpenD 登录失效、验证码、不可达或执行超时时快速失败，留下可从公开运行状态和 run 证据追溯的原因；取证文件持续可读，磁盘回收有可审核预览。

本次范围是本地源码、测试、部署配置模板、文档和**只预览**的一次性回收工具。涉及 `tick-cron`、OpenD watchdog、runtime/quality status、通知审计读取、healthcheck、日志与清理入口。用户已选择 Devflow `full` 路径并授权本地设计、实现和审查；远端只读，绝不在本轮删除远端数据、改服务、发布或升级。

非目标：重新登录富途账户；修改交易、账本或通知业务决策；发送测试告警到真实渠道；把事故时的磁盘大小或日志计数当成现值；自动删除备份、审计证据或历史 run；新建通用日志/存储框架。

| 信号 | 验收 |
| --- | --- |
| S1 | 可捕获的预检失败、子进程非零、超时及 OpenD 登录故障各有 run 级失败事件，含 `run_id/market/accounts/failure_code/stage/trigger_source/rc/first_error_at/message`；`EXEC_FAILED_RC_*` 仍可见但不是唯一证据。进程被强杀、主机掉电或磁盘满时，由 systemd 终态和状态证据缺口标明 `unknown`，不伪称已写 run 事件。 |
| S2 | 登录失效、手机或图形验证码当轮终止并返回非零；单次探测不超过现有 watchdog 的有界重试；现有独立于富途的告警路线每次事故最多提交一次；`runtime_status` 与 `quality_status` 给出相同原因码且恢复后清除。 |
| S3 | systemd tick unit 有有限超时，终态错误以 journal `PRIORITY=3` 可查；无凭证上下文的 healthcheck 返回分项 `skipped/unknown`，不因一个凭证后端异常丢失整份结果。 |
| S4 | 500 MB 审计文件仍可读取近期投递证据；指定历史时间窗可有界扫描并准确表明完整、部分、损坏或缺失，敏感字段与会话隔离维持原约束；service drift 明细可导出。 |
| S5 | 已有 `output_runs` 清理预览/确认门保留；审计与 OpenD 日志保留策略可由部署侧管理；事故候选清单得到默认只预览的逐项引用/保护检查与预计释放量，不执行删除。 |

## 当前事实和复用清单

检索范围：`tick_cron`、`multi_account_tick`、`multi_tick_watchdog`、`opend_watchdog`、`state_repo`、`runtime_status_impl`、`quality/runtime_checks`、`notification_perception_read`、`project_reader`、`healthcheck_impl`、`service_deploy`、`service_cleanup`、`runtime_logs_cli`，以及这些入口的直接测试；关键词为 `tick failure`、`audit_events`、`rotation`、`retention`、`opend_phone_verify_pending`、`service_drift`。未发现现成的跨所有外层 Tick 失败的终态 read model；不能由此推断仓外消费者不存在。

| 概念或实现 | 归属决定 |
| --- | --- |
| OpenD 失败码 | 复用 `opend_watchdog.classify_watchdog_result` 和 `opend_retcodes`；图形验证码须从宽泛的“验证码”识别中细分，最终码在所有入口一致。 |
| Tick run 身份与审计 | 复用 `run_log.create_run_id` 和 `state_repo.normalize_audit_event` 的 `error_code`、`extra` 契约；`state_repo.append_audit_event` 目前先写 shared 后写 run，latest 写失败会被吞掉，因此外层失败先用现有 `append_run_audit_jsonl` 写 run，再写 shared/latest，不将多次写入伪装成事务。外层 wrapper 只有失败时补自己的 run 级事件，不伪称它是内层扫描 run。 |
| 最新失败/恢复状态 | 新增最小的**按市场** `output_shared/state/current/tick_cron_last_result.<market>.current.json` read model，理由是现有 `audit_event_latest.current.json` 会被任意事件覆盖，`opend_phone_verify_pending.json` 只表达一种故障且不分市场。仅同市场、完整计划 Tick 的可验证成功覆盖失败；诊断、指定账户、`--no-send`、guard/幂等跳过和锁冲突不覆盖。 |
| OpenD 告警/暂停 | 复用 `multi_tick/opend_guard.py` 的 pending marker、限流和 `send_opend_alert`；不增加通知 provider。`no_send` 只抑制发送，不抑制失败记录与不健康状态。 |
| 运行/质量状态 | 复用 `runtime_status_impl` 和 `quality/runtime_checks.py`，以 read model 加现有 pending/账户 last-run 为证据，不发起实时 OpenD 查询。 |
| 审计写入和读取 | 复用 `state_repo` 的私有追加写入、`project_reader` 的 no-follow 边界及 `notification_perception_read` 的脱敏/会话过滤。当前公开读取器只读末尾 1 MiB 并标 `partial`，内部 `iter_notification_perception_events` 会整文件读入；历史时间窗扫描需新增有界流式读取，而不是另一套通知事件解释。 |
| run 和服务清理 | 复用 `service_cleanup` 已有的运行目录保留与计划摘要确认门；`research storage-gc-preview` 和 `storage-cleanup-preview` 仍只读，不加隐式删除模式。 |
| 日志 | systemd 服务已有 `StandardOutput/StandardError=journal`；`runtime_logs_cli` 当前仅列文件，因此 journal-only 需明确显示。OpenD 当前实际文件名含 `.ftlog` 和 `.logs`，不能用 `*.log` 策略假设覆盖。 |
| service drift | 复用 `service_drift_status` 的明细对象；仅在显式 CLI `--output` 时原子写 JSON，不让只读 status 工具隐式落盘。 |

## 选定设计与失败语义

### 1. Tick 终态

`tick-cron` 在获得锁后生成 wrapper `run_id`。预检拒绝、子进程退出非零、超时或启动异常时，写一个 `event_type=tick_cron`、`action=failed` 的 run 级审计事件，并更新按市场 latest read model。顶层使用现有 `run_id/error_code/message/event_at_utc`；`extra` 放 `market/accounts/stage/trigger_source/rc/first_error_at`，其中对外字段 `failure_code` 与顶层 `error_code` 同值。外层码为 `TICK_PREFLIGHT_FAILED/TICK_EXEC_FAILED/TICK_TIMEOUT/TICK_START_FAILED`，阶段分别为 `preflight/child_exit/timeout/start`。外层不知道子进程内部错误码时不得猜测；内层 watchdog 的 run/audit 记录保留权威细码，用其来源、时间和账号关联，不凭外层通用码覆盖它。可截断消息且不记录密码、验证码、环境变量或完整子进程输出。

失败写入顺序为 run、shared、按市场 latest：run 写失败不再声称满足 S1；每一步失败都保持原业务非零退出，在 stderr 的 ERROR 行报告 `FAILURE_RECORD_WRITE_FAILED` 与未完成阶段。latest 只有 durable run 证据存在后更新；shared 失败但 latest 可写时，latest 记录 `evidence_incomplete`，而不是旧 `ok`。latest 自身失败时无法由该文件自证，读取侧须交叉检查对应 systemd tick unit 的最近 `Result/ExecMainStatus` 与事件时间；unit 不可查则返回 `unknown`（并列明 latest 可能陈旧），绝不将旧 `ok` 宣称为本轮成功。这不是跨文件事务，重复恢复执行只允许新增带唯一 run ID 的事件，不回写或覆盖旧记录。进程被 SIGKILL、主机掉电、磁盘满无法保证写事件；systemd 的终态和下一次状态核对是后备证据。

只有同市场、全账户范围、非诊断且非 `--no-send` 的计划 Tick 完成实际扫描，才能覆盖按市场 latest 为 `ok`。wrapper 用内层相同的 `runtime_paths.resolve_runtime_root` 确定 runtime root，以 `OM_TICK_CRON_RUN_ID` 环境值传自己的 ID；内层在 `multi_account_tick` 唯一真实扫描完成出口、通知流程返回 `rc=0` 后，核对 `ran_pipeline_accounts` 与结果中 `ran_scan=True` 覆盖全部本轮计划账户，再向该 runtime root 下的 wrapper run 目录原子写 `state/child_tick_completion.json`，内容为 wrapper ID、inner run ID、市场、账户、`completed_at_utc`、`status=ok`。wrapper 只接受与本轮 ID/市场/完整账户集合匹配且子进程 `rc=0` 的收据；缺收据保持旧故障并显示 `completion_unknown`。`rc=0` 单独不足以证明恢复；no-account、delivery-only、项目 guard、幂等跳过以及未执行 Tick 均不写收据。read model 原子写入失败时报 ERROR；inner run 与 wrapper run 是两个身份，通过收据关联，不造同一个 ID。

`OPEND_NEEDS_PHONE_VERIFY` 现有分支把 `run_end` 写成 `skip` 并返回 0，应改为终态错误（非零），写入账户 last-run 的原因码，保留 pending marker。`tick_guard_flow` 在 marker pending 时也返回非零、同一原因码，不能标 guard 成功；人工 `--opend-phone-verify-continue` 只放行一次 watchdog 探测，不在探测前清 marker。watchdog 验证所需登录能力确实恢复后清 marker，即使这次人工 Tick 因时窗没有扫描；市场级失败仍保持到下一次完整计划 Tick 收据。探测失败则保留 marker 并返回非零。`OPEND_LOGIN_INVALID` 沿现有 fail-fast 路径；“需要图形验证码”单独归一为 `OPEND_NEEDS_PIC_VERIFY`/人工动作，不再错误标成手机验证码。恢复不能靠时间过期伪装健康；账户旧错误与市场新收据按同一市场、账号、时间与 pending 状态仲裁，不能让旧记录永久遮蔽真恢复。

告警复用现有独立于富途的通道；三种登录人工动作码归同一事故族。现有限流是先 check、发送后 record，HK/US 并发会重复提交；用现有限流状态加窄锁作原子认领，每次事故最多**一次发送尝试**，不把 provider 提交等同于投递确认。提交结果不明保持认领并给运维可读的 `delivery_unknown`，人工或恢复事件才解锁下一次事故；`no_send` 不认领也不发送。其他 OpenD 暂时故障仍遵守现有限流及重试策略。

运行状态以最新 read model 和 pending/账户错误证据判定；登录细码在其账号和时间覆盖范围内优先于外层通用码，缺文件或本轮写证据缺口是 `unknown/evidence_incomplete`，不是健康。`quality_status` 使用同一原因码生成运行检查，不因服务 unit `active` 就覆盖业务故障。状态读取不触发 OpenD、告警或写入。

`tick-cron --timeout 600` 的 systemd `TimeoutStartSec` 设为 900，`TimeoutStopSec` 给有界终止宽限；CLI 与 unit 保持可配置超时关系。失败终态用 systemd 可解析的 `<3>` 前缀写入 stderr，普通状态保持现有 stdout；渲染 unit 明示 `SyslogLevelPrefix=yes`。这只改变调度入口的分级，不重写全部日志系统。

### 2. 取证与健康检查

保留近期默认末尾读取的速度和 `partial` 声明；增加显式 `start_utc/end_utc` 时间窗入口，逐行流式扫描当前及受管理的历史审计段，单行最大 1 MiB、单次扫描最多 64 MiB、最多 50 条公开结果，并沿用时间/取消预算。会话过滤先于公开计数，复用 `_matches_event/_public_event` 脱敏；预算耗尽、坏行、缺段或首段晚于查询起点时返回 `partial`、`stop_reason`、已扫描字节和已覆盖时间范围，不能返回完整零事件。历史页绑定首次查询的段身份与完整行结束偏移；活动文件后续追加不使已有页失效，原有字节变化、段消失或轮转使游标明确失效。旧单文件仍可流式读取；首轮不新增索引或数据库，若真实 500 MB 窗口扫描超出预算再另行设计索引。

`runtime_logs_cli` 对 `kind=service` 且文件为空的 systemd profile 返回 `journal_only` 和已知 unit/安全的只读查询提示，不把零文件解释成零日志。`service_drift` CLI 显式导出完整 JSON 明细；healthcheck 仅把缺凭证所影响的分项标为 `skipped/unknown`，其他账户/行情只读检查继续，汇总不得虚报 ready。

### 3. 保留与回收

共享审计及 trade-intake 审计保持私有权限和追加语义。三类目标的 writer/reader 绑定如下：`audit_events.jsonl` 由 `state_repo.append_shared_audit_jsonl` 写，`notification_perception_read`、`runtime_logs_cli`、`research/evidence` 读取；`trade_intake/<account>/audit.jsonl` 与 `auto_trade_intake_audit.jsonl` 均由 `trades/state.append_trade_intake_audit` 写，`trades/state.read_latest_lifecycle_attempt_run_seal` 和 `runtime_status_impl` 等入口消费。现有 seal reader 逐行读取单文件，切段后必须按段顺序读到当前段，保持 `seal_count` 和最后 seal 的语义；其他只需当前尾部的入口明确返回历史段可用范围，不悄悄变成空结果。任何未识别的历史消费者出现时，先保留单文件写入，不能启用该类轮转。

采用窄范围**写入侧**轮转：各审计 writer 共用独立于被重命名文件的锁，在锁内按日期或大小先封段、再创建私有当前文件、追加完整 JSONL 行；trade-intake 的原有尾行修复与 fsync 仍在锁内，不能让外部 `copytruncate` 或无锁 rename 与已打开的描述符竞争。段采用确定性日期+序号命名，reader 同时识别当前和已封段，封段后才可压缩。切段前后行数、尾行、权限和并发读取必须验收。审计的自动删除期不在本轮启动：只给出可审查保留建议，待生产证据保留期明确后另行授权启用删除。

`output_runs` 使用现有 `om service cleanup` 的 14 天或最近 200 个与计划摘要确认机制，不再建一套删除器。OpenD 自身已产生 `.ftlog`、`.logs` 分片；部署侧只按这些已知后缀和 7 天年龄生成预览/策略，保护现用进程打开的文件与最近文件，未知扩展名不处理。备份保留只生成 TTL 候选预览，不在本轮自动删除。

一次性候选预览仅接受下列固定白名单，目标主机为 `liuxie-incus`，`output_shared` 路径相对 `/var/lib/options-monitor`；不使用通配发现未知候选：

| 类型 | 精确候选 |
| --- | --- |
| 2026-09-14 快照（12） | `last-three-terminal-repair-20260914-v1`、`intake-evidence-repair-20260914-v2`、`identity-evidence-repair-20260914-v1`、`cnooc-repair-20260914-v2`、`last-stock-production-repair-20260914-v1`、`intake-evidence-repair-20260914-v1`、`cnooc-repair-20260914`、`last-stock-repair-20260914-v1`、`last-stock-repair-20260914-v2`、`wheel-fee-repair-20260914-v3`、`wheel-fee-repair-20260914-v2`、`sy-wheel-recovery-357-20260914`，均在 `output_shared/state/`。 |
| `/tmp` 残留（8） | `/tmp/om-legacy-association-rehearsal`、`/tmp/om-hk-timing-proof`、`/tmp/om-hk-data-rehearsal`、`/tmp/om-v362-control.zocW8R`、`/tmp/om-pdd-order-repair`、`/tmp/om-v361-control.gTCvAT`、`/tmp/om-wheel-assignment-recovery-20260914`、`/tmp/om-readonly-20260919T065001.sqlite3`。 |
| 其他 | `output_shared/state/backups/` 目录、用户给定的顶层 `option_positions.sqlite3.before-{cash-rekey,realized-pnl-repair}-*.bak` 与 `state/` 下旧 SQLite 备份（仅作为待精确枚举清单）；OpenD `~/.com.futunn.FutuOpenD/Log` 下 7 天前的 `.ftlog/.logs` 段。未给具体文件名的项只能在预览中列候选，不能被视为已批准删除。 |

对每项输出 `path/realpath/type/logical_size/allocated_bytes/mtime/reference_checks/protected/reason`，缺失或身份变化也明确显示。检查当前 release 与前两个版本、现用账本、最近迁移备份、进程打开文件、软链、systemd unit、脚本和清单引用；目录嵌套去重，预计释放量只计非保护且可证明的常规文件。任何检查不可用或引用不明时 `protected=true`。工具没有删除开关；单独的生产 apply 需要另一个授权与独立执行方案。

## 切片、验证和风险

| 切片 | 交付行为 | 覆盖 | 依赖/最小验证 |
| --- | --- | --- | --- |
| A | 计划 Tick 的失败事件、OpenD 终态、状态/质量映射、journal 级别及有限 unit 超时 | S1、S2、S3 | 无；入口、watchdog、status/quality、unit 渲染回归，失败/恢复/锁冲突路径。 |
| B | 审计历史窗口流式读取、日志位置说明、drift 导出及 healthcheck 分项降级 | S4、S3 | A 的原因码；真实密集 JSONL 与 500 MB 文件的有界扫描、损坏/取消/游标变化/会话隔离和 facade 测试。 |
| C | 协调写入的轮转、现有清理入口复用和固定清单的只读回收预览 | S5、S4 | B 的历史段读取；临时根目录中验证引用/保护/幂等及并发追加与轮转前后读取。 |

每片只修改必要 owner 并跑能暴露该行为回归的测试；最终执行项目要求的完整相关门禁。测试 fixture 不连接真实 OpenD、不发送通知、不写生产 runtime。未验证的外部日志命名、权限或保留合规要求必须保留为限制，不能把源码测试当远端部署成功。

拒绝的方案：另建通用日志框架、为审计查询新增数据库、把 `service_drift` 的只读状态查询变成隐藏写入、用目录通配直接删除备份或旧 run。这些都增加状态或副作用，而现有 owner 已提供更窄的入口。
