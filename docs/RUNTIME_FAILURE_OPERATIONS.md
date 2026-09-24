# Tick 故障取证与保留操作

这份说明对应本地源码变更。发布或远端升级前，不把这里的行为当成生产现状。

## 失败与恢复

计划 Tick 的外层失败写入 `output_runs/<wrapper_run_id>/state/audit_events.jsonl`，并更新 `output_shared/state/current/tick_cron_last_result.<market>.current.json`。错误码、阶段、账户和首错时间在审计行中；`EXEC_FAILED_RC_*` 仍是终端兜底。登录失效、手机或图形验证码需要人工重新登录 OpenD，计划 Tick 返回非零。`om_runtime_status` 和 `om_quality_status` 可读取同一原因码。若 run 写入失败或系统被强杀，以 systemd 的 `Result/ExecMainStatus` 核对，不能把旧状态当作新一轮健康结果。

`./om logs --kind service` 在文件目录为空时会提示查看 systemd journal。具体故障可用 `journalctl -p err -u options-monitor-tick-hk.service`（或 `us`）；服务单元的 ERROR 行采用 systemd 可解析的级别前缀。渲染后的 tick unit 超时比 CLI 的 `--timeout` 多 300 秒，并有 30 秒停止宽限；只有受控升级后生产 unit 才会变化。

## 审计取证

系统故障告警入口清单：计划 Tick 子进程失败、trade-intake unit 失败、intake 心跳与根盘阈值、OpenD watchdog 均通过 `system_alerts` 记录指纹事故并处理恢复；通知投递降级也在该底座记录 journal 元信号，并在调用方明确传入配置时尝试外发。只读取证降级保留 journal 元信号。OpenD 原有状态文件仍负责连续失败门槛和突发限额，实际发送使用公共事故状态与稳定投递键。普通策略候选通知属于业务投递，不属于系统故障告警。

系统告警和通知投递元信号先走当前主 route；主 route 缺失或在发送前明确不可用时，尝试另一已注册 provider。WeChat 为主时，飞书兜底凭据来自进程有效环境与 systemd credential/secret backend（`OM_FEISHU_BOT_APP_ID`、`OM_FEISHU_BOT_USER_OPEN_ID`、`FEISHU_BOT_APP_SECRET`），不读取主通知 route；飞书为主时，WeChat 兜底读取 `output_shared/state/channels/wechat_clawbot/<label>/state.json` 的 token 和同目录 `bindings.json`，仅在唯一完整绑定时投递。兜底使用独立且稳定的投递键，事故状态与 run 审计记录 `fallback_used`、实际 provider 和 `delivery_confirmed`。主通道结果不明确时不跨 provider 重发，避免双发；兜底失败也不再次跳转。两条通道都不可用时留 `<3>` journal 元信号，`runtime_status.system_alert_delivery.status=degraded`，人工检查本机 journal、凭据和绑定。业务日报及普通成交回执仍按原 route 投递，不使用此兜底。

恢复通知投递未确认时，事故保留 `recovery_delivery: unknown`，并写入 `<3>SYSTEM_META_ALERT`；后续恢复检查间隔至少 600 秒后可用同一投递键重试。确认后写入 `<4>SYSTEM_META_RECOVERY`，重复检查不再发送。

成交回执的传输键由 deal 与冻结的结果修订生成；同一修订重试复用键，已确认或投递结果未知的重复尝试记为 `skipped_duplicate` 类原因。计划通知的默认传输键由 run、账户和渲染内容生成；Daily Brief 继续使用冻结的业务投递键，跨 run 重试也保持同一键。

`notification_perception_read` 默认读取当前审计文件末尾 1 MiB，超过部分明确标为 partial。历史查询同时传入 `start_utc` 和 `end_utc`（ISO-8601 UTC），按当前及 `audit_events.YYYYMMDD.NNNNNN.jsonl` 段逐行扫描，单次最多 64 MiB；超预算、损坏和缺失均标明覆盖不完整。分页必须带原查询参数与返回游标；活动文件追加允许继续，源文件被替换或轮转须重新查询。

`./om service drift --output <路径>` 显式导出完整 JSON 明细，文件权限为私有。只读状态查询不会自行创建明细文件。

`./om logs --kind audit` 只读取文件末尾 256 KiB，超过 16 MiB 的旧审计文件仍可返回近期行；`tail_truncated` 明示未覆盖全文件。`research` 的审计尾读保留最多请求行数，单行超过 1 MiB 时标 `partial`。只读取证发生读取失败或覆盖不完整时，会向本机 stderr/journal 写 `<3>READ_DIAGNOSTIC_DEGRADED`，不尝试外发通知。

## 保留与回收

共享通知审计、trade-intake 审计按 UTC 日期或 64 MiB 在写入锁内封段。历史段格式为 `<stem>.YYYYMMDD.NNNNNN.jsonl`，当前文件名不变；历史段**没有自动删除**。`output_runs` 使用已有 `./om service cleanup` 预览、计划摘要和确认门，不能对照旧报告直接清除。

一次性事故候选可在目标机器上只读运行：

```sh
./.venv/bin/python -m scripts.incident_cleanup_preview --runtime-root /var/lib/options-monitor
```

脚本只列用户提供的 12 个 repair 快照、8 个 `/tmp` 路径、指定类型的旧账本备份、迁移备份目录，以及 OpenD `Log` 下 `.ftlog`/`.logs` 文件。它报告大小、时间、进程打开文件、软链和脚本/服务清单引用；检查缺失或结果不明时标记 `protected`。整个工具没有删除参数。当前账本与最近迁移备份始终受保护。目录候选若包含最近备份，整个目录受保护。

OpenD 已自行产生按日期分片的 `.ftlog`/`.logs` 文件。部署侧保留建议是 7 天且仅处理已关闭的已知后缀；启用自动清理前仍需确认实际文件命名、进程打开状态和证据保留期。不要对活动 OpenD 日志使用 `copytruncate`。审计段、备份与历史 run 的删除期也须单独确定并授权。

仓库提供 `deploy/logrotate/options-monitor.conf.in` 作为普通 runtime `.log` 文件模板。部署时需按 service profile 填入 runtime root 和用户，并安排小时级 logrotate 调用；该模板不处理正在写入的审计段或 OpenD 自身日志。审计写入锁拒绝超过 64 MiB 的单条记录，避免单条记录突破段上限。

新生成的 `service.profile.json` 为各 OpenD service 记录 `host`/`port`。受控升级重启 OpenD 后，服务健康检查用现有 watchdog 子进程对该端点做一次无 `--ensure` 的只读登录态探测，35 秒超时；端点缺失或登录失效均使健康检查失败。同版本升级返回 `already_current`；清理默认 `dry_run`，重复预览不会删除文件。
