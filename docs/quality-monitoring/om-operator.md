# OM Quality Producer 操作契约

OM producer 只读取现有 runtime/intake/ledger/lifecycle 事实，并通过独立
`refresh_cache=True` OpenD 查询取得最终期权持仓。检查不会修改交易事件、
position lots、生命周期 case 或 OpenD 数据。

本地入口：

```text
./om quality refresh --config-key us --config-key hk
./om quality refresh --config-key us --config-key hk --no-deep
./om quality recheck-due --config-key us --config-key hk
./om quality refresh --config-key us --day-end-strict
./om quality status --json
./om-agent run --tool quality_status --input-json '{}'
```

首次 baseline 或人工强制权威对账使用默认 `refresh`；15 分钟常规定时器使用
`--no-deep`。后者会继续发布 runtime、ledger、intake、lifecycle 等当前检查，
但只在本地持仓 revision 改变、差异复查到期、日终 deadline 到期或缺少有效
baseline 时访问 OpenD；否则沿用仍在有效期内的最近一次权威 OpenD 证据。

`refresh` 会原子发布：

```text
<OM_RUNTIME_ROOT>/output_shared/state/quality/status.v1.json
<OM_RUNTIME_ROOT>/output_shared/state/quality/control_state.v1.json
```

第二个文件只保存差异首次出现时间、下一次只读复查时间、生命周期首次深对账时间、
市场交易日列表和本地 `position_lots` 控制状态哈希，不保存账户 ID、完整持仓或
OpenD 原始响应。

只读 HTTP：

```text
./om secrets set quality.read_token
./om quality serve --host 127.0.0.1 --port 8792
```

macOS 从 Keychain 读取；Linux systemd unit 通过 `LoadCredentialEncrypted=` 只注入
`quality.read_token`。`OM_QUALITY_READ_TOKEN` 仅在显式 `OM_SECRET_BACKEND=env` 的限时兼容模式下生效。

- `GET /health` 只证明 endpoint 进程可用；
- `GET /quality/status` 需要独立 bearer token，只读取已发布 artifact；
- HTTP 请求不会调用 OpenD、不会 replay repair、不会写 evidence；
- 默认只允许 loopback；生产受控内网绑定必须显式设置
  `OM_QUALITY_ALLOW_REMOTE_BIND=true` 并由外围传输层保护。

门禁：

- `OM_QUALITY_ONBOARDED=false` 时 producer 可部署和建立 baseline，但不改变消费者行为；
- 完成生产 baseline 与 Hub onboarding 后设为 `true`；
- 此后 stale artifact 或明确 blocking 结论会阻断 close advice 和正式
  option performance；
- 普通候选扫描不读取该门禁；
- 没有临时 observe/bypass 开关，门禁实现故障通过回滚 producer release 处理。

调度语义：

- 常规 producer 每 15 分钟执行 `refresh --no-deep`；
- `recheck-due` 每 1 分钟只比较控制状态哈希和差异到期时间；无变化时不重建
  artifact，也不访问 OpenD；
- 持仓首次差异保存 `next_recheck_at_utc=+1m`，第二次窗口到 `+5m`；
- 调度器只在到期时再次运行只读 refresh，不在单次进程中 sleep；
- 日终分别在所属市场时区周一至周五 `16:30` 执行
  `refresh --day-end-strict`，首次确定性差异立即阻断；
- 单市场日终刷新保留另一市场最近一次有效数据集，不把未请求市场误删；
- OpenD 权威查询不是固定分钟轮询，只在 baseline、ledger 变化、差异到期、
  日终或人工强制时发生。

systemd renderer 默认不改变现有部署。生产准备时显式加入：

```text
./om service render \
  --target systemd \
  --config-yaml <config.yaml> \
  --runtime-root /var/lib/options-monitor \
  --env-file /etc/options-monitor/options-monitor.env \
  --include-quality-monitoring \
  --include-secret-credentials
```

该选项生成：

- `options-monitor-quality-http.service`：loopback `127.0.0.1:8792`；
- `options-monitor-quality-refresh.timer`：15 分钟常规刷新；
- `options-monitor-quality-recheck.timer`：1 分钟轻量到期探测；
- `options-monitor-quality-day-end-us.timer`：美东 `16:30`；
- `options-monitor-quality-day-end-hk.timer`：香港 `16:30`。

renderer 只生成文件和安装命令，不会自行写 `/etc`、启用 timer 或启动服务。
