# 运行失败证据与保留设计

> 当前运行失败、取证与保留合同。2026-09-23 事故是历史设计输入；当前 owner 以源码为准，生产健康、磁盘占用和部署状态须从目标环境独立核验。

## 目标、范围与成功信号

目标：计划 Tick 在 OpenD 登录失效、验证码、不可达或执行超时时快速失败，留下可从公开运行状态和 run 证据追溯的原因；取证文件持续可读，磁盘回收有可审核预览。

合同覆盖 `tick-cron`、OpenD watchdog、runtime/quality status、通知审计读取、healthcheck、日志与清理入口。回收预览不授权删除，源码合同不授权发布、升级、服务变更或生产清理。

非目标：重新登录富途账户；修改交易、账本或通知业务决策；发送测试告警到真实渠道；把事故时的磁盘大小或日志计数当成现值；自动删除备份、审计证据或历史 run；新建通用日志/存储框架。

| 信号 | 验收 |
| --- | --- |
| S1 | 可捕获的预检失败、子进程非零、超时及 OpenD 登录故障各有 run 级失败事件，含 `run_id/market/accounts/failure_code/stage/trigger_source/rc/first_error_at/message`；`EXEC_FAILED_RC_*` 仍可见但不是唯一证据。进程被强杀、主机掉电或磁盘满时，由 systemd 终态和状态证据缺口标明 `unknown`，不伪称已写 run 事件。 |
| S2 | 登录失效、手机或图形验证码当轮终止并返回非零；单次探测不超过现有 watchdog 的有界重试；现有独立于富途的告警路线每次事故最多提交一次；`runtime_status` 与 `quality_status` 给出相同原因码且恢复后清除。 |
| S3 | systemd tick unit 有有限超时，终态错误以 journal `PRIORITY=3` 可查；无凭证上下文的 healthcheck 返回分项 `skipped/unknown`，不因一个凭证后端异常丢失整份结果。 |
| S4 | 500 MB 审计文件仍可读取近期投递证据；指定历史时间窗可有界扫描并准确表明完整、部分、损坏或缺失，敏感字段与会话隔离维持原约束；service drift 明细可导出。 |
| S5 | 已有 `output_runs` 清理预览/确认门保留；审计与 OpenD 日志保留策略可由部署侧管理；事故候选清单得到默认只预览的逐项引用/保护检查与预计释放量，不执行删除。 |

## 当前 owner

| 责任 | owner 与边界 |
| --- | --- |
| Tick 失败与恢复收据 | `src/application/tick_cron.py`、`src/application/multi_account_tick.py`；按市场保存终态，完整计划 Tick 收据才证明恢复 |
| OpenD 原因码与人工动作 | `src/infrastructure/opend_watchdog.py`、`src/application/tick_guard_flow.py`；登录故障非零退出，探测确认恢复后清 pending marker |
| 告警认领 | `src/application/multi_tick/opend_guard.py`；用现有限流状态和窄锁认领事故，不把发送尝试当送达 |
| 只读状态 | `src/application/agent_tools/runtime_status_impl.py`、`src/application/quality/runtime_checks.py`；读取同一终态和 pending/账户证据，不实时查询 OpenD |
| 通知取证 | `src/application/notification_perception_read.py`；近期尾部读取与显式历史窗口共享事件解释、脱敏和会话隔离 |
| 日志与清理 | `src/application/runtime_logs_cli.py`、`src/application/service_cleanup.py`、`src/application/incident_cleanup_preview.py`；日志位置明确，清理按各自预览/确认边界执行 |

## 当前失败语义

### 1. Tick 终态

`tick-cron` 在获得锁后生成 wrapper `run_id`。预检拒绝、子进程退出非零、超时或启动异常时，写一个 `event_type=tick_cron`、`action=failed` 的 run 级审计事件，并更新按市场 latest read model。顶层使用现有 `run_id/error_code/message/event_at_utc`；`extra` 放 `market/accounts/stage/trigger_source/rc/first_error_at`，其中对外字段 `failure_code` 与顶层 `error_code` 同值。外层码为 `TICK_PREFLIGHT_FAILED/TICK_EXEC_FAILED/TICK_TIMEOUT/TICK_START_FAILED`，阶段分别为 `preflight/child_exit/timeout/start`。外层不知道子进程内部错误码时不得猜测；内层 watchdog 的 run/audit 记录保留权威细码，用其来源、时间和账号关联，不凭外层通用码覆盖它。可截断消息且不记录密码、验证码、环境变量或完整子进程输出。

失败写入顺序为 run、shared、按市场 latest：run 写失败不再声称满足 S1；每一步失败都保持原业务非零退出，在 stderr 的 ERROR 行报告 `FAILURE_RECORD_WRITE_FAILED` 与未完成阶段。latest 只有 durable run 证据存在后更新；shared 失败但 latest 可写时，latest 记录 `evidence_incomplete`，而不是旧 `ok`。latest 自身失败时无法由该文件自证，读取侧须交叉检查对应 systemd tick unit 的最近 `Result/ExecMainStatus` 与事件时间；unit 不可查则返回 `unknown`（并列明 latest 可能陈旧），绝不将旧 `ok` 宣称为本轮成功。这不是跨文件事务，重复恢复执行只允许新增带唯一 run ID 的事件，不回写或覆盖旧记录。进程被 SIGKILL、主机掉电、磁盘满无法保证写事件；systemd 的终态和下一次状态核对是后备证据。

只有同市场、全账户范围、非诊断且非 `--no-send` 的计划 Tick 完成实际扫描，才能覆盖按市场 latest 为 `ok`。wrapper 用内层相同的 `runtime_paths.resolve_runtime_root` 确定 runtime root，以 `OM_TICK_CRON_RUN_ID` 环境值传自己的 ID；内层在 `multi_account_tick` 唯一真实扫描完成出口、通知流程返回 `rc=0` 后，核对 `ran_pipeline_accounts` 与结果中 `ran_scan=True` 覆盖全部本轮计划账户，再向该 runtime root 下的 wrapper run 目录原子写 `state/child_tick_completion.json`，内容为 wrapper ID、inner run ID、市场、账户、`completed_at_utc`、`status=ok`。wrapper 只接受与本轮 ID/市场/完整账户集合匹配且子进程 `rc=0` 的收据；缺收据保持旧故障并显示 `completion_unknown`。`rc=0` 单独不足以证明恢复；no-account、delivery-only、项目 guard、幂等跳过以及未执行 Tick 均不写收据。read model 原子写入失败时报 ERROR；inner run 与 wrapper run 是两个身份，通过收据关联，不造同一个 ID。

`OPEND_NEEDS_PHONE_VERIFY` 分支以终态错误和非零返回结束，账户 last-run 保留原因码与 pending marker。`tick_guard_flow` 在 marker pending 时也返回非零、同一原因码，不能标 guard 成功；人工 `--opend-phone-verify-continue` 只放行一次 watchdog 探测，不在探测前清 marker。watchdog 验证所需登录能力确实恢复后清 marker，即使这次人工 Tick 因时窗没有扫描；市场级失败仍保持到下一次完整计划 Tick 收据。探测失败则保留 marker 并返回非零。`OPEND_LOGIN_INVALID` 沿现有 fail-fast 路径；“需要图形验证码”单独归一为 `OPEND_NEEDS_PIC_VERIFY`/人工动作，不再错误标成手机验证码。恢复不能靠时间过期伪装健康；账户旧错误与市场新收据按同一市场、账号、时间与 pending 状态仲裁，不能让旧记录永久遮蔽真恢复。

告警复用现有独立于富途的通道；三种登录人工动作码归同一事故族。限流状态在窄锁内作原子认领，每次事故最多**一次发送尝试**，不把 provider 提交等同于投递确认。提交结果不明保持认领并给运维可读的 `delivery_unknown`，人工或恢复事件才解锁下一次事故；`no_send` 不认领也不发送。其他 OpenD 暂时故障仍遵守现有限流及重试策略。

运行状态以最新 read model 和 pending/账户错误证据判定；登录细码在其账号和时间覆盖范围内优先于外层通用码，缺文件或本轮写证据缺口是 `unknown/evidence_incomplete`，不是健康。`quality_status` 使用同一原因码生成运行检查，不因服务 unit `active` 就覆盖业务故障。状态读取不触发 OpenD、告警或写入。

`tick-cron --timeout 600` 的 systemd `TimeoutStartSec` 设为 900，`TimeoutStopSec` 给有界终止宽限；CLI 与 unit 保持可配置超时关系。失败终态用 systemd 可解析的 `<3>` 前缀写入 stderr，普通状态保持现有 stdout；渲染 unit 明示 `SyslogLevelPrefix=yes`。这只改变调度入口的分级，不重写全部日志系统。

### 2. 取证与健康检查

保留近期默认末尾读取的速度和 `partial` 声明；显式 `start_utc/end_utc` 时间窗入口，逐行流式扫描当前及受管理的历史审计段，单行最大 1 MiB、单次扫描最多 64 MiB、最多 50 条公开结果，并沿用时间/取消预算。会话过滤先于公开计数，复用 `_matches_event/_public_event` 脱敏；预算耗尽、坏行、缺段或首段晚于查询起点时返回 `partial`、`stop_reason`、已扫描字节和已覆盖时间范围，不能返回完整零事件。历史页绑定首次查询的段身份与完整行结束偏移；活动文件后续追加不使已有页失效，原有字节变化、段消失或轮转使游标明确失效。旧单文件仍可流式读取；首轮不新增索引或数据库，若真实 500 MB 窗口扫描超出预算再另行设计索引。

`runtime_logs_cli` 对 `kind=service` 且文件为空的 systemd profile 返回 `journal_only` 和已知 unit/安全的只读查询提示，不把零文件解释成零日志。`service_drift` CLI 显式导出完整 JSON 明细；healthcheck 仅把缺凭证所影响的分项标为 `skipped/unknown`，其他账户/行情只读检查继续，汇总不得虚报 ready。

### 3. 保留与回收

共享审计及 trade-intake 审计保持私有权限和追加语义。三类目标的 writer/reader 绑定如下：`audit_events.jsonl` 由 `state_repo.append_shared_audit_jsonl` 写，`notification_perception_read`、`runtime_logs_cli`、`research/evidence` 读取；`trade_intake/<account>/audit.jsonl` 与 `auto_trade_intake_audit.jsonl` 均由 `trades/state.append_trade_intake_audit` 写，`trades/state.read_latest_lifecycle_attempt_run_seal` 和 `runtime_status_impl` 等入口消费。现有 seal reader 逐行读取单文件，切段后必须按段顺序读到当前段，保持 `seal_count` 和最后 seal 的语义；其他只需当前尾部的入口明确返回历史段可用范围，不悄悄变成空结果。任何未识别的历史消费者出现时，先保留单文件写入，不能启用该类轮转。

采用窄范围**写入侧**轮转：各审计 writer 共用独立于被重命名文件的锁，在锁内按日期或大小先封段、再创建私有当前文件、追加完整 JSONL 行；trade-intake 的原有尾行修复与 fsync 仍在锁内，不能让外部 `copytruncate` 或无锁 rename 与已打开的描述符竞争。段采用确定性日期+序号命名，reader 同时识别当前和已封段，封段后才可压缩。切段前后行数、尾行、权限和并发读取必须验收。审计的自动删除期不在本轮启动：只给出可审查保留建议，待生产证据保留期明确后另行授权启用删除。

`output_runs` 使用现有 `om service cleanup` 的 14 天或最近 200 个与计划摘要确认机制，不再建一套删除器。OpenD 自身已产生 `.ftlog`、`.logs` 分片；部署侧只按这些已知后缀和 7 天年龄生成预览/策略，保护现用进程打开的文件与最近文件，未知扩展名不处理。备份保留只生成 TTL 候选预览，不在本轮自动删除。

事故候选预览的历史白名单由 `src/application/incident_cleanup_preview.py` 维护；文档不复制可能被误当成当前文件状态的临时路径清单。该白名单来自 2026-09-23 事故输入，目标主机与路径必须按实际预览绑定，不使用通配发现未知候选。


对每项输出 `path/realpath/type/logical_size/allocated_bytes/mtime/reference_checks/protected/reason`，缺失或身份变化也明确显示。检查当前 release 与前两个版本、现用账本、最近迁移备份、进程打开文件、软链、systemd unit、脚本和清单引用；目录嵌套去重，预计释放量只计非保护且可证明的常规文件。任何检查不可用或引用不明时 `protected=true`。工具没有删除开关；单独的生产 apply 需要另一个授权与独立执行方案。

## 验证与风险

入口与持久证据由 `tests/test_tick_cron.py`、
`tests/test_tick_account_execution_barrier.py` 和 OpenD watchdog/guard 测试覆盖；
历史读取、轮转、日志与回收预览分别见
`tests/test_notification_perception_read_tool.py`、`tests/test_audit_rotation.py`、
`tests/test_runtime_logs_journal.py`、`tests/test_service_drift_export.py` 和
`tests/test_incident_cleanup_preview.py`。

测试 fixture 不连接真实 OpenD、不发送通知、不写生产 runtime。强杀、断电、
磁盘满或日志 sink 不可用仍可能留下证据缺口；状态读取必须报告 unknown。
外部日志命名、权限、保留期和目标环境部署必须独立核验。

拒绝的方案：另建通用日志框架、为审计查询新增数据库、把 `service_drift` 的只读状态查询变成隐藏写入、用目录通配直接删除备份或旧 run。这些都增加状态或副作用，而现有 owner 已提供更窄的入口。
