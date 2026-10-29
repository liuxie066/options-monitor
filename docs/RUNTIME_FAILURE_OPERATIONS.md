# Tick 故障取证与保留操作

这份说明对应本地源码变更。发布或远端升级前，不把这里的行为当成生产现状。

## 失败与恢复

计划 Tick 的外层失败写入 `output_runs/<wrapper_run_id>/state/audit_events.jsonl`，并更新 `output_shared/state/current/tick_cron_last_result.<market>.current.json`。错误码、阶段、账户和首错时间在审计行中；`EXEC_FAILED_RC_*` 仍是终端兜底。登录失效、手机或图形验证码需要人工重新登录 OpenD，计划 Tick 返回非零。`om_runtime_status` 和 `om_quality_status` 可读取同一原因码。若 run 写入失败或系统被强杀，以 systemd 的 `Result/ExecMainStatus` 核对，不能把旧状态当作新一轮健康结果。

`./om logs --kind service` 在文件目录为空时会提示查看 systemd journal。具体故障可用 `journalctl -p err -u options-monitor-tick-hk.service`（或 `us`）；服务单元的 ERROR 行采用 systemd 可解析的级别前缀。渲染后的 tick unit 超时比 CLI 的 `--timeout` 多 300 秒，并有 30 秒停止宽限；只有受控升级后生产 unit 才会变化。

## OpenD 查询连接与重试

共享 Futu gateway、watchdog 的 global-state 查询和交易推送监听器使用同一有界连接入口。
行情和交易查询的初始握手等待为 8 秒；TCP 端口可达但协议握手未完成时，关闭 context 并报告连接超时。
已建立连接的同步查询等待 SDK 重连也设为 8 秒；发出请求后的响应等待仍由 SDK 控制，
因此 8 秒不是整次查询的硬截止时间。正常连接仍由 SDK 自动重连，账户、环境与登录态检查保留。
超时、失败和用户取消均关闭尚未交付给调用方的 context。
交易推送监听器保留原有 2 秒同步重连等待；取消或鉴权中止启动后，后台连接若稍后成功也会关闭。

查询重试只针对可识别的临时传输失败或限流，沿用现有次数、指数退避和限流最小等待。
重试预算计入请求耗时与随机退避，预算耗尽后不启动新尝试；它不能中断已经运行的同步 SDK 请求。
登录失效、验证码和参数/接口错误立即返回；有效空结果直接返回。
耗尽后保留原始异常类型和原因码，连接池用同一错误分类识别连续断线，避免将限流或登录失败当作坏连接。

交易 SDK 未提供行情 SDK 的 `is_async_connect` 参数，gateway 仅在首次启动时通过
SDK 的 `_init_connect_sync` / `_wait_reconnect` 钩子进入异步连接，后续握手仍使用 SDK 原实现。
升级 futu-api 时需验证此钩子的兼容性，以及握手不响应时的关闭、正常连接与重连路径。
上述改动是源码行为；生产效果须在受控发布、升级后的指定环境验证。

## 审计取证

系统故障告警入口清单：计划 Tick 子进程失败、trade-intake unit 失败、intake 心跳、OpenD watchdog 均通过 `system_alerts` 记录指纹事故并处理恢复；通知投递降级也在该底座记录 journal 元信号，并在调用方明确传入配置时尝试外发。只读取证降级保留 journal 元信号。根盘容量不再按分钟或阈值告警；计划 Tick 或成交摄取因存储不足失败时，仍走各自已有的失败通知或成交回执。旧 `ROOT_DISK_USAGE_85/90` 事故会被静默标记为 `retired`，不虚报磁盘已恢复。OpenD 原有状态文件仍负责连续失败门槛和突发限额，实际发送使用公共事故状态与稳定投递键。普通策略候选通知属于业务投递，不属于系统故障告警。

系统告警和通知投递元信号先走当前主 route；主 route 缺失或在发送前明确不可用时，尝试另一已注册 provider。WeChat 为主时，飞书兜底凭据来自进程有效环境与 systemd credential/secret backend（`OM_FEISHU_BOT_APP_ID`、`OM_FEISHU_BOT_USER_OPEN_ID`、`FEISHU_BOT_APP_SECRET`），不读取主通知 route；飞书为主时，WeChat 兜底读取 `output_shared/state/channels/wechat_clawbot/<label>/state.json` 的 token 和同目录 `bindings.json`，仅在唯一完整绑定时投递。兜底使用独立且稳定的投递键，事故状态与 run 审计记录 `fallback_used`、实际 provider 和 `delivery_confirmed`。主通道结果不明确时不跨 provider 重发，避免双发；兜底失败也不再次跳转。两条通道都不可用时留 `<3>` journal 元信号，`runtime_status.system_alert_delivery.status=degraded`，人工检查本机 journal、凭据和绑定。业务日报及普通成交回执仍按原 route 投递，不使用此兜底。

恢复通知投递未确认时，事故保留 `recovery_delivery: unknown`，并写入 `<3>SYSTEM_META_ALERT`；后续恢复检查间隔至少 600 秒后可用同一投递键重试。确认后写入 `<4>SYSTEM_META_RECOVERY`，重复检查不再发送。

成交回执的传输键由 deal 与冻结的结果修订生成；同一修订重试复用键，已确认或投递结果未知的重复尝试记为 `skipped_duplicate` 类原因。计划通知的默认传输键由 run、账户和渲染内容生成；Daily Brief 继续使用冻结的业务投递键，跨 run 重试也保持同一键。

`notification_perception_read` 默认读取当前审计文件末尾 1 MiB，超过部分明确标为 partial。历史查询同时传入 `start_utc` 和 `end_utc`（ISO-8601 UTC），按当前及 `audit_events.YYYYMMDD.NNNNNN.jsonl` 段逐行扫描，单次最多 64 MiB；超预算、损坏和缺失均标明覆盖不完整。请求的审计文件缺失时，窗口覆盖为 partial、完整窗口总数未知；已存在的普通空文件则可给出 complete 和精确零结果。分页必须带原查询参数与返回游标；活动文件追加允许继续，源文件被替换或轮转须重新查询。

`candidate_filter_explain(run_selector=latest_notification)` 同样读取当前及保留审计段，在最多 64 个来源、64 MiB 内按通知内层 `created_at_utc` 的本地日期定位，再校验账户、会话和已确认投递的原始 `source_run_id`。单日相关记录上限为 5000，独立于公开预览的 50 条和分页签名能力；其它日期不占这 5000 条。外层审计时间及段文件日期不裁掉合法的内层日期匹配。已知缺失、损坏、读取期间变化或超限会阻断确定性结论；找不到合格来源且请求日期早于最早保留段时，仍报告审计证据不完整。较新的合格投递若来源引用无效，或其快照缺失，不回退到旧来源；保留对应来源错误及已解析的 run ID。完整且无匹配的窗口才报告无通知来源。

历史页保留最大起始偏移 5000；下一偏移按公开结果缩减至 6500 字节后的实际返回条数计算。若仍有匹配记录、但下一偏移超过该上限，当前页保留已返回证据并标为 partial，`continuation_status=pagination_limit`，不再提供 `next_cursor`，同时提示缩小时间窗口或过滤范围。`has_more=true` 表示仍有未返回记录，不保证还能续页；原有损坏或读取停止原因仍保留。偏移 5000 的合法末页和完整空结果不因此被截断；该偏移上限不是新增的结果总条数限制。

`./om service drift --output <路径>` 显式导出完整 JSON 明细，文件权限为私有。只读状态查询不会自行创建明细文件。`runtime_status` 将普通 service drift 和检查失败汇总为 `SERVICE_DRIFT`，缺失必需单元时保留优先的 `SERVICE_DRIFT_REQUIRED_UNIT_MISSING` 告警。

`./om logs --kind audit` 只读取文件末尾 256 KiB，超过 16 MiB 的旧审计文件仍可返回近期行；`tail_truncated` 明示未覆盖全文件。`research` 的审计尾读保留最多请求行数，单行超过 1 MiB 时标 `partial`。只读取证发生读取失败或覆盖不完整时，会向本机 stderr/journal 写 `<3>READ_DIAGNOSTIC_DEGRADED`，不尝试外发通知。

## 保留与回收

共享通知审计、trade-intake 审计按 UTC 日期或 64 MiB 在写入锁内封段。历史段格式为 `<stem>.YYYYMMDD.NNNNNN.jsonl`，当前文件名不变；历史段**没有自动删除**。`output_runs` 使用已有 `./om service cleanup` 预览、计划摘要和确认门，不能对照旧报告直接清除。

一次性事故候选可在目标机器上只读运行：

```sh
./.venv/bin/python -m scripts.incident_cleanup_preview --runtime-root /var/lib/options-monitor
```

脚本只列用户提供的 12 个 repair 快照、8 个 `/tmp` 路径、指定类型的旧账本备份、迁移备份目录，以及 OpenD `Log` 下 `.ftlog`/`.logs` 文件。它报告大小、时间、进程打开文件、软链和脚本/服务清单引用；检查缺失或结果不明时标记 `protected`。整个工具没有删除参数。当前账本与最近迁移备份始终受保护。目录候选若包含最近备份，整个目录受保护。

OpenD 已自行产生按日期分片的 `.ftlog`/`.logs` 文件。部署侧保留建议是 7 天且仅处理已关闭的已知后缀；启用自动清理前仍需确认实际文件命名、进程打开状态和证据保留期。不要对活动 OpenD 日志使用 `copytruncate`。审计段、备份与历史 run 的删除期也须单独确定并授权。

仓库的可选 `deploy/logrotate/options-monitor.conf.in` 仅匹配 `logs/closed/*.log` 中已经关闭的归档；由操作人员确认描述符已关闭后放入数量有界、可重复使用的文件名槽位。模板不搬运日志，也不清理持续新增的时间戳文件名。launchd 的 `*.out.log` / `*.err.log` 是常驻进程持有的描述符，不能使用 rename/create 轮转：改名后进程仍会写旧 inode。不要把活动 stdout/stderr、审计段或 OpenD 日志移入 `closed/`。此模板不限制活动 launchd 日志大小；该场景需另行采用能重开描述符的写入或收集方案。

部署时按 service profile 填入 runtime root 和用户，并安排小时级 logrotate 调用。`create` 保留空占位文件，`ifempty` 让一次性归档在无新写入时仍按小时继续轮转、压缩和淘汰；会产生空段，每个槽位保留最多 168 段，实际时间跨度依赖调用频率。该模板不处理正在写入的审计段或 OpenD 自身日志。审计写入锁拒绝超过 64 MiB 的单条记录，避免单条记录突破段上限。

新生成的 `service.profile.json` 为各 OpenD service 记录 `host`/`port`。受控升级重启 OpenD 后，服务健康检查用现有 watchdog 子进程对该端点做一次无 `--ensure` 的只读登录态探测，35 秒超时；端点缺失或登录失效均使健康检查失败。同版本升级返回 `already_current`；清理默认 `dry_run`，重复预览不会删除文件。

### 日报准备与无通知扫描终态

runlog 的 `daily_brief_prepare` 记录 `start` 和 `ok`/`error`，`tick_latency` 中同名阶段的 `outcome` 表示准备结果。进程被硬超时终止时可能仅有 start，应结合 wrapper 的超时终态判断，不能将缺少结束记录当作成功。

普通扫描完成但无可发送日报时仍通过统一 finalization 写入账户及共享 last_run、tick metrics/history 和唯一的 `run_end`；`sent=false`，原因是 `no_daily_brief_delivery`。空闲 delivery-only 无待重试内容仍保持只读跳过。执行完成不代表通知已送达，也不改变要求全部请求账户完成的恢复回执条件。


### 日报账户分段计时

本节描述源码诊断能力；发布并部署后才会出现在实际 Tick 的 runlog 中。
`daily_brief_phase` 复用现有 runlog，`data` 含 `account`、`market`、`phase` 和
`operation`。每次实际操作先记录 `start`，结束记录 `ok` 或 `error`，附
`duration_ms` 和 `data.outcome`；异常只记录类型 `error_type`，不记录异常正文或业务 payload。

| phase | 测量边界 |
| --- | --- |
| `account_prepare` | 一个账户的普通或 delivery-only 日报准备 |
| `assemble` | 单市场日报组装，包括它实际读取的封存产物 |
| `history_validate` | 投递/恢复历史归一化、来源校验及其引用读取，或 current/revision 一致性校验 |
| `persist` | 日报仓库每次原子 JSON 编码及写入 |
| `render` | 固定日报、候选提醒、失败日报的文本或卡片 Markdown 渲染 |
| `lock_wait` | 日报仓库锁获取，含目录/文件准备和等待；不含取得锁后的业务工作 |

阶段耗时包含嵌套操作，不能把这些值相加作为总耗时。同一账户同一阶段的多次操作按日志顺序
配对；`operation` 区分具体责任函数。投递确认和 provider 发送不属于 `account_prepare`。
阶段 `ok` 表示该操作正常返回，不表示现金/候选有效或通知已送达；例如历史读取可能在捕获校验
异常后返回不可用，内层校验为 `error`，外层账户准备是否正常结束仍由实际控制流决定。

软取消/异常在展开栈时记录 `error` 并保持原异常；硬超时或强杀可能仅留下 `start`，须结合
wrapper 终态取证，不能补推成功或虚构耗时。时钟不可用时省略耗时；日志不可用不改变原业务
结果。计时只在显式账户准备范围内启用，范围结束即复原；独立查询、投递确认和其他仓库调用
不会因为这项诊断新增日志，也不更改锁策略、通知内容、持久化合同或超时配置。
